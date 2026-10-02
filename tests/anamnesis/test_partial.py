"""Partial acceptance keeps audit/progress without promoting stale project snapshots."""

from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from logox.anamnesis.models import AnamesisAnalysisRecord, MemoryChange, MemoryProposal
from logox.anamnesis.runner import AnamesisRunner
from logox.anamnesis.service import AnamesisService
from logox.anamnesis.sources import SourceCollector
from logox.config.schema import AnamesisConfig
from logox.kernel.bus import EventBus
from logox.providers.base import DeltaEvent, StopEvent
from logox.tui.render.app import InlineApp
from logox.tui.render.fullscreen import FullscreenApp
from logox.tui.render.keys import Key
from logox.tui.render.terminal import FakeTerminal
from tests.anamnesis.test_runtime import TempCase


class MixedProvider:
    def __init__(self, *, all_candidates=False, semantic_reject=False, bad_review=False):
        self.requests = []
        self.all_candidates = all_candidates
        self.semantic_reject = semantic_reject
        self.bad_review = bad_review

    async def stream(self, request):
        self.requests.append(request)
        payload = json.loads(request.messages[0].text)
        if "changes" in payload:
            response = (
                {"accepted_entry_ids": [], "reasons": {"user.learning": "语义证据不足"}}
                if self.semantic_reject
                else {"accepted_entry_ids": [c["entry_id"] for c in payload["changes"]], "reasons": {}}
            )
            if self.bad_review:
                response = {"unexpected": True}
        else:
            refs = payload["sources"]
            user = next((r for r in refs if r["kind"] == "user_message"), None)
            assistant = next((r for r in refs if r["kind"] == "assistant_statement"), None)
            analysis_id = "analysis." + refs[0]["source_id"]
            analysis = AnamesisAnalysisRecord(
                record_id=analysis_id,
                stage_id="review",
                question="哪些内容有依据？",
                rationale="区分用户原话、候选和模型转述",
                conclusion="只采纳有依据的内容",
                source_ids=[r["source_id"] for r in refs],
            )
            changes = []
            if user:
                changes.append(
                    MemoryChange(
                        entry_id="user.learning",
                        scope="user",
                        action="add",
                        new_value="目前学习 AI Agent。",
                        source_ids=[user["source_id"]],
                        rationale="用户明确表述",
                        analysis_record_id=analysis_id,
                        status="candidate" if self.all_candidates else "explicit",
                    )
                )
                changes.append(
                    MemoryChange(
                        entry_id="user.guess",
                        scope="user",
                        action="add",
                        new_value="可能是算法工程师",
                        source_ids=[user["source_id"]],
                        rationale="没有明确证据",
                        analysis_record_id=analysis_id,
                        status="candidate",
                    )
                )
            if assistant:
                changes.append(
                    MemoryChange(
                        entry_id="known_gaps",
                        scope="project",
                        action="add",
                        new_value="模型总结的缺口清单",
                        source_ids=[assistant["source_id"]],
                        rationale="仅模型转述",
                        analysis_record_id=analysis_id,
                        status="observed",
                    )
                )
            response = MemoryProposal(analyses=[analysis], changes=changes).model_dump()
        yield DeltaEvent(kind="text", text=json.dumps(response, ensure_ascii=False))
        yield StopEvent(stop_reason="end_turn")


class PartialAcceptanceTests(TempCase, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.append(
            {"role": "assistant", "turn": 1, "content": "旧会话：所有功能已完成，缺口是工具卡。", "ts": 1001}
        )

    def append(self, record):
        with self.transcript.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False) + "\n")

    def service(self, provider, window=32768):
        async def factory(collector):
            return AnamesisRunner(provider, "local", window, collector)

        service = AnamesisService(
            config=AnamesisConfig(model="local"),
            home=self.home,
            cwd=self.project,
            sessions=self.home / "sessions",
            runner_factory=factory,
        )
        service.current_session = lambda: "chat"
        return service

    async def finish(self, service):
        self.assertEqual(await service.start("nap"), "已开始入梦")
        await asyncio.wait_for(service._task, 5)

    async def test_mixed_acceptance_saves_only_approved_and_keeps_candidates_in_history(self):
        provider = MixedProvider()
        service = self.service(provider)
        self.addAsyncCleanup(service.aclose)
        await self.finish(service)
        self.assertEqual(service.status().phase, "completed", service.status().reason)
        self.assertIn("部分采纳", service.status().reason)
        archive = (self.home / "ANAMNESIS.md").read_text(encoding="utf-8")
        self.assertIn("AI Agent", archive)
        self.assertNotIn("算法工程师", archive)
        self.assertFalse((self.project / "ANAMNESIS.md").exists())
        self.assertEqual(len(service.store.global_processed()), 2)
        self.assertEqual(len(service.store.progress()["processed"]), 2)
        report = await service.latest_report()
        self.assertIn("候选（未入档）", report)
        self.assertIn("仅引用模型叙述", report)
        self.assertIn("原提案状态：observed", report)
        self.assertNotIn("模型声称完成不是成功依据", report)
        history = await service.history("chat")
        candidates = [e for e in history[0]["events"] if e.change and e.change.status == "candidate"]
        self.assertEqual(len(candidates), 2)
        self.assertEqual(await service.start("nap"), "无可整理内容")
        self.assertEqual(len(provider.requests), 2)

    async def test_all_candidates_are_completed_review_and_do_not_retry_unchanged_sources(self):
        provider = MixedProvider(all_candidates=True)
        service = self.service(provider)
        self.addAsyncCleanup(service.aclose)
        await self.finish(service)
        self.assertEqual(service.status().phase, "completed", service.status().reason)
        self.assertIn("无可采纳记忆", service.status().reason)
        self.assertFalse((self.home / "ANAMNESIS.md").exists())
        self.assertFalse((self.project / "ANAMNESIS.md").exists())
        self.assertEqual(len(service.store.global_processed()), 2)
        self.assertEqual(await service.start("nap"), "无可整理内容")
        self.assertEqual(len(provider.requests), 1)

    async def test_semantic_rejection_is_candidate_but_bad_review_contract_still_fails(self):
        provider = MixedProvider(semantic_reject=True)
        service = self.service(provider)
        self.addAsyncCleanup(service.aclose)
        await self.finish(service)
        self.assertEqual(service.status().phase, "completed", service.status().reason)
        self.assertIn("语义证据不足", await service.latest_report())
        self.assertFalse((self.home / "ANAMNESIS.md").exists())
        # New evidence triggers another batch; malformed review is a real protocol failure.
        self.append({"role": "user", "turn": 2, "content": "仍然学习 AI Agent", "ts": 2000})
        provider.semantic_reject, provider.bad_review = False, True
        await self.finish(service)
        self.assertEqual(service.status().phase, "failed")
        self.assertIn("核验响应不合约", service.status().reason)
        self.assertEqual(len(service.store.global_processed()), 2)

    async def test_empty_refusal_reason_is_candidate_with_an_explicit_fallback(self):
        class EmptyReasonProvider(MixedProvider):
            async def stream(self, request):
                if "changes" in json.loads(request.messages[0].text):
                    self.requests.append(request)
                    yield DeltaEvent(
                        kind="text",
                        text=json.dumps({"accepted_entry_ids": [], "reasons": {"user.learning": "  "}}),
                    )
                    yield StopEvent(stop_reason="end_turn")
                else:
                    async for event in super().stream(request):
                        yield event

        service = self.service(EmptyReasonProvider())
        self.addAsyncCleanup(service.aclose)
        await self.finish(service)
        self.assertEqual(service.status().phase, "completed", service.status().reason)
        report = await service.latest_report()
        self.assertIn("语义核验拒绝，未提供具体原因", report)
        self.assertIn("user / user.learning：候选（未入档）", report)
        self.assertFalse((self.home / "ANAMNESIS.md").exists())

    async def test_reviewer_unknown_accepted_identity_is_a_protocol_failure(self):
        class UnknownIdProvider(MixedProvider):
            async def stream(self, request):
                if "changes" in json.loads(request.messages[0].text):
                    self.requests.append(request)
                    yield DeltaEvent(
                        kind="text", text=json.dumps({"accepted_entry_ids": ["unknown"], "reasons": {}})
                    )
                    yield StopEvent(stop_reason="end_turn")
                else:
                    async for event in super().stream(request):
                        yield event

        service = self.service(UnknownIdProvider())
        self.addAsyncCleanup(service.aclose)
        await self.finish(service)
        self.assertEqual(service.status().phase, "failed")
        self.assertIn("核验响应包含未送审条目", service.status().reason)
        self.assertFalse(service.store.global_processed())
        self.assertFalse((self.home / "ANAMNESIS.md").exists())

    async def test_external_archive_edit_is_not_bypassed_by_partial_acceptance(self):
        service = self.service(MixedProvider())
        self.addAsyncCleanup(service.aclose)
        original = service.store.commit

        def conflict(*args, **kwargs):
            (self.home / "ANAMNESIS.md").write_text("用户手工档案", encoding="utf-8")
            return original(*args, **kwargs)

        with patch.object(service.store, "commit", side_effect=conflict):
            await self.finish(service)
        self.assertEqual(service.status().phase, "failed")
        self.assertEqual((self.home / "ANAMNESIS.md").read_text(encoding="utf-8"), "用户手工档案")
        self.assertFalse(service.store.global_processed())

    async def test_candidate_batch_continues_to_later_evidence_and_preserves_original_result(self):
        self.transcript.write_text(
            json.dumps({"type": "session_init", "cwd": str(self.project)}) + "\n", encoding="utf-8"
        )
        self.append({"role": "assistant", "turn": 1, "content": "旧模型结论" + "x" * 1900, "ts": 1000})
        self.append(
            {"role": "user", "turn": 2, "content": "我目前学习 AI Agent。" + "补充" * 275, "ts": 2000}
        )

        class SameEntryProvider(MixedProvider):
            async def stream(self, request):
                async for event in super().stream(request):
                    if isinstance(event, DeltaEvent):
                        response = json.loads(event.text)
                        if "changes" in response:
                            change = response["changes"][0]
                            change.update(entry_id="known_gaps", scope="project", status="observed")
                            response["changes"] = [change]
                            event = DeltaEvent(kind="text", text=json.dumps(response, ensure_ascii=False))
                    yield event

        service = self.service(SameEntryProvider(), window=8000)
        self.addAsyncCleanup(service.aclose)
        await self.finish(service)
        run_id = service.status().run_id
        self.assertEqual(service.status().phase, "continuing", service.status().reason)
        self.assertGreater(service.status().remaining, 0)
        self.assertEqual(len(service.store.global_processed()), 1)
        self.assertTrue(
            json.loads(service._checkpoint_path.read_text(encoding="utf-8"))["automatic_continuation"]
        )
        self.assertIn("候选（未入档）", await service.latest_report())
        await self.finish(service)
        self.assertEqual(service.status().phase, "completed", service.status().reason)
        self.assertEqual(service.status().run_id, run_id)
        self.assertEqual(len(service.store.global_processed()), 2)
        self.assertIn("AI Agent", (self.project / "ANAMNESIS.md").read_text(encoding="utf-8"))
        report = await service.latest_report()
        self.assertEqual(report.count("project / known_gaps"), 2)
        self.assertIn("project / known_gaps：候选（未入档）", report)
        self.assertIn("project / known_gaps：已保存", report)

    async def test_partial_result_restores_and_displays_in_both_modes(self):
        service = self.service(MixedProvider())
        await self.finish(service)
        await service.aclose()
        for app_type in (InlineApp, FullscreenApp):
            reopened = self.service(MixedProvider())
            app = app_type(
                runtime=SimpleNamespace(
                    kernel=SimpleNamespace(cancel=lambda: True),
                    bus=EventBus(session_id="chat"),
                    config=SimpleNamespace(),
                    model="local",
                    anamnesis=reopened,
                ),
                terminal=FakeTerminal(columns=100, rows=30),
            )
            try:
                await app._restore_anamnesis()
                app._dispatch(Key("t", ctrl=True))
                output = "\n".join(row.plain for row in app.timeline.render(96))
                self.assertIn("部分采纳", output)
                self.assertIn("候选（未入档）", output)
                self.assertIn("已保存", output)
                self.assertNotIn("入梦失败", output)
            finally:
                app.stop()
                await reopened.aclose()


class FreshnessTests(TempCase, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        with self.transcript.open("a", encoding="utf-8") as file:
            for record in (
                {"role": "tool", "turn": 1, "content": "旧 git 状态快照", "ts": 1100},
                {"role": "user", "turn": 2, "content": "项目已经继续修改", "ts": 2000},
                {"role": "assistant", "turn": 2, "content": "新回复复述旧 git 状态", "ts": 2100},
            ):
                file.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.collector = SourceCollector(self.home / "sessions", self.project)
        self.refs = self.collector.collect()
        self.provider = MixedProvider()
        self.runner = AnamesisRunner(self.provider, "local", 32768, self.collector)

    def proposal(self, refs, scope="project"):
        ids = [r.source_id for r in refs]
        analysis = AnamesisAnalysisRecord(
            record_id="a",
            stage_id="s",
            question="当前状态？",
            rationale="核对来源",
            conclusion="提出待核验事实",
            source_ids=ids,
        )
        change = MemoryChange(
            entry_id="project.state" if scope == "project" else "user.learning",
            scope=scope,
            action="add",
            new_value="状态",
            source_ids=ids,
            rationale="来源记录",
            analysis_record_id="a",
            status="observed" if scope == "project" else "explicit",
        )
        return MemoryProposal(analyses=[analysis], changes=[change])

    async def test_old_tool_plus_new_assistant_cannot_make_historical_state_current(self):
        old = next(r for r in self.refs if r.kind == "tool_result")
        new = next(r for r in self.refs if r.kind == "assistant_statement")
        accepted, rejected = await self.runner.validate(self.proposal([old, new]), {})
        self.assertFalse(accepted)
        self.assertIn("旧会话状态", rejected["project.state"])
        self.assertFalse(self.provider.requests)

    async def test_latest_real_source_can_be_reviewed_and_cutoff_is_in_review_request(self):
        latest = next(r for r in self.refs if r.kind == "user_message" and r.timestamp == 2000)
        accepted, rejected = await self.runner.validate(self.proposal([latest]), {})
        self.assertEqual(len(accepted), 1)
        self.assertFalse(rejected)
        payload = json.loads(self.provider.requests[0].messages[0].text)
        self.assertEqual(payload["project_latest_timestamp"], 2000)

    async def test_current_file_can_revalidate_old_evidence_but_changed_file_is_rejected(self):
        path = self.project / "code.py"
        path.write_text("state = 'current'\n", encoding="utf-8")
        ref = self.collector.register_code(path, path.read_text(encoding="utf-8"), 1, 1, "state = 'current'")
        old = next(r for r in self.refs if r.kind == "tool_result")
        accepted, rejected = await self.runner.validate(self.proposal([old, ref]), {})
        self.assertEqual(len(accepted), 1)
        self.assertFalse(rejected)
        path.write_text("state = 'changed'\n", encoding="utf-8")
        accepted, rejected = await self.runner.validate(self.proposal([old, ref]), {})
        self.assertFalse(accepted)
        self.assertEqual(rejected["project.state"], "来源已变化或回滚")

    async def test_unknown_project_time_stays_candidate_but_old_user_preference_is_not_aged(self):
        unknown = self.refs[0].model_copy(update={"timestamp": 0})
        self.collector.sources[unknown.source_id] = unknown
        accepted, rejected = await self.runner.validate(self.proposal([unknown]), {})
        self.assertFalse(accepted)
        self.assertIn("时间不明确", rejected["project.state"])
        accepted, rejected = await self.runner.validate(self.proposal([unknown], scope="user"), {})
        self.assertEqual(len(accepted), 1)
        self.assertFalse(rejected)
