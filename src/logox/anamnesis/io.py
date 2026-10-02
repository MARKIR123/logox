"""Small file locks and single-file durable replacement. No multi-file claim."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


class FileLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle = None

    def acquire(self) -> bool:
        if self.handle is not None:
            return True
        _plain_path(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise ValueError("锁文件不能是符号链接")
        handle = self.path.open("a+b")
        try:
            if handle.seek(0, 2) == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self.handle = handle
        return True

    def close(self) -> None:
        handle, self.handle = self.handle, None
        if handle is not None:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()

    def __enter__(self):
        if not self.acquire():
            raise BlockingIOError("入梦协调／档案正在使用")
        return self

    def __exit__(self, *_):
        self.close()


def guarded_path(path: Path, root: Path) -> Path:
    """Reject links in host-owned paths, including existing ancestor directories."""
    path, root = path.absolute(), root.absolute()
    path.relative_to(root)
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError(f"入梦保存路径不能含符号链接：{part}")
        if part == root:
            break
    if path.resolve() != path:
        raise ValueError("入梦保存目标的物理路径发生变化")
    return path


def atomic_write(path: Path, text: str) -> None:
    _plain_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def write_json(path: Path, data: Any) -> None:
    atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def read_json(path: Path, default: Any) -> Any:
    _plain_path(path)
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _plain_path(path: Path) -> None:
    absolute = path.absolute()
    if absolute.resolve() != absolute or any(p.is_symlink() for p in (absolute, *absolute.parents)):
        raise ValueError("入梦辅助文件路径含链接或物理路径发生变化")
