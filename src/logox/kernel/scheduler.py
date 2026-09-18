"""并发调度（D27 / L3）。

一批工具调用怎么执行，只看一件事：**这一批是不是全是只读的**。

* 全只读 → 并发（``Semaphore`` 限流，默认 4）
* 含任一写 → **整批串行**，且严格保持**模型给出的顺序**

为什么含写就整批串行，而不是"把写挑出来排队、只读的照样并发"
------------------------------------------------------------
模型给出的一批调用是它认为**可以一起做**的一组动作，但模型并不知道
"先读 A 再写 A" 这件事有顺序依赖。一旦批里出现写操作，任何并发都可能让
"读到的 A" 与 "写入的 A" 之间产生用户无法预料的交错。整批串行是唯一能保证
"模型写的顺序就是真实发生的顺序"的做法——代价是慢一点，收益是**结果可解释**。

事件顺序 ≠ 消息顺序（刻意为之）
-------------------------------
* ``ToolCallStarted`` / ``ToolCallFinished`` 按**真实发生顺序**发——状态栏要的是实况。
  并发时完成顺序本来就该是乱的，把它压平反而是在撒谎。
* 写进历史的 ``ToolResultBlock`` 按**模型给出的顺序**排——OpenAI 与 Anthropic 都要求
  工具结果与 ``tool_use`` 一一对应且顺序一致，顺序错了下一轮请求会被直接拒。

``ToolCallStarted`` / ``ToolCallFinished`` **必须严格配对**
--------------------------------------------------------
无论工具是成功、失败、被拒绝，还是**被取消**，每个 ``ToolCallStarted`` 都要有且只有一个
配对的 ``ToolCallFinished``。M1.5 的视觉骨架曾经漏过一次，症状是**所有工具卡片永远停在
"运行中"**（`docs/00-decision-log.md` §7.2）。因此这里用 ``try/except/finally`` 三段式
把三种出口全部覆盖，并配一条不变量用例盯着它。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Sequence
from enum import Enum
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict
from pydantic import ValidationError as PydanticValidationError

from logox.errors import ErrorCategory
from logox.kernel import events as ev
from logox.kernel.bus import EventBus
from logox.kernel.messages import ToolResultBlock
from logox.kernel.registry import ToolRegistry
from logox.kernel.turn import Turn
from logox.providers.base import ToolCallEvent
from logox.tools.base import Tool, ToolContext, ToolResult

logger = logging.getLogger(__name__)

__all__ = [
    "AllowAllDecider",
    "BatchPlan",
    "BlobStoreProtocol",
    "Decision",
    "DenyAllDecider",
    "PermissionDecider",
    "Scheduler",
    "plan_batch",
]

class BlobStoreProtocol(Protocol):
    """CAS 对象池协议（避免 Scheduler 反向依赖 store 层）。"""

    def put_file(self, file_path: Path | str) -> str: ...


#: 被拒绝 / 被取消 / 参数不合法时写进 ``ToolCallFinished.error_kind`` 的取值。
#: 用固定词汇表而不是自由字符串，是为了让界面能按类别给出不同图标。
ERROR_DENIED = "denied"
ERROR_CANCELLED = "cancelled"
ERROR_UNKNOWN_TOOL = "unknown_tool"
ERROR_INVALID_ARGS = "invalid_args"
ERROR_TOOL_RAISED = "tool_raised"


class Decision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


class PermissionDecider(Protocol):
    """权限决策的注入点（M3 用最小实现，M6 换成真实决策链）。"""

    async def decide(self, call: ToolCallEvent, tool: Tool, turn: Turn) -> Decision: ...


class AllowAllDecider:
    """放行一切。**只在注册表里全是只读工具时才允许使用**（§5.6 的安全闸门）。"""

    async def decide(self, call: ToolCallEvent, tool: Tool, turn: Turn) -> Decision:
        return Decision.ALLOW


class DenyAllDecider:
    """拒绝一切。给"没有界面但又不敢放行"的场景兜底。"""

    async def decide(self, call: ToolCallEvent, tool: Tool, turn: Turn) -> Decision:
        return Decision.DENY


class BatchPlan(BaseModel):
    """一批工具调用的执行计划。"""

    model_config = ConfigDict(frozen=True)

    concurrent: bool
    #: 执行顺序（列表下标）。串行时 == 模型给出的顺序；并发时顺序无意义
    order: list[int]
    #: 并发组的编号（``ToolCallStarted.concurrent_group``）；串行时为 ``None``
    group: int | None = None
    reason: str = ""


def plan_batch(calls: Sequence[ToolCallEvent], registry: ToolRegistry) -> BatchPlan:
    """决定这一批是并发还是串行。

    **只读性是唯一的判据**，且未知工具保守视为"写"（``registry.is_readonly`` 已如此）——
    把未知当成可并发等于赌它没有副作用，赌输的代价是数据竞争。
    """
    order = list(range(len(calls)))
    if len(calls) <= 1:
        return BatchPlan(concurrent=False, order=order, reason="只有一个调用，无需并发")

    writers = sorted({call.name for call in calls if not registry.is_readonly(call.name)})
    if writers:
        return BatchPlan(
            concurrent=False,
            order=order,
            reason=f"批内含非只读工具（{'、'.join(writers)}），整批串行以保持模型给出的顺序",
        )
    return BatchPlan(concurrent=True, order=order, group=0, reason=f"{len(calls)} 个只读调用，可并发")


class Scheduler:
    """按 :class:`BatchPlan` 执行一批工具调用。"""

    def __init__(
        self,
        bus: EventBus,
        registry: ToolRegistry,
        decider: PermissionDecider,
        *,
        concurrency: int = 4,
        cwd: Path | str = ".",
        blob_store: BlobStoreProtocol | None = None,
    ) -> None:
        if concurrency < 1:
            raise ValueError("concurrency 必须 ≥ 1")
        self._bus = bus
        self._registry = registry
        self._decider = decider
        self._concurrency = concurrency
        self._cwd = Path(cwd)
        self._blob_store = blob_store


    # ------------------------------------------------------------------ #
    # 主入口
    # ------------------------------------------------------------------ #

    async def run_batch(self, turn: Turn, calls: Sequence[ToolCallEvent]) -> list[ToolResultBlock]:
        """执行一批工具调用，返回**按模型给出顺序**排列的工具结果。"""
        if not calls:
            return []

        for call in calls:
            await self._emit_requested(turn, call, batch_size=len(calls))

        # 权限决策**先全部做完再执行**：这样被拒的调用不会与放行的调用交错，
        # 用户看到的事件顺序才是"先问完、再做"。
        outcomes: dict[str, ToolResultBlock] = {}
        approved: list[ToolCallEvent] = []
        for call in calls:
            denied = await self._authorize(turn, call)
            if denied is None:
                approved.append(call)
            else:
                # 被拒的调用**也要走 Started/Finished**：否则界面上会出现一张
                # 只有"请求"没有下文的卡片，而配对不变量也会被打破。
                await self._emit_pair(
                    turn,
                    call,
                    ok=False,
                    digest=denied.block.content,
                    content=denied.block.content,
                    error_kind=denied.kind,
                )
                outcomes[call.call_id] = denied.block

        if approved:
            plan = plan_batch(approved, self._registry)
            results = await self._execute(turn, approved, plan)
            outcomes.update({call.call_id: block for call, block in zip(approved, results, strict=True)})

        turn.tool_call_count += len(calls)
        # ★ 按模型给出的顺序返回——协议要求工具结果与 tool_use 一一对应且顺序一致
        return [outcomes[call.call_id] for call in calls]

    # ------------------------------------------------------------------ #
    # 事件
    # ------------------------------------------------------------------ #

    async def _emit_requested(self, turn: Turn, call: ToolCallEvent, *, batch_size: int) -> None:
        await self._bus.publish(
            ev.ToolCallRequested(
                session_id=self._bus.session_id,
                turn=turn.turn_index,
                call_id=call.call_id,
                name=call.name,
                args=dict(call.arguments),
                readonly=self._registry.is_readonly(call.name),
                batch_size=batch_size,
            )
        )

    async def _emit_pair(
        self,
        turn: Turn,
        call: ToolCallEvent,
        *,
        ok: bool,
        digest: str,
        content: str = "",
        error_kind: str | None,
        concurrent_group: int | None = None,
        started_at: float | None = None,
    ) -> None:
        """发出配对的 ``ToolCallStarted`` + ``ToolCallFinished``。"""
        await self._bus.publish(
            ev.ToolCallStarted(
                session_id=self._bus.session_id,
                turn=turn.turn_index,
                call_id=call.call_id,
                concurrent_group=concurrent_group,
            )
        )
        started = time.perf_counter() if started_at is None else started_at
        await self._bus.publish(
            ev.ToolCallFinished(
                session_id=self._bus.session_id,
                turn=turn.turn_index,
                call_id=call.call_id,
                ok=ok,
                duration_ms=max(0, int((time.perf_counter() - started) * 1000)),
                result_digest=digest,
                content=content,
                error_kind=error_kind,
            )
        )

    # ------------------------------------------------------------------ #
    # 权限
    # ------------------------------------------------------------------ #

    async def _authorize(self, turn: Turn, call: ToolCallEvent) -> _Denied | None:
        """决策。返回 ``None`` 表示放行，否则返回拒绝的理由与结果。"""
        tool = self._registry.get(call.name)
        if tool is None:
            # 模型幻觉出一个不存在的工具：**回灌让它自愈**，不报错（见 registry.get 的说明）
            available = "、".join(self._registry.names()) or "（无）"
            return _Denied(
                block=ToolResultBlock(
                    id=call.call_id,
                    ok=False,
                    content=f"没有名为 {call.name!r} 的工具。可用的工具：{available}",
                ),
                kind=ERROR_UNKNOWN_TOOL,
            )

        if not tool.spec.requires_permission:
            return None

        decision = await self._decider.decide(call, tool, turn)
        if decision is Decision.ALLOW:
            return None

        if decision is Decision.ASK:
            # M3 没有界面：ASK **降级为 DENY**。
            # 绝不在无人值守时默认放行——这正是 MODULE_kernel_loop §9 第 1 项的裁定。
            await self._bus.publish(
                ev.PermissionRequested(
                    session_id=self._bus.session_id,
                    turn=turn.turn_index,
                    call_id=call.call_id,
                    prompt=f"允许执行 {call.name} 吗？（当前没有权限确认界面，已按拒绝处理）",
                    risk="normal",
                )
            )
            await self._bus.publish(
                ev.PermissionResolved(
                    session_id=self._bus.session_id,
                    turn=turn.turn_index,
                    call_id=call.call_id,
                    decision="deny",
                )
            )
            return _Denied(
                block=ToolResultBlock(
                    id=call.call_id,
                    ok=False,
                    content=f"{call.name} 未执行：当前没有可用的权限确认界面，已按拒绝处理",
                ),
                kind=ERROR_DENIED,
            )

        # 决策链直接拒绝（未经询问）：这既不是"用户被问过"，也不是"用户答了"，
        # 因此**不发**权限事件——只把结果回灌给模型。
        reason = getattr(self._decider, "last_rejection_reason", None) or f"{call.name} 被策略拒绝，未执行"
        return _Denied(
            block=ToolResultBlock(id=call.call_id, ok=False, content=reason),
            kind=ERROR_DENIED,
        )

    # ------------------------------------------------------------------ #
    # 执行
    # ------------------------------------------------------------------ #

    async def _execute(
        self, turn: Turn, calls: Sequence[ToolCallEvent], plan: BatchPlan
    ) -> list[ToolResultBlock]:
        if plan.concurrent:
            return await self._run_concurrent(turn, calls, plan)
        return [await self._call_one(turn, call, concurrent_group=None) for call in calls]

    async def _run_concurrent(
        self, turn: Turn, calls: Sequence[ToolCallEvent], plan: BatchPlan
    ) -> list[ToolResultBlock]:
        """并发执行。

        **不用** ``return_exceptions=True``：失败已经在 :meth:`_call_one` 内部被转成了
        结果，所以这里真的抛出来的只可能是 :class:`asyncio.CancelledError`——而它必须
        传播出去让整个回合停下来（D51）。``return_exceptions=True`` 会把取消吞成一个
        "结果对象"，Esc 就静默失效了。
        """
        limit = asyncio.Semaphore(self._concurrency)

        async def guarded(call: ToolCallEvent) -> ToolResultBlock:
            async with limit:
                return await self._call_one(turn, call, concurrent_group=plan.group)

        return list(await asyncio.gather(*(guarded(call) for call in calls)))

    async def _call_one(
        self, turn: Turn, call: ToolCallEvent, *, concurrent_group: int | None
    ) -> ToolResultBlock:
        """执行单个工具。**三种出口（成功 / 异常 / 取消）都保证配对**。"""
        started = time.perf_counter()
        await self._bus.publish(
            ev.ToolCallStarted(
                session_id=self._bus.session_id,
                turn=turn.turn_index,
                call_id=call.call_id,
                concurrent_group=concurrent_group,
            )
        )

        try:
            result = await self._invoke(turn, call)
        except asyncio.CancelledError:
            # 先把配对的 Finished 补出去，**再**把取消继续抛上去。
            # 顺序不能反：一旦抛出，后面这行就再也不会执行了，
            # 而缺一个 Finished 就意味着界面上永远有一张转圈的卡片。
            await self._emit_finished(turn, call, started, ok=False, digest="", content="", error_kind=ERROR_CANCELLED)
            raise
        except Exception as exc:  # noqa: BLE001 - 工具异常绝不穿透循环（R6）
            result = ToolResult.failure(
                ErrorCategory.TOOL_FAILURE,
                f"工具 {call.name} 执行时抛出异常：{type(exc).__name__}: {exc}",
            )
            await self._emit_finished(turn, call, started, ok=False, digest="", content=result.content, error_kind=ERROR_TOOL_RAISED)
            return ToolResultBlock(id=call.call_id, ok=False, content=result.content)

        error_kind = None
        if not result.ok:
            error_kind = result.error.category.value if result.error else "unknown"
        await self._emit_finished(
            turn,
            call,
            started,
            ok=result.ok,
            digest=result.digest,
            content=result.content,
            error_kind=error_kind,
            change_stat=result.change_stat,
        )
        return ToolResultBlock(id=call.call_id, ok=result.ok, content=result.content)

    async def _emit_finished(
        self,
        turn: Turn,
        call: ToolCallEvent,
        started: float,
        *,
        ok: bool,
        digest: str,
        content: str = "",
        error_kind: str | None,
        change_stat: ev.ChangeStat | None = None,
    ) -> None:
        await self._bus.publish(
            ev.ToolCallFinished(
                session_id=self._bus.session_id,
                turn=turn.turn_index,
                call_id=call.call_id,
                ok=ok,
                duration_ms=max(0, int((time.perf_counter() - started) * 1000)),
                result_digest=digest,
                content=content,
                error_kind=error_kind,
                change_stat=change_stat,
            )
        )

    async def _invoke(self, turn: Turn, call: ToolCallEvent) -> ToolResult:
        """调用工具本体：参数校验 → 执行。**参数错误在这里收口成结果。**"""
        tool = self._registry.get(call.name)
        if tool is None:  # pragma: no cover - 已由 _authorize 拦下
            return ToolResult.failure(ErrorCategory.BAD_REQUEST, f"没有名为 {call.name!r} 的工具")

        try:
            args = tool.spec.params.model_validate(call.arguments)
        except PydanticValidationError as exc:
            # 参数不合法是最常见的模型错误之一，**回灌让它自己改**（D22）
            return ToolResult.failure(
                ErrorCategory.BAD_REQUEST,
                f"{call.name} 的参数不合法，请修正后重试",
                detail=_render_validation_error(exc),
            )

        ctx = ToolContext(cwd=self._cwd, is_cancelled=turn.interrupt.is_set)

        # 检查写操作快照捕获点（D102）
        target_file: Path | None = None
        before_hash: str | None = None
        if self._blob_store is not None and not tool.spec.readonly and hasattr(args, "path"):
            try:
                raw_path = Path(args.path)
                target_file = raw_path if raw_path.is_absolute() else (self._cwd.resolve() / raw_path).resolve()
                if target_file.is_file():
                    before_hash = self._blob_store.put_file(target_file)
            except Exception as exc:
                logger.debug("写前快照读取跳过：%s", exc)
                before_hash = None

        result = await tool.run(args, ctx)

        # 写入成功后捕获更新后的快照并发布事件
        if result.ok and target_file is not None and target_file.is_file() and self._blob_store is not None:
            try:
                after_hash = self._blob_store.put_file(target_file)
                try:
                    rel = str(target_file.relative_to(self._cwd.resolve())).replace("\\", "/")
                except ValueError:
                    rel = str(target_file).replace("\\", "/")
                await self._bus.publish(
                    ev.CheckpointCreated(
                        session_id=self._bus.session_id,
                        turn=turn.turn_index,
                        files=[rel],
                        path=rel,
                        before_hash=before_hash,
                        after_hash=after_hash,
                    )
                )
            except Exception as exc:
                logger.warning("记录检查点快照失败：%s", exc)

        return result



class _Denied(BaseModel):
    """内部小结构：被拒绝时的结果与 ``error_kind``（避免用裸 tuple，R5）。"""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    block: ToolResultBlock
    kind: str


def _render_validation_error(exc: PydanticValidationError) -> str:
    """把 pydantic 的校验错误压成**模型能照着改**的短句。

    原始错误里有 ``type`` / ``url`` / ``input`` 之类给开发者看的字段——它们对模型
    是纯噪声，还会白占 token。只留「字段: 原因」。
    """
    lines = []
    for item in exc.errors()[:5]:
        location = ".".join(str(part) for part in item.get("loc", ())) or "(根)"
        lines.append(f"{location}: {item.get('msg', '校验失败')}")
    return "\n".join(lines)
