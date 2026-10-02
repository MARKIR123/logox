"""Idle response timeout and visible, non-blocking Anamnesis lifecycle."""

from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from logox.anamnesis.local import make_local_runner
from logox.anamnesis.models import AnamesisAnalysisRecord, AnamesisEvent
from logox.anamnesis.runner import AnamesisRunner
from logox.anamnesis.sources import SourceCollector
from logox.config.schema import AnamesisConfig
from logox.kernel.bus import EventBus
from logox.providers.base import ChatRequest, DeltaEvent, StopEvent
from logox.tui.content.anamnesis import AnamesisCard
from logox.tui.render.app import InlineApp
from logox.tui.render.fullscreen import FullscreenApp
from logox.tui.render.terminal import FakeTerminal
from tests.anamnesis.test_runtime import TempCase


class ActivityTests(TempCase, unittest.IsolatedAsyncioTestCase):
    def runner(self, provider, timeout=0.1):
        return AnamesisRunner(
            provider, "local", 32768, SourceCollector(self.home / "sessions", self.project), timeout=timeout
        )

    async def test_continuous_reasoning_outlives_request_idle_timeout(self):
        class Provider:
            async def stream(self, request):
                for _ in range(7):
                    await asyncio.sleep(0.02)
                    yield DeltaEvent(kind="reasoning", text="核对")
                yield DeltaEvent(kind="text", text="{}")
                yield StopEvent(stop_reason="end_turn")

        runner = self.runner(Provider())
        self.assertEqual(await runner._generate(ChatRequest(model="local")), ("{}", []))
        self.assertIsNone(runner._response_deadline)

    async def test_silence_preserves_fragments_and_records_explicit_timeout(self):
        closed = asyncio.Event()

        class Provider:
            async def stream(self, request):
                try:
                    yield DeltaEvent(kind="reasoning", text="已开始核对")
                    await asyncio.Event().wait()
                finally:
                    closed.set()

        runner = self.runner(Provider(), timeout=0.03)
        events = []

        async def progress(event):
            events.append(event)

        runner.on_progress = progress
        with self.assertRaisesRegex(TimeoutError, "连续 0.03 秒未收到模型数据"):
            await runner._generate(ChatRequest(model="local"))
        self.assertTrue(closed.is_set())
        self.assertIsNone(runner._response_deadline)
        self.assertEqual(events[0]["delta"], "已开始核对")
        diagnostic = json.loads(events[-1]["result"])
        self.assertIn("未提交档案", diagnostic["failure_reason"])
        self.assertIsNone(diagnostic["stop_reason"])

    async def test_initial_silence_has_detailed_timeout_and_cancel_is_not_timeout(self):
        entered, closed = asyncio.Event(), asyncio.Event()

        class Provider:
            async def stream(self, request):
                try:
                    entered.set()
                    await asyncio.Event().wait()
                    yield StopEvent(stop_reason="end_turn")
                finally:
                    closed.set()

        runner = self.runner(Provider(), timeout=0.02)
        with self.assertRaisesRegex(TimeoutError, "未收到模型数据"):
            await runner._generate(ChatRequest(model="local"))
        self.assertTrue(closed.is_set())
        entered.clear()
        closed.clear()
        runner.timeout = 10
        task = asyncio.create_task(runner._generate(ChatRequest(model="local")))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(closed.is_set())
        self.assertIsNone(runner._response_deadline)

    async def test_raw_tool_fragments_extend_idle_deadline_before_completed_call(self):
        import httpx

        class Fragments(httpx.AsyncByteStream):
            async def __aiter__(self):
                for index, part in enumerate(['{"path":', '"src/', "example", '.py"', "}"]):
                    await asyncio.sleep(0.12)
                    call = {"index": 0, "function": {"arguments": part}}
                    if index == 0:
                        call.update(id="read1", type="function")
                        call["function"]["name"] = "read"
                    yield (
                        "data: " + json.dumps({"choices": [{"delta": {"tool_calls": [call]}}]}) + "\n\n"
                    ).encode()
                yield b'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}\n\ndata: [DONE]\n\n'

        registry = SimpleNamespace(
            spec=lambda _: SimpleNamespace(
                base_url="http://localhost:11434/v1", kind="openai_compat", context_window=32768
            )
        )
        real_client = httpx.AsyncClient
        with patch(
            "httpx.AsyncClient",
            side_effect=lambda **kwargs: real_client(
                transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=Fragments())), **kwargs
            ),
        ):
            runner = await make_local_runner(
                registry,
                AnamesisConfig(model="local", request_timeout_s=0.5),
                SourceCollector(self.home / "sessions", self.project),
            )
            _, calls = await runner._generate(ChatRequest(model="local"))
        self.assertEqual(calls[0].arguments, {"path": "src/example.py"})
        self.assertIsNone(runner._response_deadline)

    async def test_http_read_timeout_is_not_reported_as_blank_exception(self):
        import httpx

        def respond(request):
            raise httpx.ReadTimeout("", request=request)

        registry = SimpleNamespace(
            spec=lambda _: SimpleNamespace(
                base_url="http://localhost:11434/v1", kind="openai_compat", context_window=32768
            )
        )
        real_client = httpx.AsyncClient
        with patch(
            "httpx.AsyncClient",
            side_effect=lambda **kwargs: real_client(transport=httpx.MockTransport(respond), **kwargs),
        ):
            runner = await make_local_runner(
                registry,
                AnamesisConfig(model="local", request_timeout_s=0.1),
                SourceCollector(self.home / "sessions", self.project),
            )
            with self.assertRaisesRegex(RuntimeError, "连续 0.1 秒未响应.*ReadTimeout"):
                await runner._generate(ChatRequest(model="local"))

    async def test_animation_changes_within_second_and_reuses_completed_body(self):
        card = AnamesisCard("abc", started_at=10)
        card.ingest(
            AnamesisEvent(
                kind="analysis",
                run_id="abc",
                analysis=AnamesisAnalysisRecord(
                    record_id="a", stage_id="s", question="问题", rationale="依据" * 1000, conclusion="结论"
                ),
            )
        )
        first = card.render(expanded=True, width=80, color="green").plain
        body = card._body
        self.assertTrue(card.tick(10.1))
        second = card.render(expanded=True, width=80, color="green").plain
        self.assertNotEqual(first.split("\n")[0], second.split("\n")[0])
        self.assertIs(card._body, body)
        self.assertFalse(card.tick(10.1))
        self.assertTrue(card.tick(10.2))
        self.assertIs(card._body, body)

    async def test_terminal_states_stop_animation_and_display_distinct_symbols(self):
        for phase, glyph in (("completed", "✓"), ("failed", "✗"), ("paused", "Ⅱ"), ("interrupted", "■")):
            card = AnamesisCard("abc", started_at=10)
            card.tick(10.2)
            card.ingest(AnamesisEvent(kind=phase, run_id="abc", phase=phase))
            before = card.render(expanded=False, width=80, color="green").plain
            self.assertIn(glyph, before.split("\n")[0])
            self.assertFalse(card.tick(20.8))
            self.assertEqual(before, card.render(expanded=False, width=80, color="green").plain)

    async def test_both_modes_notify_end_once_without_replay_notifications(self):
        for app_type in (InlineApp, FullscreenApp):
            service = SimpleNamespace(
                current_session=lambda: "chat", on_event=None, request_close=lambda: None
            )
            runtime = SimpleNamespace(
                kernel=SimpleNamespace(cancel=lambda: True),
                bus=EventBus(session_id="chat"),
                config=SimpleNamespace(),
                model="local",
                anamnesis=service,
            )
            app = app_type(runtime=runtime, terminal=FakeTerminal(columns=80, rows=25))
            for index, phase in enumerate(("completed", "failed", "paused")):
                identity = f"run{index}"
                await app._on_anamnesis(
                    AnamesisEvent(
                        kind="started", run_id=identity, session_id="chat", phase="reviewing", sequence=1
                    )
                )
                end = AnamesisEvent(
                    kind=phase, run_id=identity, session_id="chat", phase=phase, reason="真实原因", sequence=2
                )
                await app._on_anamnesis(end)
                await app._on_anamnesis(end)
            notices = [b.text for b in app.timeline.buffer.blocks if b.kind == "notice"]
            self.assertEqual(len(notices), 3)
            for expected in ("入梦已完成", "入梦失败", "入梦已暂停"):
                self.assertTrue(any(expected in n and "真实原因" in n for n in notices))
            replay = AnamesisCard.from_preview(
                {"run_id": "old", "phase": "reviewing", "started_at": 10, "timestamp": 12}
            )
            self.assertFalse(replay.tick(14.2))
            await app._on_anamnesis(
                AnamesisEvent(kind="started", run_id="run0", session_id="chat", phase="reviewing", sequence=3)
            )
            await app._on_anamnesis(
                AnamesisEvent(
                    kind="completed", run_id="run0", session_id="chat", phase="completed", sequence=4
                )
            )
            self.assertEqual(len([b for b in app.timeline.buffer.blocks if b.kind == "notice"]), 4)
            app.stop()

    async def test_ticker_animates_even_without_model_fragments_in_both_modes(self):
        for app_type in (InlineApp, FullscreenApp):
            card = AnamesisCard("abc", started_at=10)
            service = SimpleNamespace(is_active=True, on_event=None, request_close=lambda: None)
            runtime = SimpleNamespace(
                kernel=SimpleNamespace(cancel=lambda: True),
                bus=EventBus(session_id="chat"),
                config=SimpleNamespace(),
                model="local",
                anamnesis=service,
            )
            app = app_type(runtime=runtime, terminal=FakeTerminal(columns=80, rows=25))
            app._anamnesis_block = SimpleNamespace(card=card)
            app.screen._running = True
            calls = []

            def render(calls=calls, card=card, app=app):
                calls.append(card.animation_frame)
                app.screen._running = False

            with (
                patch.object(app.screen, "request_render", side_effect=render),
                patch("logox.tui.content.anamnesis.time.time", return_value=10.1),
            ):
                await app._stream_ticker()
            self.assertEqual(calls, [1])
            app.stop()
