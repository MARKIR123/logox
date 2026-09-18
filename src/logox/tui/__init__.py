"""TUI 表现层（D12：Textual 全屏多窗格）。

**本模块的 ``__init__`` 刻意不 import textual**——`cli.py` 的 ``--version``
快速路径会在 import 链上经过它，任何重导入都会破坏 D29 的 300ms 预算。
真正需要 textual 的地方（``tui.app`` / ``tui.widgets.*``）自行导入。
"""

from __future__ import annotations

__all__: list[str] = []
