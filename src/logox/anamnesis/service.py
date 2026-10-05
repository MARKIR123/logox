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
from logox.anamnesis.io import read_json, write_json
from logox.anamnesis.models import (
    AnamesisAnalysisRecord,
    AnamesisEvent,
    AnamesisStatus,
    SourceRef,
    digest_text,
)
from logox.anamnesis.reports import build_report
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

    @property
    def is_active(self) -> bool:
        return self._task is not None and not self._task.done()

    def status(self) -> AnamesisStatus:
        if not self.config.model:
            return self._status.model_copy(update={"reason": "未配置入梦模型，请设置 [anamnesis].model"})
        return self._status

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

    def current_mode(self) -> str:
        current = self.wall_clock().strftime("%H:%M")
        start, end = self.config.sleep_start, self.config.sleep_end
        night = start <= current < end if start < end else current >= start or current < end
        return "sleep" if night else "nap"

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

    async def start(self, mode: str = "auto", *, manual: bool = True) -> str:
        if self._start_lock.locked():
            return "正在准备入梦"
        async with self._start_lock:
            try:
                return await self._start(mode, manual=manual)
            except Exception as exc:
                if not self.is_active:
                    self.coordinator.finish()
                self._status = self._status.model_copy(update={"phase": "blocked", "reason": str(exc)})
                return f"无法启动入梦：{exc}"

    async def _start(self, mode: str, *, manual: bool) -> str:
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
        for value in checkpoint.get("analyses", []):
            AnamesisAnalysisRecord.model_validate(value)
        if mode == "auto":
            continuing_sleep = (
                not manual
                and checkpoint.get("mode") == "sleep"
                and checkpoint.get("automatic_continuation")
                and checkpoint.get("owner_window") == self.coordinator.window_id
                and checkpoint.get("generation") == generation
            )
            mode = "sleep" if continuing_sleep else self.current_mode()
        if mode not in {"nap", "sleep"}:
            return "模式必须是 nap／sleep"
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
        fingerprint = (
            await self._worker(self.collector.code_fingerprint, self._stop) if mode == "sleep" else ""
        )
        work = digest_text(mode + fingerprint + "".join(r.source_id for r in pending))
        code_work = mode == "sleep" and fingerprint != progress.get("sleep_fingerprint")
        if not pending and not code_work:
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
            if signature == block["sources_signature"]:
                current_code = (
                    fingerprint
                    if mode == "sleep"
                    else await self._worker(self.collector.code_fingerprint, self._stop)
                )
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
        resume_id = (
            checkpoint.get("run_id", "")
            if checkpoint.get("mode") == mode and checkpoint.get("session_id", "") == session_id
            else ""
        )
        lease = await self._worker(self.coordinator.claim, manual=manual, run_id=resume_id)
        if lease is None:
            return "同项目窗口尚未全部空闲／前台结束，或其它项目／最近会话优先"
        if generation != self._generation or self._stop.is_set():
            self.coordinator.finish()
            return "已被用户活动唤醒"
        try:
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
            self._run(lease.run_id, mode, pending, fingerprint, work, generation, checkpoint)
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

    async def _emit(self, event: AnamesisEvent) -> None:
        data = await self._worker(self.store.append_record, event.run_id, event.model_dump(mode="json"))
        event = AnamesisEvent.model_validate(data)
        if self.on_event:
            await self.on_event(event)

    async def _run(self, run_id, mode, pending, fingerprint, work, generation, checkpoint) -> None:
        analyses = (
            [AnamesisAnalysisRecord.model_validate(a) for a in checkpoint.get("analyses", [])]
            if checkpoint.get("run_id") == run_id
            else []
        )
        continuing = checkpoint.get("run_id") == run_id
        changes = list(checkpoint.get("changes", [])) if continuing else []
        findings = list(checkpoint.get("findings", [])) if continuing else []
        plan = list(checkpoint.get("plan", [])) if continuing else []
        outcome = "failed"
        automatic_block = {}
        remaining = len(pending)
        started = (
            checkpoint.get("started_at", time.time()) if checkpoint.get("run_id") == run_id else time.time()
        )
        self._status = AnamesisStatus(
            run_id=run_id,
            mode=mode,
            phase="collecting",
            model=self.config.model,
            started_at=started,
            remaining=remaining,
        )

        def event(kind, **values):
            return AnamesisEvent(
                kind=kind,
                run_id=run_id,
                mode=mode,
                started_at=started,
                session_id=checkpoint.get("session_id", ""),
                **values,
            )

        async def emit(analysis):
            if not any(a.record_id == analysis.record_id and a == analysis for a in analyses):
                analyses.append(analysis)
            await self._emit(
                event("analysis", phase=analysis.stage_id, question=analysis.question, analysis=analysis)
            )
            self._status = self._status.model_copy(
                update={"phase": "reviewing", "question": analysis.question}
            )
            self._status = self._status.model_copy(update={"steps": len(analyses)})

        async def operation(data):
            await self._emit(event("operation", operation=data))
            if data.get("tool") == "read" and "result" in data:
                source = SourceRef.model_validate(json.loads(data["result"]))
                await self._worker(self.store.save_source, source)

        try:
            await self._emit(event("started", phase="collecting", question="回顾工作与有效记忆"))
            for analysis in analyses:
                if self.on_event:
                    await self.on_event(
                        event(
                            "analysis", phase=analysis.stage_id, question=analysis.question, analysis=analysis
                        )
                    )
            for value in checkpoint.get("code_sources", []):
                ref = SourceRef.model_validate(value)
                if ref.kind == "code" and ref.project_id == self.collector.project_id:
                    self.collector.sources[ref.source_id] = ref
            runner = await self.runner_factory(self.collector)

            async def progress(data):
                kind = data["kind"]
                await self._emit(event(kind, delta=data.get("delta", ""), result=data.get("result", "")))

            runner.on_progress = progress
            if generation != self._generation or self._stop.is_set():
                raise asyncio.CancelledError
            # Re-read shared progress after claim to avoid duplicate work completed by another window.
            progress = await self._worker(self.store.progress)
            user_seen = await self._worker(self.store.global_processed)
            pending = [
                r
                for r in pending
                if r.source_id not in user_seen
                or (r.project_id == self.collector.project_id and r.source_id not in progress["processed"])
            ]
            snapshots = {scope: await self._worker(self.store.load, scope) for scope in ("user", "project")}
            identities = {
                s for snapshot in snapshots.values() for entry in snapshot.entries for s in entry.source_ids
            }
            for source in await self._worker(self.store.load_sources, identities):
                if source.project_id == self.collector.project_id:
                    self.collector.sources.setdefault(source.source_id, source)
            # Budget includes schemas, archives, output and review overhead; unknown small windows fail closed.
            available = max(
                0, runner.window - 6000 - sum(estimate_text_tokens(s.text) for s in snapshots.values())
            )
            batch, consumed = [], 0
            for ref in pending:
                size = estimate_text_tokens(ref.model_dump_json())
                if consumed + size > min(available // 2, 10_000):
                    break
                batch.append(ref)
                consumed += size
            if pending and not batch:
                raise ValueError("独立入梦窗口不足以容纳最小资料片段")
            await self._emit(
                event(
                    "phase",
                    phase="summarizing" if mode == "nap" else "reviewing",
                    question="筛选有效事实与下一步问题",
                )
            )
            proposal = await runner.run(
                mode=mode,
                sources=batch,
                snapshots=snapshots,
                steps=self.config.nap_max_steps if mode == "nap" else self.config.sleep_max_steps,
                stop=self._stop,
                emit=emit,
                operation=operation,
                resume=[a.model_dump() for a in analyses],
                archive_limits=self.store.limits,
            )
            findings = list(dict.fromkeys(findings + proposal.review_findings))
            plan = list(dict.fromkeys(plan + proposal.next_plan))
            if not proposal.complete:
                outcome = "paused"
                reason = (
                    "本批未形成完整提案，资料游标未推进；已保留分析并停止自动重试。"
                    "请手动 /anamnesis nap 或 /anamnesis sleep 继续，或等待新资料。"
                )
                automatic_block = {
                    "sources_signature": digest_text(json.dumps(sorted(r.source_id for r in pending))),
                    "code_fingerprint": fingerprint
                    if mode == "sleep"
                    else await self._worker(self.collector.code_fingerprint, self._stop),
                    "reason": reason,
                }
                self.collector.issues.append(reason)
                self._status = self._status.model_copy(update={"reason": reason})
            else:
                await self._emit(event("phase", phase="validating", question="核对结论与原始依据是否一致"))
                accepted, rejected = await runner.validate(proposal, snapshots)
                if self._stop.is_set() or generation != self._generation:
                    raise asyncio.CancelledError
                batch_records = {}
                for change in proposal.changes:
                    reason = (
                        (rejected[change.entry_id] or "核验拒绝，未提供具体原因")
                        if change.entry_id in rejected
                        else ""
                    )
                    display = change.model_copy(update={"status": "candidate"}) if reason else change
                    result = "候选（未入档）：" + reason if reason else "等待保存"
                    record = {**display.model_dump(), "proposed_status": change.status, "result": result}
                    changes.append(record)
                    batch_records[change.entry_id] = record
                    await self._emit(event("proposal", change=display, result=result))
                # A rejected item is an audited candidate, not failure of the whole valid batch.
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
                            batch_records[change.entry_id].update(result="已保存", change_id=change_id)
                            await self._emit(event("committed", change=change, result=change_id))
                    if scope == "user":
                        await self._worker(
                            self.store.save_progress, {r.source_id for r in batch}, project_processed=set()
                        )
                processed = {r.source_id for r in batch}
                remaining = len(pending) - len(batch)
                await self._worker(
                    self.store.save_progress,
                    processed,
                    project_processed={
                        r.source_id for r in batch if r.project_id == self.collector.project_id
                    },
                    sleep_fingerprint=fingerprint if mode == "sleep" and remaining == 0 else "",
                )
                outcome = "completed" if remaining == 0 else "continuing"
                candidates = sum(record.get("status") == "candidate" for record in changes)
                reason = ""
                if candidates:
                    saved = sum(record["result"] == "已保存" for record in changes)
                    label = "部分采纳" if saved else "已整理，无可采纳记忆"
                    reason = f"{label}：已保存 {saved} 条，候选未入档 {candidates} 条；详见报告"
                if self.collector.issues and outcome == "completed":
                    reason = (reason + "；" if reason else "") + "仍有未覆盖记录；详见报告"
                if outcome == "continuing":
                    reason = f"本批已整理并保存进度，剩余 {remaining} 个资料片段待续做。" + reason
                self._status = self._status.model_copy(update={"reason": reason})
        except (asyncio.CancelledError, InterruptedError):
            outcome = "paused"
        except Exception as exc:
            self._failed_work = work
            reason = f"{type(exc).__name__}: {str(exc) or '未提供详细说明'}"
            self.collector.issues.append(reason)
            self._status = self._status.model_copy(update={"reason": reason})
        finally:
            try:
                if outcome in {"paused", "continuing"}:
                    await self._worker(
                        write_json,
                        self._checkpoint_path,
                        {
                            "run_id": run_id,
                            "session_id": checkpoint.get("session_id", ""),
                            "mode": mode,
                            "started_at": started,
                            "automatic_continuation": outcome == "continuing"
                            and generation == self._generation
                            and not self._stop.is_set(),
                            "automatic_block": automatic_block,
                            "owner_window": self.coordinator.window_id,
                            "generation": generation,
                            "analyses": [a.model_dump() for a in analyses],
                            "changes": changes,
                            "findings": findings,
                            "plan": plan,
                            "code_sources": [
                                s.model_dump() for s in self.collector.sources.values() if s.kind == "code"
                            ],
                        },
                    )
                else:
                    await self._worker(write_json, self._checkpoint_path, {})
                report = build_report(
                    mode,
                    analyses,
                    changes,
                    outcome=outcome,
                    issues=self.collector.issues,
                    findings=findings,
                    plan=plan,
                    remaining=remaining,
                    sources=self.collector.sources,
                )
                path = await self._worker(self.store.save_report, run_id, report)
                self._status = self._status.model_copy(
                    update={"phase": outcome, "remaining": remaining, "changes": len(changes)}
                )
                await self._emit(
                    event(
                        "checkpoint" if outcome == "continuing" else outcome,
                        phase=outcome,
                        reason=self._status.reason,
                        report_path=str(path),
                        findings=findings,
                        plan=plan,
                    )
                )
            except Exception as exc:
                self._status = self._status.model_copy(
                    update={"phase": "failed", "reason": f"记录保存失败：{exc}"}
                )
                if self.on_event:
                    await self.on_event(event("failed", phase="failed", reason=self._status.reason))
            finally:
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
