"""MCP 全局服务管理器（M9 / D106 / D108）。"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from logox.config.schema import McpConfig
from logox.mcp.adapter import McpMetaTool
from logox.mcp.client import McpClient
from logox.mcp.models import McpServerStatus

logger = logging.getLogger("logox.mcp.manager")


class McpManager:
    """负责管理所有已配置的 MCP 服务客户端实例与工具注入。

    特性：
    1. 全生命周期隔离：各个 Client 互不干扰，单个崩溃不影响其它；
    2. 统一暴露元工具 `mcp`：对齐 Pi 风格元工具代理模式；
    3. 全局可观测性：提供 `get_status_list()` 供 TUI `/mcp` 查询。
    """

    def __init__(
        self,
        config: McpConfig,
        cwd: Path | str | None = None,
        bus: Any | None = None,
    ) -> None:
        self.config = config
        self.cwd = Path(cwd) if cwd else Path.cwd()
        self.bus = bus

        self.clients: dict[str, McpClient] = {}
        for name, server_cfg in config.get_servers().items():
            self.clients[name] = McpClient(
                name=name,
                config=server_cfg,
                cwd=self.cwd,
                bus=self.bus,
            )

        self.meta_tool = McpMetaTool(self)

    def generate_meta_tool_description(self) -> str:
        """根据当前配置的服务动态生成元工具说明。"""
        if not self.clients:
            return "访问外部 MCP 生态工具（当前未配置外部 MCP 服务）。"

        server_summaries = []
        for name, client in self.clients.items():
            desc = client.config.description or f"执行 {name} 相关外部操作"
            server_summaries.append(f"{name}: {desc}")

        summary_text = "；".join(server_summaries)
        return (
            f"访问外部 MCP 生态工具。当前已挂载服务：[{summary_text}]。\n"
            "使用 action='list' 查询某服务包含的具体工具清单及函数签名；"
            "使用 action='call' 传入 tool 与 arguments 执行具体工具。"
        )

    def get_client(self, name: str) -> McpClient | None:
        return self.clients.get(name)

    def get_status_list(self) -> list[McpServerStatus]:
        return [client.status() for client in self.clients.values()]

    async def close_all(self) -> None:
        """关闭所有 MCP 子进程会话。"""
        tasks = [client.close() for client in self.clients.values()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
