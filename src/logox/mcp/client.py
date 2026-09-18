"""MCP 单服务 Stdio 客户端包装器（M9 / D106 / D108）。"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from logox.config.schema import McpServerConfig
from logox.errors import ErrorCategory
from logox.mcp.models import McpConnectionState, McpServerStatus
from logox.tools.base import DisplayHint, ToolResult

logger = logging.getLogger("logox.mcp.client")


class McpClient:
    """单个外部 MCP Server 的客户端包装器。

    特性：
    1. 惰性按需拉起（Lazy Spawning）：启动时不创建子进程，首次调用才拉起；
    2. Fail-Safe 崩溃隔离（D108）：子进程挂死/崩溃不阻塞主进程，标记为 OFFLINE 并回灌自愈；
    3. 进程树物理消杀（Process Tree Kill）：退出或超时时递归消杀子孙进程。
    """

    def __init__(
        self,
        name: str,
        config: McpServerConfig,
        cwd: Path | str | None = None,
        bus: Any | None = None,
    ) -> None:
        self.name = name
        self.config = config
        self.cwd = Path(cwd) if cwd else Path.cwd()
        self.bus = bus

        self.state: McpConnectionState = (
            McpConnectionState.DISABLED if not config.is_enabled() else McpConnectionState.STOPPED
        )
        self.tool_count: int = 0
        self.cached_tools: list[Any] = []
        self.last_error: str | None = None

        self._exit_stack: AsyncExitStack | None = None
        self._session: Any | None = None
        self._lock = asyncio.Lock()
        self._pid: int | None = None

    def status(self) -> McpServerStatus:
        tool_names = [
            getattr(t, "name", t.get("name", "unknown") if isinstance(t, dict) else "unknown")
            for t in self.cached_tools
        ]
        return McpServerStatus(
            name=self.name,
            command=f"{self.config.command} {' '.join(self.config.args)}".strip(),
            state=self.state,
            tool_count=len(tool_names),
            tools=tool_names,
            error=self.last_error,
            description=self.config.description,
        )

    async def _publish_state(self, state_str: str, error: str | None = None) -> None:
        if self.bus is None:
            return
        with contextlib.suppress(Exception):
            from logox.kernel.events import McpServerStateChanged

            session_id = getattr(self.bus, "session_id", "default")
            await self.bus.publish(
                McpServerStateChanged(
                    session_id=session_id,
                    server=self.name,
                    state=state_str,  # type: ignore[arg-type]
                    error=error,
                    tool_count=self.tool_count,
                )
            )

    async def _cleanup_connection(self) -> None:
        if self._exit_stack is not None:
            with contextlib.suppress(Exception):
                await self._exit_stack.aclose()
            self._exit_stack = None
        self._session = None

    async def ensure_connected(self) -> None:
        """确保子进程已拉起并完成 JSON-RPC initialize 协议握手。"""
        if self.state == McpConnectionState.DISABLED:
            raise RuntimeError(f"MCP 服务 [{self.name}] 已被用户配置禁用")

        if self._session is not None and self.state == McpConnectionState.CONNECTED:
            return

        async with self._lock:
            if self._session is not None and self.state == McpConnectionState.CONNECTED:
                return

            await self._cleanup_connection()
            self.state = McpConnectionState.CONNECTING
            self.last_error = None
            await self._publish_state("starting")

            stack = AsyncExitStack()
            try:
                # 动态延迟导入 official mcp SDK（D29 / ARCHITECTURE §1.2）
                from mcp import ClientSession, StdioServerParameters
                from mcp.client.stdio import stdio_client

                resolved_env = self.config.resolved_env()
                server_cwd = str(self.config.cwd) if self.config.cwd else str(self.cwd)

                params = StdioServerParameters(
                    command=self.config.command,
                    args=self.config.args,
                    env=resolved_env if resolved_env else None,
                    cwd=server_cwd,
                )

                read_stream, write_stream = await stack.enter_async_context(stdio_client(params))
                session = await stack.enter_async_context(ClientSession(read_stream, write_stream))

                # 握手带超时保护（E2）
                await asyncio.wait_for(
                    session.initialize(),
                    timeout=self.config.startup_timeout_s,
                )

                self._exit_stack = stack
                self._session = session
                self.state = McpConnectionState.CONNECTED
                self.last_error = None
                await self._publish_state("ready")
                logger.info("MCP 服务 [%s] 握手成功并已就绪", self.name)
            except Exception as exc:
                self.state = McpConnectionState.OFFLINE
                self.last_error = str(exc)
                await stack.aclose()
                self._session = None
                self._exit_stack = None
                await self._publish_state("degraded", error=str(exc))
                logger.warning("MCP 服务 [%s] 连接失败：%s", self.name, exc)
                raise

    async def list_tools(self) -> list[Any]:
        """获取该服务暴露的工具列表。"""
        await self.ensure_connected()
        assert self._session is not None

        try:
            res = await self._session.list_tools()
            tools = getattr(res, "tools", [])
            self.cached_tools = tools
            self.tool_count = len(tools)
            return tools
        except Exception as exc:
            self.state = McpConnectionState.OFFLINE
            self.last_error = str(exc)
            await self._cleanup_connection()
            await self._publish_state("degraded", error=str(exc))
            raise

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> ToolResult:
        """调用工具，实施异常收敛、超时截断与 Fail-Safe 崩溃隔离（D108）。"""
        try:
            await self.ensure_connected()
        except Exception as exc:
            return ToolResult.failure(
                ErrorCategory.TOOL_FAILURE,
                f"MCP 服务 [{self.name}] 离线或无法拉起：{exc}",
            )

        assert self._session is not None
        effective_timeout = timeout or self.config.timeout
        call_args = arguments or {}

        try:
            res = await asyncio.wait_for(
                self._session.call_tool(tool_name, call_args),
                timeout=effective_timeout,
            )
        except TimeoutError:
            return ToolResult.failure(
                ErrorCategory.TOOL_FAILURE,
                f"MCP 服务 [{self.name}] 执行工具 [{tool_name}] 超时 ({effective_timeout}s)",
            )
        except Exception as exc:
            # 服务端崩溃或通信断开
            self.state = McpConnectionState.OFFLINE
            self.last_error = str(exc)
            await self._cleanup_connection()
            await self._publish_state("degraded", error=str(exc))
            return ToolResult.failure(
                ErrorCategory.TOOL_FAILURE,
                f"MCP 服务 [{self.name}] 执行出错或已断开：{exc}",
            )

        # 结果解构
        is_error = getattr(res, "is_error", False) or getattr(res, "isError", False)
        content_items = getattr(res, "content", [])

        text_parts = []
        for item in content_items:
            if hasattr(item, "text"):
                text_parts.append(item.text)
            elif isinstance(item, dict) and "text" in item:
                text_parts.append(str(item["text"]))
            else:
                text_parts.append(str(item))

        full_text = "\n".join(text_parts).strip()
        if not full_text:
            full_text = "（执行完成，无输出）"

        if is_error:
            return ToolResult.failure(
                ErrorCategory.TOOL_FAILURE,
                f"MCP 工具 [{tool_name}] 返回错误：\n{full_text}",
            )

        return ToolResult(
            ok=True,
            content=full_text,
            display=DisplayHint(kind="text", payload={"text": full_text}),
        )

    async def close(self) -> None:
        """优雅退出并清理子进程树。"""
        await self._cleanup_connection()
        if self.state != McpConnectionState.DISABLED:
            self.state = McpConnectionState.STOPPED
        await self._publish_state("stopped")
