"""单回合状态（L3）。

一个 :class:`Turn` 承载"一次用户输入引发的全部事情"：累积的消息、累计用量、
中断标志、后台任务。**循环本身无状态**（``KernelLoop`` 可以被复用），
所有会变的东西都在这里——这样"这一轮到底发生了什么"永远只有一个地方可查。

中断为什么用 ``asyncio.Task.cancel()`` 而不是协作式标志
-----------------------------------------------------
协作式标志（工具/循环定期检查 ``is_cancelled``）看起来更温和，但它**不解决**
最要命的情况：模型流正卡在 ``await`` 上等下一个分片，或某个工具正卡在
``await`` 上等子进程。此时只有 ``Task.cancel()`` 能把控制权拿回来——
``asyncio`` 会在**任何 await 点**抛出 :class:`asyncio.CancelledError`。

``interrupt`` 这个 ``Event`` 只是给"能自己查"的地方用的（例如 ``read`` 在读一个
超大文件时逐块检查），它是**优化而非保证**。
"""

from __future__ import annotations

import asyncio
import time
from enum import Enum

from logox.kernel.events import Usage
from logox.kernel.messages import Message

__all__ = ["Turn", "TurnStatus"]


class TurnStatus(str, Enum):
    """回合的终态。**没有"运行中"以外的中间态**——中间态由事件序列表达。"""

    RUNNING = "running"
    DONE = "done"
    CANCELLED = "cancelled"
    FAILED = "failed"


class Turn:
    """一次用户输入引发的全部状态。"""

    __slots__ = (
        "_cancelled_at",
        "history",
        "interrupt",
        "request_index",
        "started_at",
        "status",
        "task",
        "tool_call_count",
        "turn_index",
        "turn_summary",
        "usage_total",
    )

    def __init__(self, turn_index: int, history: list[Message] | None = None) -> None:
        self.turn_index = turn_index
        #: 本回合的消息视图（会话历史 + 本轮新增）。**是列表本身而非副本**：
        #: 内核往里追加，L2 的会话对象持同一份引用（避免每轮深拷贝整个历史）。
        self.history: list[Message] = history if history is not None else []
        self.status = TurnStatus.RUNNING
        self.started_at = time.perf_counter()
        self.usage_total = Usage(input_tokens=0, output_tokens=0)
        self.tool_call_count = 0
        #: 本回合内第几次模型请求（从 0 起）；事件里的 ``request_index`` 用它
        self.request_index = 0
        self.interrupt = asyncio.Event()
        self.task: asyncio.Task[None] | None = None
        self._cancelled_at: float | None = None
        self.turn_summary: str | None = None

    # ------------------------------------------------------------------ #
    # 中断
    # ------------------------------------------------------------------ #

    def cancel(self) -> bool:
        """请求取消本回合。**幂等**——第二次调用是空操作，返回 ``False``。

        返回「是否真的被这次调用取消了」而不是 ``None``，是为了让调用方（尤其是 UI）
        能区分"我按了 Esc"和"Esc 起作用了"——前者不值得给反馈，后者值得。
        """
        if self.status is not TurnStatus.RUNNING or self._cancelled_at is not None:
            return False
        self._cancelled_at = time.perf_counter()
        self.interrupt.set()
        if self.task is not None and not self.task.done():
            self.task.cancel()
        return True

    def mark_cancelled(self) -> None:
        """由循环在捕获 ``CancelledError`` 后调用，登记终态（不改状态以外的东西）。"""
        self.status = TurnStatus.CANCELLED
        if self._cancelled_at is None:
            self._cancelled_at = time.perf_counter()

    # ------------------------------------------------------------------ #
    # 度量
    # ------------------------------------------------------------------ #

    def add_usage(self, usage: Usage) -> None:
        """累计用量。

        ``cached_input_tokens`` 用**保守合并**：任一次未上报就整体记为未上报
        （``None``）。若把"未上报"当成 0 累加，一次没上报就会让整个回合的
        缓存命中率看着像暴跌——而 D39 要求 ``None`` 与 ``0`` 严格区分。
        """
        cached: int | None
        if self.usage_total.cached_input_tokens is None or usage.cached_input_tokens is None:
            cached = None
        else:
            cached = self.usage_total.cached_input_tokens + usage.cached_input_tokens

        reasoning: int | None
        if self.usage_total.reasoning_tokens is None and usage.reasoning_tokens is None:
            reasoning = None
        else:
            reasoning = (self.usage_total.reasoning_tokens or 0) + (usage.reasoning_tokens or 0)

        self.usage_total = Usage(
            input_tokens=self.usage_total.input_tokens + usage.input_tokens,
            output_tokens=self.usage_total.output_tokens + usage.output_tokens,
            cached_input_tokens=cached,
            reasoning_tokens=reasoning,
        )

    @property
    def duration_ms(self) -> int:
        end = self._cancelled_at if self._cancelled_at is not None else time.perf_counter()
        return max(0, int((end - self.started_at) * 1000))

    @property
    def running(self) -> bool:
        return self.status is TurnStatus.RUNNING
