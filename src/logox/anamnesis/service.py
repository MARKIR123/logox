"""TUI-owned lifecycle; activity callbacks never scan or write files."""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path

from logox.anamnesis.archives import ArchiveStore
from logox.anamnesis.coordinator import AnamesisCoordinator
from logox.anamnesis.io import atomic_write, read_json, write_json
from logox.anamnesis.models import (
    AnamesisAnalysisRecord,
    AnamesisEvent,
    AnamesisResearchItem,
    AnamesisStatus,
    MemoryProposal,
    SourceRef,
    digest_text,
)
from logox.anamnesis.reports import build_report
from logox.anamnesis.research import AnamesisResearchState
from logox.anamnesis.sources import SourceCollector
from logox.context.tokens import estimate_text_tokens


class AnamesisService:
    def __init__(
        self,
        *,
        config,
        home: Path,
        cwd: Path,
        sessions: Path,
        runner_factory,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = datetime.now,
    ) -> None:
        self.config, self.clock, self.wall_clock = config, clock, wall_clock
        self.collector = SourceCollector(sessions, cwd)
        self.store = ArchiveStore(
            home, cwd, user_tokens=config.user_archive_tokens, project_tokens=config.project_archive_tokens
        )
        self.coordinator = AnamesisCoordinator(home / "anamnesis", cwd)
        self.runner_factory = runner_factory
        self.on_event: Callable[[AnamesisEvent], Awaitable[None]] | None = None
        self.foreground_busy: Callable[[], bool] = lambda: False
        self.current_session: Callable[[], str] = lambda: ""
        self.on_started: Callable[[str, str], None] | None = None
        self._status = AnamesisStatus(model=config.model)
        self._last_activity = self._last_end = clock()
        self._last_submission = 0.0
        self._generation = 0
        self._foreground = False
        self._closed = False
        self._shutdown_complete = False
        self._started = False
        self._stop = threading.Event()
        self._task: asyncio.Task | None = None
        self._poll_task: asyncio.Task | None = None
        self._failed_work = ""
        self._checkpoint_path = self.store._path(f"checkpoints/{self.store.project_key}.json")
        self._resume_id = ""
        self._start_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._empty_work = False
        self._registered_session: str | None = None
        self._live_checkpoint: dict | None = None

    @property
    def is_active(self) -> bool:
        return self._task is not None and not self._task.done()

    def status(self) -> AnamesisStatus:
        if not self.config.model:
            return self._status.model_copy(update={"reason": "未配置入梦模型，请设置 [anamnesis].model"})
        return self._status

    async def set_model(self, model: str, *, persist: Callable[[], Awaitable[None]]) -> None:
        """Switch only between runs, sharing the start lock and persisting first."""
        if not model or any(char.isspace() for char in model):
            raise ValueError("入梦模型名称不能为空或包含空格")
        if self._start_lock.locked():
            raise ValueError("入梦正在准备；请等待结束后再切换模型")
        async with self._start_lock:
            if self._closed:
                raise ValueError("入梦服务已关闭")
            if self.is_active:
                raise ValueError("入梦正在运行或暂停中；先 /anamnesis stop，等待暂停完成后再切换")
            await persist()
            self.config.model = model
            self._status = self._status.model_copy(update={"model": model})
            self._failed_work = ""
            self._empty_work = False

    def note_activity(self, kind: str = "input", monotonic_time: float | None = None) -> None:
        self._last_activity = self.clock() if monotonic_time is None else monotonic_time
        # Unsent drafts affect idle eligibility, but are not new evidence or a wake request.
        if kind != "submit":
            return
        self._generation += 1
        self._failed_work = ""
        self.request_wake("用户发送消息")

    def note_submission(self, timestamp: float | None = None) -> None:
        self._last_submission = time.time() if timestamp is None else timestamp
        self._registered_session = self.current_session()
        self.coordinator.record_submission()

    def note_foreground_state(self, busy: bool) -> None:
        self._foreground = busy
        if busy:
            self.request_wake("前台任务开始")
        else:
            self._last_end = self.clock()

    def request_wake(self, reason: str = "用户停止") -> None:
        self._stop.set()
        if self.is_active and not self._task.cancelling():
            self._status = self._status.model_copy(update={"phase": "pausing", "reason": reason})
            self._task.cancel()

    def eligible(self) -> bool:
        return (
            not self._closed
            and not self._foreground
            and not self.foreground_busy()
            and self.clock() - max(self._last_activity, self._last_end) > self.config.idle_seconds
        )

    async def _worker(self, func, *args, **kwargs):
        # Cancellation must not release the run lock while a commit worker still runs.
        task = asyncio.create_task(asyncio.to_thread(func, *args, **kwargs))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            self._stop.set()
            with contextlib.suppress(Exception):
                await task
            raise

    async def start_background(self) -> None:
        if self._started or self._closed:
            return
        self._started = True
        await self._worker(
            self.coordinator.update,
            eligible=False,
            busy=self._foreground or self.foreground_busy(),
            idle=False,
            session_id=self.current_session(),
            last_submission=self._last_submission,
        )
        await self._worker(self.store.recover_pending)
        self._poll_task = asyncio.create_task(self._poll())

    async def _poll(self) -> None:
        while not self._closed:
            try:
                await self._sync_session()
                ready = self.eligible()
                configured = bool(self.config.enabled and self.config.model)
                await self._worker(
                    self.coordinator.update,
                    eligible=configured and ready and not self._empty_work and not self.is_active,
                    idle=ready,
                    session_id=self.current_session(),
                    busy=self.is_active or self._foreground or self.foreground_busy(),
                    last_submission=self._last_submission,
                )
                if configured and ready and not self.is_active:
                    await self.start(manual=False)
            except Exception as exc:
                self._status = self._status.model_copy(update={"phase": "blocked", "reason": str(exc)})
            await asyncio.sleep(2)

    async def _sync_session(self) -> None:
        owner = self.current_session()
        if owner != self._registered_session:
            stamp = await self._worker(self.collector.last_submission, owner)
            if owner == self.current_session() and owner != self._registered_session:
                self._last_submission = stamp
                self._registered_session = owner

    async def start(self, *, manual: bool = True) -> str:
        if self._start_lock.locked():
            return "正在准备入梦"
        async with self._start_lock:
            try:
                return await self._start(manual=manual)
            except Exception as exc:
                if not self.is_active:
                    self.coordinator.finish()
                self._status = self._status.model_copy(update={"phase": "blocked", "reason": str(exc)})
                return f"无法启动入梦：{exc}"

    async def _start(self, *, manual: bool) -> str:
        if self._closed:
            return "入梦服务已关闭"
        if self.is_active:
            return "入梦正在运行／暂停中"
        if self._foreground or self.foreground_busy():
            return "前台模型、工具或审批尚未结束"
        if not self.config.enabled or not self.config.model:
            return "入梦未启用或未配置本地模型"
        if not manual and not self.eligible():
            return "尚未空闲超过 30 分钟"
        await self._sync_session()
        generation = self._generation
        session_id = self.current_session()
        self._stop = threading.Event()
        await self._worker(
            self.coordinator.update,
            eligible=True,
            busy=False,
            idle=self.eligible(),
            session_id=session_id,
            last_submission=self._last_submission,
        )
        if not await self._worker(self.coordinator.can_prepare, manual=manual):
            return "同项目窗口尚未全部空闲／前台结束，或其它项目／最近会话优先"
        checkpoint = await self._worker(read_json, self._checkpoint_path, {})
        if not isinstance(checkpoint, dict):
            raise ValueError("入梦续做记录损坏，未启动任务")
        if checkpoint.get("schema_version", 1) not in {1, 2}:
            raise ValueError("不支持的入梦检查点版本，保留原记录，未覆盖")
        if (
            checkpoint.get("schema_version") == 2
            and checkpoint.get("project_id") != self.collector.project_id
        ):
            raise ValueError("入梦检查点项目归属不一致，未覆盖")
        for value in checkpoint.get("analyses", []):
            AnamesisAnalysisRecord.model_validate(value)
        await self._worker(self.store.recover_pending)
        refs = await self._worker(self.collector.collect, self._stop)
        progress = await self._worker(self.store.progress)
        user_seen = await self._worker(self.store.global_processed)
        project_seen = set(progress["processed"])
        pending = [
            r
            for r in refs
            if r.source_id not in user_seen
            or (r.project_id == self.collector.project_id and r.source_id not in project_seen)
        ]
        code_snapshot = await self._worker(self.collector.code_snapshot, self._stop)
        fingerprint = code_snapshot["fingerprint"]
        work = digest_text(fingerprint + "".join(sorted(r.source_id for r in pending)))
        code_work = fingerprint != progress.get("code_fingerprint") and bool(
            code_snapshot["files"] or progress.get("code_fingerprint")
        )
        resumable = (
            checkpoint.get("schema_version") == 2
            and checkpoint.get("project_id") == self.collector.project_id
            and checkpoint.get("session_id", "") == session_id
            and checkpoint.get("generation") == generation
            and checkpoint.get("code_snapshot", {}).get("fingerprint") == fingerprint
            and {r.source_id for r in pending} <= set(checkpoint.get("initial_source_ids", []))
        )
        unfinished = checkpoint.get("schema_version") == 2 and any(
            item.get("status") in {"pending", "active"}
            for item in checkpoint.get("research_state", {}).get("items", [])
        )
        if not pending and not code_work and not unfinished:
            self._empty_work = True
            await self._worker(
                self.coordinator.update,
                eligible=False,
                busy=False,
                idle=self.eligible(),
                session_id=session_id,
                last_submission=self._last_submission,
            )
            if self.collector.issues:
                self._status = self._status.model_copy(
                    update={"phase": "blocked", "reason": "；".join(self.collector.issues)}
                )
                return "尚有损坏／过大资料未覆盖，详见 /anamnesis status"
            return "无可整理内容"
        self._empty_work = False
        block = checkpoint.get("automatic_block", {})
        if block and not manual:
            signature = digest_text(json.dumps(sorted(r.source_id for r in pending)))
            if signature == block.get("sources_signature"):
                current_code = fingerprint
                if current_code == block["code_fingerprint"]:
                    self._status = self._status.model_copy(
                        update={
                            "run_id": checkpoint.get("run_id", ""),
                            "phase": "paused",
                            "reason": block["reason"],
                            "remaining": len(pending),
                        }
                    )
                    await self._worker(
                        self.coordinator.update,
                        eligible=False,
                        busy=False,
                        idle=self.eligible(),
                        session_id=session_id,
                        last_submission=self._last_submission,
                    )
                    return block["reason"]
        if not manual and self._failed_work == work:
            return "同批资料已失败，等待新资料／手动重试"
        if generation != self._generation or self._stop.is_set():
            return "已被用户活动唤醒"
        if not self._last_submission:
            self._last_submission = max(
                (
                    r.timestamp
                    for r in refs
                    if r.kind == "user_message"
                    and r.timestamp is not None
                    and r.project_id == self.collector.project_id
                    and (not session_id or r.session_id == session_id)
                ),
                default=0,
            )
        await self._worker(
            self.coordinator.update,
            eligible=True,
            busy=False,
            last_submission=self._last_submission,
            idle=self.eligible(),
            session_id=session_id,
        )
        resume_id = checkpoint.get("run_id", "") if resumable else ""
        lease = await self._worker(self.coordinator.claim, manual=manual, run_id=resume_id)
        if lease is None:
            return "同项目窗口尚未全部空闲／前台结束，或其它项目／最近会话优先"
        if generation != self._generation or self._stop.is_set():
            self.coordinator.finish()
            return "已被用户活动唤醒"
        try:
            if checkpoint and checkpoint.get("schema_version", 1) < 2:
                await self._worker(self._archive_previous, checkpoint)
            if resumable:
                checkpoint = await self._worker(self._recover_checkpoint, checkpoint)
                if manual and checkpoint.get("automatic_block"):
                    checkpoint["research_state"]["repeats"] = {}
                    checkpoint["automatic_block"] = {}
                    if checkpoint.get("phase") == "failed":
                        checkpoint["pending_proposal"] = None
            else:
                previous = {
                    "run_id": checkpoint.get("run_id", ""),
                    "items": checkpoint.get("research_state", {}).get("items", []),
                }
                checkpoint = {"previous_work": previous}
            owner, new_run = await self._worker(self.store.bind_run, lease.run_id, session_id)
            checkpoint = {**checkpoint, "session_id": owner}
            if generation != self._generation or self._stop.is_set():
                self.coordinator.finish()
                return "已被用户活动唤醒"
            if new_run and self.on_started:
                self.on_started(lease.run_id, owner)
        except BaseException:
            self.coordinator.finish()
            raise
        self._task = asyncio.create_task(
            self._run(lease.run_id, pending, code_snapshot, work, generation, checkpoint)
        )

        # A task cancelled before its first instruction never executes its own finally.
        def release_if_needed(task):
            if self.coordinator._lease is lease:
                self.coordinator.finish()
                self._status = self._status.model_copy(
                    update={"run_id": lease.run_id, "phase": "paused", "reason": "启动前已唤醒"}
                )

        self._task.add_done_callback(release_if_needed)
        return "已开始入梦"

    def _archive_previous(self, checkpoint: dict) -> None:
        with self._checkpoint_path.open(encoding="utf-8", newline="") as handle:
            original = handle.read()
        destination = self.store._path(
            f"checkpoints/previous/{self.store.project_key}-{digest_text(original)}.json"
        )
        atomic_write(destination, original)

    def _recover_checkpoint(self, checkpoint: dict) -> dict:
        """Only complete validated trace events beyond the checkpoint can advance recovery."""
        saved = dict(checkpoint)
        state = AnamesisResearchState(saved.get("research_state", {}))
        sources = {**self.collector.sources}
        for value in saved.get("code_sources", []):
            ref = SourceRef.model_validate(value)
            if ref.project_id == self.collector.project_id:
                self.collector.sources[ref.source_id] = ref
            if ref.project_id == self.collector.project_id and self.collector.verify(ref.source_id):
                sources[ref.source_id] = ref
        path = self.store._run_path(saved["run_id"], "events.jsonl")
        if path.is_file():
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    try:
                        event = AnamesisEvent.model_validate_json(line)
                    except ValueError:
                        self.collector.issues.append("入梦事件尾行不完整或损坏，未用于推进恢复")
                        continue
                    if event.sequence <= saved.get("last_sequence", 0):
                        continue
                    if (
                        event.run_id != saved["run_id"]
                        or event.session_id != saved["session_id"]
                        or event.schema_version != 2
                    ):
                        raise ValueError("入梦恢复事件归属不一致")
                    if event.research_state is not None:
                        state = AnamesisResearchState(event.research_state)
                        saved["research_state"] = state.dump()
                    if (
                        event.operation
                        and event.operation.get("tool") == "read"
                        and event.operation.get("state") == "completed"
                    ):
                        ref = SourceRef.model_validate_json(event.operation["result"])
                        if ref.project_id != self.collector.project_id:
                            raise ValueError("恢复的代码来源跨项目")
                        self.collector.sources[ref.source_id] = ref
                        sources[ref.source_id] = ref
                    if event.kind in {"proposal", "committed"} and event.change:
                        record = next(
                            (
                                c
                                for c in reversed(saved.get("changes", []))
                                if c["entry_id"] == event.change.entry_id and c["scope"] == event.change.scope
                            ),
                            None,
                        )
                        if event.kind == "committed" and record:
                            record.update(result="已保存", change_id=event.result)
                        elif not record or event.kind == "proposal":
                            saved.setdefault("changes", []).append(
                                {
                                    **event.change.model_dump(),
                                    "result": "已保存" if event.kind == "committed" else event.result,
                                    "change_id": event.result if event.kind == "committed" else "",
                                }
                            )
                    if event.analysis and not any(
                        a["record_id"] == event.analysis.record_id for a in saved.get("analyses", [])
                    ):
                        saved.setdefault("analyses", []).append(event.analysis.model_dump())
                    if event.kind == "segment_proposal":
                        saved["pending_proposal"] = json.loads(event.result)
                    if event.kind in {"checkpoint", "completed", "paused", "failed"}:
                        saved["findings"], saved["plan"] = event.findings, event.plan
                    saved["last_sequence"] = event.sequence
        # Stale code or rewound dialogue cannot substantiate a recovered terminal conclusion.
        self.collector.sources.update(sources)
        for item in list(state.items.values()):
            if item.status == "resolved" and any(not self.collector.verify(s) for s in item.source_ids):
                state.items[item.item_id] = item.model_copy(
                    update={
                        "status": "pending",
                        "version": item.version + 1,
                        "analysis_ids": [],
                        "source_ids": [],
                        "conclusion": "",
                    }
                )
                state.version += 1
                saved["pending_proposal"] = None
        saved["research_state"] = state.dump()
        return saved

    async def _emit(self, event: AnamesisEvent) -> None:
        data = await self._worker(self.store.append_record, event.run_id, event.model_dump(mode="json"))
        event = AnamesisEvent.model_validate(data)
        if self._live_checkpoint is not None and event.run_id == self._live_checkpoint["run_id"]:
            saved = self._live_checkpoint
            saved["last_sequence"] = event.sequence
            if event.research_state is not None:
                saved["research_state"] = event.research_state
            if event.analysis and not any(
                a["record_id"] == event.analysis.record_id for a in saved["analyses"]
            ):
                saved["analyses"].append(event.analysis.model_dump())
            if event.self_check:
                saved["self_checks"] = [*saved.get("self_checks", []), event.self_check]
            # Streaming deltas already live in trace; meaningful state is durable before UI sees it.
            if event.kind not in {"reasoning", "text", "model_response"}:
                saved["code_sources"] = [
                    r.model_dump() for r in self.collector.sources.values() if r.kind == "code"
                ]
                await self._worker(write_json, self._checkpoint_path, saved)
        if self.on_event:
            await self.on_event(event)

    async def _run(self, run_id, pending, code_snapshot, work, generation, checkpoint) -> None:
        continuing = checkpoint.get("run_id") == run_id
        saved = dict(checkpoint) if continuing else {}
        started = saved.get("started_at", time.time())
        analyses = [AnamesisAnalysisRecord.model_validate(a) for a in saved.get("analyses", [])]
        changes = list(saved.get("changes", []))
        findings, plan = list(saved.get("findings", [])), list(saved.get("plan", []))
        state = AnamesisResearchState(
            saved.get("research_state"),
            repeat_trigger_count=self.config.repeat_trigger_count,
            self_check_max_attempts=self.config.self_check_max_attempts,
        )
        saved.update(
            schema_version=2,
            project_id=self.collector.project_id,
            run_id=run_id,
            session_id=checkpoint.get("session_id", ""),
            started_at=started,
            owner_window=self.coordinator.window_id,
            generation=generation,
            code_snapshot=code_snapshot,
            initial_source_ids=saved.get("initial_source_ids", [r.source_id for r in pending]),
            analyses=[a.model_dump() for a in analyses],
            changes=changes,
            findings=findings,
            plan=plan,
            research_state=state.dump(),
            self_checks=saved.get("self_checks", []),
            automatic_block={},
            work=work,
            phase="collecting",
            previous_work=checkpoint.get("previous_work", {}),
        )
        self._live_checkpoint = saved
        outcome, reason, remaining = "failed", "", len(pending)
        self._status = AnamesisStatus(
            run_id=run_id,
            phase="collecting",
            model=self.config.model,
            started_at=started,
            remaining=remaining,
        )

        def event(kind, **values):
            return AnamesisEvent(
                schema_version=2,
                kind=kind,
                run_id=run_id,
                started_at=started,
                session_id=saved["session_id"],
                **values,
            )

        async def emit(analysis):
            if not any(a.record_id == analysis.record_id for a in analyses):
                analyses.append(analysis)
            self._status = self._status.model_copy(
                update={"phase": "reviewing", "question": analysis.question, "steps": len(analyses)}
            )
            await self._emit(
                event("analysis", phase="reviewing", question=analysis.question, analysis=analysis)
            )

        async def operation(data):
            if data.get("tool") == "read" and "result" in data:
                await self._worker(self.store.save_source, SourceRef.model_validate_json(data["result"]))
            await self._emit(event("operation", operation=data))

        async def state_event(data):
            item = AnamesisResearchItem.model_validate(data["item"]) if data.get("item") else None
            if item:
                self._status = self._status.model_copy(update={"question": item.question})
            await self._emit(
                event(
                    data["kind"],
                    item=item,
                    research_state=data.get("research_state"),
                    self_check=data.get("self_check"),
                    question=item.question if item else "",
                )
            )

        try:
            for value in saved.get("code_sources", []):
                ref = SourceRef.model_validate(value)
                if ref.kind == "code" and ref.project_id == self.collector.project_id:
                    self.collector.sources[ref.source_id] = ref
            await self._emit(
                event(
                    "started",
                    phase="collecting",
                    question="回顾工作与选定研究事项",
                    research_state=state.dump(),
                )
            )
            runner = await self.runner_factory(self.collector)

            async def progress(data):
                await self._emit(
                    event(data["kind"], delta=data.get("delta", ""), result=data.get("result", ""))
                )

            runner.on_progress = progress
            if generation != self._generation or self._stop.is_set():
                raise asyncio.CancelledError
            processed = await self._worker(self.store.progress)
            user_seen = await self._worker(self.store.global_processed)
            pending = [
                r
                for r in pending
                if r.source_id not in user_seen
                or (r.project_id == self.collector.project_id and r.source_id not in processed["processed"])
            ]
            snapshots = {scope: await self._worker(self.store.load, scope) for scope in ("user", "project")}
            identities = {
                s for snapshot in snapshots.values() for entry in snapshot.entries for s in entry.source_ids
            }
            for source in await self._worker(self.store.load_sources, identities):
                if source.project_id == self.collector.project_id:
                    self.collector.sources.setdefault(source.source_id, source)
            prior_batch = saved.get("batch_source_ids")
            available = runner.source_budget(snapshots, state)
            batch, consumed = [], 0
            for ref in pending:
                if prior_batch is not None:
                    if ref.source_id in prior_batch:
                        batch.append(ref)
                    continue
                size = estimate_text_tokens(ref.model_dump_json())
                if consumed + size > min(available, 10000) or len(batch) >= 100:
                    break
                batch.append(ref)
                consumed += size
            if pending and not batch and prior_batch is None:
                raise ValueError("独立入梦窗口不足以容纳最小资料片段")
            saved["batch_source_ids"] = [r.source_id for r in batch]
            # Each batch has a host review item. Code review is selected once for the frozen snapshot.
            if not state.items or saved.pop("needs_batch", False):
                roots = []
                if batch:
                    roots.append(
                        AnamesisResearchItem(
                            item_id="review." + digest_text("".join(saved["batch_source_ids"]))[:12],
                            question="本批资料中哪些事实需要更新，哪些未完成问题值得研究？",
                            reason="回顾未审阅的真实资料，避免遗漏有效信息",
                            origin_source_ids=saved["batch_source_ids"],
                        )
                    )
                elif checkpoint.get("previous_work", {}).get("items"):
                    roots.append(
                        AnamesisResearchItem(
                            item_id="reassess." + run_id[:12],
                            scope="research",
                            question="此前未交代的研究是否仍需推进，有哪些真实依据或具体缺证？",
                            reason="会话或工作集变化，重新核对旧事项，不将旧分析当成新完成证明",
                            origin_kind="code_change",
                            code_snapshot_id=code_snapshot["fingerprint"],
                        )
                    )
                if (
                    code_snapshot["fingerprint"] != processed.get("code_fingerprint")
                    and (code_snapshot["files"] or processed.get("code_fingerprint"))
                    and "code." + code_snapshot["fingerprint"][:12] not in state.items
                ):
                    roots.append(
                        AnamesisResearchItem(
                            item_id="code." + code_snapshot["fingerprint"][:12],
                            scope="research",
                            question="项目入口或变化相关代码是否存在有依据的问题及可行改进？",
                            reason="代码元数据发生变化或尚无研究基线；事实须实际读取核查",
                            origin_kind="code_change",
                            code_snapshot_id=code_snapshot["fingerprint"],
                        )
                    )
                state.add(
                    roots,
                    source_ids={r.source_id for r in batch},
                    code_snapshot_id=code_snapshot["fingerprint"],
                    allow_roots=True,
                )
                for item in roots:
                    await state_event(
                        {"kind": "research_item", "item": item.model_dump(), "research_state": state.dump()}
                    )
                state.roots_open = True
            await self._emit(
                event(
                    "phase",
                    phase="reviewing",
                    question=state.current().question if state.current() else "提交本批资料审阅结果",
                )
            )
            old_files = processed.get("code_files", {})
            previous_research = await self._worker(
                read_json, self.store._path(f"research/{self.store.project_key}.json"), {}
            )
            waiting = [
                AnamesisResearchItem.model_validate(i).model_dump()
                for i in previous_research.get("waiting_evidence", [])
            ]
            file_changes = [
                {"path": path, "before": old_files.get(path), "after": code_snapshot["files"].get(path)}
                for path in sorted(old_files.keys() | code_snapshot["files"].keys())
                if old_files.get(path) != code_snapshot["files"].get(path)
            ]
            pending_proposal = saved.get("pending_proposal")
            if pending_proposal:
                from logox.anamnesis.models import AnamesisSegmentResult

                segment = AnamesisSegmentResult(
                    kind="proposal", proposal=MemoryProposal.model_validate(pending_proposal)
                )
            else:
                segment = await runner.run_segment(
                    sources=batch,
                    snapshots=snapshots,
                    research_state=state,
                    stop=self._stop,
                    emit=emit,
                    operation=operation,
                    state_event=state_event,
                    resume=[a.model_dump() for a in analyses],
                    archive_limits=self.store.limits,
                    code_snapshot={
                        "fingerprint": code_snapshot["fingerprint"],
                        "baseline_known": bool(old_files),
                        "changes": file_changes[:50],
                        "change_count": len(file_changes),
                        "previous_waiting_evidence": waiting[:16],
                        "waiting_count": len(waiting),
                        "previous_report": previous_research.get("report_path", ""),
                        "previous_work": {
                            "run_id": saved.get("previous_work", {}).get("run_id", ""),
                            "items": saved.get("previous_work", {}).get("items", [])[:16],
                        },
                    },
                )
            reason = segment.reason
            if segment.kind == "paused":
                outcome = "paused"
            elif segment.kind == "checkpoint":
                outcome = "continuing"
            elif segment.kind == "finished":
                if not state.finished or pending:
                    raise ValueError("尚有研究事项或未审资料，不能宣告完成")
                outcome = "completed"
            else:
                proposal = segment.proposal
                if proposal is None or not proposal.complete:
                    outcome, reason = (
                        "paused",
                        "本批未形成完整审阅提案，游标未推进；等待新资料或手动 /anamnesis 继续",
                    )
                else:
                    saved["pending_proposal"] = proposal.model_dump()
                    await self._emit(
                        event(
                            "segment_proposal", result=proposal.model_dump_json(), research_state=state.dump()
                        )
                    )
                    findings = list(dict.fromkeys(findings + proposal.review_findings))
                    plan = list(dict.fromkeys(plan + proposal.next_plan))
                    await self._emit(
                        event("phase", phase="validating", question="核对结论与原始依据是否一致")
                    )
                    recovered_commits = (
                        await self._worker(self.store.committed_proposal, proposal, snapshots, started)
                        if pending_proposal
                        else {}
                    )
                    uncommitted = proposal.model_copy(
                        update={
                            "changes": [
                                c for c in proposal.changes if (c.scope, c.entry_id) not in recovered_commits
                            ]
                        }
                    )
                    accepted, rejected = await runner.validate(uncommitted, snapshots)
                    if self._stop.is_set() or generation != self._generation:
                        raise asyncio.CancelledError
                    records = {}
                    for change in proposal.changes:
                        recovered_id = recovered_commits.get((change.scope, change.entry_id))
                        if recovered_id:
                            record = next(
                                (
                                    c
                                    for c in reversed(changes)
                                    if c["scope"] == change.scope and c["entry_id"] == change.entry_id
                                ),
                                None,
                            )
                            if record is None:
                                record = change.model_dump()
                                changes.append(record)
                            record.update(result="已保存", change_id=recovered_id)
                            await self._emit(event("committed", change=change, result=recovered_id))
                            continue
                        rejection = rejected.get(change.entry_id, "")
                        display = change.model_copy(update={"status": "candidate"}) if rejection else change
                        result = "候选（未入档）：" + rejection if rejection else "等待保存"
                        record = {**display.model_dump(), "proposed_status": change.status, "result": result}
                        changes.append(record)
                        records[change.entry_id] = record
                        await self._emit(event("proposal", change=display, result=result))
                    for scope in ("user", "project"):
                        scoped = [c for c in accepted if c.scope == scope]
                        if scoped:
                            change_id = await self._worker(
                                self.store.commit,
                                snapshots[scope],
                                scoped,
                                verify=self.collector.verify,
                                awake=self._stop.is_set,
                            )
                            for change in scoped:
                                records[change.entry_id].update(result="已保存", change_id=change_id)
                                await self._emit(event("committed", change=change, result=change_id))
                    review_items = [
                        i
                        for i in state.items.values()
                        if i.origin_kind == "source_review"
                        and set(i.origin_source_ids) & {r.source_id for r in batch}
                    ]
                    reviewed = (
                        {r.source_id for r in batch}
                        if all(i.status in {"resolved", "waiting_evidence"} for i in review_items)
                        else set()
                    )
                    await self._worker(
                        self.store.save_progress,
                        reviewed,
                        project_processed={
                            r.source_id
                            for r in batch
                            if r.source_id in reviewed and r.project_id == self.collector.project_id
                        },
                    )
                    saved["pending_proposal"] = None
                    remaining = len(pending) - len(reviewed)
                    if reviewed:
                        state.advance()
                    # Source review and issue completion are independent. Empty changes never finish pending issues.
                    if state.finished and remaining == 0:
                        await self._worker(
                            self.store.save_progress,
                            set(),
                            code_fingerprint=code_snapshot["fingerprint"],
                            code_files=code_snapshot["files"],
                        )
                        outcome = "completed"
                    else:
                        outcome = "continuing"
                        if state.finished and remaining:
                            # Keep complete prior items in the report; next source batch gets a new review root.
                            saved["batch_source_ids"] = None
                            # Selection of the next bounded batch happens after yielding the project queue.
                            saved["research_state"] = state.dump()
                            saved["needs_batch"] = True
                        elif not state.finished and reviewed:
                            saved["batch_source_ids"] = []
                    candidates = sum(c.get("status") == "candidate" for c in changes)
                    saved_count = sum(c["result"] == "已保存" for c in changes)
                    label = (
                        ("部分采纳" if saved_count else "已整理，无可采纳记忆") if candidates else "已整理"
                    )
                    reason = f"{label}：保存 {saved_count} 条；候选未入档 {candidates} 条。" + (
                        "选定事项和资料已交代。"
                        if outcome == "completed"
                        else "剩余资料或事项已保存，等待续做。"
                    )
        except (asyncio.CancelledError, InterruptedError):
            outcome, reason = "paused", self._status.reason or "已被用户唤醒，未完成事项已保留"
        except Exception as exc:
            self._failed_work = work
            reason = f"{type(exc).__name__}: {str(exc) or '未提供详细说明'}"
            self.collector.issues.append(reason)
        finally:
            try:
                saved.update(
                    research_state=state.dump(),
                    analyses=[a.model_dump() for a in analyses],
                    changes=changes,
                    findings=findings,
                    plan=plan,
                    phase=outcome,
                    code_sources=[
                        r.model_dump() for r in self.collector.sources.values() if r.kind == "code"
                    ],
                    automatic_continuation=outcome == "continuing"
                    and generation == self._generation
                    and not self._stop.is_set(),
                )
                if outcome in {"paused", "failed"} and not self._stop.is_set():
                    saved["automatic_block"] = {
                        "sources_signature": digest_text(json.dumps(sorted(r.source_id for r in pending))),
                        "code_fingerprint": code_snapshot["fingerprint"],
                        "reason": reason + "；请手动继续或等待新依据",
                    }
                self._status = self._status.model_copy(
                    update={
                        "phase": outcome,
                        "reason": reason,
                        "remaining": remaining,
                        "changes": len(changes),
                    }
                )
                report = build_report(
                    "",
                    analyses,
                    changes,
                    outcome=outcome,
                    issues=self.collector.issues,
                    findings=findings,
                    plan=plan,
                    remaining=remaining,
                    sources=self.collector.sources,
                    items=list(state.items.values()),
                    self_checks=saved.get("self_checks", []),
                    reason=reason,
                )
                path = await self._worker(self.store.save_report, run_id, report)
                await self._emit(
                    event(
                        "checkpoint" if outcome == "continuing" else outcome,
                        phase=outcome,
                        reason=reason,
                        report_path=str(path),
                        findings=findings,
                        plan=plan,
                        research_state=state.dump(),
                    )
                )
                if outcome == "completed":
                    await self._worker(
                        write_json,
                        self.store._path(f"research/{self.store.project_key}.json"),
                        {
                            "schema_version": 2,
                            "run_id": run_id,
                            "report_path": str(path),
                            "waiting_evidence": [
                                i.model_dump() for i in state.items.values() if i.status == "waiting_evidence"
                            ],
                        },
                    )
                    await self._worker(write_json, self._checkpoint_path, {})
            except Exception as exc:
                self._status = self._status.model_copy(
                    update={"phase": "failed", "reason": f"记录保存失败：{exc}"}
                )
                if self.on_event:
                    await self.on_event(event("failed", phase="failed", reason=self._status.reason))
            finally:
                self._live_checkpoint = None
                try:
                    await self._worker(
                        self.coordinator.update,
                        eligible=False,
                        busy=self._foreground,
                        idle=self.eligible(),
                        session_id=self.current_session(),
                        last_submission=self._last_submission,
                    )
                    await self._worker(self.coordinator.yield_queue)
                finally:
                    self.coordinator.finish()

    async def history(self, session_id: str | None = None) -> list[dict]:
        def load():
            previews = self.store.run_history(session_id)
            for preview in previews:
                if preview.get("error"):
                    continue
                preview["events"] = [
                    AnamesisEvent.model_validate({**value, "sequence": 0})
                    for key in ("analysis_events", "change_events")
                    for value in preview.get(key, {}).values()
                ]
                # Validate in the service worker, not in TUI's input/render path.
                meta = AnamesisEvent.model_validate(
                    {
                        "kind": "restored",
                        "run_id": preview["run_id"],
                        **{
                            key: preview[key]
                            for key in (
                                "mode",
                                "phase",
                                "question",
                                "reason",
                                "session_id",
                                "sequence",
                                "started_at",
                                "timestamp",
                                "report_path",
                                "findings",
                                "plan",
                            )
                            if key in preview
                        },
                        "delta": preview.get("reasoning", ""),
                    }
                )
                if not isinstance(preview.get("operations", {}), dict):
                    raise ValueError("入梦操作预览格式错误")
                preview.update({key: value for key, value in meta.model_dump().items() if key in preview})
            return previews

        return await asyncio.to_thread(load)

    async def run_record(self, run_id: str, *, trace: bool = False) -> str:
        return await asyncio.to_thread(self.store.read_run, run_id, trace=trace)

    async def latest_report(self) -> str:
        path = await self._worker(self.store.latest_report)
        return await self._worker(path.read_text, encoding="utf-8") if path else "尚无入梦报告"

    def request_close(self) -> None:
        self._closed = True
        self.request_wake("TUI 关闭")

    async def aclose(self) -> None:
        if self._shutdown_complete:
            return
        async with self._close_lock:
            if self._shutdown_complete:
                return
            await self._aclose()

    async def _aclose(self) -> None:
        self.request_close()
        if self._poll_task:
            self._poll_task.cancel()
            await asyncio.gather(self._poll_task, return_exceptions=True)
        if self._task:
            await asyncio.gather(self._task, return_exceptions=True)
        if self.coordinator.registry_path.exists():
            await self._worker(self.coordinator.unregister)
        self._shutdown_complete = True
