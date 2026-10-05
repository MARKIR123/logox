"""`/` 命令自动补全：**候选计算**（触发判据 + 匹配排序）。

设计文档：`docs/modules/MODULE_tui_completion.md`。

★ **D175：本模块不再自己实现"列表怎么显示/怎么移动"** —— 那是一套已经存在的逻辑
（`content/overlay.py` 的 `PickerState` + `render_picker`，`/model`、`/resume`、`/rewind` 都在用）。
补全与它们**只是内容不同**，交互完全一样 ⇒ **复用**。

为什么必须复用（真实教训）：我原先自己写了一份（窗口、环绕、指针、高亮各一遍），
结果**高亮的行号用成了全量下标**、而渲染按窗口画 ⇒ 选项一超过窗口大小，指针与高亮就错位。
**同一套交互实现两遍，就是把同一个 bug 写两遍**。
"""

from __future__ import annotations

from logox.tui import commands as command_catalog
from logox.tui.content.overlay import MAX_VISIBLE as PICKER_MAX_VISIBLE
from logox.tui.content.overlay import Choice, PickerState, render_picker

__all__ = [
    "MAX_VISIBLE",
    "completion_for",
    "is_completion_context",
    "render_completion",
]

#: 补全**可见几行**（与 Pi 的 `autocompleteMaxVisible` 默认值一致）。
#: ⚠️ 它只决定"显示几行"，**不决定能选到哪** —— 候选始终是全量（D174 的教训）。
MAX_VISIBLE = 5

#: 提示框宽度：整宽（D170）；实际渲染时由布局把可用宽度传进来。
BOX_WIDTH = 80


def is_completion_context(text: str, *, row: int, col: int) -> bool:
    """现在该不该显示候选（判据见设计文档 §3.1）。

    四条同时成立才显示：

    1. 光标在**第一行**（命令必须从输入最前面开始）；
    2. 该行光标之前的部分 `lstrip()` 后以 `/` 开头；
    3. 光标之前**没有空白**（说明还在打命令名，没进参数区）；
    4. 光标之前**没有第二个 `/`** —— `/g/hz/codes` 这类路径因此不触发。

    ⚠️ 第 4 条是前缀匹配之外的第二道路径保护（`is_completion_context` 自己也要被测，
    否则"前缀匹配"会替它挡住场景，用例就分不出谁在起作用 —— 实测踩过）。
    """
    lines = text.split("\n")
    if row != 0 or row >= len(lines):
        return False
    before = lines[row][:col]
    stripped = before.lstrip()
    if not stripped.startswith("/"):
        return False
    if any(ch.isspace() for ch in stripped):
        return False
    return stripped.count("/") <= 1


def completion_for(
    text: str,
    *,
    row: int = 0,
    col: int | None = None,
    available: dict[str, str] | None = None,
    planned: dict[str, str] | None = None,
    aliases: dict[str, str] | None = None,
) -> PickerState | None:
    """算候选。返回 **`PickerState`**（与 `/rewind` 等同一套状态机），不该显示时返回 ``None``。

    参数默认取**命令表**（`tui/commands.py`，界面与文档的单一事实来源），但允许注入
    —— 于是本函数可以在测试里喂任意命令集，不依赖真实表。
    """
    available = command_catalog.AVAILABLE_COMMANDS if available is None else available
    planned = command_catalog.PLANNED_COMMANDS if planned is None else planned
    aliases = command_catalog.ALIASES if aliases is None else aliases

    lines = text.split("\n")
    if col is None:
        col = len(lines[row]) if row < len(lines) else 0
    if not is_completion_context(text, row=row, col=col):
        return None

    typed = lines[row][:col].lstrip()[1:].strip().lower()

    # 候选池：可用命令在前、planned 在后；组内保持**命令表顺序**（表按用途排，比字母序有意义）
    pool: list[tuple[str, str, bool]] = [(name, desc, True) for name, desc in available.items()]
    pool += [(name, desc, False) for name, desc in planned.items()]

    exact: list[Choice] = []
    matched: list[Choice] = []
    for name, description, implemented in pool:
        via_alias = ""
        if typed:
            if name.startswith(typed):
                pass
            else:
                # 别名命中：显示**规范名**，但把用户敲的别名写进说明（避免候选里出现重复项）
                via_alias = next(
                    (
                        alias
                        for alias, target in aliases.items()
                        if target == name and alias.startswith(typed)
                    ),
                    "",
                )
                if not via_alias:
                    continue
        label = f"/{name}" if implemented else f"/{name}（未实现）"
        hint = description if not via_alias else f"{description}（别名 /{via_alias}）"
        choice = Choice(value=name, label=label, hint=hint)
        if via_alias and via_alias == typed:
            exact.append(choice)
        else:
            matched.append(choice)

    if not exact and not matched:
        return None

    ordered = exact + matched
    # ★ D174/D175：**不截断**候选（上限只决定显示几行），窗口大小按补全口径（5 行）
    return PickerState(
        title="命令",
        choices=ordered,
        all_choices=list(ordered),
        index=0,
        footer="Tab 补全 · ↑↓ 选择 · Esc 关闭",
        window_size=MAX_VISIBLE,
    )


def render_completion(state: PickerState, palette: object, *, width: int = BOX_WIDTH) -> list[object]:
    """渲染补全提示框 —— **直接复用 picker 的渲染器**（D175）。

    ``numeric=False``：补全**不支持**数字直达（按键处理里没有 `1`-`9`），
    所以不能像选择器那样显示 `[1]` 序号 —— **显示一个按了没反应的键，比少显示一个糟糕得多**。

    返回**逐行**的 `Text`（`AppLayout` 需要一个 `render(width) -> list[Text]` 的东西）。
    """
    box, _rows = render_picker(state, palette, width=width, numeric=False)  # type: ignore[arg-type]
    return list(box.split("\n", include_separator=False, allow_blank=True))


_ = PICKER_MAX_VISIBLE  # 保留导入：说明"picker 的窗口上限是另一个常量"，避免误用
