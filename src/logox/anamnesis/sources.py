"""Bounded session excerpts with physical provenance and active rewind semantics."""

from __future__ import annotations

import json
import math
import os
import threading
from datetime import datetime
from pathlib import Path

from logox.anamnesis.coordinator import project_identity
from logox.anamnesis.models import SourceRef, digest_text
from logox.paths import is_sensitive_path
from logox.store.manager import SessionManager
from logox.store.replay import filter_rewound_records

EXCLUDED_DIRS = frozenset(
    {
        ".git",
        ".logox",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".test-tmp",
        "legacy",
        "build",
        "dist",
        "audits",
    }
)


class SourceCollector:
    def __init__(self, sessions: Path, cwd: Path) -> None:
        self.sessions, self.cwd = sessions.resolve(), cwd.resolve()
        self.project_id = project_identity(self.cwd)
        self.current_bucket = SessionManager(sessions).get_project_dir(self.cwd).resolve()
        self.sources: dict[str, SourceRef] = {}
        self.issues: list[str] = []
        self._cache: dict[Path, tuple[tuple[int, int], list[SourceRef], list[str]]] = {}

    def collect(self, stop: threading.Event | None = None) -> list[SourceRef]:
        self.issues = []
        refs: list[SourceRef] = []
        if not self.current_bucket.exists() or not self.current_bucket.is_relative_to(self.sessions):
            self.sources = {}
            return []
        for folder, dirs, names in os.walk(self.current_bucket, followlinks=False):
            dirs[:] = sorted(
                d for d in dirs if d not in {"tools", ".trash"} and not (Path(folder) / d).is_symlink()
            )
            for name in sorted(names):
                if stop and stop.is_set():
                    raise InterruptedError("资料收集已唤醒")
                if not name.endswith(".jsonl"):
                    continue
                path = Path(folder) / name
                if path.is_symlink() or not path.resolve().is_relative_to(self.sessions):
                    continue
                stat = path.stat()
                stamp = (stat.st_mtime_ns, stat.st_size)
                cached = self._cache.get(path)
                if cached is None or cached[0] != stamp:
                    items, issues = self._read_session(path, stop)
                    self._cache[path] = stamp, items, issues
                else:
                    _, items, issues = cached
                refs.extend(item for item in items if item.project_id == self.project_id)
                self.issues.extend(issues)
        self.sources = {ref.source_id: ref for ref in refs}
        return refs

    @property
    def project_latest_timestamp(self) -> float:
        # Assistant narration cannot establish that an old underlying observation is current.
        return max(
            (
                ref.timestamp
                for ref in self.sources.values()
                if ref.project_id == self.project_id
                and ref.kind != "assistant_statement"
                and math.isfinite(ref.timestamp)
                and ref.timestamp > 0
            ),
            default=0,
        )

    def project_freshness_reason(self, refs: list[SourceRef], latest: float) -> str:
        if any(ref.kind == "code" for ref in refs):
            return ""  # The caller still verifies current file digests before semantic review/commit.
        dated = [
            ref.timestamp
            for ref in refs
            if ref.kind != "assistant_statement" and math.isfinite(ref.timestamp) and ref.timestamp > 0
        ]
        if not dated:
            return "项目证据时间不明确，不能据此更新当前状态；保留候选"
        if max(dated) < latest:
            return "仅有旧会话状态，已有较新项目资料；缺少当前依据，保留候选"
        return ""

    def last_submission(self, session_id: str) -> float:
        if not session_id:
            return 0
        path = self.current_bucket / f"{session_id}.jsonl"
        if path.name != f"{session_id}.jsonl" or not path.resolve().is_relative_to(self.current_bucket):
            raise ValueError("非法会话身份")
        if not path.exists() or path.is_symlink():
            return 0
        latest = 0.0
        with path.open("rb") as handle:
            while raw := handle.readline(1_048_577):
                if len(raw) > 1_048_576:
                    while raw and not raw.endswith(b"\n"):
                        raw = handle.readline(65_536)
                    continue
                try:
                    record = json.loads(raw)
                    if isinstance(record, dict) and record.get("role") == "user":
                        latest = max(latest, _timestamp(record.get("timestamp", record.get("ts", 0))))
                    elif isinstance(record, dict) and record.get("type") == "anamnesis_ref":
                        latest = max(latest, _timestamp(record.get("submission_ts", 0)))
                except (ValueError, TypeError, UnicodeError):
                    continue
        return latest

    def _read_session(self, path: Path, stop: threading.Event | None) -> tuple[list[SourceRef], list[str]]:
        records, issues = [], []
        project = (
            self.project_id if path.parent.resolve() == self.current_bucket else f"bucket:{path.parent.name}"
        )
        with path.open("rb") as handle:
            line = 0
            while True:
                if stop and stop.is_set():
                    raise InterruptedError("资料收集已唤醒")
                raw = handle.readline(1_048_577)
                if not raw:
                    break
                line += 1
                if not raw.endswith(b"\n"):
                    if len(raw) > 1_048_576:
                        while raw and not raw.endswith(b"\n"):
                            raw = handle.readline(65_536)
                        issues.append(f"{path.name}:{line} 超大记录未读取，仍有未覆盖资料")
                    else:
                        issues.append(f"{path.name}:{line} 尾行尚未完整写入")
                    continue
                try:
                    record = json.loads(raw)
                    if not isinstance(record, dict):
                        raise ValueError("不是记录对象")
                    record["_physical_line"] = line
                    record["_digest"] = digest_text(raw.decode("utf-8"))
                    int(record.get("turn", 0))
                    if record.get("type") == "session_rewind":
                        int(record.get("to_turn", 0))
                    if record.get("cwd") and not record.get("role") and record.get("type") == "session_init":
                        project = project_identity(Path(record["cwd"]))
                    records.append(record)
                except (ValueError, TypeError, UnicodeError):
                    issues.append(f"{path.name}:{line} 损坏记录，资料有缺口")
        active = filter_rewound_records(records)
        states = {
            r.get("turn", 0): str(r.get("reason", "finished"))
            for r in active
            if r.get("type") == "turn_finished"
        }
        refs = []
        for record in active + [r for r in records if r.get("type") == "session_rewind"]:
            role = record.get("role")
            kind = {"user": "user_message", "assistant": "assistant_statement", "tool": "tool_result"}.get(
                role
            )
            if record.get("type") == "session_rewind":
                kind = "history_revision"
                record = {
                    **record,
                    "content": f"历史回滚：turn >= {record['to_turn']} 的旧记录不再属于有效对话。已有档案引用这些旧来源时须重新核验，不将回滚当成新偏好。",
                }
            if kind is None:
                continue
            content = str(record.get("content", "") or "")
            blob = record.get("blob") or record.get("blob_file")
            if kind == "tool_result" and blob:
                content = f"工具 {record.get('tool_name', record.get('tool', ''))}；is_error={record.get('is_error')}; 归档指针={blob}\n{content[:1000]}"
            # A large user message is split, not silently marked fully processed after a prefix.
            for offset in range(0, len(content), 2000):
                excerpt = content[offset : offset + 2000]
                if not excerpt.strip():
                    continue
                ident = digest_text(f"{path}:{record['_physical_line']}:{record['_digest']}:{offset}")[:24]
                refs.append(
                    SourceRef(
                        source_id=ident,
                        kind=kind,
                        project_id=project,
                        path=str(path),
                        session_id=path.stem,
                        line=record["_physical_line"],
                        digest=record["_digest"],
                        content=excerpt,
                        turn=int(record.get("turn", 0)),
                        turn_state=states.get(record.get("turn", 0), "unfinished"),
                        offset=offset,
                        timestamp=_timestamp(record.get("timestamp", record.get("ts", 0))),
                    )
                )
        return refs, issues

    def verify(self, source_id: str) -> bool:
        ref = self.sources.get(source_id)
        if ref is None:
            return False
        path = Path(ref.path)
        try:
            if ref.kind == "code":
                return (
                    permitted_code_path(path, self.cwd)
                    and digest_text(path.read_text(encoding="utf-8")) == ref.digest
                )
            if ref.project_id != self.project_id or not path.resolve().is_relative_to(self.current_bucket):
                return False
            # Recheck effective view on changed files, including a rewind appended after collection.
            stat = path.stat()
            cached = self._cache.get(path)
            if cached is None or cached[0] != (stat.st_mtime_ns, stat.st_size):
                refs, _ = self._read_session(path, None)
                return any(item.source_id == source_id for item in refs)
            return any(item.source_id == source_id for item in cached[1])
        except (OSError, UnicodeError, ValueError):
            return False

    def register_code(self, path: Path, text: str, start: int, end: int, excerpt: str) -> SourceRef:
        ref = SourceRef(
            source_id=digest_text(f"{path}:{digest_text(text)}:{start}:{end}")[:24],
            kind="code",
            project_id=self.project_id,
            path=str(path),
            line=start,
            end_line=end,
            digest=digest_text(text),
            content=excerpt,
        )
        self.sources[ref.source_id] = ref
        return ref

    def code_files(self, stop: threading.Event | None = None) -> list[Path]:
        files = []
        for folder, dirs, names in os.walk(self.cwd, followlinks=False):
            if stop and stop.is_set():
                raise InterruptedError("代码枚举已唤醒")
            dirs[:] = sorted(
                d for d in dirs if d.casefold() not in EXCLUDED_DIRS and not (Path(folder) / d).is_symlink()
            )
            for name in sorted(names):
                path = Path(folder) / name
                if permitted_code_path(path, self.cwd):
                    files.append(path)
        return files

    def code_fingerprint(self, stop: threading.Event | None = None) -> str:
        return digest_text(
            "\n".join(
                f"{p.relative_to(self.cwd)}:{p.stat().st_mtime_ns}:{p.stat().st_size}"
                for p in self.code_files(stop)
            )
        )


def permitted_code_path(path: Path, cwd: Path) -> bool:
    try:
        relative = path.resolve().relative_to(cwd.resolve())
    except (ValueError, OSError):
        return False
    return (
        not path.is_symlink()
        and not is_sensitive_path(path)
        and not any(part.casefold() in EXCLUDED_DIRS for part in relative.parts)
        and path.name.casefold() != "anamnesis.md"
        and path.suffix.casefold() not in {".pem", ".key", ".pfx", ".p12"}
    )


def _timestamp(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        try:
            return datetime.fromisoformat(str(value)).timestamp()
        except ValueError:
            return 0.0
