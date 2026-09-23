"""Unified Diff 的**纯数据表示与解析**（D140 / F-50）。

它是什么
--------
两个东西，都不含任何界面成分：

* :class:`DiffHunk` / :data:`DiffLineKind` —— 一段 diff 的**结构化表示**（纯数据）；
* :func:`parse_unified_diff` —— 把 unified diff **文本**解析成上面的结构（纯字符串处理）。

为什么单独一个模块（而不是留在 ``tui/content/cards.py`` 里）
--------------------------------------------------------
因为它有**两个不同层的消费者**，而其中一个是工具层：

=======================================  ==================  ==================
谁                                      需要什么             用途
=======================================  ==================  ==================
``tools/fs_edit.py``（生成侧）           类型 + 解析          用 ``difflib`` 生成 diff 文本后
                                                              解析回 hunks，交给界面展示
``tui/content/cards.py``（渲染侧）        类型                  ``render_diff`` 画出带颜色的 hunk
``tui/content/timeline.py``              类型                 ``Block.hunks`` 的类型标注
=======================================  ==================  ==================

以前这些住在 ``tui/content/cards.py``（M1.5 时的临时决定，文件里也写着"将来搬走"），
后果是 **`import logox.tools.<任何东西>` 都会连带加载界面层**（实测：`tui` /
`tui.content` / `tui.content.cards` / `tui.format` 四个模块 + `rich.text` + 主题色板）。
也就是工具层**反向依赖**界面层 —— 而它本该能在没有界面的进程里独立使用（headless）。

**为什么落点是这里、而不是 ``tools/`` 下面**：界面也要用这个类型，
而界面层的 import 白名单（``tests/unit/test_kernel_port.py::T41``）只有
``logox.errors`` / ``logox.paths`` / ``logox.config`` / ``logox.kernel`` / ``logox.tui``
—— ``logox.tools`` **不在其中**。所以它必须是一个**两边之下的中立叶子模块**，
与 :mod:`logox.errors`、:mod:`logox.paths` 同级。

约束（**改这个文件前先读**）
---------------------------
* **零项目内依赖**：只允许标准库。任何 ``logox.*`` 的 import 都会让它重新变成
  "需要分层判断的模块"，这个文件存在的全部意义就没了。
* **不含渲染**：不许 import ``rich``。想加颜色/排版，请去 ``tui/content/cards.py``。
* 界面渲染仍留在 ``cards.py``（``render_diff`` 等），并且它从这里转出这两个名字 ——
  老的 ``from logox.tui.content.cards import DiffHunk`` 写法继续可用。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

__all__ = [
    "DiffHunk",
    "DiffLineKind",
    "parse_unified_diff",
]


# --------------------------------------------------------------------------- #
# diff 数据与解析
# --------------------------------------------------------------------------- #

DiffLineKind = Literal["context", "add", "del", "meta"]


@dataclass(frozen=True)
class DiffHunk:
    """一个 hunk（``@@`` 块）。``lines`` 为 ``(kind, text)`` 序列。"""

    header: str
    lines: tuple[tuple[DiffLineKind, str], ...] = ()

    @property
    def added(self) -> int:
        return sum(1 for kind, _ in self.lines if kind == "add")

    @property
    def removed(self) -> int:
        return sum(1 for kind, _ in self.lines if kind == "del")


def parse_unified_diff(text: str) -> list[DiffHunk]:
    """把 unified diff 文本解析成 hunks（文件头 ``---``/``+++`` 由调用方处理）。"""
    hunks: list[DiffHunk] = []
    header: str | None = None
    lines: list[tuple[DiffLineKind, str]] = []

    def flush() -> None:
        nonlocal header, lines
        if header is not None:
            hunks.append(DiffHunk(header=header, lines=tuple(lines)))
        header, lines = None, []

    for raw in text.splitlines():
        if raw.startswith("@@"):
            flush()
            header = raw
            continue
        if header is None:
            continue
        if raw.startswith("+++") or raw.startswith("---"):
            continue
        if raw.startswith("+"):
            lines.append(("add", raw[1:]))
        elif raw.startswith("-"):
            lines.append(("del", raw[1:]))
        elif raw.startswith("\\"):
            lines.append(("meta", raw))
        else:
            lines.append(("context", raw[1:] if raw.startswith(" ") else raw))
    flush()
    return hunks
