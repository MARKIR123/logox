"""Real unified lifecycle, segmentation, recovery and visible self-check contracts."""

import asyncio
import json
import threading
import unittest

from logox.anamnesis.io import read_json, write_json
from logox.anamnesis.models import AnamesisEvent, AnamesisResearchItem
from logox.anamnesis.research import AnamesisResearchState
from logox.anamnesis.runner import AnamesisRunner
from logox.anamnesis.service import AnamesisService
from logox.config.schema import AnamesisConfig
from logox.providers.base import DeltaEvent, StopEvent, ToolCallEvent
from logox.tui.content.anamnesis import AnamesisCard
from tests.anamnesis.support import finish_calls, noop
from tests.anamnesis.test_runtime import ProposalProvider, TempCase


class Script:
    def __init__(self, respond):
        self.respond = respond
        self.requests = []

    async def stream(self, request):
        self.requests.append(request)
        response = self.respond(request, len(self.requests))
        if isinstance(response, list):
            for call in response:
                yield call
        else:
            yield DeltaEvent(kind="text", text=json.dumps(response, ensure_ascii=False))
        yield StopEvent(stop_reason="end_turn")


class UnifiedTests(TempCase, unittest.IsolatedAsyncioTestCase):
    def service(self, provider, window=32768):
        async def factory(collector):
            return AnamesisRunner(provider, "local", window, collector)

        service = AnamesisService(
            config=AnamesisConfig(model="local"),
            home=self.home,
            cwd=self.project,
            sessions=self.home / "sessions",
            runner_factory=factory,
            clock=lambda: 0,
        )
        service.clock = lambda: 1801
        service.current_session = lambda: "chat"
        self.addAsyncCleanup(service.aclose)
        return service

    async def segment(self, service, manual=True):
        self.assertEqual(await service.start(manual=manual), "已开始入梦")
        await asyncio.wait_for(service._task, 10)

    async def test_empty_complete_proposals_do_not_finish_or_advance_and_remind_across_segments(self):
        provider = Script(lambda request, step: {"complete": True, "changes": []})
        service = self.service(provider)
        for _ in range(9):
            await self.segment(service)
        self.assertEqual(service.status().phase, "paused")
        self.assertFalse(service.store.global_processed())
        self.assertFalse(service.store.progress()["processed"])
        trace = [
            json.loads(line)
            for line in (await service.run_record(service.status().run_id, trace=True)).split("\n")
            if line
        ]
        reminders = [e["self_check"] for e in trace if e["kind"] == "self_check"]
        self.assertEqual([r["paused"] for r in reminders], [False, False, True])
        self.assertTrue(any("请自检" in m.text for m in provider.requests[3].messages))
        self.assertTrue(any("请自检" in m.text for m in provider.requests[6].messages))
        self.assertIn("手动", await service.start(manual=False))
        self.assertEqual(len(provider.requests), 9)
        preview = (await service.history("chat"))[0]
        card = AnamesisCard.from_preview(preview)
        self.assertIn("研究事项", card.render(expanded=True, width=100, color="green").plain)
        self.assertIn("自检", card.render(expanded=True, width=100, color="green").plain)
        self.assertNotIn("小憩", card.render(expanded=False, width=100, color="green").plain)

    async def test_more_than_64_progressing_requests_have_no_fixed_step_cap(self):
        (self.project / "code.py").write_text("line\n" * 100, encoding="utf-8")
        service = self.service(None)
        collector = service.collector
        collector.collect()
        snapshot = collector.code_snapshot()
        state = AnamesisResearchState()
        state.add(
            [
                AnamesisResearchItem(
                    item_id="code",
                    question="核对相关文件片段",
                    reason="当前变化",
                    scope="research",
                    origin_kind="code_change",
                    code_snapshot_id=snapshot["fingerprint"],
                )
            ],
            source_ids=set(),
            code_snapshot_id=snapshot["fingerprint"],
            allow_roots=True,
        )

        def respond(request, step):
            if step <= 80:
                return [
                    ToolCallEvent(
                        call_id=str(step),
                        name="read",
                        arguments={"item_id": "code", "path": "code.py", "offset": step, "limit": 1},
                    )
                ]
            if step == 81:
                ref = next(reversed(collector.sources.values()))
                return [
                    ToolCallEvent(
                        call_id="a",
                        name="record_analysis",
                        arguments={
                            "item_id": "code",
                            "record_id": "a",
                            "stage_id": "review",
                            "question": "核对？",
                            "rationale": "以实际读取为依据",
                            "conclusion": "片段已确认",
                            "source_ids": [ref.source_id],
                        },
                    ),
                    ToolCallEvent(
                        call_id="u",
                        name="update_research",
                        arguments={
                            "item_id": "code",
                            "expected_version": 0,
                            "status": "resolved",
                            "analysis_ids": ["a"],
                            "source_ids": [ref.source_id],
                            "conclusion": "片段已确认",
                        },
                    ),
                ]
            return {"complete": True, "changes": []}

        provider = Script(respond)
        runner = AnamesisRunner(provider, "local", 200000, collector)
        args = dict(
            sources=[],
            snapshots={},
            research_state=state,
            stop=threading.Event(),
            emit=noop,
            operation=noop,
            state_event=noop,
            code_snapshot={"fingerprint": snapshot["fingerprint"]},
        )
        first = await runner.run_segment(**args)
        self.assertEqual(first.kind, "checkpoint")
        self.assertEqual(len(provider.requests), 81)
        self.assertTrue(state.finished)
        second = await runner.run_segment(**args)
        self.assertEqual(second.kind, "proposal")
        self.assertEqual(len(provider.requests), 82)
        self.assertTrue(all(r.max_tokens is None for r in provider.requests))

    async def test_legacy_checkpoint_is_archived_and_never_reused_as_new_completion(self):
        service = self.service(ProposalProvider())
        old = {
            "run_id": "old",
            "mode": "sleep",
            "session_id": "chat",
            "analyses": [],
            "automatic_continuation": True,
        }
        write_json(service._checkpoint_path, old)
        await self.segment(service)
        self.assertEqual(service.status().phase, "completed")
        self.assertNotEqual(service.status().run_id, "old")
        backups = list(service.store._path("checkpoints/previous").glob("*.json"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(read_json(backups[0], {}), old)

    async def test_complete_event_tail_is_recovered_but_partial_tail_cannot_complete(self):
        service = self.service(ProposalProvider(wait=True))
        await service.start()
        await service.runner_factory(service.collector)  # No provider request or source changes.
        while not service._live_checkpoint or not service._live_checkpoint.get("research_state", {}).get(
            "items"
        ):
            await asyncio.sleep(0)
        service.request_wake()
        await service._task
        saved = read_json(service._checkpoint_path, {})
        original_state = saved["research_state"]
        run_id = saved["run_id"]
        advanced = {**original_state, "version": original_state["version"] + 1}
        service.store.append_record(
            run_id,
            AnamesisEvent(
                schema_version=2,
                kind="research_progress",
                run_id=run_id,
                session_id="chat",
                research_state=advanced,
            ).model_dump(),
        )
        with service.store._run_path(run_id, "events.jsonl").open("a", encoding="utf-8") as handle:
            handle.write('{"kind":"completed"')
        recovered = service._recover_checkpoint(saved)
        self.assertEqual(recovered["research_state"]["version"], advanced["version"])
        self.assertFalse(AnamesisResearchState(recovered["research_state"]).finished)
        self.assertTrue(any("尾行" in issue for issue in service.collector.issues))

    async def test_initial_code_snapshot_does_not_swallow_changes_during_run(self):
        path = self.project / "code.py"
        path.write_text("before", encoding="utf-8")

        def respond(request, step):
            payload = json.loads(request.messages[0].text)
            path.write_text("after change", encoding="utf-8")
            return list(finish_calls(payload, {"complete": True, "changes": []}))

        service = self.service(Script(respond))
        initial = service.collector.code_snapshot()["fingerprint"]
        await self.segment(service)
        self.assertEqual(service.status().phase, "completed")
        self.assertEqual(service.store.progress()["code_fingerprint"], initial)
        self.assertNotEqual(initial, service.collector.code_fingerprint())

    async def test_meaningful_events_are_checkpointed_before_ui_callback(self):
        service = self.service(ProposalProvider())
        observed = []

        async def show(event):
            if event.kind in {"research_item", "analysis", "checkpoint"}:
                saved = read_json(service._checkpoint_path, {})
                self.assertGreaterEqual(saved["last_sequence"], event.sequence)
                self.assertEqual(saved["run_id"], event.run_id)
                observed.append(event.kind)

        service.on_event = show
        await self.segment(service)
        self.assertIn("research_item", observed)
        self.assertIn("analysis", observed)

    async def test_committed_md_without_result_event_is_recovered_without_duplicate_add(self):
        from unittest.mock import patch

        provider = ProposalProvider()
        service = self.service(provider)
        original = service.store.commit

        def interrupted(*args, **kwargs):
            original(*args, **kwargs)
            raise InterruptedError("写入已完成，但进程未记下返回结果")

        with patch.object(service.store, "commit", side_effect=interrupted):
            await self.segment(service)
        self.assertEqual(service.status().phase, "paused")
        self.assertFalse(service.store.global_processed())
        text = (self.home / "ANAMNESIS.md").read_text(encoding="utf-8")
        identity = service.status().run_id
        await self.segment(service)
        self.assertEqual(service.status().phase, "completed", service.status().reason)
        self.assertEqual(service.status().run_id, identity)
        self.assertEqual(len(provider.requests), 2)
        self.assertEqual(len(list(service.store._path("revisions").glob("*.json"))), 1)
        self.assertEqual((self.home / "ANAMNESIS.md").read_text(encoding="utf-8"), text)
        self.assertIn("已保存", await service.latest_report())
        self.assertEqual(len(service.store.global_processed()), 1)

    async def test_old_progress_migrates_without_losing_processed_sources(self):
        service = self.service(ProposalProvider())
        path = service.store._path(f"progress/{service.store.project_key}.json")
        write_json(path, {"processed": ["old-source"], "sleep_fingerprint": "old-code"})
        self.assertEqual(service.store.progress()["code_fingerprint"], "old-code")
        service.store.save_progress({"new-source"})
        saved = read_json(path, {})
        self.assertEqual(saved["schema_version"], 2)
        self.assertEqual(set(saved["processed"]), {"old-source", "new-source"})
        self.assertEqual(saved["code_fingerprint"], "old-code")
        self.assertNotIn("sleep_fingerprint", saved)

    async def test_read_clamps_actual_range_and_empty_file_cannot_fake_unbounded_progress(self):
        path = self.project / "code.py"
        path.write_text("only line", encoding="utf-8")
        service = self.service(None)
        runner = AnamesisRunner(None, "local", 32768, service.collector)
        first = json.loads(
            await runner._read_tool("read", {"path": "code.py", "limit": 100}, threading.Event())
        )
        second = json.loads(
            await runner._read_tool("read", {"path": "code.py", "limit": 200}, threading.Event())
        )
        self.assertEqual(first["end_line"], 1)
        self.assertEqual(first["source_id"], second["source_id"])
        with self.assertRaisesRegex(ValueError, "末尾"):
            await runner._read_tool("read", {"path": "code.py", "offset": 2}, threading.Event())
        path.write_text("", encoding="utf-8")
        empty = json.loads(await runner._read_tool("read", {"path": "code.py"}, threading.Event()))
        self.assertEqual((empty["line"], empty["end_line"]), (1, 1))
        with self.assertRaisesRegex(ValueError, "末尾"):
            await runner._read_tool("read", {"path": "code.py", "offset": 2}, threading.Event())

    async def test_input_budget_checkpoints_preserve_full_tool_batches_and_can_finish(self):
        (self.project / "code.py").write_text("line\n" * 100, encoding="utf-8")
        service = self.service(None)
        collector = service.collector
        collector.collect()
        snapshot = collector.code_snapshot()
        state = AnamesisResearchState()
        state.add(
            [
                AnamesisResearchItem(
                    item_id="code",
                    question="核对片段",
                    reason="当前变化",
                    scope="research",
                    origin_kind="code_change",
                    code_snapshot_id=snapshot["fingerprint"],
                )
            ],
            source_ids=set(),
            code_snapshot_id=snapshot["fingerprint"],
            allow_roots=True,
        )

        def respond(request, step):
            if step <= 60:
                return [
                    ToolCallEvent(
                        call_id=str(step),
                        name="read",
                        arguments={"item_id": "code", "path": "code.py", "offset": step, "limit": 1},
                    )
                ]
            if step == 61:
                return list(
                    finish_calls(json.loads(request.messages[0].text), {"complete": True, "changes": []})
                )
            return {"complete": True, "changes": []}

        provider = Script(respond)
        runner = AnamesisRunner(provider, "local", 10000, collector)
        segments = []
        operations = []

        async def operation(value):
            operations.append(value)

        for _ in range(20):
            result = await runner.run_segment(
                sources=[],
                snapshots={},
                research_state=state,
                stop=threading.Event(),
                emit=noop,
                operation=operation,
                state_event=noop,
                code_snapshot={"fingerprint": snapshot["fingerprint"]},
            )
            segments.append(result.kind)
            if result.kind == "proposal":
                break
        self.assertEqual(segments[-1], "proposal")
        self.assertGreater(segments.count("checkpoint"), 1)
        self.assertTrue(state.finished)
        for call_id in {op["call_id"] for op in operations}:
            self.assertEqual(
                [op["state"] for op in operations if op["call_id"] == call_id], ["running", "completed"]
            )
        self.assertEqual(len(provider.requests), 61)

    async def test_unknown_checkpoint_version_is_retained(self):
        service = self.service(ProposalProvider())
        value = {"schema_version": 99, "run_id": "future"}
        write_json(service._checkpoint_path, value)
        self.assertIn("不支持", await service.start())
        self.assertEqual(read_json(service._checkpoint_path, {}), value)

    async def test_obsolete_config_warns_with_deletion_instruction_and_strict_rejects(self):
        from logox.config.loader import load
        from logox.errors import ConfigValidationError
        from logox.paths import LogoxPaths

        paths = LogoxPaths.at(self.home)
        paths.config.write_text(
            '[anamnesis]\nmodel="local"\nnap_max_steps=4\nsleep_start="00:00"\n', encoding="utf-8"
        )
        bundle = load(self.project, paths=paths, project_chain=[], env={})
        warnings = [
            issue for issue in bundle.warnings if issue.field and issue.field.startswith("anamnesis.")
        ]
        self.assertEqual(len(warnings), 2)
        self.assertTrue(all("已废止" in issue.message and "删除" in issue.message for issue in warnings))
        self.assertEqual(bundle.config.anamnesis.model, "local")
        with self.assertRaises(ConfigValidationError):
            load(self.project, paths=paths, project_chain=[], env={}, strict=True)
