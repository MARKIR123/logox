"""Logox —— 内核极简、外延极松、界面体面的终端 Agent（TUI）。

本模块**只暴露版本号**，禁止在此 import 任何重依赖。

这条约束不是洁癖：``cli.py`` 的 ``--version`` / ``--help`` 快速路径必须在
界面层与 Provider SDK **尚未导入**的前提下完成，才能满足 D29 的
「``--version`` < 300ms」指标。任何在此文件里 import 重依赖的改动都会
直接破坏该指标。
"""

from __future__ import annotations

__all__ = ["VERSION_INFO", "__version__"]

__version__ = "0.1.0"
VERSION_INFO: tuple[int, int, int] = (0, 1, 0)
