"""事件定义与事件总线的单元测试（MODULE_kernel_events.md §8 的 T-01 – T-26）。

用标准库 ``unittest`` 编写（D45）：在没有 pytest 的离线环境下也能运行；
异步用例使用 ``unittest.IsolatedAsyncioTestCase``，因此不需要 pytest-asyncio。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import unittest

from pydantic import ValidationError

from logox.errors import BusClosedError, SessionMismatchError, UnknownEventError
from logox.kernel import events as ev
from logox.kernel.bus import (
    PRIORITY_BUILTIN,
    PRIORITY_CORE,
    PRIORITY_HOOK,
    PRIORITY_PLUGIN,
    EventBus,
)
from tests.unit.kernel_samples import SESSION, all_event_samples

# 测试期间静音总线自身的诊断日志；需要断言日志的用例自行 assertLogs。
logging.getLogger("logox.kernel.bus").setLevel(logging.CRITICAL)


def delta(**kwargs: object) -> ev.ModelDelta:
    payload = {"session_id": SESSION, "kind": "text", "delta": "x", "request_index": 0}
    payload.update(kwargs)
    return ev.ModelDelta(**payload)  # type: ignore[arg-type]


class EventModelTests(unittest.TestCase):
    """事件模型本身的契约。"""

    def test_t11_event_is_frozen(self) -> None:
        """T-11：事件不可变——订阅者不得篡改事件对象。"""
        event = delta()
        with self.assertRaises(ValidationError):
            event.delta = "changed"  # type: ignore[misc]

    def test_t15_unknown_type_raises(self) -> None:
        """T-15：反序列化未知事件类型必须明确报错，不静默跳过。"""
        with self.assertRaises(UnknownEventError) as ctx:
            ev.parse_event('{"type": "not_a_real_event", "session_id": "s"}')
        self.assertIn("not_a_real_event", str(ctx.exception))

    def test_t16_json_roundtrip_for_every_event_type(self) -> None:
        """T-16：全部 22 个事件类型 JSON 往返无损。"""
        samples = all_event_samples()
        self.assertEqual(len(samples), 22, "事件类型数量应与 MODULE_kernel_events §4.3 一致")
        for sample in samples:
            with self.subTest(event=sample.type):
                restored = ev.parse_event(ev.dump_event(sample))
                self.assertEqual(restored, sample)

    def test_t17_discriminated_union_parses_all_types(self) -> None:
        """T-17：判别联合能把每个 JSON 解析回正确的子类。"""
        for sample in all_event_samples():
            with self.subTest(event=sample.type):
                restored = ev.parse_event(json.loads(ev.dump_event(sample)))
                self.assertIs(type(restored), type(sample))
                self.assertIn(sample.type, ev.EVENT_TYPES)

    def test_t19_cached_tokens_none_means_not_reported(self) -> None:
        """T-19：未上报缓存 → ``cache_hit_ratio`` 为 ``None``（UI 整项不显示）。"""
        usage = ev.Usage(input_tokens=100, output_tokens=10)
        self.assertIsNone(usage.cache_hit_ratio)

    def test_t20_cached_tokens_zero_means_real_zero_hit(self) -> None:
        """T-20：上报了且为零 → ``0.0``（UI 显示 ``cache 0%``），与 None 语义不同。"""
        usage = ev.Usage(input_tokens=100, output_tokens=10, cached_input_tokens=0)
        self.assertEqual(usage.cache_hit_ratio, 0.0)

    def test_t21_throughput_depends_only_on_duration_ms(self) -> None:
        """T-21：``tok/s`` 只依赖 ``duration_ms``，不受墙钟 ``ts`` 影响（E-21）。"""
        usage = ev.Usage(input_tokens=10, output_tokens=1000)
        finished = ev.ModelRequestFinished(
            session_id=SESSION, usage=usage, duration_ms=2000, first_token_ms=0, ts=1.0
        )
        skewed = ev.ModelRequestFinished(
            session_id=SESSION, usage=usage, duration_ms=2000, first_token_ms=0, ts=999_999.0
        )
        self.assertAlmostEqual(finished.tokens_per_second or 0, 500.0, places=6)
        self.assertEqual(finished.tokens_per_second, skewed.tokens_per_second)

    def test_generation_ms_excludes_first_token_wait(self) -> None:
        """D39：``tok/s`` 只算生成阶段，不含首字等待。"""
        finished = ev.ModelRequestFinished(
            session_id=SESSION,
            usage=ev.Usage(input_tokens=1, output_tokens=1),
            duration_ms=1000,
            first_token_ms=400,
        )
        self.assertEqual(finished.generation_ms, 600)

    def test_empty_output_has_no_throughput(self) -> None:
        finished = ev.ModelRequestFinished(
            session_id=SESSION,
            usage=ev.Usage(input_tokens=1, output_tokens=0),
            duration_ms=100,
        )
        self.assertIsNone(finished.tokens_per_second)

    def test_extra_field_is_rejected(self) -> None:
        """``extra="forbid"``：事件构造时的拼写错误当场暴露。"""
        with self.assertRaises(ValidationError):
            ev.ModelDelta(session_id=SESSION, kind="text", delta="x", request_index=0, typo=1)  # type: ignore[call-arg]


class BusBasicTests(unittest.IsolatedAsyncioTestCase):
    """总线基本行为。"""

    async def asyncSetUp(self) -> None:
        self.bus = EventBus(session_id=SESSION)

    async def asyncTearDown(self) -> None:
        await self.bus.aclose()

    async def test_t01_publish_without_subscribers(self) -> None:
        """T-01：没有订阅者不是错误。"""
        report = await self.bus.publish(delta())
        self.assertEqual(report.delivered, 0)
        self.assertEqual(report.failed, 0)

    async def test_t02_priority_order(self) -> None:
        """T-02：按 priority 升序调用。"""
        calls: list[str] = []

        async def make(tag: str):  # noqa: ANN202
            async def handler(event: ev.Event) -> None:
                calls.append(tag)

            return handler

        await self._subscribe("plugin", await make("plugin"), PRIORITY_PLUGIN)
        await self._subscribe("hook", await make("hook"), PRIORITY_HOOK)
        await self._subscribe("core", await make("core"), PRIORITY_CORE)
        await self._subscribe("builtin", await make("builtin"), PRIORITY_BUILTIN)

        await self.bus.publish(delta())
        self.assertEqual(calls, ["core", "builtin", "plugin", "hook"])

    async def test_t03_one_failure_does_not_affect_others(self) -> None:
        """T-03：异常隔离——中间订阅者抛错，首尾仍被调用。"""
        calls: list[str] = []

        async def first(event: ev.Event) -> None:
            calls.append("first")

        async def boom(event: ev.Event) -> None:
            raise RuntimeError("订阅者故障")

        async def last(event: ev.Event) -> None:
            calls.append("last")

        await self._subscribe("first", first, 10)
        await self._subscribe("boom", boom, 20)
        await self._subscribe("last", last, 30)

        report = await self.bus.publish(delta())
        self.assertEqual(calls, ["first", "last"])
        self.assertEqual(report.failed, 1)
        self.assertEqual(report.delivered, 2)

    async def test_t22_sync_handler_rejected_at_subscribe(self) -> None:
        """T-22：同步函数订阅必须在订阅时报错，而不是静默不调用。"""

        def sync_handler(event: ev.Event) -> None:  # pragma: no cover - 不应被调用
            pass

        with self.assertRaises(TypeError) as ctx:
            self.bus.subscribe(ev.ModelDelta, sync_handler, name="sync")  # type: ignore[arg-type]
        self.assertIn("async", str(ctx.exception))

    async def test_t18_subscribe_unsubscribe_idempotent(self) -> None:
        """T-18：重复取消订阅幂等。"""
        calls: list[int] = []

        async def handler(event: ev.Event) -> None:
            calls.append(1)

        subscription = await self._subscribe("h", handler)
        self.bus.unsubscribe(subscription)
        self.bus.unsubscribe(subscription)
        await self.bus.publish(delta())
        self.assertEqual(calls, [])

    async def test_subscribe_all_events_with_base_class(self) -> None:
        """订阅基类 ``Event`` 即订阅全部事件。"""
        seen: list[str] = []

        async def handler(event: ev.Event) -> None:
            seen.append(event.type)

        self.bus.subscribe(ev.Event, handler, name="catch-all")
        await self.bus.publish(delta())
        await self.bus.publish(ev.QueueChanged(session_id=SESSION, depth=1, action="enqueued"))
        self.assertEqual(seen, ["model_delta", "queue_changed"])

    # ------------------------------------------------------------------ #
    # 隔离（quarantine）
    # ------------------------------------------------------------------ #

    async def test_t04_quarantine_after_three_failures(self) -> None:
        """T-04：连续失败 3 次被隔离，并发布 SubscriberQuarantined（K3 可见降级）。"""
        attempts: list[int] = []
        quarantine_notices: list[ev.SubscriberQuarantined] = []

        async def failing(event: ev.Event) -> None:
            attempts.append(1)
            raise RuntimeError("总是失败")

        async def watcher(event: ev.Event) -> None:
            quarantine_notices.append(event)  # type: ignore[arg-type]

        await self._subscribe("failing", failing)
        self.bus.subscribe(ev.SubscriberQuarantined, watcher, name="watcher")

        for _ in range(3):
            await self.bus.publish(delta())

        self.assertEqual(len(attempts), 3, "第 3 次失败后才隔离")
        self.assertEqual(len(quarantine_notices), 1)
        self.assertEqual(quarantine_notices[0].subscriber, "failing")
        self.assertEqual(quarantine_notices[0].failures, 3)

        report = await self.bus.publish(delta())
        self.assertEqual(len(attempts), 3, "隔离后不再调用")
        self.assertEqual(report.skipped, 1)

    async def test_t05_success_resets_failure_counter(self) -> None:
        """T-05：一次成功即归零，偶发失败不会累积成误杀。"""
        outcomes = iter([False, False, True, False, False])

        async def flaky(event: ev.Event) -> None:
            if not next(outcomes):
                raise RuntimeError("偶发失败")

        await self._subscribe("flaky", flaky)
        for _ in range(5):
            await self.bus.publish(delta())

        self.assertEqual(self.bus.quarantined(), [])

    async def test_t06_cancelled_error_propagates(self) -> None:
        """T-06 / E-5：``CancelledError`` 必须原样传播，且不计入失败次数。

        这条守的是**「按 Esc 必须有效」**——被吞掉就会静默失效。
        """
        subscription_holder: list[object] = []

        async def cancelling(event: ev.Event) -> None:
            raise asyncio.CancelledError

        subscription = await self._subscribe("cancelling", cancelling)
        subscription_holder.append(subscription)

        with self.assertRaises(asyncio.CancelledError):
            await self.bus.publish(delta())

        self.assertEqual(subscription.failures, 0, "取消不是失败")
        self.assertFalse(subscription.quarantined)

    # ------------------------------------------------------------------ #
    # 重入保护
    # ------------------------------------------------------------------ #

    async def test_t07_nested_publish_depth_limit(self) -> None:
        """T-07 / E-7：嵌套深度超限被丢弃，并发 ErrorOccurred 告警。"""
        depth_calls: list[int] = []
        errors: list[ev.ErrorOccurred] = []

        async def watcher(event: ev.Event) -> None:
            errors.append(event)  # type: ignore[arg-type]

        async def recursive(event: ev.Event) -> None:
            depth_calls.append(1)
            await self.bus.publish(delta())

        self.bus.subscribe(ev.ErrorOccurred, watcher, name="errors")
        await self._subscribe("recursive", recursive)

        await self.bus.publish(delta())

        self.assertEqual(len(depth_calls), 5, "max_depth=5：第 6 层被丢弃")
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].category, "bus_recursion")

    async def test_t08_mutual_publish_terminates(self) -> None:
        """T-08 / E-8：两个订阅者互相发布形成循环时必须在深度上限处终止。"""

        async def on_delta(event: ev.Event) -> None:
            await self.bus.publish(ev.QueueChanged(session_id=SESSION, depth=1, action="enqueued"))

        async def on_queue(event: ev.Event) -> None:
            await self.bus.publish(delta())

        self.bus.subscribe(ev.ModelDelta, on_delta, name="on-delta")
        self.bus.subscribe(ev.QueueChanged, on_queue, name="on-queue")

        # 若深度保护失效，这里会抛 RecursionError 或挂死。
        await asyncio.wait_for(self.bus.publish(delta()), timeout=5.0)

    # ------------------------------------------------------------------ #
    # 非阻塞通道
    # ------------------------------------------------------------------ #

    async def test_t09_nonblocking_handler_does_not_block(self) -> None:
        """T-09：非阻塞订阅者不拖慢 publish；drain() 后其已执行。"""
        done: list[int] = []

        async def slow(event: ev.Event) -> None:
            await asyncio.sleep(0.05)
            done.append(1)

        self.bus.subscribe(ev.ModelDelta, slow, name="slow", blocking=False)
        await self.bus.publish(delta())
        self.assertEqual(done, [], "publish 不应等待非阻塞订阅者")

        await self.bus.drain()
        self.assertEqual(len(done), 1)

    async def test_t10_queue_full_drops_oldest_and_counts(self) -> None:
        """T-10 / K2：队列写满时丢弃最旧的并计数，publish 本身不阻塞。"""
        bus = EventBus(session_id=SESSION, queue_size=2)
        started = asyncio.Event()

        async def blocked(event: ev.Event) -> None:
            started.set()
            await asyncio.sleep(0.2)

        bus.subscribe(ev.ModelDelta, blocked, name="blocked", blocking=False)
        try:
            for _ in range(6):
                await bus.publish(delta())
            self.assertGreaterEqual(bus.dropped_queue, 1, "应有事件因队列满被丢弃")
        finally:
            await bus.aclose()

    async def test_t24_consumer_restarts_after_death(self) -> None:
        """T-24 / E-19：消费者任务意外死亡后自动重建，投递不会永久失效。"""
        done: list[int] = []

        async def handler(event: ev.Event) -> None:
            done.append(1)

        self.bus.subscribe(ev.ModelDelta, handler, name="sink", blocking=False)
        await self.bus.publish(delta())
        await self.bus.drain()

        consumer = self.bus._consumer  # noqa: SLF001 - 测试需要模拟任务猝死
        assert consumer is not None
        consumer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await consumer

        await self.bus.publish(delta())
        await self.bus.drain()
        self.assertGreaterEqual(len(done), 2, "重建后的消费者应继续投递")

    # ------------------------------------------------------------------ #
    # 版本兼容、会话边界、生命周期
    # ------------------------------------------------------------------ #

    async def test_t14_incompatible_api_version_skipped_once(self) -> None:
        """T-14 / E-12：版本不兼容的订阅者被跳过，warning 只记一次。"""
        calls: list[int] = []

        async def handler(event: ev.Event) -> None:
            calls.append(1)

        self.bus.subscribe(ev.ModelDelta, handler, name="old-plugin", requires_api=">=2,<3")

        with self.assertLogs("logox.kernel.bus", level="WARNING") as captured:
            report = await self.bus.publish(delta())
            await self.bus.publish(delta())

        self.assertEqual(calls, [])
        self.assertEqual(report.skipped, 1)
        warnings = [line for line in captured.output if "old-plugin" in line]
        self.assertEqual(len(warnings), 1, "不兼容提醒不得刷屏")

    async def test_compatible_api_version_receives(self) -> None:
        calls: list[int] = []

        async def handler(event: ev.Event) -> None:
            calls.append(1)

        self.bus.subscribe(ev.ModelDelta, handler, name="ok-plugin", requires_api=">=1,<2")
        await self.bus.publish(delta())
        self.assertEqual(calls, [1])

    async def test_t12_session_mismatch_raises(self) -> None:
        """T-12 / E-10：跨会话事件必须被拦下（``/resume`` 场景的护栏）。"""
        with self.assertRaises(SessionMismatchError) as ctx:
            await self.bus.publish(delta(session_id="other-session"))
        self.assertIn("other-session", str(ctx.exception))

    async def test_t13_publish_after_close_raises(self) -> None:
        """T-13 / E-11：关闭后发布必须明确报错，不静默丢弃。"""
        await self.bus.aclose()
        with self.assertRaises(BusClosedError):
            await self.bus.publish(delta())

    async def test_t25_drain_after_close_returns_immediately(self) -> None:
        """T-25 / E-18：关闭后 drain 立即返回，不得挂死。"""
        await self.bus.aclose()
        await asyncio.wait_for(self.bus.drain(), timeout=1.0)

    async def test_reset_reopens_bus_for_new_session(self) -> None:
        """``reset()`` 支持复用总线对象开启新会话（``/resume``）。"""
        await self.bus.aclose()
        self.bus.reset("s-0002")
        calls: list[str] = []

        async def handler(event: ev.Event) -> None:
            calls.append(event.session_id)

        self.bus.subscribe(ev.ModelDelta, handler, name="h")
        await self.bus.publish(delta(session_id="s-0002"))
        self.assertEqual(calls, ["s-0002"])

    async def test_t23_high_frequency_order_preserved(self) -> None:
        """T-23：1000 条高频事件顺序严格保持。"""
        seen: list[str] = []

        async def handler(event: ev.Event) -> None:
            seen.append(event.delta)  # type: ignore[attr-defined]

        await self._subscribe("sink", handler)
        for index in range(1000):
            await self.bus.publish(delta(delta=str(index)))

        self.assertEqual(len(seen), 1000)
        self.assertEqual(seen, [str(index) for index in range(1000)])

    # ------------------------------------------------------------------ #
    # 辅助
    # ------------------------------------------------------------------ #

    async def _subscribe(self, name: str, handler, priority: int = PRIORITY_BUILTIN):  # noqa: ANN001, ANN202
        return self.bus.subscribe(ev.ModelDelta, handler, name=name, priority=priority)


class DispatchReportTests(unittest.TestCase):
    def test_report_is_frozen(self) -> None:
        from logox.kernel.bus import DispatchReport

        report = DispatchReport()
        with self.assertRaises(ValidationError):
            report.delivered = 5  # type: ignore[misc]


class VersionSpecTests(unittest.TestCase):
    def test_version_spec_parsing(self) -> None:
        from logox.kernel.bus import _version_satisfies

        self.assertTrue(_version_satisfies(1, ">=1,<2"))
        self.assertFalse(_version_satisfies(2, ">=1,<2"))
        self.assertTrue(_version_satisfies(3, ">=1"))
        self.assertTrue(_version_satisfies(1, "==1"))
        self.assertFalse(_version_satisfies(1, "!=1"))
        self.assertFalse(_version_satisfies(1, "not-a-spec"))


if __name__ == "__main__":
    unittest.main()
