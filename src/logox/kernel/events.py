"""事件定义——Logox 内核的对外契约（D7 / D25 / D39 / D41）。

本模块定义**全部 22 个事件**及其精确 Schema。所有跨模块数据都以这里的模型
为准；厂商特有字段一律不得进入（转换只发生在 ``providers/`` 适配器内部，D9）。

两条容易踩的规则
----------------
1. **``duration_ms`` 由发射端用 ``time.perf_counter()`` 差值计算并放入事件。**
   订阅者**禁止**用两个事件的 ``ts`` 相减——``ts`` 是墙钟时间，会被 NTP
   校正跳变，减出来的时长可能为负或翻倍。``ts`` 只用于展示与排序。
2. **``Usage.cached_input_tokens`` 的 ``None`` 与 ``0`` 语义不同。**
   ``None`` = 厂商未上报 → 状态栏 ``cache`` 项**整项不显示**；
   ``0`` = 上报了且确实零命中 → 显示 ``cache 0%``。
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Annotated, Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from logox.errors import UnknownEventError

__all__ = [
    "AnyEvent",
    "ChangeStat",
    "CheckpointCreated",
    "CompactionFinished",
    "CompactionStarted",
    "ContextBuilt",
    "EVENT_TYPES",
    "ErrorOccurred",
    "Event",
    "McpServerStateChanged",
    "ModelDelta",
    "ModelRequestFinished",
    "ModelRequestStarted",
    "PermissionRequested",
    "PermissionResolved",
    "QueueChanged",
    "RetryScheduled",
    "RewindPerformed",
    "SessionEnd",
    "SessionStart",
    "SubscriberQuarantined",
    "ToolCallFinished",
    "ToolCallRequested",
    "ToolCallStarted",
    "TurnFinished",
    "Usage",
    "UserPromptSubmit",
    "dump_event",
    "new_event_id",
    "parse_event",
]


def new_event_id() -> str:
    """事件唯一 ID（用于 telemetry 关联与去重）。"""
    return uuid.uuid4().hex


def _now() -> float:
    return time.time()


# --------------------------------------------------------------------------- #
# 基类与公共载荷
# --------------------------------------------------------------------------- #

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class Event(BaseModel):
    """全部事件的基类。

    ``frozen=True`` 是硬要求：订阅者**不得**篡改事件对象，否则「同一事件被
    下游改过」这类耦合会极难排查（E-16）。
    ``extra="forbid"`` 保证事件构造时的拼写错误当场暴露。
    """

    model_config = _FROZEN

    type: str
    version: int = 1
    event_id: str = Field(default_factory=new_event_id)
    session_id: str
    turn: int = 0
    ts: float = Field(default_factory=_now)


class Usage(BaseModel):
    """模型用量。"""

    model_config = _FROZEN

    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cached_input_tokens: int | None = Field(default=None, ge=0)
    reasoning_tokens: int | None = Field(default=None, ge=0)

    @property
    def cache_hit_ratio(self) -> float | None:
        """缓存命中率；**未上报时为 ``None``**（与「上报了且为 0」严格区分）。"""
        if self.cached_input_tokens is None or self.input_tokens <= 0:
            return None
        return self.cached_input_tokens / self.input_tokens


class ChangeStat(BaseModel):
    """文件变更统计——D40 的 ``diff +N -M`` 徽标的数据来源。"""

    model_config = _FROZEN

    kind: Literal["modify", "new", "rewrite", "binary", "large"]
    added: int = Field(ge=0)
    removed: int = Field(ge=0)
    bytes_before: int | None = None
    bytes_after: int | None = None


# --------------------------------------------------------------------------- #
# A. 会话类
# --------------------------------------------------------------------------- #


class SessionStart(Event):
    type: Literal["session_start"] = "session_start"

    cwd: str
    provider: str
    model: str
    thinking_effort: str = "auto"
    shell_backend: str = "unknown"
    memory_sources: list[str] = Field(default_factory=list)
    context_window: int = 200_000
    started_at: float = Field(default_factory=_now)
    terminal_caps: dict[str, str] = Field(default_factory=dict)
    resumed: bool = False


class SessionEnd(Event):
    type: Literal["session_end"] = "session_end"

    reason: Literal["user_quit", "error", "eof"]
    duration_ms: int = Field(ge=0)


# --------------------------------------------------------------------------- #
# B. 模型类
# --------------------------------------------------------------------------- #


class UserPromptSubmit(Event):
    type: Literal["user_prompt_submit"] = "user_prompt_submit"

    text: str
    text_chars: int = Field(ge=0)
    queued: bool = False  # D41：是否来自指令队列


class ContextBuilt(Event):
    type: Literal["context_built"] = "context_built"

    message_count: int = Field(ge=0)
    token_estimate: int = Field(ge=0)
    memory_sources: list[str] = Field(default_factory=list)
    pruned_count: int = Field(default=0, ge=0)


class ModelRequestStarted(Event):
    type: Literal["model_request_started"] = "model_request_started"

    provider: str
    model: str
    token_estimate: int = Field(ge=0)
    request_index: int = Field(ge=0)


class ModelDelta(Event):
    """流式增量。**总线不合并此事件**——节流由订阅者负责（UI 按 10 fps 重绘）。"""

    type: Literal["model_delta"] = "model_delta"

    kind: Literal["text", "reasoning", "tool_args"]
    delta: str
    request_index: int = Field(ge=0)


class ModelRequestFinished(Event):
    type: Literal["model_request_finished"] = "model_request_finished"

    usage: Usage
    duration_ms: int = Field(ge=0)
    first_token_ms: int | None = None
    stop_reason: str = "stop"
    cost_usd: float | None = None
    #: 厂商**是否真的上报了**用量。
    #:
    #: ``usage`` 是必填字段，所以"没上报"只能靠一个 0 值表示——而 0 值会被
    #: 状态栏显示成 ``0 tok``，**可能被误读为"真的没消耗"**（与 D39 的 cache 项
    #: 完全同类的问题）。有了这个标志，状态栏在 ``False`` 时显示 ``—``。
    #: 新增可选字段不升版本（ARCHITECTURE §6.2）。
    usage_reported: bool = True
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)

    @property
    def generation_ms(self) -> int:
        """**生成阶段**耗时（D39：``tok/s`` 只算生成，不含排队与首字等待）。"""
        if self.first_token_ms is None:
            return max(self.duration_ms, 1)
        return max(self.duration_ms - self.first_token_ms, 1)

    @property
    def tokens_per_second(self) -> float | None:
        """输出 token 吞吐量；无输出时为 ``None``（UI 显示 ``—``）。"""
        if self.usage.output_tokens <= 0:
            return None
        return self.usage.output_tokens / (self.generation_ms / 1000)


class TurnFinished(Event):
    type: Literal["turn_finished"] = "turn_finished"

    turn_index: int = Field(ge=1)
    duration_ms: int = Field(ge=0)
    tool_call_count: int = Field(ge=0)
    usage: Usage
    #: 回合为什么结束。
    #:
    #: 没有这个字段，界面就无法区分"正常说完"与"被 Esc 打断 / 出错了"——
    #: 而**取消不是错误**，它不会发 ``ErrorOccurred``，于是界面只能看到
    #: 一个没有下文的 ``TurnFinished``，转圈动画不知道该不该停。
    #: 新增可选字段不升版本（ARCHITECTURE §6.2）。
    reason: Literal["completed", "cancelled", "error"] = "completed"
    #: 本轮回合语义摘要（由模型终态产出或内核自动生成），供 UI/持久化/上下文压缩/回滚复用
    turn_summary: str | None = None


# --------------------------------------------------------------------------- #
# C. 工具与权限类
# --------------------------------------------------------------------------- #


class ToolCallRequested(Event):
    type: Literal["tool_call_requested"] = "tool_call_requested"

    call_id: str
    name: str
    args: dict[str, Any] = Field(default_factory=dict)
    readonly: bool = False
    batch_size: int = Field(default=1, ge=1)


class PermissionRequested(Event):
    type: Literal["permission_requested"] = "permission_requested"

    call_id: str
    prompt: str
    rule_id: str | None = None
    rule_scope: str | None = None
    risk: Literal["normal", "high"] = "normal"


class PermissionResolved(Event):
    type: Literal["permission_resolved"] = "permission_resolved"

    call_id: str
    decision: Literal["allow", "deny"]
    remember: Literal["once", "session", "project"] | None = None


class ToolCallStarted(Event):
    type: Literal["tool_call_started"] = "tool_call_started"

    call_id: str
    concurrent_group: int | None = None  # D27：并发组编号；串行时为 None


class ToolCallFinished(Event):
    type: Literal["tool_call_finished"] = "tool_call_finished"

    call_id: str
    ok: bool
    duration_ms: int = Field(ge=0)
    result_digest: str = ""
    content: str = ""
    error_kind: str | None = None
    change_stat: ChangeStat | None = None


# --------------------------------------------------------------------------- #
# D. 上下文与存储类
# --------------------------------------------------------------------------- #


class CompactionStarted(Event):
    type: Literal["compaction_started"] = "compaction_started"

    strategy: str
    tokens_before: int = Field(ge=0)
    message_count_before: int = Field(ge=0)


class CompactionFinished(Event):
    type: Literal["compaction_finished"] = "compaction_finished"

    tokens_after: int = Field(ge=0)
    message_count_after: int = Field(ge=0)
    degraded: bool = False  # 摘要失败 → 降级为纯裁剪


class CheckpointCreated(Event):
    type: Literal["checkpoint_created"] = "checkpoint_created"

    files: list[str] = Field(default_factory=list)
    truncated_count: int = Field(default=0, ge=0)
    path: str = ""
    before_hash: str | None = None
    after_hash: str = ""


class RewindPerformed(Event):
    type: Literal["rewind_performed"] = "rewind_performed"

    to_turn: int = Field(ge=0)
    restored: list[str] = Field(default_factory=list)
    deleted: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)


class QueueChanged(Event):
    """指令队列变化（D41）。"""

    type: Literal["queue_changed"] = "queue_changed"

    depth: int = Field(ge=0)
    action: Literal["enqueued", "sent", "removed", "cleared", "paused"]


# --------------------------------------------------------------------------- #
# E. 系统与错误类
# --------------------------------------------------------------------------- #


class RetryScheduled(Event):
    type: Literal["retry_scheduled"] = "retry_scheduled"

    attempt: int = Field(ge=1)
    delay_s: float = Field(ge=0)
    reason: str


class ErrorOccurred(Event):
    type: Literal["error_occurred"] = "error_occurred"

    category: str
    message: str
    retryable: bool = False
    detail: str | None = None


class McpServerStateChanged(Event):
    type: Literal["mcp_server_state_changed"] = "mcp_server_state_changed"

    server: str
    state: Literal["starting", "ready", "degraded", "stopped"]
    error: str | None = None
    tool_count: int = Field(default=0, ge=0)


class SubscriberQuarantined(Event):
    """订阅者连续失败被隔离（K3：降级必须可见，否则用户只会看到「界面不刷新了」）。"""

    type: Literal["subscriber_quarantined"] = "subscriber_quarantined"

    subscriber: str
    failures: int = Field(ge=1)
    last_error: str


# --------------------------------------------------------------------------- #
# 判别联合与序列化
# --------------------------------------------------------------------------- #

_EVENT_CLASSES: tuple[type[Event], ...] = (
    SessionStart,
    SessionEnd,
    UserPromptSubmit,
    ContextBuilt,
    ModelRequestStarted,
    ModelDelta,
    ModelRequestFinished,
    TurnFinished,
    ToolCallRequested,
    PermissionRequested,
    PermissionResolved,
    ToolCallStarted,
    ToolCallFinished,
    CompactionStarted,
    CompactionFinished,
    CheckpointCreated,
    RewindPerformed,
    QueueChanged,
    RetryScheduled,
    ErrorOccurred,
    McpServerStateChanged,
    SubscriberQuarantined,
)

AnyEvent: TypeAlias = Annotated[
    SessionStart | SessionEnd | UserPromptSubmit | ContextBuilt | ModelRequestStarted | ModelDelta | ModelRequestFinished | TurnFinished | ToolCallRequested | PermissionRequested | PermissionResolved | ToolCallStarted | ToolCallFinished | CompactionStarted | CompactionFinished | CheckpointCreated | RewindPerformed | QueueChanged | RetryScheduled | ErrorOccurred | McpServerStateChanged | SubscriberQuarantined,
    Field(discriminator="type"),
]

EVENT_TYPES: dict[str, type[Event]] = {
    cls.model_fields["type"].default: cls  # type: ignore[misc]
    for cls in _EVENT_CLASSES
}
"""``type`` 字符串 → 事件类。供 telemetry 反序列化与 ``/debug`` 过滤使用。"""

_ANY_EVENT_ADAPTER = TypeAdapter(AnyEvent)


def parse_event(payload: str | bytes | dict[str, Any]) -> AnyEvent:
    """从 JSON（或已解析的 dict）还原事件。

    遇到未知 ``type`` 时抛 :class:`~logox.errors.UnknownEventError`——
    **不静默跳过**（E-13）。
    """
    if isinstance(payload, (str, bytes)):
        data: Any = json.loads(payload)
    else:
        data = payload

    if not isinstance(data, dict):
        raise UnknownEventError(repr(data), tuple(EVENT_TYPES))

    type_name = data.get("type")
    if not isinstance(type_name, str) or type_name not in EVENT_TYPES:
        raise UnknownEventError(str(type_name), tuple(EVENT_TYPES))

    return _ANY_EVENT_ADAPTER.validate_python(data)


def dump_event(event: Event) -> str:
    """序列化为 JSON 文本（telemetry 落盘用）。"""
    return event.model_dump_json()
