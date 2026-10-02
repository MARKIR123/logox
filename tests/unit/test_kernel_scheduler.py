"""并发调度测试（MODULE_kernel_loop §7.2）。

D27 的全部要害都在这里：**全只读才并发**、**含写整批串行且保持模型顺序**、
**并发组内互不影响**、**结果永远按模型给出的顺序返回**。

耗时断言全部基于受控的 ``asyncio.sleep`` 而不是真实时钟——依赖真实时钟的
"并发比串行快"断言在负载高的机器上会随机失败。
"""

from __future__ import annotations

import asyncio
import unittest
from typing import Any

from pydantic import BaseModel

from logox.errors import ErrorCategory
from logox.kernel.bus import EventBus
from logox.kernel.registry import ToolRegistry
from logox.kernel.scheduler import (
    ERROR_CANCELLED,
    AllowAllDecider,
    Decision,
    DenyAllDecider,
    Scheduler,
    plan_batch,
)
from logox.kernel.turn import Turn
from logox.providers.base import ToolCallEvent
from logox.tools.base import ToolArgs, ToolContext, ToolResult, ToolSpec
from tests.unit.kernel_support import Recorder

SLEEP_S = 0.05


class NameArgs(ToolArgs):
    #: 刻意**必填**：这样"缺字段"与"多字段"两条错误路径都能被测到
    name: str


class ConcurrencyProbe:
    """**跨工具实例**的并发探针。

    每个工具各记一份 ``_live`` 是没用的——每个工具只会看到自己那一次调用，
    ``peak`` 恒为 1。要观察"同时有几个工具在跑"，计数器必须共享。

    ``all_in`` 让"并发"这件事可以**确定性地**观察：第 ``expected`` 个工具一进来
    就把闸门放开，于是所有工具立刻一起结束。并发时它秒过；串行时第一个工具会
    一直等到超时——**测试因此不再依赖"50ms 够不够三次调度"这种运气**。
    （原先用 ``asyncio.sleep`` 计时长，在满负载跑整套测试时偶发失败。）
    """

    def __init__(self, expected: int = 0) -> None:
        self.live = 0
        self.peak = 0
        self.expected = expected
        self.all_in = asyncio.Event()

    def enter(self) -> None:
        self.live += 1
        self.peak = max(self.peak, self.live)
        if self.expected and self.live >= self.expected:
            self.all_in.set()

    def exit(self) -> None:
        self.live -= 1


class SlowTool:
    """用一个可控的等待点来观察并发 vs 串行。

    ``barrier=True`` 时等的是并发闸门（确定性），否则睡固定时长（用于"确实串行"
    这类不需要精确计时的场景）。
    """

    def __init__(
        self,
        name: str,
        *,
        readonly: bool = True,
        sleep_s: float = SLEEP_S,
        fail: bool = False,
        requires_permission: bool = True,
        probe: ConcurrencyProbe | None = None,
        barrier: bool = False,
        timeout_s: float = 2.0,
    ) -> None:
        self.spec = ToolSpec(
            name=name,
            params=NameArgs,
            readonly=readonly,
            requires_permission=requires_permission,
            summary_template=f"{name} {{name}}",
        )
        self.sleep_s = sleep_s
        self.fail = fail
        self.started: list[str] = []
        self.probe = probe or ConcurrencyProbe()
        self.barrier = barrier
        self.timeout_s = timeout_s

    @property
    def peak(self) -> int:
        return self.probe.peak

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, NameArgs)
        self.started.append(args.name)
        self.probe.enter()
        try:
            if self.barrier:
                await asyncio.wait_for(self.probe.all_in.wait(), timeout=self.timeout_s)
            else:
                await asyncio.sleep(self.sleep_s)
        finally:
            self.probe.exit()
        if self.fail:
            return ToolResult.failure(ErrorCategory.TOOL_FAILURE, f"{args.name} 失败了")
        return ToolResult(ok=True, content=f"{self.spec.name}:{args.name}")


class ExplodingTool:
    spec = ToolSpec(name="boom", params=NameArgs, readonly=True, requires_permission=False)

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        raise RuntimeError("工具内部炸了")


class CantCancelTool:
    """故意吞掉取消——验证"取消失败了也必须补上配对事件"这条兜底。"""

    spec = ToolSpec(name="stubborn", params=NameArgs, readonly=True, requires_permission=False)

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:  # pragma: no cover - 不会被正常调用
        return ToolResult(ok=True, content="ok")


def call(call_id: str, name: str, **arguments: Any) -> ToolCallEvent:
    return ToolCallEvent(call_id=call_id, name=name, arguments=arguments or {"name": "x"})


class BatchPlanTests(unittest.TestCase):
    def test_t30_single_call_is_serial(self) -> None:
        registry = ToolRegistry()
        registry.register(SlowTool("a"))
        plan = plan_batch([call("c1", "a")], registry)
        self.assertFalse(plan.concurrent)

    def test_t31_all_readonly_is_concurrent(self) -> None:
        registry = ToolRegistry()
        registry.register(SlowTool("a"))
        registry.register(SlowTool("b"))
        plan = plan_batch([call("c1", "a"), call("c2", "b")], registry)
        self.assertTrue(plan.concurrent)
        self.assertEqual(plan.group, 0)

    def test_t32_any_writer_makes_the_whole_batch_serial(self) -> None:
        """D27：批内出现任一写操作 → **整批**串行（不是只把写挑出来）。"""
        registry = ToolRegistry()
        registry.register(SlowTool("reader"))
        registry.register(SlowTool("writer", readonly=False))
        plan = plan_batch([call("c1", "reader"), call("c2", "writer")], registry)
        self.assertFalse(plan.concurrent)
        self.assertIn("writer", plan.reason)

    def test_t33_unknown_tool_counts_as_writer(self) -> None:
        """D27 的边界用例②：MCP 工具未声明只读注解 → 保守视为写。"""
        registry = ToolRegistry()
        registry.register(SlowTool("a"))
        plan = plan_batch([call("c1", "a"), call("c2", "mcp__unknown")], registry)
        self.assertFalse(plan.concurrent)

    def test_t34_serial_order_is_the_model_order(self) -> None:
        registry = ToolRegistry()
        plan = plan_batch([call("c1", "x"), call("c2", "y"), call("c3", "z")], registry)
        self.assertEqual(plan.order, [0, 1, 2])


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.bus = EventBus(session_id="s")
        self.recorder = Recorder()
        self.bus.subscribe("*", self.recorder, name="rec")
        self.registry = ToolRegistry()
        self.turn = Turn(turn_index=1)

    def _scheduler(self, **kwargs: Any) -> Scheduler:
        return Scheduler(
            self.bus,
            self.registry,
            kwargs.pop("decider", AllowAllDecider()),
            concurrency=kwargs.pop("concurrency", 4),
            cwd=".",
        )

    async def test_t35_concurrent_readonly_is_actually_parallel(self) -> None:
        """★ 三个只读工具必须**真的同时**在跑。

        用并发闸门而不是比耗时：闸门只在三个都进来时才放开，所以并发时秒过、
        串行时等到超时。耗时断言在满负载机器上会随机失败，闸门不会。
        """
        probe = ConcurrencyProbe(expected=3)
        tools = [SlowTool(name, probe=probe, barrier=True) for name in ("a", "b", "c")]
        self.registry.register_all(tools)  # type: ignore[arg-type]
        await self._scheduler().run_batch(self.turn, [call(f"c{i}", t.spec.name) for i, t in enumerate(tools)])
        self.assertEqual(probe.peak, 3)

    async def test_t36_batch_with_a_writer_runs_strictly_in_order(self) -> None:
        probe = ConcurrencyProbe(expected=2)
        first = SlowTool("a", readonly=False, sleep_s=SLEEP_S * 2, probe=probe)
        second = SlowTool("b", readonly=False, sleep_s=0.0, probe=probe)
        self.registry.register_all([first, second])  # type: ignore[arg-type]
        await self._scheduler().run_batch(self.turn, [call("c1", "a"), call("c2", "b")])
        self.assertEqual(probe.peak, 1)  # 含写 → 整批串行 → 从未同时运行
        self.assertEqual(first.started, ["x"])
        self.assertEqual(second.started, ["x"])
        self.assertFalse(probe.all_in.is_set())  # 闸门永远等不到"两个同时进来"

    async def test_t37_results_follow_the_model_order_not_completion_order(self) -> None:
        """完成顺序是反的，返回顺序必须是模型给的顺序——协议要求一一对应。"""
        probe = ConcurrencyProbe()
        slow = SlowTool("slow", sleep_s=SLEEP_S * 3, probe=probe)
        fast = SlowTool("fast", sleep_s=0.0, probe=probe)
        self.registry.register_all([slow, fast])  # type: ignore[arg-type]
        results = await self._scheduler().run_batch(
            self.turn, [call("c_slow", "slow"), call("c_fast", "fast")]
        )
        self.assertEqual([r.id for r in results], ["c_slow", "c_fast"])
        self.assertEqual(results[0].content, "slow:x")

    async def test_t38_concurrency_limit_is_respected(self) -> None:
        """6 个只读工具、并发度 2 → 任何时刻最多 2 个在跑。"""
        probe = ConcurrencyProbe(expected=0)  # 不用闸门：闸门会放开全部 6 个
        tools = [SlowTool(f"t{i}", sleep_s=SLEEP_S, probe=probe) for i in range(6)]
        self.registry.register_all(tools)  # type: ignore[arg-type]
        await self._scheduler(concurrency=2).run_batch(
            self.turn, [call(f"c{i}", f"t{i}") for i in range(6)]
        )
        self.assertLessEqual(probe.peak, 2)
        self.assertGreaterEqual(probe.peak, 2, "并发度应当真的被用满，而不是退化成串行")

    async def test_t39_one_failure_does_not_affect_siblings(self) -> None:
        ok = SlowTool("ok")
        bad = SlowTool("bad", fail=True)
        self.registry.register_all([ok, bad])  # type: ignore[arg-type]
        results = await self._scheduler().run_batch(self.turn, [call("c1", "ok"), call("c2", "bad")])
        self.assertTrue(results[0].ok)
        self.assertFalse(results[1].ok)
        self.assertEqual(ok.started, ["x"])

    async def test_t40_tool_exception_becomes_a_result_not_a_crash(self) -> None:
        """R6：工具异常绝不穿透循环。"""
        self.registry.register(ExplodingTool())  # type: ignore[arg-type]
        results = await self._scheduler().run_batch(self.turn, [call("c1", "boom")])
        self.assertFalse(results[0].ok)
        self.assertIn("RuntimeError", results[0].content)
        finished = self.recorder.of("tool_call_finished")[0]
        self.assertEqual(finished.error_kind, "tool_raised")

    async def test_t41_unknown_tool_is_fed_back_for_self_healing(self) -> None:
        """模型幻觉出一个工具名：不报错、不崩，回灌可用列表让它自己改。"""
        self.registry.register(SlowTool("a"))  # type: ignore[arg-type]
        results = await self._scheduler().run_batch(self.turn, [call("c1", "nope")])
        self.assertFalse(results[0].ok)
        self.assertIn("没有名为 'nope' 的工具", results[0].content)
        self.assertIn("a", results[0].content)  # 可用值列表
        self.assertFalse(self.recorder.of("error_occurred"))

    async def test_t42_invalid_arguments_are_fed_back_with_field_paths(self) -> None:
        """参数不合法要给出**字段路径**，否则模型不知道改哪个字段。"""
        self.registry.register(SlowTool("a"))  # type: ignore[arg-type]
        results = await self._scheduler().run_batch(
            self.turn, [call("c1", "a", wrong_field="x")]
        )
        self.assertFalse(results[0].ok)
        self.assertIn("参数不合法", results[0].content)
        self.assertIn("wrong_field", results[0].content)  # 多余字段的路径
        self.assertIn("name", results[0].content)  # 缺失必填字段的路径

    async def test_t42b_argument_errors_do_not_leak_pydantic_internals(self) -> None:
        """原始错误里的 ``type`` / ``url`` / ``input`` 对模型是纯噪声，还会白占 token。"""
        self.registry.register(SlowTool("a"))  # type: ignore[arg-type]
        results = await self._scheduler().run_batch(
            self.turn, [call("c1", "a", wrong_field="x")]
        )
        for noise in ("pydantic.dev", "'type':", "Value error,", "For further information"):
            with self.subTest(noise=noise):
                self.assertNotIn(noise, results[0].content)

    async def test_t43_denied_call_still_gets_a_started_finished_pair(self) -> None:
        """被拒的调用也要走 Started/Finished——否则界面上会有一张没有下文的卡片。"""
        self.registry.register(SlowTool("a"))  # type: ignore[arg-type]
        results = await self._scheduler(decider=DenyAllDecider()).run_batch(self.turn, [call("c1", "a")])
        self.assertFalse(results[0].ok)
        self.assertEqual(len(self.recorder.of("tool_call_started")), 1)
        finished = self.recorder.of("tool_call_finished")
        self.assertEqual(len(finished), 1)
        self.assertEqual(finished[0].error_kind, "denied")

    async def test_t44_ask_degrades_to_deny_and_says_so(self) -> None:
        """M3 没有界面：ASK 降级为 DENY，并且**派发权限事件让上层看得见**。"""

        class Asker:
            async def decide(self, call_: Any, tool: Any, turn: Any) -> Decision:
                return Decision.ASK

        self.registry.register(SlowTool("a"))  # type: ignore[arg-type]
        results = await self._scheduler(decider=Asker()).run_batch(self.turn, [call("c1", "a")])
        self.assertFalse(results[0].ok)
        self.assertIn("权限确认界面", results[0].content)
        self.assertTrue(self.recorder.of("permission_requested"))
        self.assertEqual(self.recorder.of("permission_resolved")[0].decision, "deny")

    async def test_t45_rule_denial_does_not_fake_a_user_decision(self) -> None:
        """策略直接拒绝既不是"问过用户"也不是"用户答了"，因此不发权限事件。"""

        class Denier:
            async def decide(self, call_: Any, tool: Any, turn: Any) -> Decision:
                return Decision.DENY

        self.registry.register(SlowTool("a"))  # type: ignore[arg-type]
        await self._scheduler(decider=Denier()).run_batch(self.turn, [call("c1", "a")])
        self.assertFalse(self.recorder.of("permission_requested"))
        self.assertFalse(self.recorder.of("permission_resolved"))

    async def test_t46_tool_without_permission_requirement_still_checks_the_decider(self) -> None:
        self.registry.register(SlowTool("a", requires_permission=False))  # type: ignore[arg-type]
        results = await self._scheduler(decider=DenyAllDecider()).run_batch(self.turn, [call("c1", "a")])
        self.assertFalse(results[0].ok)

    async def test_t47_cancel_finishes_every_started_tool(self) -> None:
        """E-14：中断时每个 Started 都必须有配对的 Finished，否则卡片永远转圈。"""
        tools = [SlowTool(name, sleep_s=1.0) for name in ("a", "b", "c")]
        self.registry.register_all(tools)  # type: ignore[arg-type]
        calls = [call(f"c{i}", name) for i, name in enumerate("abc")]

        task = asyncio.create_task(self._scheduler().run_batch(self.turn, calls))
        await asyncio.sleep(0.02)  # 让三个工具都进入 Started
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        started = self.recorder.of("tool_call_started")
        finished = self.recorder.of("tool_call_finished")
        self.assertEqual(len(started), 3)
        self.assertEqual(len(finished), 3)
        self.assertTrue(all(item.error_kind == ERROR_CANCELLED for item in finished))
        self.assertEqual({item.call_id for item in started}, {item.call_id for item in finished})

    async def test_t48_cancellation_is_never_swallowed_by_gather(self) -> None:
        """``return_exceptions=True`` 会把取消吞成一个值——Esc 就静默失效了。"""
        self.registry.register(SlowTool("a", sleep_s=1.0))  # type: ignore[arg-type]
        task = asyncio.create_task(self._scheduler().run_batch(self.turn, [call("c1", "a")]))
        await asyncio.sleep(0.02)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(task.cancelled())

    async def test_t49_started_finished_pairing_invariant_across_scenarios(self) -> None:
        """T-22 的不变量：**任何**场景下两个计数都相等。"""
        scenarios: list[tuple[ToolRegistry, list[ToolCallEvent], Any]] = []

        def build(*tools: Any) -> ToolRegistry:
            registry = ToolRegistry()
            registry.register_all(list(tools))
            return registry

        scenarios.append((build(SlowTool("a"), SlowTool("b")), [call("c1", "a"), call("c2", "b")], AllowAllDecider()))
        scenarios.append((build(SlowTool("a")), [call("c1", "missing")], AllowAllDecider()))
        scenarios.append((build(ExplodingTool()), [call("c1", "boom")], AllowAllDecider()))
        scenarios.append((build(SlowTool("a")), [call("c1", "a")], DenyAllDecider()))
        scenarios.append((build(SlowTool("a", readonly=False)), [call("c1", "a")], AllowAllDecider()))

        for index, (registry, calls, decider) in enumerate(scenarios):
            with self.subTest(scenario=index):
                bus = EventBus(session_id="s")
                recorder = Recorder()
                bus.subscribe("*", recorder, name="rec")
                scheduler = Scheduler(bus, registry, decider, cwd=".")
                await scheduler.run_batch(Turn(turn_index=1), calls)
                self.assertEqual(
                    len(recorder.of("tool_call_started")),
                    len(recorder.of("tool_call_finished")),
                )

    async def test_t50_empty_batch_is_a_noop(self) -> None:
        results = await self._scheduler().run_batch(self.turn, [])
        self.assertEqual(results, [])
        self.assertEqual(self.recorder.events, [])

    async def test_t51_event_order_is_requested_then_started_then_finished(self) -> None:
        self.registry.register(SlowTool("a"))  # type: ignore[arg-type]
        await self._scheduler().run_batch(self.turn, [call("c1", "a")])
        self.assertEqual(
            self.recorder.types(),
            ["tool_call_requested", "tool_call_started", "tool_call_finished"],
        )

    async def test_t52_concurrent_calls_carry_a_group_id(self) -> None:
        self.registry.register_all([SlowTool("a"), SlowTool("b")])  # type: ignore[arg-type]
        await self._scheduler().run_batch(self.turn, [call("c1", "a"), call("c2", "b")])
        self.assertTrue(all(item.concurrent_group == 0 for item in self.recorder.of("tool_call_started")))

    async def test_t53_serial_calls_have_no_group_id(self) -> None:
        self.registry.register(SlowTool("a", readonly=False))  # type: ignore[arg-type]
        await self._scheduler().run_batch(self.turn, [call("c1", "a")])
        self.assertIsNone(self.recorder.of("tool_call_started")[0].concurrent_group)

    async def test_t54_invalid_concurrency_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            Scheduler(self.bus, self.registry, AllowAllDecider(), concurrency=0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
