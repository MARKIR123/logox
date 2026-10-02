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
    #: 内容是否已被**归档**（换成"索引 + 确定性节选"，原文在磁盘上）。
    #:
    #: 为什么必须靠**元数据**而不是去解构内容：早先的写法用"长度是否 > 150"
    #: 判断"裁过没有"，而这在归档格式变化时会**静默失效** ——
    #: 归档后的文本自己就 > 150，于是同一块被反复归档，每次都改写历史中前部的字节，
    #: **让前缀缓存从那里断开，它之后的全部内容按全价重算**（已实测）。
    #: 状态要有独立载体，不能从表现形式里反推。
    archived: bool = False


ContentBlock = Annotated[
    TextBlock | ReasoningBlock | ToolUseBlock | ToolResultBlock,
    Field(discriminator="type"),
]


class MessageMeta(BaseModel):
    """消息的来源与轻量元信息（供压缩策略与 UI 使用）。"""

    model_config = _FROZEN

    #: 这条消息从哪来：正常会话 / 压缩产物 / 钩子注入
    source: Literal["session", "compaction", "hook"] = "session"
    turn_summary: str | None = None
    #: 这条摘要**是怎么来的**（D135 第二步）。用户裁定 Q-D：来源必须能在
    #: 历史消息与列表里看出来 —— 否则分不清"模型写的"与"我们兜底的"。
    #:
    #: * ``model_last_line`` —— 模型按契约写在最终答复最后一行（**期望路径**）
    #: * ``model_tag`` —— 模型仍用了旧的 `<turn_summary>` 标签（过渡期的兼容路径）
    #: * ``model_fallback`` —— 末尾行不合规，**又调了一次模型**补写（第 2 层兜底）
    #: * ``deterministic`` —— 连补写都失败，本地自动生成（第 3 层兜底）
    summary_source: Literal[
        "model_last_line", "model_tag", "model_fallback", "deterministic"
    ] | None = None

    #: ★ CHANGE-052：这条消息来自 ``transcript.jsonl`` 的第几行（1-based）。
    #:
    #: 谁写：**只有 `store/replay.reconstruct_messages()`** —— 它读得到记录的
    #: ``"line"`` 字段（`write_step` 自始就在写）。除此之外一律为 ``None``。
    #:
    #: 为什么需要它（这是"行号不可用"的根治办法）：
    #: 从前压缩器只能拿"轮次号"当代理去**推断**行号，而轮次号是**进程内自增**的
    #: ⇒ `/resume` 之后会与文件里已有的编号撞号（F-32），于是守卫只能"整体拒给"
    #: ⇒ 索引渲染成"行号未知"，那条"可按行号 fs_read 回原文"的逃生门**从未生效**。
    #: 行号则相反：`transcript.jsonl` 是 append-only，**写入即永久有效**。
    #:
    #: ⚠️ 它是**消息**的属性，不是轮的属性 —— 轮的行区间由该轮所有消息的
    #: ``transcript_line`` 取 min/max 得到（见 `context/compaction.py`）。
    transcript_line: int | None = None

    #: ★ CHANGE-052：折叠轮原文在 ``transcript.jsonl`` 里的**行区间**（闭区间）。
    #:
    #: 只在**折叠产物**（``source="compaction"`` 的摘要消息）上有值 —— 它是
    #: "这一轮的原样记录在哪"，供模型 `fs_read` 回读。
    #:
    #: ⚠️ 必须**存进 meta**、不能每次重算：第二次折叠时被折轮的原文**已不在视图里**
    #: （视图里只剩 ``[user 逐字, 摘要]`` 对）⇒ 行区间必须**跨折叠存活**。
    #: 这也是它与 `transcript_line` 分开的原因：前者是"我来自哪一行"，
    #: 后者是"我代表的那段历史在哪几行"。
    archived_from_line: int | None = None
    archived_to_line: int | None = None


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
