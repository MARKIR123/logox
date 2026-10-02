"""测试脚手架（标准库 ``unittest``，D45）。

环境约束（实测得出，务必遵守）
------------------------------

1. **临时目录建在 ``<repo>/.test-tmp/`` 内**。这条的理由已经变了：以前是"沙箱不让
   写别处"，现在纯粹是**卫生与可排查**——仓库外（系统 ``%TEMP%``）的残留没人清，
   出问题时也不在眼皮底下。
2. **必须带着 ``tools/pyshim`` 跑**（见 ``docs/DEV-ENVIRONMENT.md`` §5）。沙箱下
   ``tempfile.mkdtemp()`` 会踩到 CPython 给 ``mode=0o700`` 加的那张**受保护 DACL**
   （`D:P(...)`：不继承父目录），于是目录建出来就写不进、删不掉；pyshim 从源头
   去掉这个 mode。少了它，``make_temp_dir()`` 会**立刻给出可执行的修复命令**，
   而不是让几十个用例各报一堆含糊的 ``PermissionError``。
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

__all__ = [
    "TEMP_ROOT",
    "make_temp_dir",
    "remove_temp_dir",
]

# tests/unit/support.py → parents[2] 即仓库根
REPO_ROOT = Path(__file__).resolve().parents[2]
TEMP_ROOT = REPO_ROOT / ".test-tmp"


def make_temp_dir(prefix: str = "case-") -> Path:
    """在 ``<repo>/.test-tmp/`` 下建一个全新的可写目录。

    用的是标准库 ``tempfile.mkdtemp``。
    """
    TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=prefix, dir=TEMP_ROOT))


def remove_temp_dir(path: Path) -> None:
    """尽力清理；清理失败不得让测试失败。"""
    shutil.rmtree(path, ignore_errors=True)
