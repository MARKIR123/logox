"""Offline contracts through the real runner, service, memory loader and both TUIs."""

from __future__ import annotations

import asyncio
import json
import threading
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

from logox.anamnesis.archives import ArchiveStore
from logox.anamnesis.coordinator import AnamesisCoordinator
from logox.anamnesis.local import local_base_url, make_local_runner
from logox.anamnesis.models import AnamesisAnalysisRecord, AnamesisEvent, MemoryChange, MemoryProposal
from logox.anamnesis.runner import AnamesisRunner
from logox.anamnesis.service import AnamesisService
from logox.anamnesis.sources import SourceCollector
from logox.config.schema import AnamesisConfig
from logox.context.anamnesis import AnamesisMemory
from logox.context.builder import HierarchicalContextBuilder
from logox.context.storage import SessionTranscriptWriter
from logox.context.tokens import estimate_text_tokens
from logox.kernel.bus import EventBus
from logox.kernel.messages import user_message
from logox.providers.base import ChatRequest, DeltaEvent, StopEvent, ToolCallEvent
from logox.store.manager import SessionManager
from logox.tui.render.app import InlineApp
from logox.tui.render.fullscreen import FullscreenApp
from logox.tui.render.keys import Key
from logox.tui.render.terminal import FakeTerminal
from tests.anamnesis.support import finish_calls, noop, research
from tests.unit.support import make_temp_dir, remove_temp_dir


class TempCase(unittest.TestCase):
    def setUp(self):
        self.root = make_temp_dir("anamnesis-runtime-")
        self.addCleanup(remove_temp_dir, self.root)
        self.home, self.project = self.root / "home", self.root / "project"
        self.home.mkdir()
        self.project.mkdir()
        (self.project / ".git").mkdir()
        directory = SessionManager(self.home / "sessions").get_project_dir(self.project)
        directory.mkdir(parents=True)
        self.transcript = directory / "chat.jsonl"
        self.transcript.write_text(
            json.dumps({"type": "session_init", "cwd": str(self.project)})
            + "\n"
            + json.dumps({"role": "user", "turn": 1, "content": "我目前学习 AI Agent。", "ts": 1000})
            + "\n",
            encoding="utf-8",
        )


class ProposalProvider:
    def __init__(self, *, wait=False, complete=True):
        self.requests = []
        self.wait, self.complete = wait, complete
        self.entered = asyncio.Event()
        self.closed = False

    async def stream(self, request):
        self.requests.append(request)
        self.entered.set()
        try:
            if self.wait:
                await asyncio.Event().wait()
            payload = json.loads(request.messages[0].text)
            if "changes" in payload:
                response = {"accepted_entry_ids": [c["entry_id"] for c in payload["changes"]], "reasons": {}}
            else:
                ref = payload["sources"][0]
                analysis = AnamesisAnalysisRecord(
                    item_id=payload["current_item"]["item_id"],
                    record_id="learning",
                    stage_id="portrait",
                    question="当前学习方向是什么？",
                    rationale="用户明确说目前学习 AI Agent；不能推断其工作岗位或长期职业。",
                    evidence_summary=ref["content"],
                    source_ids=[ref["source_id"]],
                    conclusion="只记录当前学习状态",
                    scope="user",
                    decision="propose",
                )
                change = MemoryChange(
                    entry_id="user.learning",
                    action="add",
                    scope="user",
                    new_value="目前学习 AI Agent。",
                    source_ids=[ref["source_id"]],
                    rationale="明确的用户原话",
                    analysis_record_id="learning",
                    status="explicit",
                )
                entries = payload["archives"]["user"]["entries"]
                response = MemoryProposal(
                    analyses=[analysis], changes=[] if entries else [change], complete=self.complete
                ).model_dump()
            if "changes" not in payload:
                current = payload.get("current_item")
                if current and response["complete"]:
                    yield ToolCallEvent(call_id="a", name="record_analysis", arguments=analysis.model_dump())
                    yield ToolCallEvent(
                        call_id="u",
                        name="update_research",
                        arguments={
                            "item_id": current["item_id"],
                            "expected_version": current["version"],
                            "status": "resolved",
                            "analysis_ids": [analysis.record_id],
                            "source_ids": analysis.source_ids,
                            "conclusion": analysis.conclusion,
                        },
                    )
                    for item in payload.get("research_items", []):
                        if item["item_id"] == current["item_id"]:
                            continue
                        code_analysis = analysis.model_copy(
                            update={
                                "record_id": "code.analysis",
                                "item_id": item["item_id"],
                                "scope": "research",
                                "conclusion": "代码尚未核查，留待真实资料",
                                "source_ids": [],
                            }
                        )
                        yield ToolCallEvent(
                            call_id="ca", name="record_analysis", arguments=code_analysis.model_dump()
                        )
                        yield ToolCallEvent(
                            call_id="cu",
                            name="update_research",
                            arguments={
                                "item_id": item["item_id"],
                                "expected_version": item["version"],
                                "status": "waiting_evidence",
                                "analysis_ids": [code_analysis.record_id],
                                "missing_evidence": "离线测试不研究代码",
                            },
                        )
                    response["analyses"] = []
                    yield ToolCallEvent(call_id="p", name="propose_memory", arguments=response)
                else:
                    yield DeltaEvent(kind="text", text=json.dumps(response, ensure_ascii=False))
            else:
                yield DeltaEvent(kind="text", text=json.dumps(response, ensure_ascii=False))
            yield StopEvent(stop_reason="end_turn")
        finally:
            self.closed = True


class ServiceTests(TempCase, unittest.IsolatedAsyncioTestCase):
    def make_service(self, provider, **kwargs):
        async def factory(collector):
            return AnamesisRunner(provider, "test-local", 32768, collector)

        return AnamesisService(
            config=AnamesisConfig(model="test-local"),
            home=self.home,
            cwd=self.project,
            sessions=self.home / "sessions",
            runner_factory=factory,
            **kwargs,
        )

    async def test_complete_real_loop_archives_audit_report_and_no_repeat(self):
        provider = ProposalProvider()
        service = self.make_service(provider)
        events = []

        async def emit(event):
            events.append(event)

        service.on_event = emit
        self.assertEqual(await service.start(), "已开始入梦")
        await service._task
        self.assertEqual(service.status().phase, "completed")
        self.assertIn("目前学习 AI Agent", (self.home / "ANAMNESIS.md").read_text(encoding="utf-8"))
        self.assertFalse((self.project / "ANAMNESIS.md").exists())
        self.assertEqual(len(provider.requests), 2)  # Independent semantic review.
        self.assertEqual(await service.start(), "无可整理内容")
        report = await service.latest_report()
        self.assertIn("用户明确说", report)
        self.assertIn("已保存", report)
        self.assertIn("只读", report)
        self.assertTrue(any(e.kind == "committed" for e in events))
        self.assertTrue(list((self.home / "anamnesis" / "revisions").glob("*.json")))
        await service.aclose()

    async def test_submission_cancels_stream_and_preserves_same_run_on_resume(self):
        provider = ProposalProvider(wait=True)
        service = self.make_service(provider)
        await service.start()
        await provider.entered.wait()
        run_id = service.status().run_id
        service.note_activity("view")
        self.assertTrue(service.is_active)
        service.note_activity("submit")
        service.note_activity("submit")  # Repeated submissions cannot cancel cleanup a second time.
        await service._task
        self.assertTrue(provider.closed)
        self.assertFalse((self.home / "ANAMNESIS.md").exists())
        self.assertEqual(service.status().phase, "paused")
        provider.wait = False
        await service.start()
        await service._task
        self.assertNotEqual(service.status().run_id, run_id)
        self.assertEqual(service.status().phase, "completed")
        await service.aclose()

    async def test_idle_uses_later_activity_or_foreground_end_and_no_daybreak_cancel(self):
        now, local = [0], [datetime(2026, 9, 30, 0)]
        provider = ProposalProvider(wait=True)
        service = self.make_service(provider, clock=lambda: now[0], wall_clock=lambda: local[0])
        now[0] = 1801
        self.assertTrue(service.eligible())
        service.note_foreground_state(True)
        self.assertFalse(service.eligible())
        now[0] = 2000
        service.note_foreground_state(False)
        now[0] = 3800
        self.assertFalse(service.eligible())
        now[0] = 3801
        self.assertTrue(service.eligible())
        self.assertFalse(hasattr(service, "current_mode"))
        await service.start()
        await provider.entered.wait()
        local[0] = datetime(2026, 9, 30, 9)
        self.assertEqual(service.status().mode, "")
        self.assertTrue(service.is_active)
        self.assertEqual(service.status().mode, "")
        await service.aclose()
        self.assertFalse(service.is_active)
        self.assertIsNone(service.coordinator._lease)

    async def test_concurrent_start_and_terminal_close_release_run(self):
        provider = ProposalProvider(wait=True)
        service = self.make_service(provider)
        results = await asyncio.gather(service.start(), service.start())
        self.assertEqual(results.count("已开始入梦"), 1)
        await provider.entered.wait()
        await service.aclose()
        contender = AnamesisCoordinator(self.home / "anamnesis", self.project)
        contender.update(eligible=True, busy=False)
        self.assertIsNotNone(contender.claim(manual=True))
        contender.unregister()

    async def test_failed_model_does_not_update_any_archive_or_progress(self):
        async def factory(collector):
            raise ValueError("本地端点没有实际窗口")

        service = self.make_service(ProposalProvider())
        service.runner_factory = factory
        await service.start()
        await service._task
        self.assertEqual(service.status().phase, "failed")
        self.assertEqual(service.store.global_processed(), set())
        self.assertFalse((self.home / "ANAMNESIS.md").exists())
        self.assertIn("没有实际窗口", await service.latest_report())
        await service.aclose()

    async def test_step_checkpoint_keeps_unprocessed_sources(self):
        service = self.make_service(ProposalProvider(complete=False))
        await service.start()
        await service._task
        self.assertEqual(service.status().phase, "paused")
        self.assertEqual(service.store.global_processed(), set())
        self.assertTrue(service._checkpoint_path.exists())
        await service.aclose()

    async def test_automatic_unified_continuation_has_no_daybreak_boundary(self):
        class BatchProvider(ProposalProvider):
            async def stream(self, request):
                self.requests.append(request)
                for call in finish_calls(
                    json.loads(request.messages[0].text), MemoryProposal(complete=True).model_dump()
                ):
                    yield call
                yield StopEvent(stop_reason="end_turn")

        for turn, text in ((2, "甲" * 600), (3, "乙" * 600)):
            with self.transcript.open("a", encoding="utf-8") as file:
                file.write(
                    json.dumps({"role": "user", "turn": turn, "content": text, "ts": 1000 + turn}) + "\n"
                )
        provider = BatchProvider()
        local = [datetime(2026, 9, 30, 1)]
        clock = [0]
        service = self.make_service(provider, wall_clock=lambda: local[0], clock=lambda: clock[0])

        async def factory(collector):
            return AnamesisRunner(provider, "test-local", 10000, collector)

        service.runner_factory = factory
        self.addAsyncCleanup(service.aclose)
        clock[0] = 1801
        await service.start(manual=False)
        await service._task
        run_id = service.status().run_id
        self.assertEqual(service.status().phase, "continuing")
        local[0] = datetime(2026, 9, 30, 9)
        await service.start(manual=False)
        await service._task
        self.assertEqual(service.status().mode, "")
        self.assertEqual(service.status().run_id, run_id)
        for _ in range(4):
            if service.status().phase == "completed":
                break
            await service.start(manual=False)
            await service._task
        self.assertEqual(len(service.store.global_processed()), 3)

    async def test_wake_during_commit_worker_keeps_lock_until_worker_stops(self):
        service = self.make_service(ProposalProvider())
        entered, release = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()
        original = service.store.commit

        def delayed(*args, **kwargs):
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(5):
                raise TimeoutError("test release missing")
            return original(*args, **kwargs)

        service.store.commit = delayed
        await service.start()
        await asyncio.wait_for(entered.wait(), 5)
        service.note_activity("submit")
        contender = AnamesisCoordinator(self.home / "anamnesis", self.root)
        contender.update(eligible=True, busy=False)
        self.assertIsNone(contender.claim(manual=True))
        release.set()
        await service._task
        self.assertFalse((self.home / "ANAMNESIS.md").exists())
        self.assertFalse(service.store.global_processed())
        self.assertIsNotNone(contender.claim(manual=True))
        contender.unregister()
        await service.aclose()

    async def test_cancel_before_task_first_instruction_releases_lease(self):
        service = self.make_service(ProposalProvider(wait=True))
        await service.start()
        service.note_activity("submit")
        await asyncio.gather(service._task, return_exceptions=True)
        self.assertIsNone(service.coordinator._lease)
        await service.aclose()

    async def test_other_window_submission_cannot_wake_owner(self):
        provider = ProposalProvider(wait=True)
        owner = self.make_service(provider)
        other_project = self.root / "other"
        other_project.mkdir()
        other = AnamesisService(
            config=AnamesisConfig(model="test-local"),
            home=self.home,
            cwd=other_project,
            sessions=self.home / "sessions",
            runner_factory=None,
        )
        await owner.start()
        await provider.entered.wait()
        other.note_activity("submit")
        self.assertTrue(owner.is_active)
        self.assertFalse(owner._stop.is_set())
        await owner.aclose()
        await other.aclose()


class RunnerTests(TempCase, unittest.IsolatedAsyncioTestCase):
    def runner(self, provider=None, permission=None):
        collector = SourceCollector(self.home / "sessions", self.project)
        collector.collect()
        return AnamesisRunner(provider or ProposalProvider(), "test", 32768, collector, permission=permission)

    async def test_read_only_tools_reject_shell_sensitive_legacy_and_other_project(self):
        runner = self.runner()
        path = self.project / "code.py"
        path.write_text("value = 1\n", encoding="utf-8")
        result = json.loads(await runner._read_tool("read", {"path": "code.py"}, threading.Event()))
        self.assertTrue(runner.collector.verify(result["source_id"]))
        path.write_text("value = 2\n", encoding="utf-8")
        self.assertFalse(runner.collector.verify(result["source_id"]))
        for name, args in (
            ("shell", {"command": "echo hello"}),
            ("read", {"path": "../home/x"}),
            ("read", {"path": "docs/modules/LEGACY/x.md"}),
            ("read", {"path": ".env"}),
        ):
            with self.assertRaises(ValueError):
                await runner._read_tool(name, args, threading.Event())

    async def test_ask_permission_is_denied_without_prompt(self):
        permission = SimpleNamespace(
            evaluate=lambda *args, **kwargs: SimpleNamespace(decision="ask", reason="尚未授权")
        )
        runner = self.runner(permission=permission)
        (self.project / "code.py").write_text("pass", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "无人值守"):
            await runner._read_tool("read", {"path": "code.py"}, threading.Event())

    async def test_grep_and_glob_hide_legacy_and_generated_archive(self):
        runner = self.runner()
        legacy = self.project / "docs" / "LEGACY"
        legacy.mkdir(parents=True)
        for path in (legacy / "old.py", self.project / "ANAMNESIS.md", self.project / "valid.py"):
            path.write_text("needle\n", encoding="utf-8")
        glob = await runner._read_tool("glob", {"pattern": "**/*"}, threading.Event())
        grep = await runner._read_tool("grep", {"pattern": "needle"}, threading.Event())
        for result in (glob, grep):
            self.assertIn("valid.py", result)
            self.assertNotIn("old.py", result)
            self.assertNotIn("ANAMNESIS.md", result)

    async def test_cross_project_and_inferred_user_changes_never_reach_reviewer(self):
        runner = self.runner()
        ref = next(iter(runner.collector.sources.values()))
        analysis = AnamesisAnalysisRecord(
            record_id="a", stage_id="s", question="职业？", rationale="信息不足", conclusion="待确认"
        )
        changes = [
            MemoryChange(
                entry_id="user.job",
                scope="user",
                action="add",
                new_value="算法工程师",
                source_ids=[ref.source_id],
                rationale="猜测",
                analysis_record_id="a",
                status="candidate",
            ),
            MemoryChange(
                entry_id="project.other",
                scope="project",
                action="add",
                new_value="跨项目",
                source_ids=["missing"],
                rationale="无原文",
                analysis_record_id="a",
                status="observed",
            ),
        ]
        accepted, rejected = await runner.validate(
            MemoryProposal(analyses=[analysis], changes=changes, complete=True), {}
        )
        self.assertFalse(accepted)
        self.assertEqual(set(rejected), {"user.job", "project.other"})
        self.assertFalse(runner.provider.requests)

    async def test_tool_protocol_records_explicit_analysis_before_proposal(self):
        runner = self.runner()
        analysis = AnamesisAnalysisRecord(
            item_id="review",
            record_id="a",
            stage_id="s",
            question="要不要新增？",
            rationale="没有新事实",
            conclusion="保持不变",
        )

        class Tools:
            async def stream(self, request):
                yield ToolCallEvent(call_id="1", name="record_analysis", arguments=analysis.model_dump())
                yield ToolCallEvent(call_id="2", name="propose_memory", arguments={"complete": True})
                yield StopEvent(stop_reason="tool_use")

        runner.provider = Tools()
        records = []

        async def emit(value):
            records.append(value)

        store = ArchiveStore(self.home, self.project)
        result = await runner.run_segment(
            research_state=research(list(runner.collector.sources.values())),
            state_event=noop,
            sources=[],
            snapshots={k: store.load(k) for k in ("user", "project")},
            stop=threading.Event(),
            emit=emit,
            operation=emit,
        )
        self.assertEqual(records, [analysis])
        self.assertEqual(result.proposal.analyses, [analysis])


class MemoryAndQueueTests(TempCase):
    def test_code_evidence_is_persistent_after_completed_run_and_restart(self):
        path = self.project / "code.py"
        path.write_text("value = 1", encoding="utf-8")
        collector = SourceCollector(self.home / "sessions", self.project)
        source = collector.register_code(path, "value = 1", 1, 1, "1\tvalue = 1")
        store = ArchiveStore(self.home, self.project)
        store.save_source(source)
        restarted = SourceCollector(self.home / "sessions", self.project)
        restored = store.load_sources({source.source_id})
        restarted.sources.update({s.source_id: s for s in restored})
        self.assertTrue(restarted.verify(source.source_id))
        path.write_text("value = 2", encoding="utf-8")
        self.assertFalse(restarted.verify(source.source_id))

    def test_user_memory_is_independent_of_project_toggle_and_hidden_fallback(self):
        hidden = self.project / ".logox"
        hidden.mkdir()
        (hidden / "ANAMNESIS.md").write_text("兼容旧位置", encoding="utf-8")
        (self.home / "ANAMNESIS.md").write_text("明确用户事实", encoding="utf-8")
        memory = AnamesisMemory(self.project, self.home, enabled=True, ratio=0.05, project_enabled=False)
        memory.refresh(window=32768, available=32768)
        self.assertIn("明确用户事实", memory.block)
        self.assertNotIn("兼容旧位置", memory.block)
        memory.project_enabled = True
        memory.refresh(window=32768, available=32768)
        self.assertIn("兼容旧位置", memory.block)

    def test_close_is_safe_after_original_event_loop_has_closed(self):
        service = AnamesisService(
            config=AnamesisConfig(),
            home=self.home,
            cwd=self.project,
            sessions=self.home / "sessions",
            runner_factory=None,
        )

        async def first():
            await service.start_background()
            await service.aclose()

        asyncio.run(first())
        asyncio.run(service.aclose())
        self.assertTrue(service._shutdown_complete)

    def test_card_clock_reuses_expanded_analysis_layout(self):
        from logox.tui.content.anamnesis import AnamesisCard

        card = AnamesisCard("clock")
        record = AnamesisAnalysisRecord(
            record_id="a",
            stage_id="s",
            question="判断问题",
            rationale="有依据的分析" * 200,
            conclusion="待验证",
        )
        card.ingest(AnamesisEvent(kind="analysis", run_id="clock", started_at=100, analysis=record))
        card.render(expanded=True, width=60, color="green")
        body = card._body
        card.tick(160)
        result = card.render(expanded=True, width=60, color="green")
        self.assertIs(card._body, body)
        self.assertIn("1m00s", result.plain)

    def test_whole_documents_budget_priority_and_human_rules_coexist(self):
        (self.project / "ANAMNESIS.md").write_text("项目事实", encoding="utf-8")
        (self.home / "ANAMNESIS.md").write_text("用户事实" * 300, encoding="utf-8")
        (self.project / "LOGOX.md").write_text("人工要求：不要改接口", encoding="utf-8")
        memory = AnamesisMemory(self.project, self.home, enabled=True, ratio=0.05)
        memory.refresh(window=4000, available=4000)
        self.assertIn("项目事实", memory.block)
        self.assertNotIn("用户事实", memory.block)
        self.assertTrue(memory.skipped)
        self.assertLessEqual(estimate_text_tokens(memory.block), 200)
        writer = SessionTranscriptWriter(self.root / "log", "test")
        builder = HierarchicalContextBuilder(
            cwd=self.project,
            window_capacity=16000,
            reserve_tokens=1000,
            transcript_writer=writer,
            anamnesis_home=self.home,
            anamnesis_enabled=True,
        )
        context = builder.build([user_message("实现功能")])
        self.assertIn("人工要求", context.system)
        self.assertIn("项目事实", context.system)
        memory.refresh(window=4000, available=0)
        self.assertEqual(memory.block, "")
        self.assertEqual((self.project / "ANAMNESIS.md").read_text(encoding="utf-8"), "项目事实")

    def test_disabled_memory_does_not_read_archives(self):
        memory = AnamesisMemory(self.project, self.home, enabled=False, ratio=0.05)
        with patch("pathlib.Path.read_text", side_effect=AssertionError("unexpected read")):
            memory.refresh(window=16000, available=16000)
        self.assertEqual(memory.block, "")

    def test_queue_deduplicates_projects_and_yields_between_batches(self):
        other = self.root / "other"
        other.mkdir()
        root = self.home / "anamnesis"
        old = AnamesisCoordinator(root, other, window_id="old")
        newest = AnamesisCoordinator(root, self.project, window_id="new")
        duplicate = AnamesisCoordinator(root, self.project, window_id="duplicate")
        for owner, stamp in ((old, 1), (newest, 3), (duplicate, 2)):
            owner.record_submission()
            owner.update(eligible=True, busy=False, last_submission=stamp)
        self.assertIsNone(duplicate.claim())
        self.assertIsNone(old.claim())
        self.assertIsNotNone(newest.claim())
        newest.yield_queue()
        newest.finish()
        self.assertIsNone(newest.claim())
        self.assertIsNotNone(old.claim())
        old.finish()
        for owner in (newest, old, duplicate):
            owner.unregister()


class LocalTransportTests(TempCase, unittest.IsolatedAsyncioTestCase):
    async def test_remote_or_credential_urls_never_make_network_request(self):
        for url in (
            "https://example.com",
            "http://127.0.0.1.evil.test",
            "http://user:pass@localhost:1",
            "http://localhost/v1?x=1",
        ):
            with self.assertRaises(ValueError):
                local_base_url(url)
        self.assertEqual(local_base_url("http://localhost:11434/v1"), "http://localhost:11434")

    async def test_static_window_and_chat_disable_redirects_and_proxy_environment(self):
        import httpx

        collector = SourceCollector(self.home / "sessions", self.project)
        registry = SimpleNamespace(
            spec=lambda name: SimpleNamespace(
                base_url="http://localhost:11434/v1", kind="openai_compat", context_window=32768
            )
        )
        real_client = httpx.AsyncClient
        options = []

        def respond(request):
            self.assertEqual(request.url.host, "localhost")
            self.assertEqual(request.method, "POST")
            self.assertEqual(request.url.path, "/v1/chat/completions")
            return httpx.Response(
                200, text='data: {"choices":[{"delta":{"content":"{}"},"finish_reason":"stop"}]}\n\n'
            )

        def client(**kwargs):
            options.append(kwargs)
            return real_client(transport=httpx.MockTransport(respond), **kwargs)

        with patch("httpx.AsyncClient", side_effect=client):
            runner = await make_local_runner(registry, AnamesisConfig(model="local"), collector)
            self.assertEqual(options, [])  # Static configuration does not probe the server.
            await runner._generate(ChatRequest(model="local", max_tokens=16))
        self.assertEqual(len(options), 1)
        self.assertEqual(runner.window, 32768)
        self.assertTrue(all(o["follow_redirects"] is False and o["trust_env"] is False for o in options))

    async def test_redirect_chat_is_rejected_without_following(self):
        import httpx

        registry = SimpleNamespace(
            spec=lambda name: SimpleNamespace(
                base_url="http://localhost:1", kind="openai_compat", context_window=32768
            )
        )
        real_client = httpx.AsyncClient
        requests = []

        def respond(request):
            requests.append(request)
            return httpx.Response(302, headers={"location": "https://example.com"})

        with (
            patch(
                "httpx.AsyncClient",
                side_effect=lambda **kwargs: real_client(transport=httpx.MockTransport(respond), **kwargs),
            ),
            self.assertRaisesRegex(RuntimeError, "302"),
        ):
            runner = await make_local_runner(
                registry,
                AnamesisConfig(model="local"),
                SourceCollector(self.home / "sessions", self.project),
            )
            await runner._generate(ChatRequest(model="local", max_tokens=16))
        self.assertEqual(len(requests), 1)

    async def test_real_local_adapter_sse_proposal_review_and_commit(self):
        import httpx

        registry = SimpleNamespace(
            spec=lambda name: SimpleNamespace(
                base_url="http://localhost:11434/v1", kind="openai_compat", context_window=32768
            )
        )
        real_client = httpx.AsyncClient
        requests = []

        def respond(request):
            requests.append(request)
            if request.url.path == "/api/ps":
                return httpx.Response(
                    200, json={"models": [{"name": "local:latest", "context_length": 32768}]}
                )
            payload = json.loads(request.content)
            self.assertEqual(payload["model"], "local")
            self.assertNotIn("max_tokens", payload)
            self.assertNotIn("max_completion_tokens", payload)
            if "独立核查" in payload["messages"][0]["content"]:
                answer = {"accepted_entry_ids": ["user.learning"], "reasons": {}}
                self.assertFalse(payload.get("tools"))
            else:
                data = json.loads(payload["messages"][1]["content"])
                ref = data["sources"][0]
                analysis = AnamesisAnalysisRecord(
                    record_id="a",
                    stage_id="s",
                    question="学习状态？",
                    rationale="明确原话",
                    evidence_summary=ref["content"],
                    source_ids=[ref["source_id"]],
                    conclusion="目前学习 AI Agent",
                    scope="user",
                )
                change = MemoryChange(
                    entry_id="user.learning",
                    scope="user",
                    action="add",
                    new_value="目前学习 AI Agent",
                    source_ids=[ref["source_id"]],
                    analysis_record_id="a",
                    rationale="明确原话",
                    status="explicit",
                )
                answer = MemoryProposal(complete=True, analyses=[analysis], changes=[change]).model_dump()
                calls = list(finish_calls(data, answer))
                self.assertEqual(
                    {t["function"]["name"] for t in payload["tools"]},
                    {
                        "plan_research",
                        "update_research",
                        "record_analysis",
                        "propose_memory",
                        "source",
                        "read",
                        "glob",
                        "grep",
                    },
                )
            data = {
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": json.dumps(answer, ensure_ascii=False)},
                        "finish_reason": None,
                    }
                ]
            }
            if "独立核查" not in payload["messages"][0]["content"]:
                data["choices"][0]["delta"] = {
                    "tool_calls": [
                        {
                            "index": index,
                            "id": call.call_id,
                            "type": "function",
                            "function": {
                                "name": call.name,
                                "arguments": json.dumps(call.arguments, ensure_ascii=False),
                            },
                        }
                        for index, call in enumerate(calls)
                    ]
                }
            thought_field = (
                "reasoning_content" if "独立核查" in payload["messages"][0]["content"] else "reasoning"
            )
            thinking = {"choices": [{"index": 0, "delta": {thought_field: "独立核对原始依据"}}]}
            stop = {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
            return httpx.Response(
                200,
                text=f"data: {json.dumps(thinking)}\n\ndata: {json.dumps(data)}\n\ndata: {json.dumps(stop)}\n\ndata: [DONE]\n\n",
                headers={"content-type": "text/event-stream"},
            )

        async def factory(collector):
            return await make_local_runner(registry, AnamesisConfig(model="local"), collector)

        service = AnamesisService(
            config=AnamesisConfig(model="local"),
            home=self.home,
            cwd=self.project,
            sessions=self.home / "sessions",
            runner_factory=factory,
        )
        events = []

        async def receive(event):
            events.append(event)

        service.on_event = receive
        with patch(
            "httpx.AsyncClient",
            side_effect=lambda **kwargs: real_client(transport=httpx.MockTransport(respond), **kwargs),
        ):
            await service.start()
            await service._task
        self.assertEqual(service.status().phase, "completed", service.status().reason)
        self.assertEqual(len(requests), 2)
        self.assertEqual(len([e for e in events if e.delta == "独立核对原始依据"]), 2)
        self.assertTrue(all(r.url.host == "localhost" for r in requests))
        self.assertIn("AI Agent", (self.home / "ANAMNESIS.md").read_text(encoding="utf-8"))
        await service.aclose()


class CardTests(TempCase, unittest.IsolatedAsyncioTestCase):
    async def test_both_modes_keep_one_card_on_pause_resume_and_editing_does_not_wake(self):
        for app_type in (InlineApp, FullscreenApp):
            service = AnamesisService(
                config=AnamesisConfig(),
                home=self.home,
                cwd=self.project,
                sessions=self.home / "sessions",
                runner_factory=None,
            )
            runtime = SimpleNamespace(
                kernel=SimpleNamespace(cancel=lambda: True),
                bus=EventBus(session_id="card"),
                config=SimpleNamespace(),
                model="test",
                anamnesis=service,
            )
            app = app_type(runtime=runtime, terminal=FakeTerminal(columns=70, rows=20))
            analysis = AnamesisAnalysisRecord(
                record_id="a",
                stage_id="portrait",
                question="学习什么？",
                rationale="用户明确说明方向",
                evidence_summary="学习 AI Agent",
                conclusion="记录当前方向",
            )
            for event in (
                AnamesisEvent(kind="started", run_id="abc", phase="collecting"),
                AnamesisEvent(kind="analysis", run_id="abc", analysis=analysis),
                AnamesisEvent(kind="paused", run_id="abc", phase="paused"),
                AnamesisEvent(kind="started", run_id="abc", phase="reviewing"),
            ):
                await app._on_anamnesis(event)
            blocks = [b for b in app.timeline.buffer.blocks if b.kind == "anamnesis"]
            self.assertEqual(len(blocks), 1)
            blocks[0].expanded = True
            output = "\n".join(row.plain for row in app.timeline.render(68))
            self.assertIn("用户明确说明方向", output)
            self.assertIn("记录当前方向", output)
            initial = service._generation
            app._dispatch(Key("up"))
            self.assertEqual(service._generation, initial)
            app._dispatch(Key("x", char="x"))
            self.assertEqual(service._generation, initial)
            self.assertEqual(app.editor.inner.text, "x")
            app.stop()
            await service.aclose()
