"""事件 → 会话度量的归约（D39）。

这是状态栏**唯一**的数据来源。它把事件流折算成一组可直接渲染的数值，
因此状态栏组件完全不需要理解事件语义——这正是 D7 事件总线设计的回报。

三条必须守住的口径（都在 M1 的事件契约里定死了）
------------------------------------------------
1. **``cached_input_tokens`` 的 ``None`` 与 ``0`` 语义不同**：``None`` = 厂商未上报
   → 状态栏 ``cache`` 项**整项不显示**；``0`` = 上报了确实零命中 → 显示 ``cache 0%``。
2. **``tok/s`` 只算生成阶段**（不含排队与首字等待），且只从事件的 ``duration_ms`` 算，
   **绝不用 ``ts`` 相减**——墙钟会被 NTP 校正跳变（K4）。
3. **用量只在 ``ModelRequestFinished`` 累加**，``TurnFinished.usage`` 不再累加
   （它是回合合计，加两次会把用量翻倍）。

线程模型：``handle`` 是 async 但**内部无 await**，且只做算术——因此它注册为
``blocking=True`` 也不会拖慢总线。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from logox.kernel.events import (
    AnyEvent,
    CompactionFinished,
    ContextBuilt,
    ErrorOccurred,
    McpServerStateChanged,
    ModelDelta,
    ModelRequestFinished,
    ModelRequestStarted,
    PermissionRequested,
    PermissionResolved,
    QueueChanged,
    RetryScheduled,
    SessionStart,
    SubscriberQuarantined,
    ToolCallFinished,
    ToolCallRequested,
    ToolCallStarted,
    TurnFinished,
    UserPromptSubmit,
)

__all__ = ["DEFAULT_CONTEXT_WINDOW", "MetricsReducer", "RetryState", "SessionMetrics"]

DEFAULT_CONTEXT_WINDOW = 200_000
"""默认上下文窗口（token）。M2 接入 Provider 后应改为从模型信息读取。"""


class RetryState(BaseModel):
    """正在进行的重试（状态栏右端显示倒计时）。"""

    model_config = ConfigDict(frozen=True)

    attempt: int
    delay_s: float
    reason: str


class SessionMetrics(BaseModel):
    """一次会话的全部可渲染度量。**不可变快照语义**——状态栏只读它。"""

    model_config = ConfigDict(extra="forbid")

    # 环境
    provider: str = ""
    model: str = ""
    thinking_effort: str = "auto"
    shell_backend: str = ""

    # 轮次与耗时
    turn: int = 1
    total_ms: int = 0
    llm_ms: int = 0
    tool_ms: int = 0

    # 用量
    usage_input: int = 0
    usage_output: int = 0
    cached_input: int | None = None
    cost_usd: float = 0.0
    throughput: float | None = None

    # 上下文
    context_tokens: int = 0
    context_window: int = DEFAULT_CONTEXT_WINDOW

    # 运行态
    generating: bool = False
    running_tool: str | None = None
    tool_calls: int = 0
    queue_depth: int = 0
    pending_permissions: int = 0
    permission_mode: str = "default"
    retry: RetryState | None = None
    last_error: str | None = None

    # 计数与集合
    compact_count: int = 0
    memory_source_count: int = 0
    degraded_mcp: list[str] = Field(default_factory=list)
    quarantined: list[str] = Field(default_factory=list)

    # -- 派生值 --------------------------------------------------------- #

    @property
    def cache_ratio(self) -> float | None:
        """缓存命中率；**未上报时为 ``None``**（UI 应整项隐藏，而非显示 0%）。"""
        if self.cached_input is None or self.usage_input <= 0:
            return None
        return self.cached_input / self.usage_input

    @property
    def context_ratio(self) -> float:
        if self.context_window <= 0:
            return 0.0
        return min(1.0, self.context_tokens / self.context_window)

    @property
    def total_tokens(self) -> int:
        return self.usage_input + self.usage_output

    @property
    def busy(self) -> bool:
        """是否有东西正在跑（状态栏据此决定是否显示 ``esc to interrupt``）。"""
        return self.generating or self.running_tool is not None


class MetricsReducer:
    """把事件流归约成 :class:`SessionMetrics`（总线订阅者）。

    典型用法::

        reducer = MetricsReducer(context_window=200_000)
        bus.subscribe(Event, reducer.handle, name="metrics", priority=PRIORITY_BUILTIN)
    """

    def __init__(self, *, context_window: int = DEFAULT_CONTEXT_WINDOW) -> None:
        self.metrics = SessionMetrics(context_window=context_window)
        # ToolCallStarted 只带 call_id 不带工具名，因此需要从 ToolCallRequested 记住映射。
        self._call_names: dict[str, str] = {}

    # ------------------------------------------------------------------ #
    # 总线订阅者入口
    # ------------------------------------------------------------------ #

    async def handle(self, event: AnyEvent) -> None:
        """总线订阅者入口（异步）。内部**无 await**，因此不会拖慢总线。"""
        self.apply(event)

    def apply(self, event: AnyEvent) -> None:
        """同步归约一个事件。

        与 :meth:`handle` 是同一套逻辑——之所以同时提供同步入口，是为了让
        **确定性回放**（截图、测试、离线重放）不必为每条事件起一个事件循环。
        """
        handler = _HANDLERS.get(type(event))
        if handler is not None:
            handler(self, event)  # type: ignore[arg-type]

    # ------------------------------------------------------------------ #
    # 各事件的处理
    # ------------------------------------------------------------------ #

    def _on_session_start(self, event: SessionStart) -> None:
        context_window = getattr(event, "context_window", None) or self.metrics.context_window
        # 保持同一个 SessionMetrics 实例引用不变（就地重置与更新），避免外部持有者（如 StatusComponent）对象脱节
        self.metrics.context_window = context_window
        self.metrics.provider = event.provider
        self.metrics.model = event.model
        self.metrics.thinking_effort = event.thinking_effort
        self.metrics.shell_backend = event.shell_backend
        self.metrics.memory_source_count = len(event.memory_sources)
        if not getattr(event, "resumed", False):
            self.metrics.context_tokens = 0
            self.metrics.turn = 1
        self.metrics.usage_input = 0
        self.metrics.usage_output = 0
        self.metrics.cached_input = None
        self.metrics.cost_usd = 0.0
        self.metrics.throughput = None
        self.metrics.total_ms = 0
        self.metrics.llm_ms = 0
        self.metrics.tool_ms = 0
        self.metrics.generating = False
        self.metrics.running_tool = None
        self.metrics.tool_calls = 0
        self.metrics.queue_depth = 0
        self.metrics.pending_permissions = 0
        self.metrics.retry = None
        self.metrics.last_error = None
        self.metrics.compact_count = 0
        self.metrics.degraded_mcp = []
        self.metrics.quarantined = []
        self._call_names.clear()

    def _on_user_prompt(self, event: UserPromptSubmit) -> None:
        self.metrics.running_tool = None
        self.metrics.generating = True

    def _on_context_built(self, event: ContextBuilt) -> None:
        self.metrics.context_tokens = event.token_estimate

    def _on_request_started(self, event: ModelRequestStarted) -> None:
        self.metrics.generating = True
        self.metrics.running_tool = None
        self.metrics.retry = None

    def _on_delta(self, event: ModelDelta) -> None:
        # 只要收到增量就说明在生成——可修正"请求已发出但首字未到"的空窗。
        self.metrics.generating = True

    def _on_request_finished(self, event: ModelRequestFinished) -> None:
        metrics = self.metrics
        metrics.generating = False
        metrics.llm_ms += event.duration_ms
        # 用量只在这里累加（TurnFinished.usage 是回合合计，加两次会翻倍）
        metrics.usage_input += event.usage.input_tokens
        metrics.usage_output += event.usage.output_tokens
        if event.usage.input_tokens > 0:
            # ★ 优先用“**上下文总量**”（CHANGE-005）：厂商口径不同 ——
            #   OpenAI 兼容的 `input_tokens` 本身是总量，Anthropic 的**不含**缓存读写。
            #   之前直接拿 `input_tokens` 当上下文大小，于是接 Anthropic 端点时
            #   状态栏那个百分比会**偏低**（命中缓存越多偏得越离谱）——
            #   而用户正是靠它判断“要不要压缩 / 刚才是压过了”。
            #   缺失（老适配器 / 未上报）才退回旧口径。
            total = event.usage.context_tokens
            metrics.context_tokens = total if total is not None else event.usage.input_tokens
        if event.usage.cached_input_tokens is not None:
            metrics.cached_input = (metrics.cached_input or 0) + event.usage.cached_input_tokens
        if event.cost_usd is not None:
            metrics.cost_usd += event.cost_usd
        metrics.throughput = event.tokens_per_second

    def _on_tool_requested(self, event: ToolCallRequested) -> None:
        self._call_names[event.call_id] = event.name

    def _on_tool_started(self, event: ToolCallStarted) -> None:
        self.metrics.running_tool = self._call_names.get(event.call_id, "tool")

    def _on_tool_finished(self, event: ToolCallFinished) -> None:
        self.metrics.tool_calls += 1
        self.metrics.tool_ms += event.duration_ms
        self.metrics.running_tool = None
        self._call_names.pop(event.call_id, None)

    def _on_turn_finished(self, event: TurnFinished) -> None:
        self.metrics.turn = event.turn_index + 1
        self.metrics.total_ms += event.duration_ms
        self.metrics.generating = False
        self.metrics.running_tool = None

    def _on_permission_requested(self, event: PermissionRequested) -> None:
        self.metrics.pending_permissions += 1

    def _on_permission_resolved(self, event: PermissionResolved) -> None:
        self.metrics.pending_permissions = max(0, self.metrics.pending_permissions - 1)

    def _on_compaction_finished(self, event: CompactionFinished) -> None:
        self.metrics.compact_count += 1
        self.metrics.context_tokens = event.tokens_after

    def _on_retry(self, event: RetryScheduled) -> None:
        self.metrics.retry = RetryState(attempt=event.attempt, delay_s=event.delay_s, reason=event.reason)

    def _on_error(self, event: ErrorOccurred) -> None:
        self.metrics.last_error = f"{event.category}: {event.message}"

    def _on_queue_changed(self, event: QueueChanged) -> None:
        self.metrics.queue_depth = event.depth

    def _on_mcp_state(self, event: McpServerStateChanged) -> None:
        degraded = [name for name in self.metrics.degraded_mcp if name != event.server]
        if event.state in ("degraded", "stopped"):
            degraded.append(event.server)
        self.metrics.degraded_mcp = degraded

    def _on_quarantined(self, event: SubscriberQuarantined) -> None:
        if event.subscriber not in self.metrics.quarantined:
            self.metrics.quarantined = [*self.metrics.quarantined, event.subscriber]


_HANDLERS: dict[type, object] = {
    SessionStart: MetricsReducer._on_session_start,
    UserPromptSubmit: MetricsReducer._on_user_prompt,
    ContextBuilt: MetricsReducer._on_context_built,
    ModelRequestStarted: MetricsReducer._on_request_started,
    ModelDelta: MetricsReducer._on_delta,
    ModelRequestFinished: MetricsReducer._on_request_finished,
    ToolCallRequested: MetricsReducer._on_tool_requested,
    ToolCallStarted: MetricsReducer._on_tool_started,
    ToolCallFinished: MetricsReducer._on_tool_finished,
    TurnFinished: MetricsReducer._on_turn_finished,
    PermissionRequested: MetricsReducer._on_permission_requested,
    PermissionResolved: MetricsReducer._on_permission_resolved,
    CompactionFinished: MetricsReducer._on_compaction_finished,
    RetryScheduled: MetricsReducer._on_retry,
    ErrorOccurred: MetricsReducer._on_error,
    QueueChanged: MetricsReducer._on_queue_changed,
    McpServerStateChanged: MetricsReducer._on_mcp_state,
    SubscriberQuarantined: MetricsReducer._on_quarantined,
}
