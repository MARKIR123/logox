"""会话度量归约的测试（MODULE_tui.md §8 的 ``test_metrics.py``）。

重点是 D39 的三条口径：``None`` vs ``0``、``tok/s`` 只算生成阶段、
以及用量不被重复累加。
"""

from __future__ import annotations

import unittest

from logox.kernel import events as ev
from logox.tui.metrics import MetricsReducer

SESSION = "s-tui"


def usage(**kwargs: object) -> ev.Usage:
    payload = {"input_tokens": 100, "output_tokens": 50}
    payload.update(kwargs)
    return ev.Usage(**payload)  # type: ignore[arg-type]


class MetricsTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.reducer = MetricsReducer(context_window=1000)

    async def feed(self, *events: ev.Event) -> None:
        for event in events:
            await self.reducer.handle(event)

    def delta(self, **kwargs: object) -> ev.ModelDelta:
        payload = {"session_id": SESSION, "kind": "text", "delta": "x", "request_index": 0}
        payload.update(kwargs)
        return ev.ModelDelta(**payload)  # type: ignore[arg-type]


class CacheRatioTests(MetricsTestCase):
    async def test_not_reported_stays_none(self) -> None:
        """厂商未上报 → ``cache_ratio`` 为 ``None`` → UI 整项不显示。"""
        await self.feed(
            ev.ModelRequestFinished(session_id=SESSION, usage=usage(), duration_ms=1000)
        )
        self.assertIsNone(self.reducer.metrics.cache_ratio)
        self.assertIsNone(self.reducer.metrics.cached_input)

    async def test_reported_zero_shows_zero(self) -> None:
        """上报了且为零 → ``0.0`` → UI 显示 ``cache 0%``。"""
        await self.feed(
            ev.ModelRequestFinished(
                session_id=SESSION, usage=usage(cached_input_tokens=0), duration_ms=1000
            )
        )
        self.assertEqual(self.reducer.metrics.cache_ratio, 0.0)

    async def test_ratio_computed_and_accumulated(self) -> None:
        await self.feed(
            ev.ModelRequestFinished(
                session_id=SESSION, usage=usage(cached_input_tokens=40), duration_ms=100
            ),
            ev.ModelRequestFinished(
                session_id=SESSION, usage=usage(cached_input_tokens=60), duration_ms=100
            ),
        )
        metrics = self.reducer.metrics
        self.assertEqual(metrics.cached_input, 100)
        self.assertEqual(metrics.usage_input, 200)
        self.assertAlmostEqual(metrics.cache_ratio or 0, 0.5)

    async def test_none_after_zero_does_not_reset_to_none(self) -> None:
        """已上报过的命中数不应被后续未上报的响应抹掉。"""
        await self.feed(
            ev.ModelRequestFinished(
                session_id=SESSION, usage=usage(cached_input_tokens=30), duration_ms=100
            ),
            ev.ModelRequestFinished(session_id=SESSION, usage=usage(), duration_ms=100),
        )
        self.assertEqual(self.reducer.metrics.cached_input, 30)


class ThroughputTests(MetricsTestCase):
    async def test_throughput_uses_duration_ms_not_ts(self) -> None:
        """K4：``tok/s`` 只依赖 ``duration_ms``；墙钟 ``ts`` 跳变不影响结果。"""
        await self.feed(
            ev.ModelRequestFinished(
                session_id=SESSION,
                usage=usage(output_tokens=1000),
                duration_ms=2000,
                first_token_ms=0,
                ts=1.0,
            )
        )
        first = self.reducer.metrics.throughput

        # 换一个全新的归约器，只把 ts 改成"跨了十年"，其余完全相同
        self.reducer = MetricsReducer(context_window=1000)
        await self.feed(
            ev.ModelRequestFinished(
                session_id=SESSION,
                usage=usage(output_tokens=1000),
                duration_ms=2000,
                first_token_ms=0,
                ts=999_999.0,
            )
        )
        self.assertEqual(first, self.reducer.metrics.throughput)
        self.assertAlmostEqual(first or 0, 500.0, places=6)

    async def test_throughput_excludes_first_token_wait(self) -> None:
        """D39：只算生成阶段——首字等待的 400ms 不计入。"""
        await self.feed(
            ev.ModelRequestFinished(
                session_id=SESSION,
                usage=usage(output_tokens=600),
                duration_ms=1000,
                first_token_ms=400,
            )
        )
        self.assertAlmostEqual(self.reducer.metrics.throughput or 0, 1000.0, places=6)

    async def test_no_output_tokens_yields_none(self) -> None:
        await self.feed(
            ev.ModelRequestFinished(
                session_id=SESSION, usage=usage(output_tokens=0), duration_ms=100
            )
        )
        self.assertIsNone(self.reducer.metrics.throughput)


class UsageAccountingTests(MetricsTestCase):
    async def test_turn_usage_is_not_double_counted(self) -> None:
        """``TurnFinished.usage`` 是回合合计，累加它会使用量翻倍。"""
        await self.feed(
            ev.ModelRequestFinished(session_id=SESSION, usage=usage(), duration_ms=100),
            ev.TurnFinished(
                session_id=SESSION, turn_index=1, duration_ms=500, tool_call_count=0, usage=usage()
            ),
        )
        metrics = self.reducer.metrics
        self.assertEqual(metrics.usage_input, 100, "只应累加一次")
        self.assertEqual(metrics.usage_output, 50)

    async def test_cost_accumulates(self) -> None:
        await self.feed(
            ev.ModelRequestFinished(session_id=SESSION, usage=usage(), duration_ms=1, cost_usd=0.001),
            ev.ModelRequestFinished(session_id=SESSION, usage=usage(), duration_ms=1, cost_usd=0.002),
        )
        self.assertAlmostEqual(self.reducer.metrics.cost_usd, 0.003)


class TurnAndTimingTests(MetricsTestCase):
    async def test_turn_starts_at_one_and_increments(self) -> None:
        self.assertEqual(self.reducer.metrics.turn, 1)
        await self.feed(
            ev.TurnFinished(
                session_id=SESSION, turn_index=1, duration_ms=1000, tool_call_count=0, usage=usage()
            )
        )
        self.assertEqual(self.reducer.metrics.turn, 2)  # 已完成 1 回合 → 当前是第 2 回合

    async def test_timings_accumulate_from_duration_ms(self) -> None:
        await self.feed(
            ev.ModelRequestFinished(session_id=SESSION, usage=usage(), duration_ms=700),
            ev.ToolCallStarted(session_id=SESSION, call_id="c1"),
            ev.ToolCallFinished(session_id=SESSION, call_id="c1", ok=True, duration_ms=300),
            ev.TurnFinished(
                session_id=SESSION, turn_index=1, duration_ms=1100, tool_call_count=1, usage=usage()
            ),
        )
        metrics = self.reducer.metrics
        self.assertEqual(metrics.llm_ms, 700)
        self.assertEqual(metrics.tool_ms, 300)
        self.assertEqual(metrics.total_ms, 1100)


class RunningStateTests(MetricsTestCase):
    async def test_running_tool_name_resolved_from_requested_event(self) -> None:
        """``ToolCallStarted`` 只带 call_id，名称必须回查 ``ToolCallRequested``。"""
        await self.feed(
            ev.ToolCallRequested(session_id=SESSION, call_id="c1", name="shell"),
            ev.ToolCallStarted(session_id=SESSION, call_id="c1"),
        )
        self.assertEqual(self.reducer.metrics.running_tool, "shell")
        self.assertTrue(self.reducer.metrics.busy)

        await self.feed(ev.ToolCallFinished(session_id=SESSION, call_id="c1", ok=True, duration_ms=10))
        self.assertIsNone(self.reducer.metrics.running_tool)
        self.assertFalse(self.reducer.metrics.busy)

    async def test_generating_flag_follows_request_lifecycle(self) -> None:
        await self.feed(ev.ModelRequestStarted(session_id=SESSION, provider="p", model="m", token_estimate=1, request_index=0))
        self.assertTrue(self.reducer.metrics.busy)
        await self.feed(ev.ModelRequestFinished(session_id=SESSION, usage=usage(), duration_ms=10))
        self.assertFalse(self.reducer.metrics.generating)

    async def test_pending_permissions_counted(self) -> None:
        await self.feed(
            ev.PermissionRequested(session_id=SESSION, call_id="c1", prompt="?"),
            ev.PermissionRequested(session_id=SESSION, call_id="c2", prompt="?"),
        )
        self.assertEqual(self.reducer.metrics.pending_permissions, 2)
        await self.feed(ev.PermissionResolved(session_id=SESSION, call_id="c1", decision="allow"))
        self.assertEqual(self.reducer.metrics.pending_permissions, 1)

    async def test_permission_count_never_negative(self) -> None:
        await self.feed(ev.PermissionResolved(session_id=SESSION, call_id="cx", decision="deny"))
        self.assertEqual(self.reducer.metrics.pending_permissions, 0)

    async def test_retry_state_set_and_cleared(self) -> None:
        await self.feed(ev.RetryScheduled(session_id=SESSION, attempt=2, delay_s=4.0, reason="429"))
        retry = self.reducer.metrics.retry
        self.assertIsNotNone(retry)
        assert retry is not None
        self.assertEqual(retry.attempt, 2)

        await self.feed(ev.ModelRequestStarted(session_id=SESSION, provider="p", model="m", token_estimate=1, request_index=0))
        self.assertIsNone(self.reducer.metrics.retry)


class ContextAndMiscTests(MetricsTestCase):
    async def test_context_ratio_from_context_built(self) -> None:
        await self.feed(ev.ContextBuilt(session_id=SESSION, message_count=5, token_estimate=750))
        metrics = self.reducer.metrics
        self.assertEqual(metrics.context_tokens, 750)
        self.assertAlmostEqual(metrics.context_ratio, 0.75)

    async def test_context_ratio_clamped_to_one(self) -> None:
        await self.feed(ev.ContextBuilt(session_id=SESSION, message_count=5, token_estimate=99999))
        self.assertEqual(self.reducer.metrics.context_ratio, 1.0)

    async def test_compaction_updates_context_and_count(self) -> None:
        await self.feed(
            ev.ContextBuilt(session_id=SESSION, message_count=20, token_estimate=900),
            ev.CompactionFinished(session_id=SESSION, tokens_after=200, message_count_after=4),
        )
        metrics = self.reducer.metrics
        self.assertEqual(metrics.context_tokens, 200)
        self.assertEqual(metrics.compact_count, 1)

    async def test_queue_depth_tracked(self) -> None:
        await self.feed(ev.QueueChanged(session_id=SESSION, depth=3, action="enqueued"))
        self.assertEqual(self.reducer.metrics.queue_depth, 3)

    async def test_mcp_degraded_then_recovered(self) -> None:
        await self.feed(
            ev.McpServerStateChanged(session_id=SESSION, server="fs", state="ready", tool_count=3),
            ev.McpServerStateChanged(session_id=SESSION, server="mem", state="degraded", error="boom"),
        )
        self.assertEqual(self.reducer.metrics.degraded_mcp, ["mem"])
        await self.feed(ev.McpServerStateChanged(session_id=SESSION, server="mem", state="ready"))
        self.assertEqual(self.reducer.metrics.degraded_mcp, [])

    async def test_quarantined_subscribers_listed_once(self) -> None:
        await self.feed(
            ev.SubscriberQuarantined(session_id=SESSION, subscriber="telemetry", failures=3, last_error="x"),
            ev.SubscriberQuarantined(session_id=SESSION, subscriber="telemetry", failures=3, last_error="x"),
        )
        self.assertEqual(self.reducer.metrics.quarantined, ["telemetry"])

    async def test_session_start_resets_metrics(self) -> None:
        await self.feed(ev.ContextBuilt(session_id=SESSION, message_count=5, token_estimate=500))
        await self.feed(
            ev.SessionStart(
                session_id=SESSION,
                cwd="/w",
                provider="deepseek",
                model="deepseek-v4-pro",
                thinking_effort="high",
                shell_backend="gitbash",
                memory_sources=["~/.logox/LOGOX.md"],
            )
        )
        metrics = self.reducer.metrics
        self.assertEqual(metrics.context_tokens, 0, "新会话必须清零")
        self.assertEqual(metrics.model, "deepseek-v4-pro")
        self.assertEqual(metrics.thinking_effort, "high")
        self.assertEqual(metrics.memory_source_count, 1)

    async def test_session_start_sets_context_window(self) -> None:
        await self.feed(
            ev.SessionStart(
                session_id=SESSION,
                cwd="/w",
                provider="deepseek",
                model="deepseek-v4-pro",
                context_window=1_048_576,
            )
        )
        self.assertEqual(self.reducer.metrics.context_window, 1_048_576)

    async def test_request_finished_syncs_context_tokens(self) -> None:
        await self.feed(
            ev.ModelRequestFinished(
                session_id=SESSION,
                usage=usage(input_tokens=250, output_tokens=30),
                duration_ms=500,
            )
        )
        self.assertEqual(self.reducer.metrics.context_tokens, 250)


class EffortColourTests(unittest.TestCase):
    """D161：档位词按档位上色，且 `off` 也显示。

    要防的缺陷
    ----------
    这 5 个 `thinking_*` token 从 D79 起就存在，**却从未被任何代码读取** ——
    档位词一直写死用 `text_muted`。所以"改了主题里的 thinking_high 却没反应"
    是一个真实存在过的状态。这组用例就是钉住"它现在真的被读了"。
    """

    def _line(self, effort: str, *, width: int = 60) -> tuple[str, list]:
        from logox.config.schema import StatusItems, TimingFields
        from logox.tui.content.status import StatusContext, build_status_line
        from logox.tui.metrics import SessionMetrics
        from logox.tui.theme import load_theme

        metrics = SessionMetrics(
            model="m",
            thinking_effort=effort,
            context_tokens=10,
            context_window=1000,
            cached_input=0,
            usage_input=10,
            usage_output=1,
        )
        ctx = StatusContext(
            palette=load_theme().palette,
            width=width,
            items=StatusItems(model=True),
            timing_fields=TimingFields(),
        )
        text = build_status_line(metrics, ctx)
        return text.plain, text.spans

    @staticmethod
    def _style_of(spans: list, plain: str, needle: str) -> str:
        """取 `needle` 那一段文字**自己**的样式。

        ⚠️ 不能写成"spans 里有任何一个等于期望色就算过" —— 那是**弱断言**：
        实测第一版就是这么写的，而 `low` 那一项在被破坏时**假通过**了，
        因为**模型名用的是 `accent`，与 `thinking_low` 恰好同色**。
        "别的地方也有这个颜色"不等于"这段文字是这个颜色"。
        """
        start = plain.index(needle)
        for span in spans:
            if span.start <= start < span.end:
                return str(span.style)
        raise AssertionError(f"找不到覆盖 {needle!r} 的 span（spans={spans}）")

    def test_every_level_uses_its_own_token(self) -> None:
        """5 个档位各取各的 token（`auto` 复用 `medium`，见 `EFFORT_TOKENS` 的说明）。"""
        from logox.tui.content.status import EFFORT_TOKENS
        from logox.tui.theme import load_theme

        palette = load_theme().palette
        for effort, token in EFFORT_TOKENS.items():
            with self.subTest(effort=effort):
                plain, spans = self._line(effort)
                expected = str(getattr(palette, token))
                actual = self._style_of(spans, plain, f"· {effort}")
                self.assertIn(
                    expected,
                    actual,
                    f"档位 {effort} 的样式应当是 {token}={expected}，实际 {actual!r}",
                )

    def test_auto_and_medium_share_a_colour_by_design(self) -> None:
        """`auto` 没有独立 token → 复用 `thinking_medium`。这是**刻意的**，不是漏配。

        档位有 5 个（off/low/medium/high/auto）而 token 只有 4 个。
        `auto` 的语义是"由模型自己决定"，视觉上落在中间档是诚实的。
        """
        from logox.tui.content.status import EFFORT_TOKENS

        self.assertEqual(EFFORT_TOKENS["auto"], EFFORT_TOKENS["medium"])
        self.assertEqual(EFFORT_TOKENS["off"], "thinking_off")

    def test_off_is_displayed_now(self) -> None:
        """**行为变更**：`off` 此前被 `!= "off"` 整个藏起来，现在必须显示。

        理由（D161 的裁定）：状态行的职责是**事实公示** ——
        "我把思考关掉了"是一个应该看得见的事实，而不是一个需要隐藏的默认值。
        """
        plain, _spans = self._line("off")
        self.assertIn("m · off", plain)

    def test_effort_colours_differ_from_each_other(self) -> None:
        """**细粒度是真的**：5 个档位不能退化成同一个颜色。

        若将来有人把它们统一指着 `text_muted`（旧行为），这条会红 ——
        而"颜色即档位"这个设计意图也就没了。
        """
        from logox.tui.content.status import EFFORT_TOKENS
        from logox.tui.theme import load_theme

        palette = load_theme().palette
        values = {token: str(getattr(palette, token)) for token in set(EFFORT_TOKENS.values())}
        self.assertEqual(
            len(set(values.values())),
            len(values),
            f"四个档位 token 必须是四个不同的颜色，实际 {values}",
        )


class StatusLineDisplayTests(unittest.TestCase):
    def test_status_line_shows_auto_effort_and_context_fraction(self) -> None:
        from logox.config.schema import StatusItems, TimingFields
        from logox.tui.content.status import StatusContext, build_status_line
        from logox.tui.metrics import SessionMetrics
        from logox.tui.theme import load_theme

        metrics = SessionMetrics(
            model="deepseek-flash",
            thinking_effort="auto",
            context_tokens=250,
            context_window=1_048_576,
            cached_input=0,
            usage_input=250,
            usage_output=30,
        )
        ctx = StatusContext(
            palette=load_theme().palette,
            width=100,
            items=StatusItems(),
            timing_fields=TimingFields(),
        )
        rendered = build_status_line(metrics, ctx).plain
        self.assertIn("deepseek-flash · auto", rendered)
        self.assertIn("ctx 250/1.0M (0%)", rendered)
        self.assertIn("cache 0%", rendered)


class RobustnessTests(MetricsTestCase):
    async def test_every_event_type_is_handled_without_error(self) -> None:
        """归约器必须能安全吞下**全部 22 种事件**（未关心的类型静默忽略）。"""
        from tests.unit.kernel_samples import all_event_samples

        for sample in all_event_samples():
            with self.subTest(event=sample.type):
                await self.reducer.handle(sample)

    async def test_unknown_events_do_not_corrupt_state(self) -> None:
        before = self.reducer.metrics.model_copy(deep=True)
        await self.feed(ev.SessionEnd(session_id=SESSION, reason="user_quit", duration_ms=10))
        self.assertEqual(self.reducer.metrics.turn, before.turn)


if __name__ == "__main__":
    unittest.main()
