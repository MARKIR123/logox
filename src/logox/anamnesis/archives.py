"""Host-owned archive commits with source, version, cancellation and recovery checks."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from logox.anamnesis.coordinator import project_identity
from logox.anamnesis.io import FileLock, atomic_write, guarded_path, read_json, write_json
from logox.anamnesis.models import ArchiveSnapshot, MemoryChange, MemoryEntry, SourceRef, digest_text
from logox.context.tokens import estimate_text_tokens

MARKER = "<!-- LOGOX ANAMNESIS v1 -->"


class ArchiveStore:
    def __init__(self, home: Path, cwd: Path, *, user_tokens: int = 800, project_tokens: int = 1600) -> None:
        self.home, self.cwd = home.absolute(), cwd.resolve()
        self.root = self.home / "anamnesis"
        self.project_id = project_identity(self.cwd)
        self.project_key = digest_text(self.project_id)[:24]
        self.limits = {"user": user_tokens, "project": project_tokens}
        self._sequences: dict[str, int] = {}
        self._trace_sizes: dict[str, int] = {}

    def _path(self, relative: str) -> Path:
        return guarded_path(self.root / relative, self.home)

    def target(self, scope: str) -> Path:
        if scope == "user":
            return guarded_path(self.home / "ANAMNESIS.md", self.home)
        if scope == "project":
            target = guarded_path(self.cwd / "ANAMNESIS.md", self.cwd)
            if target == self.home / "ANAMNESIS.md":
                raise ValueError("项目与用户档案目标重叠")
            return target
        raise ValueError("未知档案作用域")

    def load(self, scope: str) -> ArchiveSnapshot:
        target = self.target(scope)
        if target.exists() and target.stat().st_size > 65_536:
            raise ValueError("档案超过 64KiB，需人工整理后再自动更新")
        text = target.read_text(encoding="utf-8") if target.exists() else ""
        entries = []
        managed = text.startswith(MARKER) or not text.strip()
        if text.startswith(MARKER):
            # Machine metadata is kept in the auxiliary directory, never trusted from the MD.
            state = read_json(self._path(f"archives/{self._key(scope)}.json"), {})
            if state.get("digest") == digest_text(text):
                entries = [MemoryEntry.model_validate(e) for e in state.get("entries", [])]
            else:
                managed = False
        return ArchiveSnapshot(
            scope=scope,
            project_id=self.project_id,
            digest=digest_text(text),
            text=text,
            entries=entries,
            managed=managed,
        )

    def _key(self, scope: str) -> str:
        return "user" if scope == "user" else self.project_key

    def render(self, scope: str, entries: list[MemoryEntry]) -> str:
        title = "用户记忆" if scope == "user" else "项目记忆"
        parts = [MARKER, f"# ANAMNESIS · {title}", "", "可纠正的背景事实；当前指令和人工规则优先。", ""]
        for entry in entries:
            parts.extend(
                [
                    f"## {entry.category} · {entry.entry_id}",
                    entry.value,
                    f"来源：{', '.join(entry.source_ids)}",
                    "",
                ]
            )
        return "\n".join(parts).rstrip() + "\n"

    def commit(
        self,
        snapshot: ArchiveSnapshot,
        changes: list[MemoryChange],
        *,
        verify: Callable[[str], bool],
        awake: Callable[[], bool],
    ) -> str:
        if not changes:
            return "无变更"
        if not snapshot.managed:
            raise ValueError("档案已由用户手工维护或修改；保留提案，不自动覆盖")
        scope = snapshot.scope
        with FileLock(self._path(f"locks/{self._key(scope)}.lock")):
            current = self.load(scope)
            if current.digest != snapshot.digest:
                raise ValueError("档案版本冲突；保留提案")
            entries = {e.entry_id: e for e in current.entries}
            seen = set()
            for change in changes:
                if change.scope != scope or change.entry_id in seen or change.status == "candidate":
                    raise ValueError("作用域／重复条目／候选状态不允许提交")
                seen.add(change.entry_id)
                if not all(verify(s) for s in change.source_ids):
                    raise ValueError("来源已失效或变化，不能提交")
                old = entries.get(change.entry_id)
                if change.action == "add" and old is not None:
                    raise ValueError("新增条目已存在")
                if change.action != "add" and (old is None or old.value != change.old_value):
                    raise ValueError("变更旧值不匹配")
                if change.action == "delete":
                    del entries[change.entry_id]
                else:
                    if not change.new_value.strip():
                        raise ValueError("不能保存空事实")
                    entries[change.entry_id] = MemoryEntry(
                        entry_id=change.entry_id,
                        category=change.category,
                        value=change.new_value,
                        source_ids=change.source_ids,
                    )
            text = self.render(scope, list(entries.values()))
            if estimate_text_tokens(text) > self.limits[scope]:
                raise ValueError("候选档案超出正文预算，需模型精简后重提")
            if awake():
                raise InterruptedError("已唤醒，不启动档案提交")
            change_id = uuid.uuid4().hex
            journal = self._path(f"revisions/{change_id}.json")
            data = {
                "id": change_id,
                "scope": scope,
                "project_id": self.project_id,
                "old_digest": current.digest,
                "new_digest": digest_text(text),
                "old_text": current.text,
                "new_text": text,
                "old_entries": [e.model_dump() for e in current.entries],
                "entries": [e.model_dump() for e in entries.values()],
                "changes": [c.model_dump() for c in changes],
                "state": "prepared",
                "time": time.time(),
            }
            write_json(journal, data)
            if awake() or self.load(scope).digest != current.digest:
                raise InterruptedError("唤醒或外部编辑；已保留候选与旧版")
            atomic_write(self.target(scope), text)
            write_json(
                self._path(f"archives/{self._key(scope)}.json"),
                {"digest": data["new_digest"], "entries": data["entries"]},
            )
            data["state"] = "committed"
            write_json(journal, data)
            return change_id

    def recover_pending(self) -> list[str]:
        results = []
        folder = self._path("revisions")
        if not folder.exists():
            return results
        for path in sorted(folder.glob("*.json")):
            data = read_json(path, {})
            if data.get("state") != "prepared" or (
                data.get("scope") == "project" and data.get("project_id") != self.project_id
            ):
                continue
            scope = data["scope"]
            with FileLock(self._path(f"locks/{self._key(scope)}.lock")):
                target = self.target(scope)
                actual = digest_text(target.read_text(encoding="utf-8") if target.exists() else "")
                if actual == data["new_digest"]:
                    write_json(
                        self._path(f"archives/{self._key(scope)}.json"),
                        {"digest": actual, "entries": data["entries"]},
                    )
                    data["state"] = "committed"
                elif actual == data["old_digest"]:
                    data["state"] = "not_applied"
                else:
                    data["state"] = "external_conflict"
                write_json(path, data)
                results.append(f"{data['id']}: {data['state']}")
        return results

    def progress(self) -> dict:
        return read_json(
            self._path(f"progress/{self.project_key}.json"), {"processed": [], "sleep_fingerprint": ""}
        )

    def save_source(self, source: SourceRef) -> None:
        if source.kind != "code" or source.project_id != self.project_id or not source.source_id.isalnum():
            raise ValueError("不能保存未知／跨项目代码来源")
        write_json(self._path(f"evidence/{self.project_key}/{source.source_id}.json"), source.model_dump())

    def load_sources(self, identities: set[str]) -> list[SourceRef]:
        found = []
        for ident in identities:
            if not ident.isalnum():
                continue
            data = read_json(self._path(f"evidence/{self.project_key}/{ident}.json"), {})
            if not data:
                continue
            source = SourceRef.model_validate(data)
            if source.kind == "code" and source.project_id == self.project_id and source.source_id == ident:
                found.append(source)
        return found

    def global_processed(self) -> set[str]:
        return set(read_json(self._path("progress/user.json"), {"processed": []})["processed"])

    def save_progress(
        self, processed: set[str], *, project_processed: set[str] | None = None, sleep_fingerprint: str = ""
    ) -> None:
        with FileLock(self._path("locks/progress.lock")):
            old = self.progress()
            old["processed"] = sorted(
                set(old["processed"]) | (processed if project_processed is None else project_processed)
            )
            if sleep_fingerprint:
                old["sleep_fingerprint"] = sleep_fingerprint
            write_json(self._path(f"progress/{self.project_key}.json"), old)
            user = self.global_processed() | processed
            write_json(self._path("progress/user.json"), {"processed": sorted(user)})

    def bind_run(self, run_id: str, session_id: str) -> tuple[str, bool]:
        path = self._run_path(run_id, "preview.json")
        data = read_json(path, {})
        if data:
            return data.get("session_id", ""), False
        if self._run_path(run_id, "events.jsonl").exists():
            # Legacy traces never captured ownership; do not guess a conversation.
            write_json(path, {"run_id": run_id, "session_id": "", "sequence": 0})
            return "", False
        write_json(path, {"run_id": run_id, "session_id": session_id, "sequence": 0})
        return session_id, True

    def _run_path(self, run_id: str, filename: str) -> Path:
        if not run_id or not run_id.isascii() or not run_id.isalnum():
            raise ValueError("非法运行身份")
        return self._path(f"runs/{self.project_key}/{run_id}/{filename}")

    def run_history(self, session_id: str | None = None) -> list[dict]:
        directory = self._path(f"runs/{self.project_key}")
        if not directory.exists():
            return []
        results = []
        for folder in directory.iterdir():
            if not folder.is_dir() or not folder.name.isascii() or not folder.name.isalnum():
                continue
            try:
                path = self._run_path(folder.name, "preview.json")
                data = read_json(path, {})
                if not isinstance(data, dict):
                    raise ValueError("预览格式错误")
                if data.get("run_id", folder.name) != folder.name:
                    raise ValueError("预览身份不匹配")
                if session_id is not None and (not session_id or data.get("session_id") != session_id):
                    continue
                results.append({**data, "run_id": folder.name})
            except (ValueError, OSError) as exc:
                if session_id is None:
                    results.append({"run_id": folder.name, "error": f"预览读取失败：{exc}"})
                else:
                    raise ValueError(f"入梦 {folder.name} 预览读取失败：{exc}") from exc
        return sorted(results, key=lambda d: d.get("started_at", 0))

    def read_run(self, run_id: str, *, trace: bool = False) -> str:
        path = self._run_path(run_id, "events.jsonl" if trace else "report.md")
        return path.read_text(encoding="utf-8") if path.exists() else "该次入梦尚无记录／报告"

    def _trace_sequence(self, run_id: str, path: Path, fallback: int) -> int:
        size = path.stat().st_size if path.exists() else 0
        sequence = max(fallback, self._sequences.get(run_id, 0))
        if run_id in self._sequences and self._trace_sizes.get(run_id) == size:
            return sequence
        if path.exists():
            with path.open("rb") as handle:
                handle.seek(max(0, path.stat().st_size - 1_048_576))
                tail = handle.read()
            for raw in reversed(tail.split(b"\n")):
                try:
                    value = json.loads(raw)
                    if isinstance(value, dict) and isinstance(value.get("sequence"), int):
                        sequence = max(sequence, value["sequence"])
                        break
                except (ValueError, UnicodeError):
                    continue
        self._sequences[run_id] = sequence
        return sequence

    def append_record(self, run_id: str, data: dict) -> dict:
        # The complete trace is authoritative; preview state never enters model history.
        path = self._run_path(run_id, "events.jsonl")
        preview_path = self._run_path(run_id, "preview.json")
        preview = read_json(preview_path, {})
        data = {
            **data,
            "sequence": self._trace_sequence(run_id, path, preview.get("sequence", 0)) + 1,
            "timestamp": time.time(),
            "session_id": preview.get("session_id", data.get("session_id", "")),
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        separator = ""
        if path.exists() and path.stat().st_size:
            with path.open("rb") as handle:
                handle.seek(-1, 2)
                if handle.read(1) != b"\n":
                    separator = "\n"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(separator + json.dumps(data, ensure_ascii=False) + "\n")
            handle.flush()
        self._sequences[run_id] = data["sequence"]
        self._trace_sizes[run_id] = path.stat().st_size
        preview.update(run_id=run_id, session_id=data["session_id"], sequence=data["sequence"])
        if data.get("kind") == "started":
            preview["reason"] = ""
        for key in ("mode", "phase", "question", "reason", "started_at", "report_path"):
            if data.get(key):
                preview[key] = data[key]
        preview["timestamp"] = data["timestamp"]
        for key in ("findings", "plan"):
            if data.get(key):
                preview[key] = data[key][-30:]
        if data.get("delta"):
            preview["reasoning"] = (preview.get("reasoning", "") + data["delta"])[-12000:]
        for field, identity, limit in (("analysis", "record_id", 64), ("change", "entry_id", 50)):
            value = data.get(field)
            if value:
                records = preview.setdefault(field + "_events", {})
                key = value.get("scope", "") + ":" + value[identity]
                records[key] = data
                while len(records) > limit:
                    del records[next(iter(records))]
        if data.get("operation"):
            ops = preview.setdefault("operations", {})
            op = {**data["operation"]}
            for key in ("result", "error"):
                if key in op:
                    op[key] = str(op[key])[:2000]
            ops[op.get("call_id", str(data["sequence"]))] = op
            while len(ops) > 32:
                del ops[next(iter(ops))]
        write_json(preview_path, preview)
        return data

    def save_report(self, run_id: str, text: str) -> Path:
        if not run_id.isalnum():
            raise ValueError("非法运行身份")
        path = self._path(f"runs/{self.project_key}/{run_id}/report.md")
        atomic_write(path, text)
        write_json(self._path(f"reports/{self.project_key}.json"), {"path": str(path), "run_id": run_id})
        return path

    def latest_report(self) -> Path | None:
        data = read_json(self._path(f"reports/{self.project_key}.json"), {})
        path = Path(data["path"]) if data else None
        return guarded_path(path, self.root) if path else None

    def revert(self, change_id: str, *, awake: Callable[[], bool] = lambda: False) -> str:
        if not change_id.isalnum():
            raise ValueError("非法变更身份")
        data = read_json(self._path(f"revisions/{change_id}.json"), {})
        if data.get("state") != "committed" or data.get("project_id") != self.project_id:
            raise ValueError("变更不存在或不属于当前项目")
        scope = data["scope"]
        with FileLock(self._path(f"locks/{self._key(scope)}.lock")):
            current = self.load(scope)
            if current.digest != data["new_digest"] or awake():
                raise ValueError("撤销版本冲突或已唤醒")
            ident = uuid.uuid4().hex
            reverse = {
                **data,
                "id": ident,
                "old_digest": data["new_digest"],
                "new_digest": data["old_digest"],
                "old_text": data["new_text"],
                "new_text": data["old_text"],
                "entries": data["old_entries"],
                "old_entries": data["entries"],
                "reverts": change_id,
                "state": "prepared",
            }
            journal = self._path(f"revisions/{ident}.json")
            write_json(journal, reverse)
            if self.load(scope).digest != current.digest or awake():
                raise ValueError("撤销前版本变化或已唤醒")
            atomic_write(self.target(scope), reverse["new_text"])
            write_json(
                self._path(f"archives/{self._key(scope)}.json"),
                {"digest": reverse["new_digest"], "entries": reverse["entries"]},
            )
            reverse["state"] = "committed"
            write_json(journal, reverse)
            return ident
