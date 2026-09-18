"""MCP 工具适配器与极限脱水器（M9 / D106 / D107 / D108）。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from logox.errors import ErrorCategory
from logox.tools.base import (
    DisplayHint,
    Tool,
    ToolArgs,
    ToolContext,
    ToolResult,
    ToolSpec,
)


def condense_mcp_tool(tool: dict[str, Any] | Any) -> str:
    """【纯代码极限脱水器 (Schema Condenser)】
    将臃肿的 MCP Tool JSON Schema 压缩成紧凑单行函数签名。
    纯代码规则解析，耗时 < 0.05ms，0 外部依赖，0 Token 成本，0 幻觉。

    例：
    输入：GitHub create_issue 的 350 字符完整 Schema
    输出："- create_issue(owner: str, repo: str, title: str, body?: str): Create a new issue..."
    """
    if hasattr(tool, "model_dump"):
        tool_dict = tool.model_dump()
    elif isinstance(tool, dict):
        tool_dict = tool
    else:
        tool_dict = {
            "name": getattr(tool, "name", "unknown"),
            "description": getattr(tool, "description", ""),
            "inputSchema": getattr(tool, "inputSchema", None) or getattr(tool, "input_schema", {}),
        }

    name = tool_dict.get("name", "unknown")
    raw_desc = (tool_dict.get("description") or "").strip().split("\n")[0]
    desc = (raw_desc[:57] + "...") if len(raw_desc) > 60 else raw_desc

    schema = tool_dict.get("inputSchema") or tool_dict.get("input_schema") or {}
    properties: dict[str, Any] = schema.get("properties") or {}
    required_set = set(schema.get("required") or [])

    type_map = {
        "string": "str",
        "integer": "int",
        "number": "float",
        "boolean": "bool",
        "array": "list",
        "object": "dict",
    }

    params = []
    for param_name, prop in properties.items():
        raw_type = prop.get("type", "any") if isinstance(prop, dict) else "any"
        type_str = type_map.get(raw_type, raw_type)
        if param_name in required_set:
            params.append(f"{param_name}: {type_str}")
        else:
            params.append(f"{param_name}?: {type_str}")

    sig = f"- {name}({', '.join(params)})"
    if desc:
        sig += f": {desc}"
    return sig


class McpMetaArgs(ToolArgs):
    """Pi 风格 MCP 元工具参数模型。"""

    server: str = Field(description="目标 MCP 服务名（如 github, postgres）")
    action: Literal["list", "call"] = Field(
        description="操作动作：'list' 列出该服务支持的所有工具及其紧凑函数签名；'call' 调用指定工具"
    )
    tool: str | None = Field(
        default=None,
        description="要调用的工具名称（action='call' 时必填，如 'create_issue'）",
    )
    arguments: dict[str, Any] | None = Field(
        default=None,
        description="工具参数字典（action='call' 时按工具签名提供对应参数）",
    )


class McpMetaTool(Tool):
    """Pi 风格元工具代理（Proxy Meta-Tool Pattern）。

    全局仅向大模型暴露单个紧凑元工具 `mcp`，消耗约 150~200 Token，
    彻底反制平铺模式消耗 5,000~10,000 Token 的 'Context Hog' 弊端。
    通过两阶段渐进式披露：
    1. action='list'：查询特定服务的工具紧凑目录；
    2. action='call'：代理转发参数并执行。
    """

    def __init__(self, manager: Any, description: str = "") -> None:
        self.manager = manager
        self._custom_desc = description

    @property
    def spec(self) -> ToolSpec:
        desc = self._custom_desc or self.manager.generate_meta_tool_description()
        return ToolSpec(
            name="mcp",
            description=desc,
            params=McpMetaArgs,
            readonly=False,
            requires_permission=True,
            summary_template="MCP {server} -> {action} {tool}",
            source="mcp",
        )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        server_name = args.get("server", "").strip()
        action = args.get("action", "").strip()

        if not server_name:
            return ToolResult.failure(ErrorCategory.TOOL_FAILURE, "必须指定目标 MCP 服务名 (server)")

        client = self.manager.get_client(server_name)
        if client is None:
            available = list(self.manager.clients.keys())
            return ToolResult.failure(
                ErrorCategory.TOOL_FAILURE,
                f"未找到 MCP 服务 [{server_name}]。当前已配置的可用服务：{available}",
            )

        if action == "list":
            try:
                tools = await client.list_tools()
            except Exception as exc:
                return ToolResult.failure(
                    ErrorCategory.TOOL_FAILURE,
                    f"获取 MCP 服务 [{server_name}] 工具列表失败：{exc}",
                )

            if not tools:
                msg = f"MCP 服务 [{server_name}] 未暴露任何工具。"
                return ToolResult(ok=True, content=msg, display=DisplayHint(kind="text", payload={"text": msg}))

            sigs = [condense_mcp_tool(t) for t in tools]
            catalog = f"MCP 服务 [{server_name}] 可用工具清单（共 {len(tools)} 个）：\n" + "\n".join(sigs)
            return ToolResult(ok=True, content=catalog, display=DisplayHint(kind="text", payload={"text": catalog}))

        if action == "call":
            tool_name = (args.get("tool") or "").strip()
            if not tool_name:
                return ToolResult.failure(ErrorCategory.TOOL_FAILURE, "action='call' 时必须提供 tool 参数")
            tool_args = args.get("arguments") or {}
            return await client.call_tool(tool_name, tool_args)

        return ToolResult.failure(
            ErrorCategory.TOOL_FAILURE,
            f"未知 action: {action!r}，支持 'list' 或 'call'",
        )


class McpProxyTool(Tool):
    """远程 MCP Server 的单个直连工具代理（用于直接命名空间映射 mcp__{server}__{tool}）。"""

    def __init__(
        self,
        server_name: str,
        raw_name: str,
        client: Any,
        description: str = "",
        params_schema: dict[str, Any] | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.server_name = server_name
        self.raw_name = raw_name
        self.client = client
        self.timeout = timeout
        self.params_schema = params_schema or {}

        # 命名空间双下划线隔离（D107）
        self.scoped_name = f"mcp__{server_name}__{raw_name}"
        clean_desc = (description or "").strip()
        if len(clean_desc) > 300:
            clean_desc = clean_desc[:297] + "..."
        self._description = clean_desc

    @property
    def spec(self) -> ToolSpec:
        # 使用动态 ToolSpec，对于直连工具，默认使用 ToolArgs
        return ToolSpec(
            name=self.scoped_name,
            description=self._description,
            params=ToolArgs,
            readonly=False,
            requires_permission=True,
            summary_template=f"MCP [{self.server_name}] {self.raw_name}",
            source="mcp",
        )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return await self.client.call_tool(self.raw_name, args, timeout=self.timeout)
