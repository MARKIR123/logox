"""No automatic retry of unfinished batches; proposals reference immutable analyses."""

from __future__ import annotations

import asyncio
import json
import threading
import unittest
from datetime import datetime
from types import SimpleNamespace

from logox.anamnesis.models import AnamesisAnalysisRecord, AnamesisEvent, MemoryProposal
from logox.anamnesis.runner import AnamesisRunner
from logox.anamnesis.service import AnamesisService
from logox.anamnesis.sources import SourceCollector
from logox.config.schema import AnamesisConfig
from logox.kernel.bus import EventBus
from logox.providers.base import DeltaEvent, StopEvent, ToolCallEvent
from logox.tui.content.anamnesis import AnamesisCard
from logox.tui.render.app import InlineApp
from logox.tui.render.fullscreen import FullscreenApp
from logox.tui.render.terminal import FakeTerminal
from tests.anamnesis.support import finish_calls, noop, research
from tests.anamnesis.test_runtime import ProposalProvider, TempCase


class EmptyProvider:
    def __init__(self, complete=True):
        self.complete = complete
        self.requests = []

    async def stream(self, request):
        self.requests.append(request)
        if self.complete:
            for call in finish_calls(
                json.loads(request.messages[0].text), MemoryProposal(complete=True).model_dump()
            ):
                yield call
        else:
            yield DeltaEvent(kind="text", text=MemoryProposal(complete=False).model_dump_json())
        yield StopEvent(stop_reason="end_turn")


class ContinuationTests(TempCase, unittest.IsolatedAsyncioTestCase):
    def service(self, provider, window=32768, **kwargs):
        async def factory(collector):
            return AnamesisRunner(provider, "local", window, collector)

        service = AnamesisService(
            config=AnamesisConfig(model="local"),
            home=self.home,
            cwd=self.project,
            sessions=self.home / "sessions",
            runner_factory=factory,
            **kwargs,
        )
        service.current_session = lambda: "chat"
        self.addAsyncCleanup(service.aclose)
        return service

    async def finish(self, service, manual=True):
        self.assertEqual(await service.start(manual=manual), "已开始入梦")
        await asyncio.wait_for(service._task, 5)

    def append(self, text="新指令"):
        with self.transcript.open("a", encoding="utf-8") as file:
            file.write(json.dumps({"role": "user", "turn": 2, "content": text, "ts": 2000}) + "\n")

    async def test_unfinished_batch_blocks_repeated_auto_and_reopen(self):
        now = [0]
        provider = ProposalProvider(complete=False)
        service = self.service(provider, clock=lambda: now[0])
        now[0] = 1801
        await self.finish(service)
        run_id = service.status().run_id
        count = len(provider.requests)
        checkpoint = json.loads(service._checkpoint_path.read_text(encoding="utf-8"))
        self.assertFalse(checkpoint["automatic_continuation"])
        self.assertIn("automatic_block", checkpoint)
        self.assertIn("未推进", await service.latest_report())
        for _ in range(3):
            self.assertIn("手动", await service.start(manual=False))
        self.assertEqual(len(provider.requests), count)
        self.assertFalse(service.store.global_processed())
        self.assertEqual(len(service.store.run_history()), 1)
        other = self.service(provider, clock=lambda: 0)
        other.clock = lambda: 1801
        other.note_submission(1001)  # Make this window the project queue candidate.
        self.assertIn("手动", await other.start(manual=False))
        await other.aclose()
        await service.aclose()
        reopened = self.service(provider, clock=lambda: 0)
        reopened.clock = lambda: 1801
        self.assertIn("手动", await reopened.start(manual=False))
        self.assertEqual(len(provider.requests), count)
        self.assertEqual(reopened.status().run_id, run_id)
        self.assertEqual(reopened.status().phase, "paused")

    async def test_manual_retry_preserves_run_and_finishes_original_sources(self):
        provider = ProposalProvider(complete=False)
        service = self.service(provider)
        await self.finish(service)
        identity = service.status().run_id
        provider.complete = True
        await self.finish(service)
        self.assertEqual(service.status().run_id, identity)
        self.assertEqual(service.status().phase, "completed")
        self.assertEqual(len(service.store.global_processed()), 1)
        self.assertEqual(json.loads(service._checkpoint_path.read_text(encoding="utf-8")), {})
        self.assertIn("AI Agent", (self.home / "ANAMNESIS.md").read_text(encoding="utf-8"))

    async def test_new_real_source_unlocks_without_skipping_original_batch(self):
        provider = EmptyProvider(False)
        service = self.service(provider, clock=lambda: 0)
        service.clock = lambda: 1801
        await self.finish(service)
        self.append()
        provider.complete = True
        await self.finish(service, manual=False)
        self.assertEqual(service.status().phase, "completed")
        self.assertEqual(len(service.store.global_processed()), 2)

    async def test_code_change_unlocks_and_draft_or_empty_submission_does_not(self):
        now = [0]
        provider = EmptyProvider(False)
        service = self.service(provider, clock=lambda: now[0])
        now[0] = 1801
        await self.finish(service)
        service.note_activity("input")
        now[0] += 1801
        self.assertIn("手动", await service.start(manual=False))
        service.note_activity("submit")  # No actual new source in the transcript.
        now[0] += 1801
        self.assertIn("手动", await service.start(manual=False))
        self.assertEqual(len(provider.requests), 1)
        (self.project / "new.py").write_text("value = 1\n", encoding="utf-8")
        provider.complete = True
        await self.finish(service, manual=False)
        self.assertEqual(len(provider.requests), 2)
        self.assertEqual(service.status().phase, "completed")

    async def test_completed_batch_checkpoint_is_quiet_and_daybreak_has_no_effect(self):
        self.transcript.write_text(
            json.dumps({"type": "session_init", "cwd": str(self.project)}) + "\n", encoding="utf-8"
        )
        self.append("甲" * 600)
        self.append("乙" * 600)
        now, local = [0], [datetime(2026, 10, 1, 1)]
        provider = EmptyProvider()
        service = self.service(provider, window=10000, clock=lambda: now[0], wall_clock=lambda: local[0])
        events = []

        async def emit(event):
            events.append(event)

        service.on_event = emit
        now[0] = 1801
        await self.finish(service, manual=False)
        self.assertEqual(service.status().phase, "continuing", service.status().reason)
        self.assertEqual(service.status().remaining, 1)
        self.assertIn("剩余", service.status().reason)
        self.assertEqual(events[-1].kind, "checkpoint")
        identity = service.status().run_id
        self.assertEqual(len(service.store.global_processed()), 1)
        local[0] = datetime(2026, 10, 1, 9)
        await self.finish(service, manual=False)
        self.assertEqual(service.status().mode, "")
        self.assertEqual(service.status().run_id, identity)
        self.assertEqual(service.status().phase, "completed")
        self.assertEqual(len(service.store.global_processed()), 2)
        self.assertFalse(any(e.kind == "paused" for e in events))
        self.assertEqual(sum(e.kind == "completed" for e in events), 1)

    async def test_both_apps_keep_one_card_and_only_notify_real_end(self):
        for app_type in (InlineApp, FullscreenApp):
            runtime = SimpleNamespace(
                kernel=SimpleNamespace(cancel=lambda: True),
                bus=EventBus(session_id="chat"),
                config=SimpleNamespace(),
                model="local",
                anamnesis=SimpleNamespace(
                    current_session=lambda: "chat", on_event=None, request_close=lambda: None
                ),
            )
            app = app_type(runtime=runtime, terminal=FakeTerminal(columns=90, rows=30))
            self.addCleanup(app.stop)
            for sequence, kind, phase in (
                (1, "started", "reviewing"),
                (2, "checkpoint", "continuing"),
                (3, "started", "reviewing"),
                (4, "checkpoint", "continuing"),
                (5, "started", "reviewing"),
                (6, "completed", "completed"),
            ):
                await app._on_anamnesis(
                    AnamesisEvent(
                        kind=kind,
                        run_id="one",
                        session_id="chat",
                        phase=phase,
                        sequence=sequence,
                        reason="本批已保存，剩余 1 个片段" if kind == "checkpoint" else "",
                    )
                )
            self.assertEqual(len([b for b in app.timeline.buffer.blocks if b.kind == "anamnesis"]), 1)
            notices = [b.text for b in app.timeline.buffer.blocks if b.kind == "notice"]
            self.assertEqual(len(notices), 1)
            self.assertIn("入梦已完成", notices[0])
            self.assertEqual(app._anamnesis_block.card.reason, "")

    async def test_repeated_tool_protocol_failure_pauses_once_and_manual_reference_finishes(self):
        original = {}

        def responder(request, step):
            if step == 1:
                ref = json.loads(request.messages[0].text)["sources"][0]
                original.update(
                    AnamesisAnalysisRecord(
                        item_id=json.loads(request.messages[0].text)["current_item"]["item_id"],
                        record_id="saved",
                        stage_id="review",
                        question="学习方向？",
                        rationale="明确用户原话",
                        conclusion="AI Agent",
                        source_ids=[ref["source_id"]],
                    ).model_dump()
                )
                return ToolCallEvent(call_id="first", name="record_analysis", arguments=original)
            return ToolCallEvent(
                call_id=f"bad{step}",
                name="propose_memory",
                arguments={
                    "analyses": [{**original, "conclusion": "重复改写"}],
                    "changes": [],
                },
            )

        provider = ScriptProvider(responder)
        service = self.service(provider, clock=lambda: 0)
        service.clock = lambda: 1801
        events = []

        async def emit(event):
            events.append(event)

        service.on_event = emit
        await self.finish(service)
        self.assertEqual(service.status().phase, "paused")
        self.assertEqual(len(provider.requests), 10)
        self.assertEqual(sum(e.kind == "paused" for e in events), 1)
        self.assertEqual(sum(bool(e.operation and e.operation.get("state") == "failed") for e in events), 9)
        self.assertFalse(service.store.global_processed())
        self.assertIn("手动", await service.start(manual=False))
        self.assertEqual(len(provider.requests), 10)
        identity = service.status().run_id

        # Manual recovery must actually dispose of the pending item, not merely return an empty proposal.
        def complete_pending(request, step):
            payload = json.loads(request.messages[0].text)
            current = payload["current_item"]
            if current:
                return ToolCallEvent(
                    call_id="resolved",
                    name="update_research",
                    arguments={
                        "item_id": current["item_id"],
                        "expected_version": current["version"],
                        "status": "resolved",
                        "analysis_ids": ["saved"],
                        "source_ids": original["source_ids"],
                        "conclusion": original["conclusion"],
                    },
                )
            return {"analyses": [], "changes": [], "complete": True}

        provider.responder = complete_pending
        await self.finish(service)
        self.assertEqual(service.status().run_id, identity)
        self.assertEqual(service.status().phase, "continuing")
        await self.finish(service)
        self.assertEqual(service.status().phase, "completed")
        self.assertIn("明确用户原话", await service.latest_report())
        self.assertEqual(len(service.store.global_processed()), 1)

    async def test_waiting_card_static_and_restores_as_resumable_pause(self):
        card = AnamesisCard("one", started_at=10)
        card.ingest(
            AnamesisEvent(kind="checkpoint", run_id="one", phase="continuing", reason="剩余 1 个片段")
        )
        self.assertFalse(card.tick(20))
        self.assertIn("等待续做", card.render(expanded=False, width=90, color="green").plain)
        reopened = AnamesisCard.from_preview({"run_id": "one", "phase": "continuing"})
        self.assertEqual(reopened.phase, "paused")
        self.assertFalse(reopened.tick(21))
        card.ingest(AnamesisEvent(kind="started", run_id="one", phase="reviewing"))
        self.assertTrue(card.tick(22.1))


class ScriptProvider:
    def __init__(self, responder):
        self.responder, self.requests = responder, []

    async def stream(self, request):
        self.requests.append(request)
        response = self.responder(request, len(self.requests))
        if isinstance(response, dict) and "complete" not in response:
            response["complete"] = True
        if isinstance(response, ToolCallEvent) and response.name == "propose_memory":
            response = response.model_copy(update={"arguments": {"complete": True, **response.arguments}})
        if isinstance(response, ToolCallEvent):
            yield response
            yield StopEvent(stop_reason="tool_use")
        else:
            yield DeltaEvent(kind="text", text=json.dumps(response, ensure_ascii=False))
            yield StopEvent(stop_reason="end_turn")


class ProposalReferenceTests(TempCase, unittest.IsolatedAsyncioTestCase):
    async def run_script(self, responder):
        collector = SourceCollector(self.home / "sessions", self.project)
        refs = collector.collect(threading.Event())
        provider = ScriptProvider(responder)
        runner = AnamesisRunner(provider, "local", 32768, collector)
        records, operations = [], []

        async def emit(value):
            records.append(value)

        async def operation(value):
            operations.append(value)

        result = await runner.run_segment(
            research_state=research(refs),
            state_event=noop,
            sources=refs,
            snapshots={
                scope: SimpleNamespace(entries=[], model_dump=lambda: {}) for scope in ("user", "project")
            },
            stop=threading.Event(),
            emit=emit,
            operation=operation,
        )
        return result.proposal, records, operations, provider

    def analysis(self, request):
        ref = json.loads(request.messages[0].text)["sources"][0]
        return AnamesisAnalysisRecord(
            item_id="review",
            record_id="fact",
            stage_id="review",
            question="用户学习什么？",
            rationale="来自明确用户原话",
            conclusion="AI Agent",
            source_ids=[ref["source_id"]],
        ).model_dump()

    async def test_conflicting_copy_has_actionable_feedback_and_reference_succeeds(self):
        original = {}

        def responder(request, step):
            if step == 1:
                original.update(self.analysis(request))
                return ToolCallEvent(call_id="a", name="record_analysis", arguments=original)
            if step == 2:
                return ToolCallEvent(
                    call_id="b",
                    name="propose_memory",
                    arguments={"analyses": [{**original, "rationale": "模型重新措辞"}], "changes": []},
                )
            feedback = request.messages[-1].blocks[0].content
            self.assertIn("fact", feedback)
            self.assertIn("analyses=[]", feedback)
            self.assertIn("rationale", feedback)
            return ToolCallEvent(
                call_id="c", name="propose_memory", arguments={"analyses": [], "changes": []}
            )

        result, records, operations, provider = await self.run_script(responder)
        self.assertTrue(result.complete)
        self.assertEqual(len(records), 1)
        self.assertEqual(result.analyses[0].rationale, original["rationale"])
        self.assertEqual(operations[0]["state"], "failed")
        self.assertIn("analysis_id_prefix", json.loads(provider.requests[0].messages[0].text))
        self.assertIn("analyses=[]", provider.requests[0].system)

    async def test_unknown_source_feedback_names_invalid_identity_and_can_repair(self):
        def responder(request, step):
            analysis = self.analysis(request)
            if step == 1:
                return ToolCallEvent(
                    call_id="a",
                    name="record_analysis",
                    arguments={**analysis, "source_ids": ["bad-short-id"]},
                )
            self.assertIn("bad-short-id", request.messages[-1].blocks[0].content)
            self.assertIn("完整", request.messages[-1].blocks[0].content)
            return {"analyses": [analysis], "changes": []}

        result, records, _, _ = await self.run_script(responder)
        self.assertTrue(result.complete)
        self.assertEqual(len(records), 1)

    async def test_plain_json_repair_reports_actual_identity_conflict(self):
        def responder(request, step):
            analysis = self.analysis(request)
            if step == 1:
                return ToolCallEvent(call_id="a", name="record_analysis", arguments=analysis)
            if step == 2:
                return {"analyses": [{**analysis, "conclusion": "换一种说法"}], "changes": []}
            self.assertIn("fact", request.messages[-1].text)
            self.assertIn("analyses=[]", request.messages[-1].text)
            return {"analyses": [], "changes": []}

        result, records, _, _ = await self.run_script(responder)
        self.assertTrue(result.complete)
        self.assertEqual(records[0].conclusion, "AI Agent")

    async def test_explicit_revision_uses_new_identity_and_preserves_original(self):
        def responder(request, step):
            analysis = self.analysis(request)
            if step == 1:
                return ToolCallEvent(call_id="a", name="record_analysis", arguments=analysis)
            return {
                "analyses": [
                    {
                        **analysis,
                        "record_id": "fact-v2",
                        "revises_record_id": "fact",
                        "conclusion": "只记录当前学习方向",
                    }
                ],
                "changes": [],
            }

        result, records, _, _ = await self.run_script(responder)
        self.assertTrue(result.complete)
        self.assertEqual([a.record_id for a in records], ["fact", "fact-v2"])
        self.assertEqual(result.analyses[0].conclusion, "AI Agent")
