"""Persistent Anamnesis ownership, real-time visibility and project admission contracts."""

from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from logox.anamnesis.coordinator import AnamesisCoordinator
from logox.anamnesis.models import AnamesisAnalysisRecord, AnamesisEvent, MemoryProposal
from logox.anamnesis.runner import AnamesisRunner
from logox.anamnesis.service import AnamesisService
from logox.anamnesis.sources import SourceCollector
from logox.config.schema import AnamesisConfig
from logox.context.storage import SessionTranscriptWriter
from logox.kernel.bus import EventBus
from logox.providers.base import DeltaEvent, StopEvent, ToolCallEvent
from logox.store.manager import SessionManager
from logox.store.replay import load_session_records, reconstruct_messages, replay_into_timeline
from logox.tui.content.anamnesis import AnamesisCard
from logox.tui.render.app import InlineApp
from logox.tui.render.fullscreen import FullscreenApp
from logox.tui.render.terminal import FakeTerminal
from tests.anamnesis.test_runtime import ProposalProvider, TempCase


class VisibleProvider(ProposalProvider):
    def __init__(self):
        super().__init__()
        self.release = asyncio.Event()

    async def stream(self, request):
        yield DeltaEvent(kind="reasoning", text="先核对用户原话，避免把项目要求当成长期偏好。")
        await self.release.wait()
        async for event in super().stream(request):
            yield event


class HistoryTests(TempCase, unittest.IsolatedAsyncioTestCase):
    def service(self, provider=None, owner="chat"):
        async def factory(collector):
            return AnamesisRunner(provider or ProposalProvider(), "local", 32768, collector)

        service = AnamesisService(
            config=AnamesisConfig(model="local"),
            home=self.home,
            cwd=self.project,
            sessions=self.home / "sessions",
            runner_factory=factory,
        )
        service.current_session = lambda: owner
        return service

    def app(self, service, app_type=InlineApp):
        runtime = SimpleNamespace(
            kernel=SimpleNamespace(cancel=lambda: True),
            bus=EventBus(session_id="irrelevant-bus-id"),
            config=SimpleNamespace(),
            model="local",
            anamnesis=service,
        )
        return app_type(runtime=runtime, terminal=FakeTerminal(columns=80, rows=25))

    async def test_live_reasoning_before_stop_and_cancel_remains_visible_after_restart(self):
        provider = VisibleProvider()
        service = self.service(provider)
        observed = asyncio.Event()

        async def receive(event):
            if event.delta:
                observed.set()

        service.on_event = receive
        await service.start("nap")
        await asyncio.wait_for(observed.wait(), 5)
        self.assertTrue(service.is_active)
        self.assertFalse((self.home / "ANAMNESIS.md").exists())
        snapshot = (await service.history("chat"))[0]
        self.assertIn("先核对用户原话", snapshot["reasoning"])
        service.note_activity("submit")
        await service._task
        await service.aclose()
        for app_type in (InlineApp, FullscreenApp):
            reopened = self.service()
            app = self.app(reopened, app_type)
            await app._restore_anamnesis()
            await app._restore_anamnesis()
            blocks = [b for b in app.timeline.buffer.blocks if b.kind == "anamnesis"]
            self.assertEqual(len(blocks), 1)
            self.assertEqual(blocks[0].card.phase, "paused")
            self.assertIn("先核对用户原话", blocks[0].card.reasoning)
            self.assertEqual(blocks[0].card.reason, "用户发送消息")
            self.assertFalse(blocks[0].card.tick(9999999999))
            app.stop()
            await reopened.aclose()

    async def test_reference_restores_original_position_and_never_enters_messages(self):
        service = self.service()
        writer = SessionTranscriptWriter(log_file=self.transcript)
        service.on_started = lambda run_id, owner: writer.write_step(
            turn=0, step=0, role="", event_type="anamnesis_ref", run_id=run_id
        )
        await service.start("nap")
        await service._task
        writer.write_step(turn=2, step=0, role="user", event_type="user_prompt", content="后续任务")
        records = load_session_records(self.transcript)
        messages = reconstruct_messages(records)
        self.assertEqual([m.text for m in messages], ["我目前学习 AI Agent。", "后续任务"])
        await service.aclose()
        for app_type in (InlineApp, FullscreenApp):
            reopened = self.service()
            app = self.app(reopened, app_type)
            replay_into_timeline(records, app.timeline)
            await app._restore_anamnesis()
            blocks = app.timeline.buffer.blocks
            self.assertEqual([b.kind for b in blocks], ["user", "anamnesis", "user"])
            self.assertEqual(blocks[1].card.phase, "completed")
            self.assertIn("learning", blocks[1].card.analyses)
            self.assertTrue(blocks[1].card.changes)
            self.assertEqual(len([r for r in records if r.get("type") == "anamnesis_ref"]), 1)
            app.stop()
            await reopened.aclose()

    async def test_other_session_and_delayed_restore_cannot_leak_card(self):
        service = self.service()
        owner = ["chat"]
        service.current_session = lambda: owner[0]
        await service.start("nap")
        await service._task
        app = self.app(service)
        owner[0] = "new-chat"
        await app._restore_anamnesis()
        self.assertEqual(app.timeline.buffer.blocks, [])
        await app._on_anamnesis(
            AnamesisEvent(
                kind="analysis", run_id=service.status().run_id, session_id="chat", delta="旧会话内容"
            )
        )
        self.assertEqual(app.timeline.buffer.blocks, [])
        entered, release = asyncio.Event(), asyncio.Event()
        original = service.history

        async def delayed(session_id):
            result = await original(session_id)
            entered.set()
            await release.wait()
            return result

        service.history = delayed
        owner[0] = "chat"
        task = asyncio.create_task(app._restore_anamnesis())
        await entered.wait()
        owner[0] = "new-chat"
        release.set()
        await task
        self.assertEqual(app.timeline.buffer.blocks, [])
        self.assertEqual(len(await original()), 1)
        app.stop()
        await service.aclose()

    async def test_restore_snapshot_never_overwrites_newer_live_event(self):
        service = self.service()
        service.store.bind_run("abc", "chat")
        await service._emit(
            AnamesisEvent(
                kind="analysis",
                run_id="abc",
                phase="reviewing",
                analysis=AnamesisAnalysisRecord(
                    record_id="old", stage_id="s", question="原问题", rationale="原依据", conclusion="原结论"
                ),
            )
        )
        app = self.app(service)
        entered, release = asyncio.Event(), asyncio.Event()
        original = service.history

        async def delayed(owner):
            snapshot = await original(owner)
            entered.set()
            await release.wait()
            return snapshot

        service.history = delayed
        restore = asyncio.create_task(app._restore_anamnesis())
        await entered.wait()
        await service._emit(AnamesisEvent(kind="reasoning", run_id="abc", delta="新片段"))
        release.set()
        await restore
        self.assertIn("新片段", app.timeline.buffer.blocks[0].card.reasoning)
        self.assertEqual(app.timeline.buffer.blocks[0].card.sequence, 2)
        self.assertIn("old", app.timeline.buffer.blocks[0].card.analyses)
        app.stop()
        await service.aclose()

    async def test_same_run_continuation_keeps_owner_and_new_session_gets_new_run(self):
        provider = ProposalProvider(complete=False)
        service = self.service(provider)
        owner = ["chat"]
        service.current_session = lambda: owner[0]
        await service.start("nap")
        await service._task
        original = service.status().run_id
        owner[0] = "new-chat"
        service.note_submission()
        await service.start("nap")
        await service._task
        self.assertNotEqual(service.status().run_id, original)
        self.assertEqual((await service.history("chat"))[0]["run_id"], original)
        self.assertEqual(len(await service.history("new-chat")), 1)
        await service.aclose()

    async def test_operation_start_result_and_findings_plan_are_on_restored_card(self):
        class ReadProvider:
            def __init__(self):
                self.requests = 0

            async def stream(self, request):
                self.requests += 1
                if self.requests == 1:
                    yield ToolCallEvent(call_id="read1", name="read", arguments={"path": "code.py"})
                    yield ToolCallEvent(call_id="read2", name="read", arguments={"path": "missing.py"})
                else:
                    yield DeltaEvent(
                        kind="text",
                        text=MemoryProposal(
                            review_findings=["有重复分支，收益尚未测试"], next_plan=["比较两个实现再安排测试"]
                        ).model_dump_json(),
                    )
                yield StopEvent(stop_reason="end_turn")

        (self.project / "code.py").write_text("pass", encoding="utf-8")
        service = self.service(ReadProvider())
        events = []

        async def receive(event):
            events.append(event)

        service.on_event = receive
        await service.start("sleep")
        await service._task
        self.assertEqual(service.status().phase, "completed", service.status().reason)
        operations = [e.operation for e in events if e.operation]
        self.assertEqual([o["state"] for o in operations], ["running", "completed", "running", "failed"])
        card = AnamesisCard.from_preview((await service.history("chat"))[0])
        text = card.render(expanded=True, width=80, color="green").plain
        for expected in ("code.py", "missing.py", "重复分支", "安排测试", "文件／来源读取明细"):
            self.assertIn(expected, text)
        self.assertIn("read1", card.operations)
        self.assertIn("read2", card.operations)
        await service.aclose()

    async def test_preview_is_bounded_full_trace_and_legacy_runs_remain_accessible(self):
        service = self.service()
        service.store.bind_run("abc", "chat")
        for index in range(40):
            await service._emit(
                AnamesisEvent(
                    kind="reasoning",
                    run_id="abc",
                    delta=str(index) + "x" * 1000,
                    operation={
                        "call_id": str(index),
                        "tool": "read",
                        "arguments": {"path": "code.py"},
                        "result": "y" * 5000,
                    },
                )
            )
        preview = (await service.history("chat"))[0]
        self.assertEqual(len(preview["reasoning"]), 12000)
        self.assertEqual(len(preview["operations"]), 32)
        self.assertTrue(all(len(o["result"]) == 2000 for o in preview["operations"].values()))
        trace = await service.run_record("abc", trace=True)
        self.assertEqual(len(trace.strip().split("\n")), 40)
        self.assertIn("y" * 5000, trace)
        legacy = service.store._run_path("old", "events.jsonl")
        legacy.parent.mkdir(parents=True)
        legacy.write_text('{"kind":"started"}\n', encoding="utf-8")
        self.assertEqual(len(await service.history("chat")), 1)
        self.assertEqual(len(await service.history()), 2)
        self.assertIn("started", await service.run_record("old", trace=True))
        self.assertEqual(service.store.bind_run("old", "chat"), ("", False))
        await service.aclose()

    async def test_preview_write_failure_and_partial_trace_tail_do_not_reuse_sequence(self):
        service = self.service()
        service.store.bind_run("abc", "chat")
        with (
            patch("logox.anamnesis.archives.write_json", side_effect=OSError("preview failed")),
            self.assertRaises(OSError),
        ):
            service.store.append_record("abc", {"kind": "reasoning", "run_id": "abc", "delta": "已收到"})
        second = service.store.append_record("abc", {"kind": "paused", "run_id": "abc", "phase": "paused"})
        self.assertEqual(second["sequence"], 2)
        path = service.store._run_path("abc", "events.jsonl")
        with path.open("a", encoding="utf-8") as handle:
            handle.write('{"kind":"partial"')
        from logox.anamnesis.archives import ArchiveStore

        reopened = ArchiveStore(self.home, self.project)
        third = reopened.append_record("abc", {"kind": "completed", "run_id": "abc", "phase": "completed"})
        self.assertEqual(third["sequence"], 3)
        records = path.read_text(encoding="utf-8").split("\n")
        self.assertEqual(json.loads(records[-2])["sequence"], 3)
        self.assertEqual(records[-3], '{"kind":"partial"')
        await service.aclose()

    async def test_another_window_failed_preview_invalidates_previous_instance_sequence_cache(self):
        from logox.anamnesis.archives import ArchiveStore

        service = self.service()
        first = service.store
        first.bind_run("abc", "chat")
        first.append_record("abc", {"kind": "started", "run_id": "abc"})
        second = ArchiveStore(self.home, self.project)
        with (
            patch("logox.anamnesis.archives.write_json", side_effect=OSError("preview failed")),
            self.assertRaises(OSError),
        ):
            second.append_record("abc", {"kind": "paused", "run_id": "abc"})
        result = first.append_record("abc", {"kind": "completed", "run_id": "abc"})
        self.assertEqual(result["sequence"], 3)
        trace = await service.run_record("abc", trace=True)
        self.assertEqual([json.loads(line)["sequence"] for line in trace.strip().split("\n")], [1, 2, 3])
        await service.aclose()

    async def test_failed_preview_keeps_trace_and_reports_nonempty_exception_type(self):
        service = self.service()

        async def fail(collector):
            raise TimeoutError()

        service.runner_factory = fail
        await service.start("nap")
        await service._task
        self.assertIn("TimeoutError", service.status().reason)
        self.assertIn("未提供详细说明", await service.latest_report())
        preview = service.store._run_path(service.status().run_id, "preview.json")
        preview.write_text("{broken", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "预览读取失败"):
            await service.history("chat")
        self.assertIn("TimeoutError", await service.run_record(service.status().run_id, trace=True))
        self.assertIn("预览读取失败", (await service.history())[0]["error"])
        with self.assertRaises(ValueError):
            await service.run_record("../abc", trace=True)
        await service.aclose()

    async def test_history_report_and_trace_commands_select_run_without_model_requests(self):
        provider = ProposalProvider()
        service = self.service(provider)
        await service.start("nap")
        await service._task
        run_id = service.status().run_id
        app = self.app(service)
        panels = []

        async def show(component):
            panels.append(component.text.plain)

        app.push_overlay = show
        for command in ("history", "report " + run_id, "trace " + run_id):
            await app.commands._cmd_anamnesis(command)
        self.assertIn(run_id, panels[0])
        self.assertIn("chat", panels[0])
        self.assertIn("阶段分析", panels[1])
        self.assertIn('"sequence"', panels[2])
        self.assertEqual(len(provider.requests), 2)
        app.stop()
        await service.aclose()

    async def test_busy_sibling_prevents_session_scan_before_run_admission(self):
        service = self.service()
        sibling = AnamesisCoordinator(self.home / "anamnesis", self.project, window_id="busy")
        sibling.update(eligible=False, idle=False, busy=True, last_submission=2)
        with patch.object(service.collector, "collect", side_effect=AssertionError("must not scan")):
            self.assertNotEqual(await service.start("nap"), "已开始入梦")
        self.assertIsNone(service._task)
        sibling.unregister()
        await service.aclose()

    async def test_disabled_sibling_is_registered_and_blocks_automatic_project_admission(self):
        inactive = self.service(owner="other-chat")
        inactive.config = AnamesisConfig(enabled=False)
        await inactive.start_background()
        ready = self.service()
        ready.clock = lambda: 100000
        ready._last_activity = ready._last_end = 0
        self.assertNotEqual(await ready.start("nap", manual=False), "已开始入梦")
        self.assertIsNone(ready._task)
        await inactive.aclose()
        self.assertEqual(await ready.start("nap", manual=False), "已开始入梦")
        await ready._task
        await ready.aclose()

    async def test_other_window_in_same_project_does_not_cancel_running_owner(self):
        provider = VisibleProvider()
        owner = self.service(provider)
        other = self.service(owner="other-chat")
        seen = asyncio.Event()

        async def receive(event):
            if event.delta:
                seen.set()

        owner.on_event = receive
        await owner.start("nap")
        await asyncio.wait_for(seen.wait(), 5)
        other.note_activity("submit")
        other.note_submission()
        self.assertTrue(owner.is_active)
        self.assertFalse(owner._stop.is_set())
        owner.note_activity("submit")
        await owner._task
        await owner.aclose()
        await other.aclose()

    async def test_streaming_reasoning_reuses_completed_analysis_layout(self):
        card = AnamesisCard("abc")
        for index in range(20):
            card.ingest(
                AnamesisEvent(
                    kind="analysis",
                    run_id="abc",
                    analysis=AnamesisAnalysisRecord(
                        record_id=str(index),
                        stage_id="s",
                        question="问题",
                        rationale="长说明" * 400,
                        conclusion="结论",
                    ),
                )
            )
        card.render(expanded=True, width=80, color="green")
        body = card._body
        card.ingest(AnamesisEvent(kind="reasoning", run_id="abc", delta="新思考片段"))
        rendered = card.render(expanded=True, width=80, color="green").plain
        self.assertIs(card._body, body)
        self.assertIn("新思考片段", rendered)
        self.assertIn("结论", rendered)

    async def test_closed_window_running_snapshot_does_not_keep_ticking(self):
        card = AnamesisCard.from_preview(
            {"run_id": "abc", "phase": "reviewing", "started_at": 10, "timestamp": 15, "sequence": 2}
        )
        self.assertEqual(card.phase, "interrupted")
        self.assertEqual(card.elapsed, 5)
        self.assertFalse(card.tick(200))


class ProjectQueueTests(TempCase):
    def coordinator(self, name, *, project=None, stamp=1, idle=True, busy=False, eligible=True):
        owner = AnamesisCoordinator(self.home / "anamnesis", project or self.project, window_id=name)
        owner.update(eligible=eligible, idle=idle, busy=busy, last_submission=stamp)
        self.addCleanup(owner.unregister)
        return owner

    def test_busy_or_recently_active_sibling_blocks_project_but_not_other_project(self):
        old = self.coordinator("old", stamp=1)
        recent = self.coordinator("recent", stamp=3, idle=False, busy=True, eligible=False)
        other = self.coordinator("other", project=self.root, stamp=2)
        self.assertIsNone(old.claim())
        self.assertIsNone(old.claim(manual=True))
        self.assertIsNotNone(other.claim())
        other.finish()
        recent.update(eligible=False, idle=False, busy=False, last_submission=3)
        self.assertIsNone(old.claim())
        recent.update(eligible=True, idle=True, busy=False, last_submission=3)
        self.assertIsNone(old.claim())
        self.assertIsNotNone(recent.claim())
        recent.finish()

    def test_latest_conversation_only_and_closed_or_expired_siblings_do_not_block(self):
        old = self.coordinator("old", stamp=1)
        recent = self.coordinator("recent", stamp=2, idle=False, eligible=False)
        self.assertIsNone(old.claim())
        recent.unregister()
        self.assertIsNotNone(old.claim())
        old.finish()
        recent.update(eligible=False, idle=False, busy=True, last_submission=2)
        with patch("logox.anamnesis.coordinator.time.time", return_value=9999999999):
            old.update(eligible=True, idle=True, busy=False, last_submission=1)
            self.assertIsNotNone(old.claim())
        old.finish()

    def test_submission_time_uses_user_event_not_later_model_output(self):
        writer = SessionTranscriptWriter(log_file=self.transcript)
        writer.write_step(
            turn=2, step=0, role="user", event_type="user_prompt", content="任务", timestamp=1200
        )
        writer.write_step(
            turn=2, step=1, role="assistant", event_type="model_output", content="输出", timestamp=9000
        )
        collector = SourceCollector(self.home / "sessions", self.project)
        self.assertEqual(collector.last_submission("chat"), 1200)
        writer.write_step(
            turn=0, step=0, role="", event_type="anamnesis_ref", run_id="abc", submission_ts=1300
        )
        self.assertEqual(collector.last_submission("chat"), 1300)

    def test_current_project_only_even_if_closed_project_has_user_facts(self):
        other = self.root / "other"
        other.mkdir()
        folder = SessionManager(self.home / "sessions").get_project_dir(other)
        folder.mkdir(parents=True)
        foreign = folder / "foreign.jsonl"
        foreign.write_text(
            json.dumps({"type": "session_init", "cwd": str(other)})
            + "\n"
            + json.dumps({"role": "user", "content": "其它项目私有资料"})
            + "\n",
            encoding="utf-8",
        )
        collector = SourceCollector(self.home / "sessions", self.project)
        read = collector._read_session
        with patch.object(collector, "_read_session", wraps=read) as observed:
            refs = collector.collect()
        self.assertTrue(refs)
        self.assertTrue(all(r.project_id == collector.project_id for r in refs))
        self.assertNotIn(str(foreign), [str(call.args[0]) for call in observed.call_args_list])


if __name__ == "__main__":
    unittest.main()
