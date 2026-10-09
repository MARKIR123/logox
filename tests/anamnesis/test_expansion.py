"""Real keyboard/click paths must expose saved and live Anamnesis reasoning."""

from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from logox.anamnesis.models import AnamesisEvent
from logox.anamnesis.runner import AnamesisRunner
from logox.anamnesis.service import AnamesisService
from logox.config.schema import AnamesisConfig
from logox.kernel.bus import EventBus
from logox.tui.content.anamnesis import AnamesisCard
from logox.tui.content.cards import CardContext
from logox.tui.content.timeline import Block, TimelineBuffer, TimelineRenderCache, render_cached
from logox.tui.render.app import InlineApp
from logox.tui.render.fullscreen import FullscreenApp
from logox.tui.render.keys import Key
from logox.tui.render.terminal import FakeTerminal
from logox.tui.theme import load_theme
from tests.anamnesis.test_runtime import ProposalProvider, TempCase


class ExpansionTests(TempCase, unittest.IsolatedAsyncioTestCase):
    def service(self, provider=None):
        async def factory(collector):
            return AnamesisRunner(provider or ProposalProvider(), "local", 32768, collector)

        service = AnamesisService(
            config=AnamesisConfig(model="local"),
            home=self.home,
            cwd=self.project,
            sessions=self.home / "sessions",
            runner_factory=factory,
        )
        service.current_session = lambda: "chat"
        return service

    def app(self, service, app_type):
        runtime = SimpleNamespace(
            kernel=SimpleNamespace(cancel=lambda: True),
            bus=EventBus(session_id="chat"),
            config=SimpleNamespace(),
            model="local",
            anamnesis=service,
        )
        return app_type(runtime=runtime, terminal=FakeTerminal(columns=100, rows=30))

    def text(self, app):
        return "\n".join(row.plain for row in app.timeline.render(96))

    async def test_ctrl_t_opens_live_reasoning_and_closes_without_waking_in_both_modes(self):
        for app_type in (InlineApp, FullscreenApp):
            service = self.service()
            app = self.app(service, app_type)
            await app._on_anamnesis(
                AnamesisEvent(kind="started", run_id="abc", session_id="chat", phase="reviewing", sequence=1)
            )
            await app._on_anamnesis(
                AnamesisEvent(
                    kind="reasoning", run_id="abc", session_id="chat", delta="先核对用户原话", sequence=2
                )
            )
            self.assertNotIn("先核对用户原话", self.text(app))
            generation = service._generation
            app._dispatch(Key("t", ctrl=True))
            self.assertIn("先核对用户原话", self.text(app))
            await app._on_anamnesis(
                AnamesisEvent(
                    kind="reasoning", run_id="abc", session_id="chat", delta="再比较实现方案", sequence=3
                )
            )
            self.assertIn("再比较实现方案", self.text(app))
            app._dispatch(Key("t", ctrl=True))
            self.assertNotIn("先核对用户原话", self.text(app))
            self.assertEqual(service._generation, generation)
            self.assertFalse(service._stop.is_set())
            app.stop()
            await service.aclose()

    async def test_only_ctrl_t_controls_anamnesis_and_new_cards_follow_reasoning_state(self):
        for app_type in (InlineApp, FullscreenApp):
            service = self.service()
            app = self.app(service, app_type)
            await app._on_anamnesis(AnamesisEvent(kind="reasoning", run_id="abc", delta="卡片思考"))
            app._dispatch(Key("o", ctrl=True))
            self.assertNotIn("卡片思考", self.text(app))
            self.assertTrue(app.timeline.buffer.expand_tools)
            app._dispatch(Key("t", ctrl=True))
            self.assertIn("卡片思考", self.text(app))
            app._dispatch(Key("o", ctrl=True))
            self.assertIn("卡片思考", self.text(app))
            await app._on_anamnesis(AnamesisEvent(kind="reasoning", run_id="def", delta="新运行思考"))
            self.assertIn("新运行思考", self.text(app))
            app._dispatch(Key("t", ctrl=True))
            self.assertNotIn("新运行思考", self.text(app))
            app.stop()
            await service.aclose()

    async def test_ctrl_t_after_history_restore_exposes_actual_saved_reasoning(self):
        service = self.service()
        service.store.bind_run("abc", "chat")
        service.store.append_record(
            "abc", AnamesisEvent(kind="reasoning", run_id="abc", delta="历史中的真实思考").model_dump()
        )
        service.store.append_record(
            "abc", AnamesisEvent(kind="failed", run_id="abc", phase="failed", reason="服务中断").model_dump()
        )
        await service.aclose()
        for app_type in (InlineApp, FullscreenApp):
            reopened = self.service()
            app = self.app(reopened, app_type)
            await app._restore_anamnesis()
            self.assertNotIn("历史中的真实思考", self.text(app))
            app._dispatch(Key("t", ctrl=True))
            self.assertIn("历史中的真实思考", self.text(app))
            self.assertIn("服务中断", self.text(app))
            app.stop()
            await reopened.aclose()

    async def test_click_after_keyboard_expansion_and_global_collapse_share_visible_state(self):
        service = self.service()
        app = self.app(service, InlineApp)
        await app._on_anamnesis(AnamesisEvent(kind="reasoning", run_id="abc", delta="点击可见思考"))
        app._dispatch(Key("t", ctrl=True))
        self.assertIn("点击可见思考", self.text(app))
        block = next(b for b in app.timeline.buffer.blocks if b.kind == "anamnesis")
        line = next(s for s, _, b in app.timeline.block_ranges if b is block)
        self.assertTrue(app.timeline.toggle_card_at_line(line))
        self.assertNotIn("点击可见思考", self.text(app))
        line = next(s for s, _, b in app.timeline.block_ranges if b is block)
        self.assertTrue(app.timeline.toggle_card_at_line(line))
        self.assertIn("点击可见思考", self.text(app))
        app._dispatch(Key("t", ctrl=True))
        self.assertNotIn("点击可见思考", self.text(app))
        self.assertIsNone(block.expanded)
        app.stop()
        await service.aclose()

    async def test_expansion_cache_uses_reasoning_state_and_empty_model_output_is_honest(self):
        card = AnamesisCard("abc")
        block, cache = Block(kind="anamnesis", card=card), TimelineRenderCache()
        context = CardContext(palette=load_theme("logox-dark").palette, width=96)
        folded = render_cached([block], cache, context=context, expand_reasoning=False)
        self.assertNotIn("尚未收到", folded.text.plain)
        open_card = render_cached([block], cache, context=context, expand_reasoning=True)
        self.assertIn("尚未收到模型返回的思考文本", open_card.text.plain)
        self.assertFalse(render_cached([block], cache, context=context, expand_reasoning=True).cache_rebuilt)
        closed = render_cached([block], cache, context=context, expand_reasoning=False)
        self.assertNotIn("尚未收到", closed.text.plain)
        self.assertEqual(card.reasoning, "")

    async def test_setters_and_overrides_preserve_normal_type_independence(self):
        buffer = TimelineBuffer()
        card = Block(kind="anamnesis", card=AnamesisCard("abc"), expanded=True)
        buffer.blocks.append(card)
        buffer.toggle_expand_reasoning()
        self.assertIsNone(card.expanded)
        self.assertFalse(buffer.expand_reasoning)
        buffer.toggle_expand_reasoning()
        self.assertTrue(buffer.expand_reasoning)
        self.assertFalse(buffer.expand_tools)
        buffer.set_expand_tools(True)
        self.assertTrue(buffer.expand_reasoning)
        self.assertTrue(buffer.expand_tools)
        card.expanded = True
        buffer.toggle_expand_tools()
        self.assertTrue(card.expanded)
        self.assertTrue(buffer.expand_reasoning)
        buffer.set_expand_reasoning(False)
        self.assertIsNone(card.expanded)
        self.assertFalse(buffer.expand_reasoning)
        self.assertFalse(buffer.expand_tools)

    async def test_editing_and_read_only_commands_keep_live_dream_running(self):
        for app_type in (InlineApp, FullscreenApp):
            provider = ProposalProvider(wait=True)
            service = self.service(provider)
            app = self.app(service, app_type)
            app._loop = asyncio.get_running_loop()
            try:
                await service.start()
                await asyncio.wait_for(provider.entered.wait(), 2)
                generation = service._generation
                for key in (
                    Key("x", char="x"),
                    Key("backspace"),
                    Key("paste", char="未发送的草稿"),
                    Key("t", ctrl=True),
                    Key("escape"),
                    Key("c", ctrl=True),
                ):
                    app._dispatch(key)
                    self.assertTrue(service.is_active)
                    self.assertFalse(service._stop.is_set())
                    self.assertEqual(service._generation, generation)
                self.assertEqual(app.editor.inner.text, "未发送的草稿")

                async def close_panel(*args, **kwargs):
                    return None

                pending = []

                def spawn(coro, pending=pending):
                    task = asyncio.create_task(coro)
                    pending.append(task)
                    return task

                with (
                    patch.object(app, "push_overlay", side_effect=close_panel),
                    patch.object(app, "_spawn", side_effect=spawn),
                ):
                    for command in (
                        "/anamnesis status",
                        "/anamnesis history",
                        "/anamnesis report",
                        "/anamnesis trace",
                    ):
                        app._on_submit(command)
                        await pending.pop()
                        self.assertTrue(service.is_active)
                        self.assertFalse(service._stop.is_set())
                        self.assertEqual(service._generation, generation)
            finally:
                app.stop()
                await service.aclose()

    async def test_editor_enter_submits_and_pauses_before_foreground_start(self):
        for app_type in (InlineApp, FullscreenApp):
            provider = ProposalProvider(wait=True)
            service = self.service(provider)
            app = self.app(service, app_type)
            app._loop = asyncio.get_running_loop()
            submitted = asyncio.Event()
            received = []

            async def start(text, received=received, service=service, submitted=submitted):
                received.append(text)
                self.assertTrue(service._stop.is_set())
                submitted.set()

            app.runtime.kernel.start = start
            try:
                await service.start()
                await asyncio.wait_for(provider.entered.wait(), 2)
                app._dispatch(Key("paste", char="新的用户指令"))
                self.assertTrue(service.is_active)
                app._dispatch(Key("enter"))
                await asyncio.wait_for(submitted.wait(), 2)
                await asyncio.wait_for(service._task, 2)
                self.assertEqual(received, ["新的用户指令"])
                self.assertEqual(service.status().phase, "paused")
                self.assertEqual(service.status().reason, "用户发送消息")
                self.assertTrue(provider.closed)
                self.assertFalse((self.home / "ANAMNESIS.md").exists())
            finally:
                app.stop()
                await service.aclose()

    async def test_explicit_stop_command_pauses_live_dream_in_both_modes(self):
        for app_type in (InlineApp, FullscreenApp):
            provider = ProposalProvider(wait=True)
            service = self.service(provider)
            app = self.app(service, app_type)
            try:
                await service.start()
                await asyncio.wait_for(provider.entered.wait(), 2)
                await app.submit("/anamnesis stop")
                await asyncio.wait_for(service._task, 2)
                self.assertEqual(service.status().phase, "paused")
                self.assertEqual(service.status().reason, "用户停止入梦")
                self.assertTrue(provider.closed)
            finally:
                app.stop()
                await service.aclose()

    async def test_unsubmitted_draft_does_not_discard_a_completed_proposal(self):
        for index, app_type in enumerate((InlineApp, FullscreenApp), start=2):
            with self.transcript.open("a", encoding="utf-8") as file:
                file.write(
                    json.dumps(
                        {"role": "user", "turn": index, "content": f"继续学习 AI Agent，第 {index} 轮"}
                    )
                    + "\n"
                )
            provider = ProposalProvider()
            release = asyncio.Event()
            original_stream = provider.stream

            async def waiting_stream(
                request, provider=provider, release=release, original_stream=original_stream
            ):
                provider.entered.set()
                await release.wait()
                async for event in original_stream(request):
                    yield event

            provider.stream = waiting_stream
            service = self.service(provider)
            app = self.app(service, app_type)
            try:
                self.assertEqual(await service.start(), "已开始入梦")
                await asyncio.wait_for(provider.entered.wait(), 2)
                generation = service._generation
                app._dispatch(Key("paste", char="想好了再发送"))
                self.assertEqual(service._generation, generation)
                self.assertFalse(service._stop.is_set())
                release.set()
                await asyncio.wait_for(service._task, 3)
                self.assertEqual(service.status().phase, "completed", service.status().reason)
                self.assertIn("AI Agent", (self.home / "ANAMNESIS.md").read_text(encoding="utf-8"))
                self.assertEqual(app.editor.inner.text, "想好了再发送")
            finally:
                app.stop()
                await service.aclose()
