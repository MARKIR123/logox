"""Window registration and recency ordering, independent of lock acquisition order."""

from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from logox.anamnesis.io import FileLock, guarded_path, read_json, write_json


def project_identity(cwd: Path) -> str:
    text = str(cwd.resolve())
    return os.path.normcase(text)


@dataclass
class RunLease:
    run_id: str
    window_id: str
    project_id: str
    lock: FileLock

    def close(self) -> None:
        self.lock.close()


class AnamesisCoordinator:
    def __init__(self, root: Path, cwd: Path, *, window_id: str | None = None) -> None:
        self.root = guarded_path(root.absolute(), root.absolute())
        self.cwd = project_identity(cwd)
        self.window_id = window_id or uuid.uuid4().hex
        self.registry_path = self.root / "windows.json"
        self.sequence = 0
        self._submission_pending = False
        self._lease: RunLease | None = None

    def record_submission(self) -> None:
        # Input path changes memory only; disk sequence is allocated by the next poll.
        self._submission_pending = True

    def update(
        self,
        *,
        eligible: bool,
        busy: bool,
        last_submission: float = 0,
        idle: bool | None = None,
        session_id: str = "",
    ) -> None:
        with FileLock(self.root / "coordination.lock"):
            data = read_json(self.registry_path, {"sequence": 0, "windows": {}})
            windows = data["windows"]
            for key, value in list(windows.items()):
                if not _alive(int(value["pid"])):
                    del windows[key]
            previous = windows.get(self.window_id, {})
            if self._submission_pending:
                data["sequence"] += 1
                self.sequence = data["sequence"]
                self._submission_pending = False
                data.setdefault("served", {}).pop(self.cwd, None)
            else:
                self.sequence = previous.get("sequence", self.sequence)
            windows[self.window_id] = {
                "pid": os.getpid(),
                "project_id": self.cwd,
                "eligible": eligible,
                "idle": eligible if idle is None else idle,
                "session_id": session_id,
                "busy": busy,
                "sequence": self.sequence,
                "last_submission": last_submission,
                "heartbeat": time.time(),
            }
            write_json(self.registry_path, data)

    def _candidates(self, data: dict, manual: bool) -> list:
        # Group all live windows first: a busy sibling must not disappear from eligibility.
        live = [
            (key, value)
            for key, value in data["windows"].items()
            if _alive(int(value["pid"])) and time.time() - value["heartbeat"] < 20
        ]
        live.sort(key=lambda row: (row[1]["last_submission"], row[1]["sequence"], row[0]), reverse=True)
        projects = {}
        for row in live:
            projects.setdefault(row[1]["project_id"], []).append(row)
        candidates = []
        for members in projects.values():
            owner, state = members[0]
            if any(value["busy"] for _, value in members):
                continue
            ready = all(value.get("idle", value["eligible"]) for _, value in members) and state["eligible"]
            if (manual and owner == self.window_id) or ready:
                candidates.append((owner, state))
        return candidates

    def can_prepare(self, *, manual: bool = False) -> bool:
        with FileLock(self.root / "coordination.lock"):
            data = read_json(self.registry_path, {"sequence": 0, "windows": {}})
            candidates = self._candidates(data, manual)
            if manual:
                return any(owner == self.window_id for owner, _ in candidates)
            fresh = [row for row in candidates if row[1]["project_id"] not in data.get("served", {})]
            selected = fresh or candidates
            return bool(selected and selected[0][0] == self.window_id)

    def claim(self, *, manual: bool = False, run_id: str = "") -> RunLease | None:
        with FileLock(self.root / "coordination.lock"):
            data = read_json(self.registry_path, {"sequence": 0, "windows": {}})
            candidates = self._candidates(data, manual)
            served = data.setdefault("served", {})
            fresh = (
                candidates if manual else [row for row in candidates if row[1]["project_id"] not in served]
            )
            if candidates and not fresh:
                served.clear()
                write_json(self.registry_path, data)
            elif fresh:
                candidates = fresh
            if manual:
                if not any(owner == self.window_id for owner, _ in candidates):
                    return None
            elif not candidates or candidates[0][0] != self.window_id:
                return None
            run_lock = FileLock(self.root / "running.lock")
            if not run_lock.acquire():
                return None
            self._lease = RunLease(run_id or uuid.uuid4().hex, self.window_id, self.cwd, run_lock)
            return self._lease

    def yield_queue(self) -> None:
        with FileLock(self.root / "coordination.lock"):
            data = read_json(self.registry_path, {"sequence": 0, "windows": {}})
            data.setdefault("served", {})[self.cwd] = time.time()
            write_json(self.registry_path, data)

    def finish(self) -> None:
        if self._lease:
            self._lease.close()
            self._lease = None

    def unregister(self) -> None:
        self.finish()
        with FileLock(self.root / "coordination.lock"):
            data = read_json(self.registry_path, {"sequence": 0, "windows": {}})
            data["windows"].pop(self.window_id, None)
            write_json(self.registry_path, data)


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        import ctypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        handle = kernel.OpenProcess(0x1000, False, pid)
        if handle:
            kernel.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel.CloseHandle(handle)
            return True
        return ctypes.get_last_error() == 5
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
