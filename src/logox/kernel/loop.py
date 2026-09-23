"""Agent 循环——内核唯一主干（D7 / D22 / D27 / D39 / D51 / D52）。

它做四件事，且**只做**这四件事：
1. 拿到本轮消息（由注入的 ``ContextBuilder`` 组装，内核不生产上下文）
2. 调 Provider，把 ``ProviderEvent`` 翻译成总线事件，并按 D52 的边界决定是否重试
3. 把模型要的工具交给 ``Scheduler`` 执行，结果回灌
4. 直到模型不再要工具为止

三条最容易写错的地方（都有专门用例盯着）
--------------------------------------
1. **``CancelledError`` 绝不能被当成普通异常吞掉。** 吞掉它 = 按 Esc 静默失效。
   这里只在一个地方捕获它，而且捕获后立刻转成"回合被取消"这个**一等结果**。
2. **重试只看一个条件：有没有已经吐过内容**（D52）。吐过了就不重试——
   宁可让用户手动重发，也绝不让他看到两段重复甚至矛盾的文本。
3. **中断后必须补齐工具结果**（E-3）。少一条 ``tool_result``，
   **下一轮请求会被厂商直接 400**——而这条历史会一直留在会话里，
   等于"按一次 Esc 让本次会话从此再也发不出请求"。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from logox.errors import ErrorCategory, LogoxError
from logox.kernel import events as ev
from logox.kernel.bus import EventBus
from logox.kernel.messages import (
    Message,
    MessageMeta,
    ReasoningBlock,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from logox.kernel.registry import ToolRegistry
from logox.kernel.scheduler import AllowAllDecider, BlobStoreProtocol, PermissionDecider, Scheduler
from logox.kernel.turn import Turn, TurnStatus
from logox.kernel.summary import (
    SUMMARY_MODEL_FALLBACK_MIN_CHARS,
    SUMMARY_SYSTEM_PROMPT,
    deterministic_summary,
    extract_trailing_summary,
    interrupted_summary,
    normalize_model_summary,
    render_turn_transcript,
)
from logox.providers.base import (
    ChatRequest,
    DeltaEvent,
    Provider,
    ProviderErrorEvent,
    StopEvent,
    Stopwatch,
    ThinkingConfig,
    ToolCallEvent,
    UsageEvent,
)

logger = logging.getLogger(__name__)

__all__ = [
    "ContextBuilder",
    "ContextBundle",
    "CostEstimator",
    "KernelLoop",
    "SimpleContextBuilder",
    "TurnInProgressError",
]

#: 中断时合成的工具结果文案。措辞刻意只说"没有返回结果"——
#: 因为我们**无法区分**那个工具是"根本没开始"还是"跑到一半被打断"，
#: 而说错会让模型基于一个错误的假设继续推理。
INTERRUPTED_TOOL_CONTENT = "该工具没有返回结果：这一轮被用户中断了。"


class TurnInProgressError(LogoxError):
    """上一个回合还没结束时又提交了新输入。

    M3 **直接拒绝**而不入队：指令队列（D41）是 M4 的界面能力，内核此时若悄悄排队，
    UI 就无法知道"还有几条在等"，D41 的队列提示框会少显示内容。
    """

    def __init__(self, turn_index: int) -> None:
        super().__init__(
            f"第 {turn_index} 轮还在进行中，无法提交新输入。"
            "请等它结束，或先中断它（Esc / cancel()）。"
        )
        self.turn_index = turn_index


# --------------------------------------------------------------------------- #
# 注入点：上下文组装（M7 的真实实现落在 context/builder.py）
# --------------------------------------------------------------------------- #


class CompactionReport(BaseModel):
    """一次压缩的**事实报告**（D156）。

    **为什么由 context 层产出、内核层发布**：压缩发生在 `context/`（L4），
    而事件总线属于内核（L3）。让 L4 直接拿总线等于让下层反向依赖上层
    （与"`kernel/events.py` 不能 import `tools`"是同一条规矩）。
    所以这里定义**内核侧的中性数据**，由 L4 填写、L3 发布 —— 既守住分层，
    也让"到底有没有发生压缩、压了多少"变成可单测的事实。

    ⚠️ 背景（F-54）：在这之前 `CompactionStarted` / `CompactionFinished`
    **全项目没有任何发布者**，于是四条订阅线全是死的 ——
    时间线看不到压缩提示、`compact_count` 永远是 0、`pre_compact` 钩子永不触发、
    事后也无从查证。本报告就是那根缺失的线。
    """

    model_config = ConfigDict(frozen=True)

    tokens_before: int = Field(ge=0)
    tokens_after: int = Field(ge=0)
    message_count_before: int = Field(ge=0)
    message_count_after: int = Field(ge=0)
    #: 被修剪（归档/掏空）的工具结果条数
    pruned_count: int = Field(default=0, ge=0)
    #: 本次折叠掉的轮数（0 = 只做了工具修剪，没有折叠）
    folded_turns: int = Field(default=0, ge=0)
    #: ``"prune"`` 或 ``"prune+fold"``
    strategy: str = "prune"
    #: 折叠时是否用了**确定性兜底摘要**（某几轮没有 `turn_summary`）
    degraded: bool = False


class CompactionPlan(BaseModel):
    """"这次组装**将要**发生压缩"的预测（D167）。

    为什么单独一个类型：`pre_compact` 钩子需要在**改动历史之前**拿到"将要用多少 token、
    涉及多少条消息"，而压缩本身发生在 `context/` 层（L4）里、总线属于内核（L3）。
    于是由 builder 产出这份**纯数据**、内核据此发布 `CompactionStarted` —— 与
    `CompactionReport` 是同一条分层规矩（L4 不碰总线）。
    """

    model_config = ConfigDict(frozen=True)

    #: 预测的压缩前规模（锚点 + 增量）
    tokens_before: int = Field(ge=0)
    message_count_before: int = Field(ge=0)


class ContextBundle(BaseModel):
    """组装好的模型输入。"""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    system: str = ""
    #: **包含**本轮的用户消息（由 builder 决定怎么表示）
    messages: list[Message] = Field(default_factory=list)
    #: token 估算。**未估算时是 0**，调用方据此显示 `—` 而不是一个假数字
    token_estimate: int = 0
    memory_sources: list[str] = Field(default_factory=list)
    pruned_count: int = 0
    #: ★ D156：本次组装**是否发生了压缩**（None = 没有）。内核据此发布 `CompactionFinished`。
    compaction: CompactionReport | None = None


class ContextBuilder(Protocol):
    """上下文组装。**内核只调用它，不实现它**（L3 不认识 L4 的 context/）。

    ``last_usage``（CHANGE-005 裁定 1）：上一次模型请求厂商上报的真实用量。
    压缩的触发判据要用它，因为**纯估算最坏会低估一半**（估算器的校准系数
    被夹在 ``[0.5, 2.0]`` 里），而固定 reserve 拦不住“低估一半”。
    适配层不认字段名的实现（如 ``SimpleContextBuilder``）直接忽略即可。
    """

    def build(
        self, history: list[Message], *, last_usage: ev.Usage | None = None
    ) -> ContextBundle: ...


class CostEstimator(Protocol):
    """费用估算的注入点。

    **内核不自己算钱**：价格表属于 L5 适配层（`providers/pricing.py`），内核引用它
    就等于 L3 反向依赖 L5。所以这里只收一个纯函数——装配根把
    ``providers.pricing.estimate_cost_usd`` 传进来即可。

    不传就**不算**（``cost_usd=None``）。这比"内核import一张价格表然后算出一个
    没人复核的数字"要诚实：费用本来就是估算，谁来估应当是可替换的。
    """

    def __call__(self, usage: ev.Usage, model: str) -> float | None: ...


class SimpleContextBuilder:
    """M3 的最小实现：system 直通 + 历史直通，**不裁剪、不估算**。

    ``token_estimate`` 如实返回 ``0`` 表示"未估算"。编一个假的数字会让状态栏的
    上下文占用率看起来煞有介事，而它是错的——那比显示 `—` 糟糕得多。
    """

    def __init__(self, system: str = "", *, memory_sources: list[str] | None = None) -> None:
        self._system = system
        self._memory_sources = list(memory_sources or [])

    def build(
        self, history: list[Message], *, last_usage: ev.Usage | None = None
    ) -> ContextBundle:
        return ContextBundle(
            system=self._system,
            messages=list(history),
            token_estimate=0,
            memory_sources=list(self._memory_sources),
        )


# --------------------------------------------------------------------------- #
# 模型阶段的结果
# --------------------------------------------------------------------------- #


class _ModelOutcome(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    text: str = ""
    reasoning: str = ""
    reasoning_signature: str | None = None
    tool_calls: list[ToolCallEvent] = Field(default_factory=list)
    stop_reason: str = "unknown"
    usage: ev.Usage | None = None
    #: 模型请求本身成功（不代表模型没报错）——失败时整个回合已经结束了
    ok: bool = True


class _Accumulator:
    """模型流式产出的**可变**累积器。

    它之所以是一个独立对象而不是 ``_consume`` 里的局部变量，是因为**取消**：
    流被中断时局部变量会随栈一起消失，而用户已经在屏幕上看到了那半句话。
    D51 要求"用户看到什么就留下什么"，所以累积器必须能被中断路径拿到并落地成消息。

    （一个对象换来的是"按 Esc 不会抹掉你刚读到的内容"。）
    """

    __slots__ = ("reasoning", "signature", "text", "tool_calls")

    def __init__(self) -> None:
        self.text: list[str] = []
        self.reasoning: list[str] = []
        self.signature: str | None = None
        self.tool_calls: list[ToolCallEvent] = []

    @property
    def empty(self) -> bool:
        return not (self.text or self.reasoning or self.tool_calls)

    def outcome(self, *, stop_reason: str, usage: ev.Usage | None) -> _ModelOutcome:
        return _ModelOutcome(
            text="".join(self.text),
            reasoning="".join(self.reasoning),
            reasoning_signature=self.signature,
            tool_calls=list(self.tool_calls),
            stop_reason=stop_reason,
            usage=usage,
        )


class _ProviderFailure(Exception):
    """把 :class:`ProviderErrorEvent` 变成可 ``except`` 的东西，便于统一重试逻辑。"""

    def __init__(self, event: ProviderErrorEvent, acc: _Accumulator | None = None) -> None:
        super().__init__(event.message)
        self.event = event
        self.acc = acc

    @property
    def category(self) -> ErrorCategory:
        return self.event.category

    @property
    def is_truncated(self) -> bool:
        return bool(getattr(self.event, "is_truncated", False)) or ("Token 上限" in self.event.message)


# --------------------------------------------------------------------------- #
# 内核循环
# --------------------------------------------------------------------------- #


class KernelLoop:
    """一次会话的 Agent 循环。**本身无状态**——所有会变的东西都在 :class:`Turn` 里。"""

    def __init__(
        self,
        bus: EventBus,
        provider: Provider,
        registry: ToolRegistry,
        context_builder: ContextBuilder,
        decider: PermissionDecider | None = None,
        *,
        model: str = "",
        temperature: float | None = None,
        max_tokens: int | None = None,
        thinking: ThinkingConfig | None = None,
        max_iterations: int = 50,
        max_retries: int = 3,
        retry_base_s: float = 0.5,
        retry_max_s: float = 30.0,
        concurrency: int = 4,
        cwd: Path | str = ".",
        cost_estimator: CostEstimator | None = None,
        sleep_fn: Callable[[float], Awaitable[None]] | None = None,
        clock: Callable[[], float] | None = None,
        jitter: Callable[[], float] | None = None,
        blob_store: BlobStoreProtocol | None = None,
        model_summary_fallback: bool = True,
    ) -> None:
        if max_iterations < 1:
            raise ValueError("max_iterations 必须 ≥ 1")
        #: 第 2 层兜底（再调一次模型补写摘要）是否启用。
        #:
        #: 默认开 —— 它是「本来没拿到摘要」时的补救；关掉就只剩本地生成（第 3 层）。
        #: 为什么做成开关而不是写死：这是一次**额外计费请求**，
        #: 关心成本的用户、以及大量脚本化测试，都需要能关。
        self._model_summary_fallback = model_summary_fallback
        if max_retries < 0:
            raise ValueError("max_retries 不能为负")

        self._bus = bus
        self._provider = provider
        self._registry = registry
        self._builder = context_builder
        self._decider: PermissionDecider = decider if decider is not None else AllowAllDecider()
        self._model = model
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._thinking = thinking
        self._max_iterations = max_iterations
        self._max_retries = max_retries
        self._retry_base_s = retry_base_s
        self._retry_max_s = retry_max_s
        self._cost_estimator = cost_estimator
        self._clock = clock or time.perf_counter
        self._sleep = sleep_fn or asyncio.sleep
        self._jitter = jitter or (lambda: 1.0)
        self._blob_store = blob_store

        self._scheduler = Scheduler(
            bus, registry, self._decider, concurrency=concurrency, cwd=cwd, blob_store=blob_store
        )


        #: 会话消息历史。L2 的会话对象与持久化订阅者读它；内核只往里追加。
        self.history: list[Message] = []
        #: ★ CHANGE-005：**上一次模型请求厂商上报的真实用量**。
        #: 下一次组装上下文时把它交给 builder，作为压缩的触发判据。
        #: 刻意保存**原始值**（未上报就是 None，而不是 0）—— 0 会被读成"上下文是空的"，
        #: 于是压缩永远不触发。
        self._last_request_usage: ev.Usage | None = None
        self._turns: list[Turn] = []

    # ------------------------------------------------------------------ #
    # 提交与取消
    # ------------------------------------------------------------------ #

    @property
    def current_turn(self) -> Turn | None:
        turn = self._turns[-1] if self._turns else None
        return turn if turn is not None and turn.running else None

    @property
    def turn_index(self) -> int:
        """下一个轮次号减一 —— 即“历史里已有的用户输入数”（跨 resume 单调）。"""
        return self._next_turn_index() - 1

    def _next_turn_index(self) -> int:
        """下一个轮次号 = 历史里已有的用户输入数 + 1。

        ⚠️ **不能用** ``len(self._turns) + 1``：``_turns`` 是**进程级**的，
        而 ``/resume`` 会把 ``history`` 整体换掉却不会同步它 —— 于是新进程里轮次号
        从 1 重新开始，与文件里已有的轮次号**冲突**。

        实测后果（真实会话 `tui-34248.jsonl`）：同一个文件里 ``turn=1`` 出现 **3 次**，
        于是 ``SessionTranscriptWriter.turn_lines`` 把三次合并成一个区间
        （``{1: (1, 238)}``，“第 1 轮”覆盖整个文件），归档索引里的
        「第 N 轮 · 行 X~Y」就指向了**错误的行**（F-32）。

        **从 history 推导则天然跨 resume 单调，而且状态只有一个来源。**
        """
        return 1 + sum(1 for message in self.history if message.role == "user")

    async def start(self, text: str) -> Turn:
        """开一个回合并**在后台运行**，立刻返回 :class:`Turn`。

        后台运行而不是直接 await，是因为界面必须在回合进行中就能按 Esc——
        若 ``submit`` 同步等到结束，取消就无从下手。

        末尾那次 ``await asyncio.sleep(0)`` 不是凑数：``create_task`` 只是把协程**排队**，
        若调用方紧接着就 ``cancel()``，CancelledError 会在协程**还没开始执行**时被
        ``throw`` 进去——于是 ``_run`` 顶部的 ``try`` 根本没机会进入，收尾逻辑
        （登记终态、补齐工具结果、发 ``TurnFinished``）全部不会运行，
        界面上就会留下一个永远转圈的回合。让出一次事件循环确保任务已经跑起来。
        """
        running = self.current_turn
        if running is not None:
            raise TurnInProgressError(running.turn_index)

        turn = Turn(turn_index=self._next_turn_index(), history=self.history)
        self._turns.append(turn)
        turn.task = asyncio.get_running_loop().create_task(
            self._run(turn, text), name=f"logox-turn-{turn.turn_index}"
        )
        await asyncio.sleep(0)
        return turn

    async def submit(self, text: str) -> Turn:
        """开一个回合并等它结束（文本模式与测试用）。"""
        turn = await self.start(text)
        return await self.wait(turn)

    async def wait(self, turn: Turn) -> Turn:
        if turn.task is not None:
            await turn.task
        return turn

    def cancel(self) -> bool:
        """取消当前回合。幂等；返回"是否真的取消了点什么"。"""
        turn = self.current_turn
        return False if turn is None else turn.cancel()

    def set_thinking(self, thinking: ThinkingConfig | str | None) -> None:
        """改变后续请求的思考档位（D58）。

        **只影响"下一次将要发起的请求"，不打断正在进行的回合。** 原因很具体：
        请求参数在 ``_model_phase`` 开头就被固化进 :class:`ChatRequest` 了
        （``thinking=self._thinking`` 是一次取值），因此**正在流式生成的那一次请求
        拿的是旧档位**——这是不可避免的，重试路径也依赖"每次重试用同一份请求"这条
        不变量（见 §5.3，D52）。要"当场生效"就只剩"取消当前请求再重发"这一条路，
        而那会让用户正在读的回答被拦腰截断，代价远大于收益。

        可见的效果是：**本回合内的后续请求、以及之后的每个回合，都用新档位。**

        :param thinking: ``ThinkingConfig``、档位字符串（``"high"`` 等），
            或 ``None`` 表示"不干预"（等价于 ``auto``）。
            **接受字符串是为了让界面不必 import ``providers``**——界面只该知道
            "档位"这个概念，不该认识 `ThinkingConfig` 这个厂商侧的数据模型
            （MODULE_tui_integration §2.2 的 B1/B2 红线）。转换由内核做。
        """
        if isinstance(thinking, str):
            from logox.providers.base import ThinkingConfig as _ThinkingConfig

            thinking = _ThinkingConfig(effort=thinking)  # type: ignore[arg-type]
        self._thinking = thinking

    def set_model(self, model: str) -> None:
        """切换后续请求使用的模型（``/model``）。

        与 :meth:`set_thinking` 同一形状：**只影响下一次将要发起的请求**，
        不打断正在进行的回合（正在生成的那一次已经带着旧模型发出去了）。

        **为什么要有这个方法，而不是让界面直接改 `_model`**：`_model` 是私有字段，
        而"界面能改哪些东西"应当是**显式且可测试**的契约。一个公开方法同时说明了
        意图（换模型）与边界（不打断当前回合），也让测试能断言它。
        """
        if not model.strip():
            raise ValueError("模型名不能为空")
        self._model = model

    def set_provider(self, new_provider: object) -> None:
        """热替换 Provider 实例（``/login`` 换供应商或换密钥）。

        同样只影响**下一次请求**。适配器本身无状态（每次 ``stream()`` 收一份请求），
        因此替换是安全的——这一点由 M2 的契约测试保证（同一实例并发调用必须安全）。
        """
        self._provider = new_provider  # type: ignore[assignment]

    # ------------------------------------------------------------------ #
    # 回合主干
    # ------------------------------------------------------------------ #

    async def _run(self, turn: Turn, text: str) -> None:
        try:
            await self._body(turn, text)
        except asyncio.CancelledError:
            # ★ 唯一的 CancelledError 捕获点。
            # 这里**不重新抛出**是刻意的：取消在本设计里是"回合的一等终态"
            # （TurnFinished(reason="cancelled")），已经完整地表达出来了；
            # 再抛一次只会让 `await submit()` 变成一个需要调用方 try/except 的 API，
            # 而调用方对取消没有任何补充信息可做。
            turn.mark_cancelled()
            self._complete_history(turn)
            if not turn.turn_summary:
                # ★ CHANGE-052：摘要 = **用户问题 + 异常说明**（用户裁定）。
                #   折叠之后该轮原文整段消失，"第 N 轮被中断"没说这一轮想干什么 ——
                #   模型接着干活时不知道"用户当时要的东西"还需不需要做。
                turn.turn_summary = interrupted_summary(text, "本轮被中断")
            await self._emit_turn_finished(turn, "cancelled")
        except _ProviderFailure:
            # ErrorOccurred 已经在 raise 之前发过了；这里只负责收尾
            turn.status = TurnStatus.FAILED
            self._complete_history(turn)
            if not turn.turn_summary:
                turn.turn_summary = interrupted_summary(text, "模型请求失败")
            await self._emit_turn_finished(turn, "error")
        except LogoxError:
            # 总线已关闭之类的框架级错误：状态登记好再放行，**绝不吞**
            turn.status = TurnStatus.FAILED
            raise

    async def _build_context(self, turn: Turn) -> ContextBundle:
        """组装视图并把 ``ContextBuilt`` 发到总线上。

        回合开始时与**回合内复检**（CHANGE-005 裁定 8）共用同一个入口 ——
        两处各写一遍，迟早会有一处忘了把``last_usage`` 传下去。
        """
        # ★ D167 / F-55：先问"这次会不会压"，会的话在**动手之前**发 `CompactionStarted`
        #   —— 这正是 `pre_compact` 钩子需要的挂点（此前该事件没有任何发布者 ⇒ 钩子永不触发）。
        #   调用 `plan()` 是可选的（`getattr`）：不认识它的 ContextBuilder 实现照旧工作
        #   （与"适配层不认字段名的实现直接忽略"是同一风格）。
        planner = getattr(self._builder, "plan", None)
        if callable(planner):
            plan = planner(self.history, last_usage=self._last_request_usage)
            if plan is not None:
                await self._bus.publish(
                    ev.CompactionStarted(
                        session_id=self._bus.session_id,
                        turn=turn.turn_index,
                        # 具体策略要等真正折叠时才知道（工具修剪 / 折叠 / 两者）
                        strategy="auto",
                        tokens_before=plan.tokens_before,
                        message_count_before=plan.message_count_before,
                    )
                )

        bundle = self._builder.build(self.history, last_usage=self._last_request_usage)
        # ★ D156 / F-54：把"压缩发生了"这件事**发到总线上**。
        #   在补上这一处之前，`CompactionStarted`/`CompactionFinished` 全项目没有发布者，
        #   于是时间线提示、`compact_count`、`pre_compact` 钩子、事后查证**四条线全是死的**。
        #   只发 Finished（不发 Started）：本项目的压缩是**本地纯计算、不调模型**，
        #   亚毫秒级完成，"正在进行"这个阶段事实上不存在（详见 CHANGE-026 §2.2）。
        if bundle.compaction is not None:
            await self._bus.publish(
                ev.CompactionFinished(
                    session_id=self._bus.session_id,
                    turn=turn.turn_index,
                    tokens_after=bundle.compaction.tokens_after,
                    message_count_after=bundle.compaction.message_count_after,
                    degraded=bundle.compaction.degraded,
                    tokens_before=bundle.compaction.tokens_before,
                    pruned_count=bundle.compaction.pruned_count,
                    folded_turns=bundle.compaction.folded_turns,
                    strategy=bundle.compaction.strategy,
                )
            )
        await self._bus.publish(
            ev.ContextBuilt(
                session_id=self._bus.session_id,
                turn=turn.turn_index,
                message_count=len(bundle.messages),
                token_estimate=bundle.token_estimate,
                memory_sources=list(bundle.memory_sources),
                pruned_count=bundle.pruned_count,
            )
        )
        return bundle

    async def _body(self, turn: Turn, text: str) -> None:
        self.history.append(_user_message(text))
        await self._bus.publish(
            ev.UserPromptSubmit(
                session_id=self._bus.session_id,
                turn=turn.turn_index,
                text=text,
                text_chars=len(text),
            )
        )

        bundle = await self._build_context(turn)

        #: 本轮的请求消息视图：随模型响应与工具结果增长
        messages: list[Message] = list(bundle.messages)

        continuation_count = 0
        max_continuations = 1
        truncation_healing_count = 0
        max_truncation_healings = 1

        iteration = 0
        current_limit = self._max_iterations
        while iteration < current_limit:
            iteration += 1
            # ★ CHANGE-005 裁定 8：**回合内复检水位线**。
            #
            # 一个回合里模型请求要跑好几轮：每次工具结果回来又是一轮。
            # 而压缩原来只在回合开始时看一次 —— 于是"回合开始时还没满"的上下文
            # 会带着本回合的工具输出一路涨到**超窗口**，厂商直接回 400。
            # 这是 pi 在 CHANGELOG 0.84.4 修过的同一个 bug
            # （"compacts between tool execution and the next assistant response
            #   in the same run"）——两家踩的是同一个坑。
            #
            # 为什么放这里而不是 `_model_phase` 内部：增量来自**工具结果**，
            # 而工具结果是在这个 while 的每轮之间追加的；重试不会新增上下文。
            if iteration > 1:
                bundle = await self._build_context(turn)
                messages = list(bundle.messages)
            try:
                produced = await self._model_phase(turn, messages, bundle)
            except _ProviderFailure as failure:
                # ★ D121：工具参数因 Token 上限截断的自愈闭环（Self-Healing via Error Feedback）
                if failure.is_truncated and truncation_healing_count < max_truncation_healings:
                    truncation_healing_count += 1
                    # 1. 刚才已产出的部分消息同步进请求视图
                    if turn.history and turn.history[-1] not in messages:
                        messages.append(turn.history[-1])

                    # 2. 注入自愈纠偏系统指引
                    healing_prompt = (
                        "[系统提示：刚才发起的工具参数因达到单次输出 Token 上限被截断，目标文件未被修改。\n"
                        "【铁律】：严禁使用 write 工具全量覆盖重写已有大文件！\n"
                        "请立即改用 edit 工具进行精确局部替换（只输出需改动的几行/几块代码），或分批完成。]"
                    )
                    healing_msg = Message(role="user", blocks=[TextBlock(text=healing_prompt)])
                    messages.append(healing_msg)
                    self.history.append(healing_msg)

                    # 3. 发布提示通知用户/界面，说明正在自动回灌自愈
                    await self._bus.publish(
                        ev.ModelDelta(
                            session_id=self._bus.session_id,
                            turn=turn.turn_index,
                            kind="text",
                            delta="\n[!] 工具参数由于达到单次输出 Token 上限被截断，已自动回灌模型使用 edit 工具自愈...\n",
                            request_index=turn.request_index,
                        )
                    )
                    continue

                raise

            messages.append(produced.assistant_message)
            self.history.append(produced.assistant_message)

            if not produced.tool_calls:
                has_text = any(
                    isinstance(b, TextBlock) and bool(b.text and b.text.strip())
                    for b in produced.assistant_message.blocks
                )
                has_reasoning = any(
                    isinstance(b, ReasoningBlock) and bool(b.text and b.text.strip())
                    for b in produced.assistant_message.blocks
                )
                is_truncated = produced.stop_reason == "max_tokens"

                # 场景 1（D118 铁律）：大思考未输出正文，或被 Token 上限截断
                if (has_reasoning or is_truncated) and not has_text and turn.tool_call_count == 0:
                    # 纯思考绝不算有效对话完成，严禁提取与兜底生成摘要！
                    turn.turn_summary = None

                    # 若在续写预算内：自动发起静默续写接力
                    if continuation_count < max_continuations:
                        continuation_count += 1
                        continuation_msg = Message(
                            role="user",
                            blocks=[
                                TextBlock(
                                    text="[系统提示：思考已结束或单次输出已达上限，请立即直接输出正文答复或调用工具。]"
                                )
                            ],
                        )
                        messages.append(continuation_msg)
                        self.history.append(continuation_msg)
                        continue

                    # 续写已达上限依然没有任何交付物：标记为未完结失败，绝不发 completed
                    turn.status = TurnStatus.FAILED
                    turn.turn_summary = interrupted_summary(text, "本轮无有效输出")
                    await self._emit_turn_finished(turn, "error")
                    return

                # 场景 2：正常结束（有正文交付物、或已执行过工具、或非思考模型的普通停机）
                turn.status = TurnStatus.DONE
                clean_msg = produced.assistant_message

                # 只有非空正文或工具执行后才提取/生成摘要；纯空输出绝不伪造假摘要
                if has_text or turn.tool_call_count > 0:
                    # ★ D135：摘要 = **位置契约**（最终答复的最后一行）。
                    #
                    #   L1 末尾行（`model_last_line`）—— **期望路径**，零成本
                    #   L2 再调一次模型补写（`model_fallback`）—— 用户裁定 Q-D
                    #   L3 本地自动生成（`deterministic`）—— 最后一道，绝不失败
                    #
                    #   ⚠️ 正文**只被读**，从不被改 —— "吞正文"这个故障类别已从机制上消失。
                    verdict = extract_trailing_summary(clean_msg.text)
                    summary = verdict.summary
                    source = "model_last_line" if summary else None
                    # 三层兜底的**链路诊断**（D135-4）：只记 L1 不够 ——
                    # 必须能区分"模型不配合" / "我们拒得太严" / "补写失败"。
                    chain: list[str] = [verdict.reason]

                    if (
                        not summary
                        and self._model_summary_fallback
                        and self._worth_a_model_summary(turn, clean_msg)
                    ):
                        # ★ 成本闸门：两字回答不值得再发一次请求（见 `_worth_a_model_summary`）
                        summary, l2_outcome = await self._summarize_turn_via_model(turn)
                        chain.append(l2_outcome)
                        if summary:
                            source = "model_fallback"
                    elif not summary:
                        chain.append("l2_skipped")

                    if not summary:
                        summary = deterministic_summary(
                            clean_msg,
                            tool_call_count=turn.tool_call_count,
                            turn_index=turn.turn_index,
                        )
                        source = "deterministic"
                        chain.append("l3")

                    turn.turn_summary = summary
                    turn.summary_source = source
                    turn.summary_reason = " → ".join(chain)
                else:
                    turn.turn_summary = None

                meta_kwargs = clean_msg.meta.model_dump() if clean_msg.meta else {}
                if turn.turn_summary:
                    meta_kwargs["turn_summary"] = turn.turn_summary
                    # ★ 用户裁定 Q-D：来源要**落进历史消息的 meta**，
                    #   这样历史里任何一条消息都能自证“摘要是不是模型写的”。
                    meta_kwargs["summary_source"] = turn.summary_source
                clean_msg = Message(
                    role=clean_msg.role,
                    blocks=clean_msg.blocks,
                    meta=MessageMeta(**meta_kwargs),
                )
                messages[-1] = clean_msg
                self.history[-1] = clean_msg

                await self._emit_turn_finished(turn, "completed")
                return

            results = await self._scheduler.run_batch(turn, produced.tool_calls)
            tool_message = Message(
                role="tool",
                blocks=[ToolResultBlock(id=item.id, ok=item.ok, content=item.content) for item in results],
            )
            messages.append(tool_message)
            self.history.append(tool_message)

            if iteration >= current_limit and await self._request_continuation(turn, iteration):
                # 尝试人在回路 (HITL) 轮次续期
                current_limit += self._max_iterations
                continue

        # 到达上限且未获续期：明确报错，不静默停止
        await self._bus.publish(
            ev.ErrorOccurred(
                session_id=self._bus.session_id,
                turn=turn.turn_index,
                category=ErrorCategory.BAD_REQUEST.value,
                message=f"连续 {iteration} 轮都在请求工具，已停止本回合。",
                retryable=False,
                detail="若任务确实需要更多轮次，请在配置里调大 kernel.max_iterations 或在续期弹窗中允许继续运行。",
            )
        )
        turn.status = TurnStatus.FAILED
        if not turn.turn_summary:
            turn.turn_summary = interrupted_summary(text, f"超过最大迭代限制（{iteration} 轮）")
        await self._emit_turn_finished(turn, "error")

    async def _request_continuation(self, turn: Turn, iteration: int) -> bool:
        """询问是否允许继续运行工具轮次（类 Claude Code 的 HITL 续期）。"""
        if hasattr(self._decider, "ask_continuation"):
            try:
                return bool(await self._decider.ask_continuation(turn, iteration))
            except Exception as exc:
                logger.warning("请求轮次续期异常：%s", exc)
                return False

        prompter = getattr(self._decider, "prompter", None)
        if prompter is not None and hasattr(prompter, "ask_continuation"):
            try:
                return bool(await prompter.ask_continuation(turn, iteration))
            except Exception as exc:
                logger.warning("请求续期弹窗异常：%s", exc)
                return False

        return False

    # ------------------------------------------------------------------ #
    # 模型阶段（D52 的落点）
    # ------------------------------------------------------------------ #

    async def _model_phase(
        self, turn: Turn, messages: list[Message], bundle: ContextBundle
    ) -> _ModelProduced:
        request = ChatRequest(
            model=self._model,
            system=bundle.system,
            messages=list(messages),
            tools=self._registry.schemas(),
            temperature=self._temperature,
            max_tokens=self._max_tokens,
            thinking=self._thinking,
        )

        attempt = 0
        while True:
            request_index = turn.request_index
            turn.request_index += 1
            await self._bus.publish(
                ev.ModelRequestStarted(
                    session_id=self._bus.session_id,
                    turn=turn.turn_index,
                    provider=getattr(self._provider, "name", "unknown"),
                    model=self._model,
                    token_estimate=bundle.token_estimate,
                    request_index=request_index,
                )
            )
            watch = Stopwatch(clock=self._clock)
            acc = _Accumulator()
            try:
                outcome = await self._consume(turn, request, request_index, watch, acc)
            except asyncio.CancelledError:
                # ★ D51：取消也要把**已经吐出来的内容**写进历史。
                # 不写的话，用户屏幕上明明有半句话，而历史里什么都没有——
                # 下一轮模型完全不知道刚才说过什么，用户会觉得工具"失忆"了。
                self._commit_partial(turn, acc)
                raise
            except _ProviderFailure as failure:
                # 同一条原则：厂商把连接断了，但用户已经看到的那部分仍然是真实发生过的
                self._commit_partial(turn, acc)
                # ★ D121：若是因 Token 截断导致的工具参数不完整，直接向上抛出给自愈状态机
                if failure.is_truncated:
                    raise
                # `first_token_ms is not None` 就是"已经吐过内容"的判据：
                # 只有真正可见的文本/推理增量才会 mark()。
                next_attempt = await self._handle_failure(
                    turn, failure, attempt, content_seen=watch.first_token_ms is not None
                )
                if next_attempt is None:
                    raise
                attempt = next_attempt
                continue

            await self._emit_request_finished(turn, outcome, watch)
            turn.add_usage(outcome.usage if outcome.usage is not None else _ZERO_USAGE)
            return _ModelProduced.from_outcome(outcome)

    async def _consume(
        self,
        turn: Turn,
        request: ChatRequest,
        request_index: int,
        watch: Stopwatch,
        acc: _Accumulator,
    ) -> _ModelOutcome:
        """读一个完整的模型流，把增量即时翻译成总线事件并累积进 ``acc``。"""
        usage: ev.Usage | None = None
        stop_reason = "unknown"

        async for event in self._provider.stream(request):
            if isinstance(event, DeltaEvent):
                if event.vendor_data and "signature" in event.vendor_data:
                    # 只带签名的增量（Anthropic thinking，见 providers.base.DeltaEvent）：
                    # 它不携带任何可见文本，并**并入上一个推理块**。
                    acc.signature = str(event.vendor_data["signature"])
                    continue
                if not event.text:
                    continue  # 空增量不发事件，否则界面会被大量空事件灌满
                watch.mark()
                if event.kind == "reasoning":
                    acc.reasoning.append(event.text)
                    await self._bus.publish(
                        ev.ModelDelta(
                            session_id=self._bus.session_id,
                            turn=turn.turn_index,
                            kind=event.kind,
                            delta=event.text,
                            request_index=request_index,
                        )
                    )
                else:
                    # ⚠️ D135：这里**不再有"流式摘要过滤"** —— 摘要改成位置契约之后，
                    # 正文里不会再有需要拦截的标记；把增量原样交给界面与持久化，
                    # 也就从机制上不可能"吞掉文字"。
                    acc.text.append(event.text)
                    await self._bus.publish(
                        ev.ModelDelta(
                            session_id=self._bus.session_id,
                            turn=turn.turn_index,
                            kind=event.kind,
                            delta=event.text,
                            request_index=request_index,
                        )
                    )
            elif isinstance(event, ToolCallEvent):
                acc.tool_calls.append(event)
                # 适配层给的是**装配完成**的调用（不是参数片段），所以这里只有"一次性"的
                # tool_args 增量。保留它是因为界面需要在这个时刻出现工具卡片。
                await self._bus.publish(
                    ev.ModelDelta(
                        session_id=self._bus.session_id,
                        turn=turn.turn_index,
                        kind="tool_args",
                        delta=json.dumps(event.arguments, ensure_ascii=False),
                        request_index=request_index,
                    )
                )
            elif isinstance(event, UsageEvent):
                usage = event.usage
            elif isinstance(event, StopEvent):
                stop_reason = event.stop_reason
            elif isinstance(event, ProviderErrorEvent):
                raise _ProviderFailure(event, acc=acc)
            # ProviderEvent 是封闭联合；新增类型时这里会静默忽略——见 base.py 的 __all__

        return acc.outcome(stop_reason=stop_reason, usage=usage)

    def _commit_partial(self, turn: Turn, acc: _Accumulator) -> None:
        """把中断/失败前已产出的内容落成一条 assistant 消息（D51）。

        **空累积不写。** 写一条空消息会在历史里留下一个"模型什么都没说"的回合，
        而它其实是被打断的——那属于伪造历史。
        """
        if acc.empty:
            return
        turn.history.append(_ModelProduced.from_outcome(acc.outcome(stop_reason="unknown", usage=None)).assistant_message)

    async def _handle_failure(
        self, turn: Turn, failure: _ProviderFailure, attempt: int, content_seen: bool
    ) -> int | None:
        """决定是否重试。返回下一次的 ``attempt``，或 ``None`` 表示不再重试（调用方抛出）。

        **D52 是这里唯一的判据**：只要已经吐过内容，就不再自动重试。
        """
        event = failure.event
        if not event.category.retryable:
            await self._emit_error(turn, event, retryable=False)
            return None

        if content_seen:
            await self._emit_error(
                turn,
                event,
                retryable=False,
                override_message=(
                    f"{event.message}（已经输出了一部分内容，为避免出现重复文本，不再自动重试）"
                ),
            )
            return None

        if attempt >= self._max_retries:
            await self._emit_error(
                turn,
                event,
                retryable=True,
                override_message=f"{event.message}（已重试 {attempt} 次仍未成功）",
            )
            return None

        delay = self._backoff(attempt, event.retry_after_s)
        await self._bus.publish(
            ev.RetryScheduled(
                session_id=self._bus.session_id,
                turn=turn.turn_index,
                attempt=attempt + 1,
                delay_s=delay,
                reason=event.message,
            )
        )
        await self._sleep(delay)
        return attempt + 1

    def _backoff(self, attempt: int, retry_after_s: float | None) -> float:
        """退避时长。

        厂商给了 ``Retry-After`` 就**以它为准**——它知道自己的限流窗口，我们猜的不如它给的准；
        只把它裁剪到上限以内，避免一个异常大的值把界面卡死。
        没有就指数退避 + 抖动（抖动避免多会话同时重试打同一个端点）。
        """
        if retry_after_s is not None and retry_after_s > 0:
            return min(float(retry_after_s), self._retry_max_s)
        raw = self._retry_base_s * (2**attempt)
        return min(raw, self._retry_max_s) * self._jitter()

    async def _emit_error(
        self,
        turn: Turn,
        event: ProviderErrorEvent,
        *,
        retryable: bool,
        override_message: str | None = None,
    ) -> None:
        await self._bus.publish(
            ev.ErrorOccurred(
                session_id=self._bus.session_id,
                turn=turn.turn_index,
                category=event.category.value,
                message=override_message or event.message,
                retryable=retryable,
                detail=event.detail,
            )
        )

    async def _emit_request_finished(
        self, turn: Turn, outcome: _ModelOutcome, watch: Stopwatch
    ) -> None:
        usage = outcome.usage if outcome.usage is not None else _ZERO_USAGE
        # ★ CHANGE-005：记下**本次请求真实喂进去的上下文大小**，供下一次组装用。
        #   注意存的是 `outcome.usage`（**未上报就是 None**），不是上面那个补过 0 的 `usage`。
        self._last_request_usage = outcome.usage
        cost: float | None = None
        if outcome.usage is not None and self._model and self._cost_estimator is not None:
            cost = self._cost_estimator(usage, self._model)
        tool_calls_payload = [
            {"id": call.call_id, "name": call.name, "arguments": dict(call.arguments)}
            for call in outcome.tool_calls
        ]
        await self._bus.publish(
            ev.ModelRequestFinished(
                session_id=self._bus.session_id,
                turn=turn.turn_index,
                usage=usage,
                duration_ms=watch.elapsed_ms,
                first_token_ms=watch.first_token_ms,
                stop_reason=outcome.stop_reason,
                cost_usd=cost,
                # 厂商没上报用量 → 状态栏显示 `—`，而不是把 0 当成"真的没消耗"
                usage_reported=outcome.usage is not None,
                tool_calls=tool_calls_payload,
            )
        )

    # ------------------------------------------------------------------ #
    # 收尾
    # ------------------------------------------------------------------ #

    def _turn_messages(self, turn: Turn) -> list[Message]:
        """取**本轮**的消息切片（给摘要器用）。

        怎么定位"本轮"：从**最后一个真正的用户输入**开始 —— 工具结果在我们这里是
        ``role="tool"`` 的独立消息，所以 ``role=="user"`` 就是用户本人说的话。
        这样不需要额外记录"本轮起点"状态，也就不会与将来的改动失配。
        """
        for index in range(len(turn.history) - 1, -1, -1):
            if turn.history[index].role == "user":
                return list(turn.history[index:])
        return []

    async def _summarize_turn_via_model(self, turn: Turn) -> tuple[str | None, str]:
        """**第 2 层兜底**（D135 第二步 / 用户裁定 Q-D）：末尾行不合规时，**再问一次模型**。

        上下文只用**本轮对话**（用户输入 + 本轮回答 + 工具结果），并做有界截断，
        避免"为了补一句摘要反而付一大笔 token"。

        三条硬约束（都不是可选项）：

        1. **不写回历史** —— 这次请求是**旁路产物**。哪怕只把它追加进 ``self.history``，
           改动也会落在下一轮请求的前缀上，KV cache 直接失效（D114 用摘要折叠本来就为了保 cache）。
        2. **不上屏** —— 不发布 ``ModelRequestStarted`` / ``ModelDelta``：
           用户看到的对话流必须与"模型真正回答的内容"一一对应，否则会凭空多出一段。
        3. **失败就降级** —— 任何异常 / 超时 / 空结果都返回 ``None``，交给第 3 层本地生成。
           ⚠️ 摘要是一条"锦上添花"的旁路，**绝不能因为它把回合搞成失败**。
        """
        messages = self._turn_messages(turn)
        transcript = render_turn_transcript(messages) if messages else ""
        if not transcript.strip():
            return None, "l2_no_input"

        request = ChatRequest(
            model=self._model,
            system=SUMMARY_SYSTEM_PROMPT,
            messages=[_user_message(transcript)],
            tools=[],  # ← 这一层只要一句话，不给工具
            temperature=0.2,
            max_tokens=200,  # ← 一句摘要足够；也防它写成长文
            thinking=None,  # ← 不需要思考
        )

        chunks: list[str] = []
        usage: ev.Usage | None = None
        try:
            async with asyncio.timeout(SUMMARY_TIMEOUT_S):
                async for event in self._provider.stream(request):
                    if isinstance(event, DeltaEvent):
                        if event.kind != "reasoning" and event.text:
                            chunks.append(event.text)
                    elif isinstance(event, UsageEvent):
                        usage = event.usage
                    elif isinstance(event, StopEvent):
                        break
                    elif isinstance(event, ProviderErrorEvent):
                        # ★ D135-4：适配层把厂商错误包成**事件**而不是异常（见 `providers/base.py`）——
                        # 不处理它，`async for` 就只是"结束了"，于是**被误诊成"空响应/被校验拒"**。
                        # （这条正是加了链路诊断之后才显形的：诊断说 `l2_rejected`，真相是厂商报错。）
                        return None, f"l2_error:{event.category}"
        except Exception as exc:  # noqa: BLE001 - 兜底路径：任何失败都降级，绝不外抛
            # ⚠️ 只 `logger.warning` 是不够的：本项目的日志**没有落到文件**
            #（`~/.logox/logs/logox.log` 实测仍是 0 字节），所以失败原因必须**跟事件一起落盘**。
            logger.warning("摘要补写失败，降级到本地生成：%s", exc)
            return None, f"l2_error:{type(exc).__name__}"

        # 成败都要记账：这次调用**真的花了钱**，不记就等于让费用统计说谎
        turn.add_usage(usage if usage is not None else _ZERO_USAGE)

        # ⚠️ 补写结果**必须过同一套校验**（用户裁定 Q-A/Q-B）：
        # 模型对"写一句摘要"的执行力并不比"写在最后一行"更好 —— 它会带解释、写多行、写成标题。
        summary = normalize_model_summary("\n".join(chunks))
        return (summary, "l2_ok") if summary else (None, "l2_rejected")

    def _worth_a_model_summary(self, turn: Turn, message: Message) -> bool:
        """**值不值得**为这一轮再发一次请求补写摘要？（成本闸门）

        两字回答（"ok" / "完成"）本地兜底就够，为它再花一次请求是纯浪费；
        而"真干了活"的轮次 —— **调过工具**，或正文本身就够长 —— 才值得。
        阈值是 `SUMMARY_MODEL_FALLBACK_MIN_CHARS`（200 可见字符）。
        """
        if turn.tool_call_count > 0:
            return True
        return len(message.text.strip()) >= SUMMARY_MODEL_FALLBACK_MIN_CHARS

    async def _emit_turn_finished(self, turn: Turn, reason: str) -> None:
        await self._bus.publish(
            ev.TurnFinished(
                session_id=self._bus.session_id,
                turn=turn.turn_index,
                turn_index=turn.turn_index,
                duration_ms=turn.duration_ms,
                tool_call_count=turn.tool_call_count,
                usage=turn.usage_total,
                reason=reason,  # type: ignore[arg-type]
                turn_summary=turn.turn_summary,
                summary_source=turn.summary_source,
                summary_reason=turn.summary_reason,
            )
        )

    def _complete_history(self, turn: Turn) -> None:
        """**E-3：补齐缺失的工具结果。**

        中断可能让历史停在"``assistant`` 里有 3 个 ``tool_use``、``tool`` 里只有 2 个
        ``tool_result``"的状态。OpenAI 与 Anthropic 都要求二者一一对应，
        少一条下一轮请求就被 400——而这条历史会一直留在会话里，
        于是"按一次 Esc"会让本次会话**再也发不出任何请求**。
        """
        pending: list[str] = []
        last_tool_message: int | None = None
        for index, message in enumerate(turn.history):
            for block in message.blocks:
                if isinstance(block, ToolUseBlock):
                    pending.append(block.id)
                elif isinstance(block, ToolResultBlock) and block.id in pending:
                    pending.remove(block.id)
            if message.role == "tool":
                last_tool_message = index

        if not pending:
            return

        def synthetic(call_id: str) -> ToolResultBlock:
            return ToolResultBlock(id=call_id, ok=False, content=INTERRUPTED_TOOL_CONTENT)

        if last_tool_message is not None:
            # 并入最后一条 tool 消息，而不是再追加一条：连续两条同角色消息会让
            # Anthropic 的"角色必须交替"校验失败。
            existing = turn.history[last_tool_message]
            turn.history[last_tool_message] = Message(
                role="tool",
                blocks=[*existing.blocks, *(synthetic(cid) for cid in pending)],
                meta=existing.meta,
            )
        else:
            turn.history.append(Message(role="tool", blocks=[synthetic(cid) for cid in pending]))


# --------------------------------------------------------------------------- #
# 小助手
# --------------------------------------------------------------------------- #

_ZERO_USAGE = ev.Usage(input_tokens=0, output_tokens=0)

#: 第 2 层兜底（模型补写摘要）的超时（秒）。
#:
#: 为什么必须有超时：这一层是「本来就没拿到摘要」时的补救，**绝不能拖住回合收尾** ——
#: 用户已经看到回答结束，界面却还停在「正在生成」是不可接受的。
#: 20s 足够一次短请求（输入 ≤12k 字符、max_tokens=200）；超时即降级到本地生成。
SUMMARY_TIMEOUT_S = 20.0


class _ModelProduced(BaseModel):
    """一次模型请求的产出：可直接追加进历史的消息 + 工具调用。"""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    assistant_message: Message
    tool_calls: list[ToolCallEvent] = Field(default_factory=list)
    stop_reason: str = "unknown"

    @classmethod
    def from_outcome(cls, outcome: _ModelOutcome) -> _ModelProduced:
        blocks: list[Any] = []
        if outcome.reasoning:
            blocks.append(ReasoningBlock(text=outcome.reasoning, signature=outcome.reasoning_signature))
        if outcome.text:
            blocks.append(TextBlock(text=outcome.text))
        blocks.extend(
            ToolUseBlock(id=call.call_id, name=call.name, input=dict(call.arguments))
            for call in outcome.tool_calls
        )
        if not blocks:
            blocks.append(TextBlock(text=""))
        meta = MessageMeta()
        return cls(
            assistant_message=Message(role="assistant", blocks=blocks, meta=meta),
            tool_calls=list(outcome.tool_calls),
            stop_reason=outcome.stop_reason,
        )


def _user_message(text: str) -> Message:
    return Message(role="user", blocks=[TextBlock(text=text)])
