"""F-56（模型切换同步窗口与 κ 桶，D159）与 F-55（`pre_compact` 钩子挂点，D167）的用例。"""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from logox.app import build_runtime
from logox.config.schema import ContextConfig, LogoxConfig, ProviderConfig
from logox.context.builder import HierarchicalContextBuilder
from logox.context.compaction import Compactor
from logox.context.memory import ProjectMemory
from logox.context.storage import SessionTranscriptWriter
from logox.kernel import events as ev
from logox.kernel import events as ev
from logox.kernel.bus import EventBus
from logox.kernel.loop import CompactionPlan, CompactionReport, ContextBundle, KernelLoop
from logox.kernel.messages import Message, TextBlock
from logox.kernel.turn import Turn
from logox.paths import LogoxPaths
from tests.unit.support import make_temp_dir, remove_temp_dir

WINDOW = {"window_capacity": 2000, "max_budget_tokens": 1500, "target_budget_tokens": 1000}


def history(turns: int, *, chars: int = 200) -> list[Message]:
    """造一段会触发压缩的历史（每条消息都足够长）。"""
    out: list[Message] = []
    for index in range(turns):
        out.append(Message(role="user", blocks=[TextBlock(text=f"第{index}问" + "问" * chars)]))
        out.append(Message(role="assistant", blocks=[TextBlock(text=f"第{index}答" + "答" * chars)]))
    return out


class WindowHotSwapTests(unittest.TestCase):
    """F-56：换模型 ⇒ 水位线必须跟着换（否则会算出高于真实窗口的触发线）。"""

    def test_t01_set_window_capacity_recomputes_watermarks(self) -> None:
        compactor = Compactor(window_capacity=1_000_000, reserve_tokens=32_768)
        self.assertEqual(compactor.high_watermark, 1_000_000 - 32_768)

        compactor.set_window_capacity(64_000)
        self.assertEqual(compactor.window_capacity, 64_000)
        # 小窗口保护：reserve ≤ 窗口/4
        self.assertEqual(compactor.reserve_tokens, 16_000)
        self.assertEqual(compactor.high_watermark, 64_000 - 16_000)
        self.assertEqual(compactor.low_watermark, int(64_000 * 0.5))

    def test_t02_zero_or_negative_window_is_ignored(self) -> None:
        compactor = Compactor(window_capacity=100_000)
        before = compactor.high_watermark
        compactor.set_window_capacity(0)
        self.assertEqual(compactor.high_watermark, before)

    def test_t03_builder_set_model_switches_key_window_and_drops_the_anchor(self) -> None:
        tmp = make_temp_dir("f56-builder-")
        try:
            builder = HierarchicalContextBuilder(
                system="sys",
                cwd=tmp,
                transcript_writer=SessionTranscriptWriter(base_dir=tmp / "s", session_id="m"),
                window_capacity=1_000_000,
                model_key="deepseek/deepseek-flash",
            )
            builder.build(history(2))
            builder.build(history(2), last_usage=ev.Usage(input_tokens=10, output_tokens=1, context_tokens=10))
            self.assertIsNotNone(builder.ledger.anchor)

            builder.set_model(model_key="anthropic/claude-sonnet-4-5", window_capacity=200_000)

            self.assertEqual(builder.model_key, "anthropic/claude-sonnet-4-5")
            self.assertEqual(builder.compactor.model_key, "anthropic/claude-sonnet-4-5", "κ 没换桶")
            self.assertEqual(builder.window_capacity, 200_000)
            self.assertEqual(builder.compactor.window_capacity, 200_000, "水位线没跟着换")
            # 换模型后锚点必须失效（predict 会比 model_key，不靠人记得重置）
            prediction = builder.ledger.predict(
                [], system_prompt="s", estimator=builder.estimator,
                model_key=builder.model_key, prefix_digest="d",
            )
            self.assertFalse(prediction.used_anchor)
        finally:
            remove_temp_dir(tmp)


class ApplyModelTests(unittest.TestCase):
    """F-56：装配根的 `apply_model` 一次更新三处（内核 / 计量 / 状态栏窗口）。"""

    def setUp(self) -> None:
        self.root = make_temp_dir("f56-apply-")
        self.addCleanup(remove_temp_dir, self.root)
        self.paths = LogoxPaths.at(self.root)

    def _runtime(self):
        config = LogoxConfig(
            provider=ProviderConfig(name="deepseek", model="deepseek-flash"),
            context=ContextConfig(),
        )
        return build_runtime(SimpleNamespace(config=config, sources=[]), self.root, self.paths)

    def test_t04_apply_model_updates_kernel_builder_and_status_bar(self) -> None:
        runtime = self._runtime()
        old_window = runtime.reducer.metrics.context_window

        window = runtime.apply_model("deepseek-v4-pro")

        self.assertEqual(runtime.kernel._model, "deepseek-v4-pro")
        self.assertEqual(runtime.model, "deepseek-v4-pro")
        self.assertEqual(
            runtime.context_builder.model_key, "deepseek/deepseek-v4-pro", "κ 桶没更新"
        )
        self.assertEqual(runtime.context_builder.compactor.window_capacity, window)
        self.assertEqual(runtime.reducer.metrics.context_window, window, "状态栏分母没更新")
        self.assertTrue(window and window > 0)
        del old_window

    def test_t05_unknown_model_keeps_the_current_window(self) -> None:
        runtime = self._runtime()
        before = runtime.context_builder.window_capacity

        window = runtime.apply_model("my-custom-model")

        self.assertIsNone(window, "查不到的模型应当返回 None（不伪造窗口）")
        self.assertEqual(runtime.context_builder.window_capacity, before, "查不到就沿用旧窗口")
        self.assertEqual(runtime.context_builder.model_key, "deepseek/my-custom-model", "κ 桶仍要换")


class PlanHookTests(unittest.IsolatedAsyncioTestCase):
    """F-55：`plan()` 让 `pre_compact` 钩子在**动手之前**有挂点。"""

    def _builder(self, tmp: Path, **kwargs) -> HierarchicalContextBuilder:
        with mock.patch(
            "logox.context.builder.find_project_memory",
            return_value=ProjectMemory(sources=[], total_tokens=0),
        ):
            return HierarchicalContextBuilder(
                system="sys",
                cwd=tmp,
                transcript_writer=SessionTranscriptWriter(base_dir=tmp / "s", session_id="plan"),
                **{**WINDOW, **kwargs},
            )

    def test_t06_plan_reports_pending_compaction(self) -> None:
        tmp = make_temp_dir("f55-plan-")
        try:
            # ⚠️ 窗口必须**远大于固定系统提示**，否则这条用例的前提不成立。
            #
            # 它想验的是"**历史**变长才触发压缩"。而系统提示是**固定开销**：
            # 实测 ≈1572 token（其中回合摘要契约就占 ~1160）。
            # 默认 `WINDOW` 的水位是 1500 ⇒ **光系统提示 + 1 轮历史 = 1572 就超线**，
            # 于是 `plan(history(1))` 报要压缩 ⇒ 误判为失败。
            # 实测：原值下只剩 **14 token 余量** —— 任何给契约加一句话的改动都会顶红
            #（D160 改契约措辞时就是这么撞上的）。
            #
            # ⚠️ 不能用 `max_budget_tokens` 来放宽：那个参数**只能压低水位、不能抬高**
            #（CHANGE-004 的明文约定："闸门只能压低，不能抬高"），
            # 传更大的值会被 `min()` 吃掉、毫无效果。**能抬高水位的只有 `window_capacity`。**
            #
            # 取值依据：水位要落在 **1572（1 轮）与 ~12360（30 轮）之间**，且离两端都远。
            #   12000 - min(32768, 12000//4=3000) = 9000 → 再被 max_budget 6000 压低 = **6000**
            builder = self._builder(tmp, window_capacity=12000, max_budget_tokens=6000)
            self.assertIsNone(builder.plan(history(1)), "没到水位线却报了要压缩")
            plan = builder.plan(history(30))
            self.assertIsNotNone(plan, "到了水位线却没报要压缩（钩子就永远不会触发）")
            assert plan is not None
            self.assertGreater(plan.tokens_before, 0)
            self.assertGreater(plan.message_count_before, 0)
        finally:
            remove_temp_dir(tmp)

    async def test_t07_kernel_publishes_started_before_finished(self) -> None:
        """事件顺序：`CompactionStarted` 必须在 `CompactionFinished` **之前**。"""

        class _FakeBuilder:
            def plan(self, history, *, last_usage=None):  # noqa: ANN001, ANN202
                return CompactionPlan(tokens_before=1234, message_count_before=42)

            def build(self, history, *, last_usage=None):  # noqa: ANN001, ANN202
                return ContextBundle(
                    system="sys",
                    messages=[Message(role="user", blocks=[TextBlock(text="hi")])],
                    token_estimate=10,
                    compaction=CompactionReport(
                        tokens_before=1234, tokens_after=10,
                        message_count_before=42, message_count_after=1,
                    ),
                )

        bus = EventBus(session_id="f55")
        order: list[str] = []

        async def _on_started(event: ev.CompactionStarted) -> None:
            order.append("started")
            assert event.tokens_before == 1234
            assert event.message_count_before == 42

        async def _on_finished(event: ev.CompactionFinished) -> None:  # noqa: ARG001
            order.append("finished")

        bus.subscribe(ev.CompactionStarted, _on_started, name="probe-started")
        bus.subscribe(ev.CompactionFinished, _on_finished, name="probe-finished")

        class _NoProvider:
            async def stream(self, request):  # noqa: ANN001, ANN202
                if False:  # pragma: no cover
                    yield None

        from logox.kernel.registry import ToolRegistry

        kernel = KernelLoop(bus, _NoProvider(), ToolRegistry(), _FakeBuilder(), model="m")  # type: ignore[arg-type]
        await kernel._build_context(Turn(0))
        await asyncio.sleep(0)

        self.assertEqual(order, ["started", "finished"], "顺序错了 —— 钩子就失去'压缩前'的意义")

    async def test_t08_builder_without_plan_is_still_supported(self) -> None:
        """不实现 `plan()` 的 ContextBuilder（测试脚手架）必须照旧工作。"""

        class _MinimalBuilder:
            def build(self, history, *, last_usage=None):  # noqa: ANN001, ANN202
                return ContextBundle(system="s", messages=[], token_estimate=1)

        bus = EventBus(session_id="f55b")
        seen: list[str] = []

        async def _on_started(event: ev.CompactionStarted) -> None:  # noqa: ARG001
            seen.append("started")

        bus.subscribe(ev.CompactionStarted, _on_started, name="probe")

        class _NoProvider:
            async def stream(self, request):  # noqa: ANN001, ANN202
                if False:  # pragma: no cover
                    yield None

        from logox.kernel.registry import ToolRegistry

        kernel = KernelLoop(bus, _NoProvider(), ToolRegistry(), _MinimalBuilder(), model="m")  # type: ignore[arg-type]
        await kernel._build_context(Turn(0))
        await asyncio.sleep(0)

        self.assertEqual(seen, [], "没有 plan() 就不该偷偷发 Started")


class ApplyCompactTests(unittest.IsolatedAsyncioTestCase):
    """D161：`/compact` 手动压缩 —— 走装配根，事件与状态栏一起更新。"""

    def setUp(self) -> None:
        self.root = make_temp_dir("manual-compact-")
        self.addCleanup(remove_temp_dir, self.root)
        self.paths = LogoxPaths.at(self.root)

    def _runtime(self):
        config = LogoxConfig(
            provider=ProviderConfig(name="deepseek", model="deepseek-flash"),
            context=ContextConfig(reserve_tokens=1000, low_watermark_ratio=0.5),
        )
        return build_runtime(SimpleNamespace(config=config, sources=[]), self.root, self.paths)

    async def test_t09_apply_compact_publishes_events_and_updates_metrics(self) -> None:
        runtime = self._runtime()
        events: list[str] = []

        async def _on_started(event: ev.CompactionStarted) -> None:  # noqa: ARG001
            events.append("started")

        async def _on_finished(event: ev.CompactionFinished) -> None:  # noqa: ARG001
            events.append("finished")

        runtime.bus.subscribe(ev.CompactionStarted, _on_started, name="probe-started")
        runtime.bus.subscribe(ev.CompactionFinished, _on_finished, name="probe-finished")

        runtime.kernel.history.extend(history(30, chars=400))

        report = await runtime.apply_compact()
        await asyncio.sleep(0)

        self.assertIsNotNone(report, "手动压缩没有产生报告")
        assert report is not None
        self.assertGreater(report.tokens_before, report.tokens_after)
        self.assertEqual(events, ["started", "finished"], "手动压缩的事件顺序/发布不对")
        self.assertEqual(runtime.reducer.metrics.context_tokens, report.tokens_after)

    async def test_t10_apply_compact_is_a_noop_when_nothing_to_do(self) -> None:
        runtime = self._runtime()
        runtime.kernel.history.extend(history(1))
        self.assertIsNone(await runtime.apply_compact(), "短历史不该产生压缩报告")


class FoldInvalidatesAnchorTests(unittest.TestCase):
    """D161：**任何折叠**都必须作废锚点（它重写了历史前缀）。"""

    def test_t11_a_fold_drops_the_anchor(self) -> None:
        tmp = make_temp_dir("fold-anchor-")
        try:
            builder = HierarchicalContextBuilder(
                system="sys",
                cwd=tmp,
                transcript_writer=SessionTranscriptWriter(base_dir=tmp / "s", session_id="f"),
                # ⚠️ 这里**不能**沿用模块级的 `WINDOW`（它带 `max_budget_tokens=1500`）——
                #    那会把水位线压到 1500，第一次 build 就折叠了，反而造不出
                #    「先建好锚点、再折叠」这个待验证的时序。
                window_capacity=200_000,
                reserve_tokens=4_000,
                keep_recent_turns=2,
            )
            long_history = history(30)
            builder.build(long_history)
            builder.build(
                long_history,
                last_usage=ev.Usage(input_tokens=100, output_tokens=1, context_tokens=100),
            )
            self.assertIsNotNone(builder.ledger.anchor)
            generation_before = builder.ledger.generation

            builder.force_compact(long_history)

            self.assertGreater(builder.ledger.generation, generation_before, "折叠没推进代际")
            self.assertIsNone(
                builder.ledger.anchor, "折叠之后锚点仍指向不存在的列表（会悄悄算错）"
            )
        finally:
            remove_temp_dir(tmp)


if __name__ == "__main__":
    unittest.main()
