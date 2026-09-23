"""帮助内容（**纯逻辑，不依赖任何界面框架**）。

布局
----
* **双栏**（内容宽度 ≥74）：左键位、右命令，一次看全
* **紧凑分组**（46–73）：键位按分组压成几行
* 更窄时紧凑版会被裁切（终端实在太窄，屏幕上也放不下更多了）

两条纪律
--------
1. **内容全部来自单一事实来源**：键位来自 :mod:`logox.tui.keymap`，
   命令来自 :mod:`logox.tui.commands`。本模块只负责排版。
   ⚠️ 这里**不再自己维护一份命令清单**——历史上 ``help_content`` 与
   ``render/commands.py`` 各有一份，于是"命令加了但帮助没更新"完全可能发生，
   而帮助里写着一个按了没反应的命令，比少写一个糟糕得多。
2. **列宽不能用 ``str.ljust()`` 对齐**——它按字符数算，而中文占 2 cell，必然歪。
   所有补白一律走 ``pad_right`` / ``clip``（按 cell）。
"""

from __future__ import annotations

from rich.cells import cell_len
from rich.text import Text

from logox.config.schema import ThemePalette
from logox.tui.commands import command_catalog
from logox.tui.format import clip, pad_right
from logox.tui.keymap import KEYMAP_SECTIONS, format_keymap, section_of

__all__ = [
    "BOX_WIDTH",
    "COMPACT_GROUPS",
    "layout_for",
    "render_help",
]

#: 布局常量（全部按 **cell** 计）
SECTION_COLUMN = 5
#: 键位列宽。必须容得下最长的键位：`← → ↑ ↓ Home End` 与 `Ctrl+W / Alt+退格`。
KEY_COLUMN = 22
RIGHT_COLUMN = 24
GAP = 2

#: 理想的整行宽度（= 左栏 + 间距 + 右栏）；实际会按终端宽度收缩
BOX_WIDTH = 80

#: 低于此内容宽度时放弃双栏，改用分组紧凑版
TWO_COLUMN_MIN_WIDTH = 76
#: 极窄终端下的最小内容宽度（再窄就只能截断了）
COMPACT_MIN_WIDTH = 30

#: 右栏每行放几条命令（``/help`` 这类长度不一，两条正好填满一栏又不折行）
COMMANDS_PER_ROW = 2


def _compact_groups() -> tuple[tuple[str, str], ...]:
    """极窄终端下的键位摘要：**从 KEYMAP 现算**，不另写一份。

    历史教训：这里原来是一份手写的常量，于是它和 ``KEYMAP`` 一起漂移——
    帮助的窄屏版本里写着 ``Ctrl+2…6 侧栏``，而侧栏早就删了。
    """
    groups: list[tuple[str, str]] = []
    for name in KEYMAP_SECTIONS:
        items = section_of(name)
        if not items:
            continue
        groups.append((name, " · ".join(f"{item.keys} {item.action}" for item in items)))
    return tuple(groups)


#: 兼容旧名字（测试与文档引用它）：内容由 :func:`_compact_groups` 现算。
COMPACT_GROUPS: tuple[tuple[str, str], ...] = _compact_groups()


def layout_for(width: int) -> tuple[int, int, int]:
    """按终端宽度算出 ``(内容宽度, 左栏宽度, 行为列宽度)``。

    内容宽度要扣掉浮层边框（2）与横向内边距——算漏了就会出现"整行被折行、
    右栏错位"（`render/components/overlay.py` 的 `frame_box` 会在两边各加 1 格）。
    """
    # ★ D165：**整宽**（用户要求「所有提示框一律整宽展示」）。
    #   原先 `min(BOX_WIDTH, width - 6)` 会在宽终端上留出两侧空白，而权限/选择弹窗是整宽的 ——
    #   同一屏里两种宽度看着像没对齐。
    content = max(COMPACT_MIN_WIDTH, width - 4)
    left = content - RIGHT_COLUMN - GAP
    action = left - 2 - SECTION_COLUMN - KEY_COLUMN
    return content, left, max(10, action)


def _key_rows(action_width: int, left_width: int) -> list[str]:
    """左栏各行：分组标签与首行键位同行，省掉分组独立成行的开销。"""
    rows: list[str] = []
    for section, keys, action in format_keymap():
        label = pad_right(section, SECTION_COLUMN) if section else " " * SECTION_COLUMN
        key_cell = clip(keys, KEY_COLUMN) if cell_len(keys) > KEY_COLUMN else pad_right(keys, KEY_COLUMN)
        rows.append(pad_right("  " + label + key_cell + clip(action, action_width), left_width))
    return rows


def _command_rows(ready: list[str], planned: list[str]) -> list[str]:
    """右栏各行：可用命令在前（`✓`），未实现的在后（`○`）。

    **每条命令只放名字、不带说明**——说明由打错命令时的提示负责。
    理由是空间：右栏只有二十几格，带说明会各占一行，一折行反而把命令名自己挤掉了。
    """
    def chunk(names: list[str]) -> list[str]:
        return [
            "  " + " ".join(f"/{name}" for name in names[index : index + COMMANDS_PER_ROW])
            for index in range(0, len(names), COMMANDS_PER_ROW)
        ]

    rows = ["✓ 当前可用"]
    rows.extend(chunk(ready))
    if planned:
        rows.append("○ 还没实现")
        rows.extend(chunk(planned))
    return rows


def command_names() -> tuple[list[str], list[str]]:
    """``(可用, 未实现)`` 的命令名（**从 `tui/commands.py` 现算**）。"""
    ready = [row[0][1:] for row in command_catalog() if row[2]]
    planned = [row[0][1:] for row in command_catalog() if not row[2]]
    return ready, planned


def render_help(palette: ThemePalette, *, width: int = 100) -> Text:
    """渲染帮助内容（纯函数，可单测）。"""
    content, left_width, action_width = layout_for(width)
    ready, planned = command_names()
    out = Text()
    out.append(" Logox 帮助", style=f"bold {palette.accent}")
    out.append("        Esc 关闭\n", style=palette.text_faint)
    out.append("─" * content + "\n", style=palette.border_subtle)

    if content >= TWO_COLUMN_MIN_WIDTH:
        keys = _key_rows(action_width, left_width)
        commands = _command_rows(ready, planned)
        out.append(pad_right("  键盘", left_width + GAP), style=f"bold {palette.text_primary}")
        out.append("命令\n", style=f"bold {palette.text_primary}")
        for index in range(max(len(keys), len(commands))):
            if index < len(keys):
                out.append(keys[index] + " " * GAP, style=palette.text_muted)
            else:
                out.append(" " * (left_width + GAP))
            if index < len(commands):
                out.append(clip(commands[index], RIGHT_COLUMN) + "\n", style=palette.accent)
            else:
                out.append("\n")
        return out

    # 紧凑分组版：键位按分组压成几行；命令**分成多行**（一行放不下会被裁掉尾巴，
    # 而"命令列表只剩一个尾巴"正是本文件开头警告的那类缺陷）
    out.append(" 键盘\n", style=f"bold {palette.text_primary}")
    for label, description in COMPACT_GROUPS:
        prefix = "  " + pad_right(label, SECTION_COLUMN)
        out.append(prefix, style=f"bold {palette.text_muted}")
        out.append(
            clip(description, max(8, content - cell_len(prefix) - 1)) + "\n",
            style=palette.text_muted,
        )
    out.append(" 命令\n", style=f"bold {palette.text_primary}")
    wrap = max(8, content - 4)
    per_line = 5
    for index in range(0, len(ready), per_line):
        group = "  ".join(f"/{name}" for name in ready[index : index + per_line])
        out.append("  · " + clip(group, wrap) + "\n", style=palette.accent)
    for index in range(0, len(planned), per_line):
        group = "  ".join(f"/{name}" for name in planned[index : index + per_line])
        out.append("  ○ " + clip(group, wrap) + "\n", style=palette.text_faint)
    out.append(
        "    " + clip("· 当前可用    ○ 还没实现", wrap) + "\n", style=palette.text_faint
    )
    return out
