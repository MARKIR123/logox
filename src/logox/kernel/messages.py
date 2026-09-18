"""中立消息模型（ARCHITECTURE §4.1 / §4.2 的落地）。

这是内核与 Provider 之间的**唯一数据形状**。厂商特有字段一律不得进入这里
（转换只发生在 ``providers/`` 适配器内部，D9）。

一个必要的让步：``ReasoningBlock.signature``
-------------------------------------------
Anthropic 的 thinking 块带一个 ``signature``，**多轮工具调用时必须原样回传**，
否则下一次请求会被拒绝。而消息历史由内核持有、下一轮再交给适配器——所以签名
**必须**能随消息存活。

因此这里的做法是：把它标为**厂商不透明数据**——内核只负责原样保存与回传，
**绝不解释它的含义**。这是本文件与设计文档的一处有意偏差，已在
``docs/modules/MODULE_providers.md`` 的实现结果中登记。
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "ContentBlock",
    "Message",
    "MessageMeta",
    "ReasoningBlock",
    "TextBlock",
    "ToolResultBlock",
    "ToolSchema",
    "ToolUseBlock",
    "text_message",
    "user_message",
]

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class TextBlock(BaseModel):
    """正文文本块。"""

    model_config = _FROZEN

    type: Literal["text"] = "text"
    text: str


class ReasoningBlock(BaseModel):
    """推理内容（思维链）。

    D31：**不计入上下文预算**，压缩时优先剔除；切换模型时也应剔除（厂商格式不通用）。
    """

    model_config = _FROZEN

    type: Literal["reasoning"] = "reasoning"
    text: str
    #: 厂商不透明数据（Anthropic 的 ``signature``）。**内核只存不解释。**
    signature: str | None = None


class ToolUseBlock(BaseModel):
    """模型请求调用某个工具。``input`` 是**已解析**的参数 dict。"""

    model_config = _FROZEN

    type: Literal["tool_use"] = "tool_use"
    id: str
    name: str
    input: dict[str, Any] = Field(default_factory=dict)


class ToolResultBlock(BaseModel):
    """工具执行结果。

    ``ok=False`` 时，内核可以把 ``content`` 回灌给模型让它自愈（D22 的
    ``ErrorCategory.feedable_to_model``）。
    """

    model_config = _FROZEN

    type: Literal["tool_result"] = "tool_result"
    id: str
    ok: bool = True
    content: str = ""


ContentBlock = Annotated[
    TextBlock | ReasoningBlock | ToolUseBlock | ToolResultBlock,
    Field(discriminator="type"),
]


class MessageMeta(BaseModel):
    """消息的来源与轻量元信息（供压缩策略与 UI 使用）。"""

    model_config = _FROZEN

    created_at: float | None = None
    token_estimate: int | None = None
    #: 这条消息从哪来：正常会话 / 压缩产物 / 钩子注入
    source: Literal["session", "compaction", "hook"] = "session"
    turn_summary: str | None = None


class Message(BaseModel):
    """一条消息。``role="tool"`` 用于承载工具结果（OpenAI 兼容格式需要独立角色）。"""

    model_config = _FROZEN

    role: Literal["system", "user", "assistant", "tool"]
    blocks: list[ContentBlock] = Field(default_factory=list)
    meta: MessageMeta = Field(default_factory=MessageMeta)

    # -- 便捷读取 ------------------------------------------------------- #

    @property
    def text(self) -> str:
        """把所有文本块拼起来（推理块**不计入**）。"""
        return "".join(block.text for block in self.blocks if isinstance(block, TextBlock))

    def blocks_of(self, block_type: type) -> list[Any]:
        return [block for block in self.blocks if isinstance(block, block_type)]


class ToolSchema(BaseModel):
    """工具的**线级描述**，供 Provider 转成各家的工具定义。

    M5 的 ``tools/`` 会持有更丰富的 ``ToolSpec``（含 handler、readonly、
    摘要模板等），并由它产出这里的 ``ToolSchema``。适配层只需要描述，
    不需要知道工具怎么执行。
    """

    model_config = _FROZEN

    name: str
    description: str = ""
    #: JSON Schema（由 pydantic 模型生成，D26）
    parameters: dict[str, Any] = Field(default_factory=lambda: {"type": "object", "properties": {}})


def user_message(text: str) -> Message:
    return Message(role="user", blocks=[TextBlock(text=text)])


def text_message(role: Literal["system", "user", "assistant"], text: str) -> Message:
    return Message(role=role, blocks=[TextBlock(text=text)])
