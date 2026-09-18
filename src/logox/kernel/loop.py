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


class ContextBuilder(Protocol):
    """上下文组装。**内核只调用它，不实现它**（L3 不认识 L4 的 context/）。"""

    def build(self, history: list[Message]) -> ContextBundle: ...


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

    def build(self, history: list[Message]) -> ContextBundle:
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
    turn_summary: str | None = None
    #: 模型请求本身成功（不代表模型没报错）——失败时整个回合已经结束了
    ok: bool = True


class _Accumulator:
    """模型流式产出的**可变**累积器。

    它之所以是一个独立对象而不是 ``_consume`` 里的局部变量，是因为**取消**：
    流被中断时局部变量会随栈一起消失，而用户已经在屏幕上看到了那半句话。
    D51 要求"用户看到什么就留下什么"，所以累积器必须能被中断路径拿到并落地成消息。

    （一个对象换来的是"按 Esc 不会抹掉你刚读到的内容"。）
    """

    __slots__ = ("reasoning", "signature", "text", "tool_calls", "turn_summary")

    def __init__(self) -> None:
        self.text: list[str] = []
        self.reasoning: list[str] = []
        self.signature: str | None = None
        self.tool_calls: list[ToolCallEvent] = []
        self.turn_summary: str | None = None

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
            turn_summary=self.turn_summary,
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
    ) -> None:
        if max_iterations < 1:
            raise ValueError("max_iterations 必须 ≥ 1")
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
        return len(self._turns)

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

        turn = Turn(turn_index=len(self._turns) + 1, history=self.history)
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
                turn.turn_summary = f"在第 {turn.turn_index} 轮被中断"
            await self._emit_turn_finished(turn, "cancelled")
        except _ProviderFailure:
            # ErrorOccurred 已经在 raise 之前发过了；这里只负责收尾
            turn.status = TurnStatus.FAILED
            self._complete_history(turn)
            if not turn.turn_summary:
                turn.turn_summary = f"第 {turn.turn_index} 轮模型请求失败"
            await self._emit_turn_finished(turn, "error")
        except LogoxError:
            # 总线已关闭之类的框架级错误：状态登记好再放行，**绝不吞**
            turn.status = TurnStatus.FAILED
            raise

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

        bundle = self._builder.build(self.history)
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
                    turn.turn_summary = f"第 {turn.turn_index} 轮模型输出未包含有效正文或工具调用"
                    await self._emit_turn_finished(turn, "error")
                    return

                # 场景 2：正常结束（有正文交付物、或已执行过工具、或非思考模型的普通停机）
                turn.status = TurnStatus.DONE
                summary = produced.turn_summary
                clean_msg = produced.assistant_message

                # 只有非空正文或工具执行后才提取/生成摘要；纯空输出绝不伪造假摘要
                if has_text or turn.tool_call_count > 0:
                    if not summary:
                        clean_msg, summary = _extract_and_strip_turn_summary(clean_msg)
                    else:
                        clean_msg, _ = _extract_and_strip_turn_summary(clean_msg)

                    if summary:
                        turn.turn_summary = summary
                    else:
                        turn.turn_summary = _fallback_turn_summary(clean_msg, turn)
                else:
                    turn.turn_summary = None

                meta_kwargs = clean_msg.meta.model_dump() if clean_msg.meta else {}
                if turn.turn_summary:
                    meta_kwargs["turn_summary"] = turn.turn_summary
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
            turn.turn_summary = f"超过最大迭代限制 ({iteration} 轮)"
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
        summary_filter = StreamSummaryFilter()

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
                    for clean_chunk in summary_filter.feed(event.text):
                        if not clean_chunk:
                            continue
                        acc.text.append(clean_chunk)
                        await self._bus.publish(
                            ev.ModelDelta(
                                session_id=self._bus.session_id,
                                turn=turn.turn_index,
                                kind=event.kind,
                                delta=clean_chunk,
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

        for remaining_chunk in summary_filter.flush():
            if remaining_chunk:
                acc.text.append(remaining_chunk)
                await self._bus.publish(
                    ev.ModelDelta(
                        session_id=self._bus.session_id,
                        turn=turn.turn_index,
                        kind="text",
                        delta=remaining_chunk,
                        request_index=request_index,
                    )
                )

        if summary_filter.summary:
            acc.turn_summary = summary_filter.summary

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

class StreamSummaryFilter:
    """流式 Token 增量中的 <turn_summary> 标签拦截过滤器。

    解决痛点：大模型在最后一轮末尾输出 <turn_summary>...</turn_summary> 时，
    若不加拦截直接作为 ModelDelta 广播，终端 TUI 时间线会把 XML 标签实时打印在屏幕上。

    本过滤器在 ModelDelta 发布前执行实时透明截获：
    1. 遇到可能是 <turn_summary> 的前缀时微量缓冲；
    2. 一旦进入标签，丢弃 <turn_summary> 及其前置换行，后续内容存入 summary_buffer；
    3. 遇到 </turn_summary> 时闭合标签，将提取出的纯摘要存入 self.summary；
    4. 若未匹配为标签（如普通代码中的 <stdio.h>），原样吐出缓冲内容，零丢字、零延迟。
    """

    def __init__(self) -> None:
        self.summary: str | None = None
        self._buffer: str = ""
        self._in_tag = False
        self._summary_buffer: str = ""

    def feed(self, chunk: str) -> list[str]:
        if self._in_tag:
            self._summary_buffer += chunk
            close_idx = self._summary_buffer.lower().find("</turn_summary>")
            if close_idx != -1:
                self._in_tag = False
                self.summary = (self.summary or "") + self._summary_buffer[:close_idx].strip()
                remaining = self._summary_buffer[close_idx + len("</turn_summary>"):]
                self._summary_buffer = ""
                if remaining:
                    return self.feed(remaining)
                return []
            return []

        combined = self._buffer + chunk
        self._buffer = ""

        lower_combined = combined.lower()
        idx = lower_combined.find("<turn_summary>")
        if idx != -1:
            text_before = combined[:idx].rstrip("\r\n")
            emits = [text_before] if text_before else []
            self._in_tag = True
            after_tag = combined[idx + len("<turn_summary>"):]
            close_idx = after_tag.lower().find("</turn_summary>")
            if close_idx != -1:
                self._in_tag = False
                self.summary = after_tag[:close_idx].strip()
                remaining = after_tag[close_idx + len("</turn_summary>"):]
                if remaining:
                    emits.extend(self.feed(remaining))
            else:
                self._summary_buffer += after_tag
            return emits

        max_prefix_len = min(len(combined), len("<turn_summary>") - 1)
        match_len = 0
        for k in range(max_prefix_len, 0, -1):
            suffix = combined[-k:]
            if "<turn_summary>".startswith(suffix.lower()):
                match_len = k
                break

        if match_len > 0:
            p_idx = len(combined) - match_len
            while p_idx > 0 and combined[p_idx - 1] in "\r\n":
                p_idx -= 1
            emit_text = combined[:p_idx]
            self._buffer = combined[p_idx:]
            return [emit_text] if emit_text else []

        nl_len = 0
        while nl_len < len(combined) and combined[-(nl_len + 1)] in "\r\n" and nl_len < 4:
            nl_len += 1
        if nl_len > 0:
            emit_text = combined[:-nl_len]
            self._buffer = combined[-nl_len:]
            return [emit_text] if emit_text else []

        return [combined] if combined else []

    def flush(self) -> list[str]:
        emits = []
        if self._in_tag:
            if self._summary_buffer:
                self.summary = (self.summary or "") + self._summary_buffer.strip()
                self._summary_buffer = ""
            self._in_tag = False
        elif self._buffer:
            emits.append(self._buffer)
            self._buffer = ""
        return emits


_ZERO_USAGE = ev.Usage(input_tokens=0, output_tokens=0)


class _ModelProduced(BaseModel):
    """一次模型请求的产出：可直接追加进历史的消息 + 工具调用。"""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    assistant_message: Message
    tool_calls: list[ToolCallEvent] = Field(default_factory=list)
    turn_summary: str | None = None
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
        if outcome.turn_summary:
            meta = MessageMeta(turn_summary=outcome.turn_summary)
        return cls(
            assistant_message=Message(role="assistant", blocks=blocks, meta=meta),
            tool_calls=list(outcome.tool_calls),
            turn_summary=outcome.turn_summary,
            stop_reason=outcome.stop_reason,
        )


def _user_message(text: str) -> Message:
    return Message(role="user", blocks=[TextBlock(text=text)])


_TURN_SUMMARY_PATTERN = re.compile(r"<turn_summary.*?>(.*?)(?:</turn_summary>|$)", re.DOTALL | re.IGNORECASE)


def _extract_and_strip_turn_summary(message: Message) -> tuple[Message, str | None]:
    """从 assistant 消息中提取 <turn_summary> 标签并从文本块中剥离，确保终端与历史干净。"""
    summary: str | None = None
    new_blocks = []
    modified = False

    for block in message.blocks:
        if isinstance(block, TextBlock) and "<turn_summary" in block.text.lower():
            match = _TURN_SUMMARY_PATTERN.search(block.text)
            if match:
                summary = match.group(1).strip()
                cleaned_text = _TURN_SUMMARY_PATTERN.sub("", block.text).rstrip()
                new_blocks.append(TextBlock(text=cleaned_text))
                modified = True
                continue
        new_blocks.append(block)

    if modified:
        return Message(role=message.role, blocks=new_blocks, meta=message.meta), summary
    return message, summary


def _fallback_turn_summary(message: Message, turn: Turn) -> str:
    """当大模型未显式输出 <turn_summary> 标签时的确定性安全兜底。"""
    for block in message.blocks:
        if isinstance(block, TextBlock) and block.text.strip():
            first_line = block.text.strip().splitlines()[0].strip()
            first_line = first_line.lstrip("#*-> ").strip()
            if first_line:
                return (first_line[:37] + "...") if len(first_line) > 40 else first_line
    if turn.tool_call_count > 0:
        return f"执行了 {turn.tool_call_count} 次工具操作并完成"
    return f"完成第 {turn.turn_index} 轮交互"
