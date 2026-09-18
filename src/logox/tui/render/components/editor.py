"""多行输入框（D80 / MODULE_tui_render §5）。

范围（刻意收紧）
----------------
Pi 的 ``Editor`` 是 **1916 行**（多行编辑 + 自动补全 + 大段粘贴折叠 + 撤销栈 +
水平滚动 + IME 光标 + 历史）。我们只做**用得着的部分**，目标 ~200 行：

* 多行编辑、光标移动（←→↑↓、Home/End、Ctrl+A/Ctrl+E）
* 删词（Ctrl+W / Alt+Backspace）、删到行首行尾（Ctrl+U / Ctrl+K）
* 提交（Enter）与换行（Alt+Enter / Ctrl+J）
* 历史（↑↓，在单行时生效）
* **IME 光标标记**（中文输入法候选窗靠它定位，§10 第 2 项）

**不做**：撤销栈、大段粘贴折叠、Ctrl+] 字符跳转、水平滚动。
理由见 `MODULE_tui_render.md` §5——它们要么收益低，要么需要额外的状态机，
而当前阶段的首要目标是"把骨架跑起来"。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

from rich.cells import cell_len
from rich.text import Text

from logox.tui.render.ansi import visible_width
from logox.tui.render.component import Component
from logox.tui.render.components.text import CURSOR_MARKER
from logox.tui.render.keys import Key

__all__ = ["BoxedEditor", "Editor", "EditorProtocol"]


@runtime_checkable
class EditorProtocol(Component, Protocol):
    """可插拔输入框契约：任何自定义输入框只要实现此协议，即可无缝插入 Logox。"""

    @property
    def text(self) -> str: ...

    def set_text(self, text: str) -> None: ...

    def clear(self) -> None: ...

    @property
    def focused(self) -> bool: ...

    @focused.setter
    def focused(self, value: bool) -> None: ...

    def handle_input(self, key: Key) -> bool: ...

    def render(self, width: int) -> list[Text]: ...

#: 输入区最多显示几行（超过则滚动，避免把时间线挤没）
MAX_VISIBLE_LINES = 6

#: 历史最多记多少条
HISTORY_LIMIT = 100


def _wrap_segments(line: str, width: int) -> list[tuple[str, int]]:
    """把一行按 **cell 宽度**切成小段，返回 ``[(段文本, 段起点字符下标)]``。

    返回"起点下标"是为了让光标能落回**正确的段**——光标位置是字符下标，
    而折行之后它属于哪一段需要现算（算错的话 IME 候选窗会偏到上一行）。
    """
    if width <= 0:
        return [("", 0)]
    if not line:
        return [("", 0)]
    segments: list[tuple[str, int]] = []
    current = ""
    used = 0
    start_index = 0
    for index, char in enumerate(line):
        char_width = cell_len(char)
        if used + char_width > width:
            segments.append((current, start_index))
            current, used, start_index = "", 0, index
        current += char
        used += char_width
    segments.append((current, start_index))
    return segments


class Editor:
    """一个**有状态**的多行输入框。

    与 `Component` 协议一致：``render(width) -> list[Text]``。
    """

    def __init__(
        self,
        *,
        on_submit: Callable[[str], None] | None = None,
        prompt: str = "❯ ",
        hint: str = "",
        hint_style: str = "",
    ) -> None:
        self._lines: list[str] = [""]
        self._row = 0
        self._col = 0
        self.on_submit = on_submit
        self.prompt = prompt
        self.history: list[str] = []
        self._history_index: int | None = None
        #: 草稿（浏览历史时把当前输入暂存起来，回到"最新"时恢复）
        self._draft = ""
        self.focused = True
        #: 空输入时右侧的淡色提示（见 `_add_hint`）
        self.hint = hint
        self.hint_style = hint_style

    # ------------------------------------------------------------------ #
    # 取值
    # ------------------------------------------------------------------ #

    @property
    def text(self) -> str:
        return "\n".join(self._lines)

    def set_text(self, text: str) -> None:
        self._lines = text.split("\n") or [""]
        self._row = len(self._lines) - 1
        self._col = len(self._lines[self._row])

    def clear(self) -> None:
        self._lines = [""]
        self._row = 0
        self._col = 0
        self._history_index = None

    # ------------------------------------------------------------------ #
    # 输入
    # ------------------------------------------------------------------ #

    def handle_input(self, key: Key) -> bool:
        if key.name == "paste":
            # ★ 一次粘贴 = **一次插入**，绝不能逐字符走按键路径。
            #
            # 为什么：粘贴内容里的换行如果被当成 Enter，就会**一次性提交多条消息**
            # （粘贴 10 行 = 发 10 条）；而里面的 `\r` 还会被当成"确认"，
            # 于是半句话就被发出去了。这里把整段按行插进编辑框，
            # 用户看到的是"待发送的一段草稿"，按 Enter 才提交。
            self.insert_text(key.char or "")
            return True
        handler = self._HANDLERS.get(self._key_name(key))
        if handler is None:
            if key.printable and key.char:
                self._insert(key.char)
                return True
            return False
        handler(self)
        return True

    def insert_text(self, text: str) -> None:
        """把一段文本插到光标处（换行保留为多行，**不提交**）。"""
        if not text:
            return
        # 终端可能给 `\r\n`、`\r` 或 `\n`，统一成 `\n`——否则会多出空行。
        normalised = text.replace("\r\n", "\n").replace("\r", "\n")
        for index, line in enumerate(normalised.split("\n")):
            if index:
                self._newline()
            if line:
                self._insert(line)

    @staticmethod
    def _key_name(key: Key) -> str:
        """把 ``Key`` 归一成处理表里的名字（``ctrl+a`` / ``alt+enter`` / ``enter``）。"""
        parts = []
        if key.ctrl:
            parts.append("ctrl")
        if key.alt:
            parts.append("alt")
        parts.append(key.name)
        return "+".join(parts)

    # -- 编辑动作 ------------------------------------------------------- #

    def _insert(self, char: str) -> None:
        line = self._lines[self._row]
        self._lines[self._row] = line[: self._col] + char + line[self._col :]
        self._col += len(char)

    def _newline(self) -> None:
        line = self._lines[self._row]
        self._lines[self._row] = line[: self._col]
        self._lines.insert(self._row + 1, line[self._col :])
        self._row += 1
        self._col = 0

    def _backspace(self) -> None:
        if self._col > 0:
            line = self._lines[self._row]
            self._lines[self._row] = line[: self._col - 1] + line[self._col :]
            self._col -= 1
        elif self._row > 0:
            # 行首退格 → 与上一行合并（常规编辑器行为）
            previous = self._lines[self._row - 1]
            self._col = len(previous)
            self._lines[self._row - 1] = previous + self._lines[self._row]
            del self._lines[self._row]
            self._row -= 1

    def _delete(self) -> None:
        line = self._lines[self._row]
        if self._col < len(line):
            self._lines[self._row] = line[: self._col] + line[self._col + 1 :]
        elif self._row < len(self._lines) - 1:
            self._lines[self._row] = line + self._lines[self._row + 1]
            del self._lines[self._row + 1]

    def _delete_word_back(self) -> None:
        line = self._lines[self._row]
        if self._col == 0:
            self._backspace()
            return
        index = self._col
        while index > 0 and line[index - 1].isspace():
            index -= 1
        while index > 0 and not line[index - 1].isspace():
            index -= 1
        self._lines[self._row] = line[:index] + line[self._col :]
        self._col = index

    def _delete_to_line_start(self) -> None:
        line = self._lines[self._row]
        self._lines[self._row] = line[self._col :]
        self._col = 0

    def _delete_to_line_end(self) -> None:
        self._lines[self._row] = self._lines[self._row][: self._col]

    # -- 光标移动 ------------------------------------------------------- #

    def _left(self) -> None:
        if self._col > 0:
            self._col -= 1
        elif self._row > 0:
            self._row -= 1
            self._col = len(self._lines[self._row])

    def _right(self) -> None:
        if self._col < len(self._lines[self._row]):
            self._col += 1
        elif self._row < len(self._lines) - 1:
            self._row += 1
            self._col = 0

    def _up(self) -> None:
        if self._row > 0:
            self._row -= 1
            self._col = min(self._col, len(self._lines[self._row]))
        else:
            self._history_prev()

    def _down(self) -> None:
        if self._row < len(self._lines) - 1:
            self._row += 1
            self._col = min(self._col, len(self._lines[self._row]))
        else:
            self._history_next()

    def _home(self) -> None:
        self._col = 0

    def _end(self) -> None:
        self._col = len(self._lines[self._row])

    def _word_back(self) -> None:
        line = self._lines[self._row]
        index = self._col
        while index > 0 and index <= len(line) and line[index - 1].isspace():
            index -= 1
        while index > 0 and index <= len(line) and not line[index - 1].isspace():
            index -= 1
        self._col = index

    def _word_forward(self) -> None:
        line = self._lines[self._row]
        index = self._col
        length = len(line)
        while index < length and line[index].isspace():
            index += 1
        while index < length and not line[index].isspace():
            index += 1
        self._col = index

    def _delete_word_forward(self) -> None:
        line = self._lines[self._row]
        index = self._col
        length = len(line)
        if index >= length:
            return
        end = index
        while end < length and not line[end].isspace():
            end += 1
        while end < length and line[end].isspace():
            end += 1
        self._lines[self._row] = line[:index] + line[end:]

    # -- 历史 ----------------------------------------------------------- #

    def _history_prev(self) -> None:
        if not self.history:
            return
        if self._history_index is None:
            self._draft = self.text
            self._history_index = len(self.history)
        if self._history_index > 0:
            self._history_index -= 1
            self.set_text(self.history[self._history_index])

    def _history_next(self) -> None:
        if self._history_index is None:
            return
        self._history_index += 1
        if self._history_index >= len(self.history):
            self._history_index = None
            self.set_text(self._draft)
        else:
            self.set_text(self.history[self._history_index])

    # -- 提交 ----------------------------------------------------------- #

    def _submit(self) -> None:
        text = self.text.strip()
        if not text:
            return
        self.history.append(self.text)
        if len(self.history) > HISTORY_LIMIT:
            del self.history[: len(self.history) - HISTORY_LIMIT]
        self.clear()
        if self.on_submit is not None:
            self.on_submit(text)

    # ------------------------------------------------------------------ #
    # 渲染
    # ------------------------------------------------------------------ #

    def render(self, width: int) -> list[Text]:
        """渲染输入区：提示符 + 各行（**按宽度折行**）+ IME 光标标记。

        光标位置用**零宽 APC 标记**告诉渲染器（见 `CURSOR_MARKER`）——
        终端据此把硬件光标移过去，中文输入法的候选窗才会出现在正确位置。

        为什么**折行**而不是水平滚动：折行实现简单且**不隐藏内容**。
        Pi 的 Editor 做了水平滚动，但那需要额外的偏移状态与光标映射；
        当前阶段（把骨架跑起来）折行是更划算的取舍。

        ⚠️ 折行后**光标必须落在正确的那个小段**上——否则标记会跑到上一行末尾，
        候选窗位置就偏了。
        """
        inner = max(1, width - cell_len(self.prompt))
        start = max(0, len(self._lines) - MAX_VISIBLE_LINES)
        rows: list[Text] = []

        for index in range(start, len(self._lines)):
            line = self._lines[index]
            prefix = self.prompt if index == start else " " * cell_len(self.prompt)
            for segment, segment_start in _wrap_segments(line, inner):
                piece = Text(prefix)
                if index == self._row and self.focused and segment_start <= self._col <= segment_start + len(segment):
                    col = self._col - segment_start
                    piece.append(segment[:col])
                    piece.append(CURSOR_MARKER)
                    piece.append(segment[col:])
                else:
                    piece.append(segment)
                rows.append(piece)

        if not rows:
            rows = [Text(self.prompt + CURSOR_MARKER)]
        self._add_hint(rows, width)
        return rows

    def _add_hint(self, rows: list[Text], width: int) -> None:
        """输入框为空时，在最右边淡淡地写一句"还能按什么"。

        为什么放在这里而不是状态行：状态行现在是**度量**（模型 / token / 费用 /
        上下文占用），再塞键位提示就会把它们挤掉；而用户的眼睛本来就在光标那里。
        一旦开始输入它立刻消失——它是**首次使用时的指路牌**，不是常驻装饰。
        """
        if not self.hint or len(rows) != 1 or self._lines != [""]:
            return
        prefix_width = cell_len(self.prompt)
        gap = width - prefix_width - cell_len(self.hint)
        if gap < 2:
            return  # 放不下就不放（**宁可没有提示，也不能超宽**）
        rows[0].append(" " * gap)
        rows[0].append(self.hint, style=self.hint_style)

    def invalidate(self) -> None:
        """输入框没有缓存，无需作废（保留接口以满足 `Component` 协议）。"""
        return None

    #: 键名 → 动作。**集中成一张表**：这样"支持哪些键"一眼可查、也便于测试遍历。
    _HANDLERS: dict[str, Callable[[Editor], None]] = {}


Editor._HANDLERS = {
    "enter": Editor._submit,
    "alt+enter": Editor._newline,
    "ctrl+j": Editor._newline,
    "backspace": Editor._backspace,
    "delete": Editor._delete,
    "left": Editor._left,
    "right": Editor._right,
    "up": Editor._up,
    "down": Editor._down,
    "home": Editor._home,
    "end": Editor._end,
    "ctrl+a": Editor._home,
    "ctrl+e": Editor._end,
    "ctrl+w": Editor._delete_word_back,
    "alt+backspace": Editor._delete_word_back,
    "ctrl+u": Editor._delete_to_line_start,
    "ctrl+k": Editor._delete_to_line_end,
    "alt+b": Editor._word_back,
    "alt+left": Editor._word_back,
    "alt+f": Editor._word_forward,
    "alt+right": Editor._word_forward,
    "alt+d": Editor._delete_word_forward,
    "ctrl+delete": Editor._delete_word_forward,
}


class BoxedEditor:
    """圆角矩形输入框装饰器组件（D89）。

    包装任意实现 EditorProtocol 的输入组件，在它周围渲染自适应的圆角边框：
    ╭────────────────────────────────────────╮
    │ ❯ 输入内容...             [Enter 发送] │
    ╰────────────────────────────────────────╯

    在窄屏（width < 10）下自动降级为无边框原生流，保证不溢出换行。
    """

    def __init__(
        self,
        inner: EditorProtocol,
        *,
        border_style: str = "",
    ) -> None:
        self.inner = inner
        self.border_style = border_style

    @property
    def text(self) -> str:
        return self.inner.text

    def set_text(self, text: str) -> None:
        self.inner.set_text(text)

    def clear(self) -> None:
        self.inner.clear()

    @property
    def focused(self) -> bool:
        return self.inner.focused

    @focused.setter
    def focused(self, value: bool) -> None:
        self.inner.focused = value

    @property
    def on_submit(self) -> Any:
        return getattr(self.inner, "on_submit", None)

    @on_submit.setter
    def on_submit(self, callback: Any) -> None:
        if hasattr(self.inner, "on_submit"):
            self.inner.on_submit = callback

    def handle_input(self, key: Key) -> bool:
        return self.inner.handle_input(key)

    def invalidate(self) -> None:
        self.inner.invalidate()

    def render(self, width: int) -> list[Text]:
        # 当宽度过窄（< 10）时，降级为无边框，保证每行不会超宽溢出
        if width < 10:
            return self.inner.render(width)

        # 内部可用宽度：左右各留 "│ " 与 " │"（共 4 个 cell）
        inner_width = max(1, width - 4)
        inner_rows = self.inner.render(inner_width)

        # 顶边：╭───╮
        top = Text("╭" + "─" * (width - 2) + "╮", style=self.border_style)

        # 中间各行：│ <inner> │
        middle: list[Text] = []
        for row in inner_rows:
            line = Text("│ ", style=self.border_style)
            line.append_text(row)
            used = 2 + visible_width(row.plain)
            pad = width - used - 2
            if pad > 0:
                line.append(" " * pad)
            line.append(" │", style=self.border_style)
            middle.append(line)

        # 底边：╰───╯
        bottom = Text("╰" + "─" * (width - 2) + "╯", style=self.border_style)

        return [top, *middle, bottom]
