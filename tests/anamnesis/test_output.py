"""Generation has no LOGOX output cap; provider truncation remains fail-closed."""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

from logox.anamnesis.models import MemoryProposal
from logox.anamnesis.runner import AnamesisRunner
from logox.anamnesis.service import AnamesisService
from logox.anamnesis.sources import SourceCollector
from logox.config.schema import AnamesisConfig
from logox.kernel.bus import EventBus
from logox.kernel.events import Usage
from logox.kernel.messages import user_message
from logox.providers.base import ChatRequest, DeltaEvent, StopEvent, UsageEvent
from logox.tui.render.app import InlineApp
from logox.tui.render.fullscreen import FullscreenApp
from logox.tui.render.terminal import FakeTerminal
from tests.anamnesis.test_runtime import ProposalProvider, TempCase


class LongProvider(ProposalProvider):
    async def stream(self, request):
        yield DeltaEvent(kind="reasoning", text="开始核对" + "思考" * 35000)
        yield DeltaEvent(kind="reasoning", text="继续核对" + "依据" * 35000 + "已经查完依据")
        async for event in super().stream(request):
            if isinstance(event, StopEvent):
                yield UsageEvent(usage=Usage(input_tokens=1000, output_tokens=16000))
            yield event


class TruncatedProvider(ProposalProvider):
    async def stream(self, request):
        yield DeltaEvent(kind="reasoning", text="正在核对，但最终输出尚未完成。")
        async for event in super().stream(request):
            if isinstance(event, StopEvent):
                yield UsageEvent(usage=Usage(input_tokens=1000, output_tokens=4096))
                yield StopEvent(stop_reason="max_tokens")
            else:
                yield event


class OutputTests(TempCase, unittest.IsolatedAsyncioTestCase):
    def service(self, provider):
        async def factory(collector):
            return AnamesisRunner(provider, "local", 32768, collector)

        service = AnamesisService(
            config=AnamesisConfig(model="local"),
            home=self.home,
            cwd=self.project,
            sessions=self.home / "sessions",
            runner_factory=factory,
        )
        service.current_session = lambda: "chat"
        return service

    async def test_long_reasoning_is_complete_on_disk_bounded_in_preview_and_restored(self):
        provider = LongProvider()
        service = self.service(provider)
        await service.start("nap")
        await service._task
        self.assertEqual(service.status().phase, "completed", service.status().reason)
        self.assertTrue(all(r.max_tokens is None for r in provider.requests))
        self.assertEqual(len(provider.requests), 2)  # Proposal and independent semantic review.
        preview = (await service.history("chat"))[0]
        self.assertEqual(len(preview["reasoning"]), 12000)
        trace = [
            json.loads(line)
            for line in (await service.run_record(preview["run_id"], trace=True)).split("\n")
            if line
        ]
        reasoning = "".join(r["delta"] for r in trace if r["kind"] == "reasoning")
        self.assertGreater(len(reasoning), 280000)
        self.assertEqual(reasoning.count("开始核对"), 2)
        self.assertEqual(reasoning.count("已经查完依据"), 2)
        diagnostics = [json.loads(r["result"]) for r in trace if r["kind"] == "model_response"]
        self.assertEqual(len(diagnostics), 2)
        self.assertTrue(all(d["max_tokens"] is None for d in diagnostics))
        self.assertTrue(all(d["usage"]["output_tokens"] == 16000 for d in diagnostics))
        await service.aclose()
        for app_type in (InlineApp, FullscreenApp):
            reopened = self.service(ProposalProvider())
            runtime = SimpleNamespace(
                kernel=SimpleNamespace(cancel=lambda: True),
                bus=EventBus(session_id="unused"),
                config=SimpleNamespace(),
                model="local",
                anamnesis=reopened,
            )
            app = app_type(runtime=runtime, terminal=FakeTerminal(columns=80, rows=25))
            await app._restore_anamnesis()
            card = next(b.card for b in app.timeline.buffer.blocks if b.kind == "anamnesis")
            self.assertIn("已经查完依据", card.reasoning)
            self.assertIn("已经查完", card.render(expanded=True, width=80, color="green").plain)
            self.assertEqual(card.phase, "completed")
            app.stop()
            await reopened.aclose()

    async def test_server_truncation_preserves_reasoning_usage_and_never_commits_even_valid_json(self):
        service = self.service(TruncatedProvider())
        await service.start("nap")
        await service._task
        self.assertEqual(service.status().phase, "failed")
        self.assertIn("4096 token", service.status().reason)
        self.assertIn("未提交档案", service.status().reason)
        self.assertFalse((self.home / "ANAMNESIS.md").exists())
        self.assertFalse((self.project / "ANAMNESIS.md").exists())
        preview = (await service.history("chat"))[0]
        self.assertIn("尚未完成", preview["reasoning"])
        trace = [
            json.loads(line)
            for line in (await service.run_record(preview["run_id"], trace=True)).split("\n")
            if line
        ]
        self.assertFalse(any(r["kind"] in {"analysis", "committed"} for r in trace))
        diagnostic = json.loads(next(r["result"] for r in trace if r["kind"] == "model_response"))
        self.assertEqual(diagnostic["stop_reason"], "max_tokens")
        self.assertEqual(diagnostic["usage"]["output_tokens"], 4096)
        self.assertIsNone(diagnostic["max_tokens"])
        await service.aclose()
        reopened = self.service(ProposalProvider())
        card_preview = (await reopened.history("chat"))[0]
        self.assertEqual(card_preview["phase"], "failed")
        self.assertIn("尚未完成", card_preview["reasoning"])
        await reopened.aclose()

    async def test_large_final_text_is_not_cut_by_previous_character_limit(self):
        body = " " * 130000 + MemoryProposal().model_dump_json()

        class Provider:
            async def stream(self, request):
                yield DeltaEvent(kind="text", text=body)
                yield StopEvent(stop_reason="end_turn")

        runner = AnamesisRunner(
            Provider(), "local", 32768, SourceCollector(self.home / "sessions", self.project)
        )
        text, calls = await runner._generate(ChatRequest(model="local"))
        self.assertEqual(text, body)
        self.assertEqual(calls, [])
        self.assertTrue(MemoryProposal.model_validate_json(text).complete)

    async def test_unknown_usage_is_not_fabricated_as_zero(self):
        class Provider:
            async def stream(self, request):
                yield DeltaEvent(kind="reasoning", text="尚在核对")
                yield StopEvent(stop_reason="max_tokens")

        runner = AnamesisRunner(
            Provider(), "local", 32768, SourceCollector(self.home / "sessions", self.project)
        )
        events = []

        async def progress(event):
            events.append(event)

        runner.on_progress = progress
        with self.assertRaisesRegex(ValueError, "服务未提供用量"):
            await runner._generate(ChatRequest(model="local"))
        self.assertEqual(events[0]["delta"], "尚在核对")
        diagnostic = json.loads(events[-1]["result"])
        self.assertIsNone(diagnostic["usage"])

    async def test_unlimited_generation_still_rejects_input_without_headroom(self):
        provider = ProposalProvider()
        runner = AnamesisRunner(
            provider, "local", 4096, SourceCollector(self.home / "sessions", self.project)
        )
        with self.assertRaisesRegex(ValueError, "上下文不足"):
            await runner._generate(ChatRequest(model="local", messages=[user_message("x" * 15000)]))
        self.assertEqual(provider.requests, [])
