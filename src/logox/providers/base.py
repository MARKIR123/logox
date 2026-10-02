"""Provider 薄协议与统一事件（D9 / D22 / D39 / D42）。

设计要点
--------
* **统一事件只描述"发生了什么"**，不携带厂商字段，也不做重试决策（N1）。
* **可注入的「原始分片源」接缝**（``RawStream``）：生产用官方 SDK，测试喂夹具。
  这样 SSE 装配、用量归一化、档位映射、错误分类**全部能在离线环境测到**——
  而这些正是适配层唯一容易出错的地方。
* ``ToolCallEvent`` 携带的是**装配完成的参数 dict**，不是 JSON 片段：把片段透传给
  内核等于把厂商细节漏到上层。
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Callable, Mapping
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from logox.errors import ErrorCategory
from logox.kernel.events import Usage

__all__ = [
    "ChatRequest",
    "DeltaEvent",
    "ModelInfo",
    "Provider",
    "ProviderErrorEvent",
    "ProviderEvent",
    "RawStream",
    "StopEvent",
    "StopReason",
    "Stopwatch",
    "ThinkingConfig",
    "ToolCallBuffer",
    "ToolCallEvent",
    "UsageEvent",
    "int_or_none",
    "normalize_stop_reason",
]

_FROZEN = ConfigDict(frozen=True, extra="forbid")

StopReason = Literal["end_turn", "tool_use", "max_tokens", "stop_sequence", "unknown"]

#: 结束原因归一化。厂商用词不同（``stop`` / ``tool_calls`` / ``end_turn`` …），
#: 内核需要的是**语义**而不是各家字符串。
_STOP_REASONS: dict[str, StopReason] = {
    # OpenAI 兼容
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "content_filter": "end_turn",
    # Anthropic
    "end_turn": "end_turn",
    "tool_use": "tool_use",
    "max_tokens": "max_tokens",
    "stop_sequence": "stop_sequence",
    "pause_turn": "end_turn",
    "refusal": "end_turn",
}


def normalize_stop_reason(raw: str | None) -> StopReason:
    """把厂商的结束原因映射为统一语义；未知值归一化为 ``unknown``（E-4，不崩）。"""
    if not raw:
        return "unknown"
    return _STOP_REASONS.get(raw, "unknown")


def int_or_none(value: Any) -> int | None:
    """尽力取整数；**取不到就是 ``None``，绝不用 0 冒充**（D39 / E-7）。

    这一条直接决定状态栏 ``cache`` 项是否显示：``None`` → 整项不显示，
    ``0`` → 显示 ``cache 0%``。二者语义完全不同。
    """
    if value is None:
        return None
    if isinstance(value, bool):  # bool 是 int 子类，必须先行排除
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _dig(source: Any, *path: str) -> Any:
    """安全地沿路径取值（任一层缺失都返回 ``None``，不抛异常）。

    同时支持 dict 与对象：厂商 SDK 有时给 dict（``model_dump()`` 的结果），
    有时给 pydantic 对象（测试里直接构造的），两种都要能取。
    """
    current = source
    for key in path:
        current = current.get(key) if isinstance(current, Mapping) else getattr(current, key, None)
        if current is None:
            return None
    return current


__all__ += ["dig"]
dig = _dig


# --------------------------------------------------------------------------- #
# 统一事件
# --------------------------------------------------------------------------- #


class ProviderEvent(BaseModel):
    """适配器产出的中立事件基类。"""

    model_config = _FROZEN


class DeltaEvent(ProviderEvent):
    """流式增量。``kind`` 区分正文与推理内容（D31）。

    ``vendor_data`` 是**厂商不透明数据的受限通道**：目前只用于承载
    Anthropic 的 thinking ``signature``——它必须在下一轮请求里**原样回传**，
    否则工具调用会失败。内核只负责把它存进 ``ReasoningBlock.signature`` 再传回来，
    **绝不解释其含义**。这是一处刻意保留的例外，且仅此一处。

    签名的传递方式（Anthropic 专用，内核需照此实现）
    ------------------------------------------------
    厂商在思考文本**之后**才下发 ``signature_delta``，因此无法附在已有增量上。
    约定：**``kind="reasoning"`` 且 ``text==""``、``vendor_data`` 含 ``signature``
    的增量不携带任何思考文本，它只表示"把该签名设到紧邻上文那个推理块上"。**
    内核遇到它时既不新建推理块、也不追加文本，只更新签名。
    """

    kind: Literal["text", "reasoning"]
    text: str
    vendor_data: dict[str, Any] | None = None


class ToolCallEvent(ProviderEvent):
    """**装配完成**的工具调用：``arguments`` 是解析好的 dict，不是 JSON 片段。"""

    call_id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    index: int = 0


class UsageEvent(ProviderEvent):
    usage: Usage


class StopEvent(ProviderEvent):
    stop_reason: StopReason
    model: str = ""


class ProviderErrorEvent(ProviderEvent):
    """错误**分类**结果——不含重试决策（那属于内核，D22）。"""

    category: ErrorCategory
    message: str
    retry_after_s: float | None = None
    detail: str | None = None
    is_truncated: bool = False


# --------------------------------------------------------------------------- #
# 请求与模型信息
# --------------------------------------------------------------------------- #


class ThinkingConfig(BaseModel):
    """思考档位（D42）。``auto`` 表示不干预，交由模型自身决定。"""

    model_config = _FROZEN

    effort: Literal["off", "low", "medium", "high", "auto"] = "auto"

    @property
    def active(self) -> bool:
        """是否需要向厂商传递参数。``auto`` 不需要。"""
        return self.effort != "auto"


class ChatRequest(BaseModel):
    """一次模型请求（中立形式）。"""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    model: str
    system: str = ""
    messages: list[Any] = Field(default_factory=list)  # list[Message]，避免此处循环导入
    tools: list[Any] = Field(default_factory=list)  # list[ToolSpec]
    temperature: float | None = None
    max_tokens: int | None = None
    thinking: ThinkingConfig | None = None


class ModelInfo(BaseModel):
    """模型元信息。``context_window`` 供 ``context/`` 计算占用率（D39 需要）。"""

    model_config = _FROZEN

    id: str
    provider: str = ""
    supports_thinking: bool = False
    context_window: int | None = None
    input_price_per_mtok: float | None = None
    output_price_per_mtok: float | None = None


# --------------------------------------------------------------------------- #
# 可测试的接缝
# --------------------------------------------------------------------------- #

RawChunks = AsyncIterator[Mapping[str, Any]]
"""厂商原始分片流（每块是一个 dict）。生产由官方 SDK 提供，测试由夹具提供。"""

RawStream = Callable[[ChatRequest], RawChunks]
"""把请求变成一个原始分片流。**这是本模块最重要的可测试性设计。**"""


class Provider(Protocol):
    """模型适配器的统一接口。"""

    name: str

    def list_models(self) -> list[ModelInfo]: ...

    def stream(self, request: ChatRequest) -> AsyncIterator[ProviderEvent]: ...

    def window_for(self, model_id: str) -> int | None: ...


# --------------------------------------------------------------------------- #
# 工具调用装配
# --------------------------------------------------------------------------- #


class ToolCallBuffer:
    """把厂商分片下发的工具调用**装配**成一个完整调用。

    厂商把工具参数当作增量 JSON 字符串分片下发（``{"pa`` → ``th": "a`` → ``.py"}``），
    因此必须在这里拼完再交给上层。装配失败的调用**绝不半成品外泄**（E-2）。
    """

    __slots__ = ("_fragments", "call_id", "index", "name")

    def __init__(self, index: int, *, call_id: str = "", name: str = "") -> None:
        self.index = index
        self.call_id = call_id
        self.name = name
        self._fragments: list[str] = []

    @property
    def arguments(self) -> str:
        """完整参数文本；分片到齐后再合并，避免每次追加复制全部前缀。"""
        return "".join(self._fragments)

    @arguments.setter
    def arguments(self, value: str) -> None:
        self._fragments = [value] if value else []

    def merge(self, *, call_id: str | None = None, name: str | None = None, fragment: str | None = None) -> None:
        """并入一个分片。

        工具名由厂商**一次性下发完整值**（OpenAI 在首个分片、Anthropic 在
        ``content_block_start``），因此这里是"非空即替换"，而不是拼接——
        拼接会在厂商重发同名时把名字弄成 ``readread``。
        """
        if call_id:
            self.call_id = call_id
        if name:
            self.name = name
        if fragment:
            self._fragments.append(fragment)

    def finalize(self) -> ToolCallEvent | dict[str, str]:
        """产出 :class:`ToolCallEvent`；参数无法解析时**返回错误描述**而不是半成品。"""
        if not self.name:
            return {"error": "工具调用缺少函数名", "detail": self.arguments[:500]}
        raw = self.arguments.strip()
        if not raw:
            parsed: dict[str, Any] = {}  # 无参数是合法的（E-10）
        else:
            try:
                candidate = json.loads(raw)
            except json.JSONDecodeError as exc:
                return {
                    "error": f"工具参数不是合法 JSON：{exc.msg}",
                    "detail": raw[:500],
                }
            if not isinstance(candidate, dict):
                return {"error": "工具参数应当是 JSON 对象", "detail": raw[:500]}
            parsed = candidate
        return ToolCallEvent(
            call_id=self.call_id or f"call_{self.index}",
            name=self.name,
            arguments=parsed,
            index=self.index,
        )


class Stopwatch:
    """记录**首个内容增量**的时刻，供 D39 的 ``first_token_ms`` 使用。

    ``tok/s`` 只算生成阶段（不含排队与首字等待），所以必须精确标记"第一个字到了"。

    归属说明：适配器**不再**自己计时——统一事件里没有时间字段，适配器算了也没人读。
    内核循环（M3）在发起请求时归零、收到第一个 ``ModelDelta`` 时打点，同样精确，
    且不必让每个适配器各维护一份状态。此构件即供那一处使用。

    ``clock`` 可注入是为了让"首字延迟"这类时长能在测试里**确定性地**断言——
    依赖真实时钟的时长断言早晚会变成随机失败的测试。
    """

    __slots__ = ("_clock", "_first_token_at", "_started_at")

    def __init__(self, clock: Callable[[], float] | None = None) -> None:
        self._clock: Callable[[], float] = clock or time.perf_counter
        self._started_at = self._clock()
        self._first_token_at: float | None = None

    def mark(self) -> None:
        """在产出第一个内容增量时调用（可重复调用，只记第一次）。"""
        if self._first_token_at is None:
            self._first_token_at = self._clock()

    def reset(self) -> None:
        """为新一次请求归零（同一会话会连续发很多次请求）。"""
        self._started_at = self._clock()
        self._first_token_at = None

    @property
    def elapsed_ms(self) -> int:
        return max(0, int((self._clock() - self._started_at) * 1000))

    @property
    def first_token_ms(self) -> int | None:
        if self._first_token_at is None:
            return None
        return max(0, int((self._first_token_at - self._started_at) * 1000))
