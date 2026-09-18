"""MCP 运行时状态与数据模型（M9 / D106 / D108）。"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class McpConnectionState(str, Enum):
    """MCP 服务的生命周期连接状态。"""

    STOPPED = "stopped"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    OFFLINE = "offline"  # 异常退出 / 无法连接（Fail-Safe 降级状态）
    DISABLED = "disabled"  # 用户在配置中明确禁用


class McpServerStatus(BaseModel):
    """MCP 服务的状态展示快照（供 TUI /mcp 与可观测性查询）。"""

    model_config = ConfigDict(frozen=True)

    name: str
    command: str = ""
    state: McpConnectionState = McpConnectionState.STOPPED
    tool_count: int = 0
    tools: list[str] = Field(default_factory=list)
    error: str | None = None
    description: str = ""
