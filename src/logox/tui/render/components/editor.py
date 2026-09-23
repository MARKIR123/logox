"""多行输入框（D80 / MODULE_tui_render §5）。

范围（刻意收紧）
----------------
Pi 的 ``Editor`` 是 **1916 行**（多行编辑 + 自动补全 + 大段粘贴折叠 + 撤销栈 +
水平滚动 + IME 光标 + 历史）。我们只做**用得着的部分**，目标 ~200 行：

* 多行编辑、光标移动（←→↑↓、Home/End、Ctrl+A/Ctrl+E）
* 删词（Ctrl+W / Alt+Backspace）、删到行首行尾（Ctrl+U / Ctrl+K）
* 提交（Enter）与换行（**只有** Ctrl+Enter / Ctrl+J；D129 去掉了 Shift+Enter 与 Alt+Enter）
* **无提示符**：默认 `prompt=""`（用户裁定去掉 `❯`；参数保留，可插拔）
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

    # ★ D169：`cursor` / `apply_completion` **不进协议** —— 补全对它们用 `getattr` 探测，
    #   不强制所有自定义输入框实现（否则测试里的假编辑器全要改，只为一个可选能力）。

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
        #: 行首提示符。**默认空**（用户裁定：`❯` 是噪音，边框已经说明"这里是输入框"）。
        #:
        #: 保留这个参数而不是删掉：它是**可插拔**的（见 `MODULE_tui_render.md` §5），
        #: 想换回 `❯ ` 或换成 `> ` 只需在装配处传一个字符串，不必改编辑器。
        prompt: str = "",
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

    def set_cursor(self, row: int, col: int) -> None:
        """显式设置光标位置 (row, col)，自动限制在合法字符边界内。"""
        self._row = max(0, min(row, len(self._lines) - 1))
        self._col = max(0, min(col, len(self._lines[self._row])))

    def set_cursor_from_cell(self, visual_row: int, visual_col: int, width: int = 80) -> None:
        """根据视觉单元格 (visual_row, visual_col) 设置光标，兼容折行与 CJK 双宽字符。"""
        inner = max(1, width - cell_len(self.prompt))
        start = max(0, len(self._lines) - MAX_VISIBLE_LINES)

        current_visual_row = 0
        matched = False

        for index in range(start, len(self._lines)):
            line = self._lines[index]
            prefix_width = cell_len(self.prompt) if index == start else cell_len(" " * cell_len(self.prompt))
            segments = _wrap_segments(line, inner)
            for segment, segment_start in segments:
                if current_visual_row == visual_row:
                    matched = True
                    self._row = index
                    col_offset = max(0, visual_col - prefix_width)
                    accumulated = 0
                    char_offset = 0
                    for c_idx, ch in enumerate(segment):
                        w = cell_len(ch)
                        if accumulated + w > col_offset:
                            if col_offset >= accumulated + (w + 1) // 2:
                                char_offset = c_idx + 1
                            else:
                                char_offset = c_idx
                            break
                        accumulated += w
                        char_offset = c_idx + 1
                    self._col = segment_start + char_offset
                    return
                current_visual_row += 1

        if not matched and self._lines:
            self._row = len(self._lines) - 1
            self._col = len(self._lines[self._row])

    # -- D169：`/` 命令补全需要的两个成员 ------------------------------- #
    @property
    def cursor(self) -> tuple[int, int]:
        """当前 `(行, 列)`。补全靠它判定"光标是否还在命令名里"。"""
        return self._row, self._col

    def apply_completion(self, text: str) -> bool:
        """把**当前行**的命令词替换成 ``text``，光标移到末尾（D169）。

        为什么放在编辑器里：只有它知道 buffer 与光标；放外面就要把内部结构暴露出去。
        守卫：只在"第一行且该行以 `/` 开头"时动手 —— 与补全的触发判据一致，
        防止将来有人在别的上下文误调（那时替换会把用户正文切掉）。
        """
        if self._row != 0:
            return False
        line = self._lines[0]
        if not line.lstrip().startswith("/"):
            return False
        indent = line[: len(line) - len(line.lstrip())]
        self._lines[0] = indent + text
        self._col = len(self._lines[0])
        return True
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
        if handler is None and key.shift:
            # ★ D124：**shift 透明回退**。
            #
            # 为什么必须有这一层：`shift` 一旦参与归一化，**所有**带 shift 的键都会
            # 换一个名字去查表。`Shift+↑` 会从 `"up"`（能上移）变成 `"shift+up"`；
            # `Alt+Shift+←` 会从 `"alt+left"`（词跳跃）变成 `"alt+shift+left"`。
            # 这些键在改动前**碰巧能用**，改动后会**静默失效** —— 而"方向键不管用"
            # 很难被联想到"我们给 shift 加了一维"。
            #
            # 规则：**显式登记优先，未登记则视为 shift 不存在**（= 改动前的行为）。
            # 于是 `Shift+↑` 这类导航键**逐字节保持原样**；
            # 而 `Shift+Enter` 被 D129 显式登记为 `_ignore`，**不会**被回退成提交。
            handler = self._HANDLERS.get(self._key_name(key, ignore_shift=True))
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
    def _key_name(key: Key, *, ignore_shift: bool = False) -> str:
        """把 ``Key`` 归一成处理表里的名字（``ctrl+a`` / ``ctrl+enter`` / ``shift+up``）。

        ⚠️ **``shift`` 这一维不能省**（D124）。省了会怎样：``Key("enter", shift=True)``
        会被归一成 ``"enter"`` → 命中 `_submit`：一个**没被登记的**组合键就这么
        “退化成提交”了，而用户往往只是想换行 —— 半句话离手。
        （D129 之后 `Shift+Enter` 已显式登记为不做事，但**其它**将来新出现的
        ``shift+某键`` 仍然靠这一维才不会被静默降级。）

        ⚠️ **代价要说清**：本函数是**共用**的，加上 ``shift`` 会连带改变**所有**带 shift 的键。
        ``Shift+↑`` 的编码 ``CSI 1;2A`` 本来就被解析出 ``shift=True``（见 ``render/keys.py``），
        于是它会从 ``"up"``（能上移）变成 ``"shift+up"``（无 handler = **静默失效**）。
        所以 :meth:`handle_input` 里配了 **shift 透明回退**：显式登记优先，未登记则
        按"没按 shift"再查一次表 —— 保证非目标的组合键**零回归**。
        """
        parts = []
        if key.ctrl:
            parts.append("ctrl")
        if key.alt:
            parts.append("alt")
        if key.shift and not ignore_shift:
            parts.append("shift")
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

    def _ignore(self) -> None:
        """吞掉这个按键，**什么也不做**（D129 引入，现在只服务 `Alt+Enter`）。

        为什么需要一个"什么也不做"的处理器，而不是直接不登记：
        不登记的话 `Alt+Shift+←` 之类的组合会走 **shift 透明回退**（为了不让
        `Shift+↑` 这类导航键失效而设的）—— 而对**回车**而言，回退的终点是 `_submit`：
        一个没被登记的组合键就这么“降级成提交”了，半句话离手。

        代价说明：`Alt+Enter` 在能区分修饰位的终端上按下去**不会有任何反应**；
        在分不开的终端上（如 Windows Terminal）它本来就被终端自己吃掉
        （WT 默认把 Alt+Enter 绑给了“切换全屏”）。
        """
        return None

    def _submit(self) -> None:
        """``Enter``：提交；**但行尾那个 ``\\`` 是例外**（D131）。

        ★ 这条是照参考实现 Pi 搬过来的（``pi-tui/dist/components/editor.js`` 的提交分支）：

        .. code-block:: javascript

           // Workaround for terminals without Shift+Enter support:
           // If char before cursor is \\, delete it and insert newline instead of submitting.

        **为什么需要它**：相当多的终端**根本区分不出 ``Shift+Enter`` 与 ``Enter``**
        —— 两者是同一个字节 ``\\r``（本机探针实测 **Windows Terminal 就是如此**）。
        那种终端上用户没有任何办法在多行输入里换行。Pi 的答案是给一条**纯文本**的退路：
        行尾打一个 ``\\`` 再回车 = 换行（那个反斜杠被吃掉，它是"我要换行"的标记）。

        ⚠️ **代价（Pi 也一样，不是我们引入的）**：行尾真想要一个反斜杠时得多按一次
        —— 比如 Windows 路径 ``C:\\``。这是刻意接受的取舍：
        用一个稀有写法换一个**否则完全不可用**的能力。
        """
        line = self._lines[self._row]
        if self._col > 0 and line[self._col - 1] == "\\":
            self._backspace()
            self._newline()
            return

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
    # `Shift+Enter` 换行（D130 恢复；D129 曾按用户要求短暂去掉）。
    #
    # ⚠️ 前提是**终端能把它与 `Enter` 分开**：传统终端里两者是**同一个字节** `\r`，
    # 只有支持 Kitty 键盘协议（`CSI 13;2u`）或 xterm `modifyOtherKeys`
    # （`CSI 27;2;13~`）的终端才区分得开；本机探针实测 **Windows Terminal 区分不开**
    # （按 Shift+Enter 收到的就是 `\r`），那种终端上它仍然等于提交（物理上限）。
    "shift+enter": Editor._newline,
    # `Alt+Enter`：D129 已按用户裁定**去掉**，且**故意不给它"回退成 Enter"** ——
    # 在能区分修饰位的终端上，回退会让"想换行的人把半句话发出去"：
    # 这是本项目反复定为最糟的失败方式（比"没反应"糟得多）。
    # 所以它被 :meth:`_ignore` **吃掉且什么也不做**。
    "alt+enter": Editor._ignore,
    # `Ctrl+Enter` 换行。两条来源，都**不靠**"猜"：
    #
    # 1. ``Ctrl+J`` 与 Windows Terminal 上的 ``Ctrl+Enter`` 发的都是 **LF**，
    #    而 `keys.py` 把单独的 LF 解析成"带修饰的 Enter"（D128，与 Pi 一致）；
    # 2. 支持 Kitty（`CSI 13;5u`）或 modifyOtherKeys（`CSI 27;5;13~`）的终端直接给修饰位。
    #
    # 不支持的终端上它**退化成普通 Enter（= 提交）**，不会"按了没反应"。
    "ctrl+enter": Editor._newline,
    "ctrl+j": Editor._newline,
    "backspace": Editor._backspace,
    "delete": Editor._delete,
    "left": Editor._left,
    "right": Editor._right,
    "up": Editor._up,
    "down": Editor._down,
    "home": Editor._home,
    "end": Editor._end,
    # D124：``shift`` 参与归一化之后，带 shift 的方向键会换名字查表。
    # 下面 6 条**显式**写出来，是为了让"支持哪些键"在这张表里一眼可查；
    # 它们与 :meth:`handle_input` 的 **shift 透明回退**是同一个目标，
    # 即使漏掉某一条也不会失效（回退会兜住）。将来做"shift 选择文本"时，
    # 在这里改指向即可，不必动回退规则。
    "shift+up": Editor._up,
    "shift+down": Editor._down,
    "shift+left": Editor._left,
    "shift+right": Editor._right,
    "shift+home": Editor._home,
    "shift+end": Editor._end,
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
    │ 输入内容...                [Enter 发送] │
    ╰────────────────────────────────────────╯

    在窄屏（width < 10）下自动降级为无边框原生流，保证不溢出换行。
    """

    def __init__(
        self,
        inner: EditorProtocol,
        *,
        border_style: str = "",
        text_style: str = "",
    ) -> None:
        self.inner = inner
        self.border_style = border_style
        #: 输入正文的前景色。**必须显式给**，不能靠"没样式就让终端默认" ——
        #: 理由见 :meth:`_tinted`。
        self.text_style = text_style

    @property
    def text(self) -> str:
        return self.inner.text

    def set_text(self, text: str) -> None:
        self.inner.set_text(text)

    def clear(self) -> None:
        self.inner.clear()

    def set_cursor(self, row: int, col: int) -> None:
        """显式设置光标位置 (row, col)（委托给内层）。"""
        setter = getattr(self.inner, "set_cursor", None)
        if callable(setter):
            setter(row, col)

    def set_cursor_from_cell(self, box_row: int, box_col: int, width: int = 80) -> None:
        """从 BoxedEditor 的相对坐标 (box_row, box_col) 定位光标。

        box_row: 0 是顶边 ╭──╮，1..N 是内容行，N+1 是底边 ╰──╯
        box_col: 0..1 是左边框 '│ '，2.. 是内容字符，末尾是 ' │'
        """
        setter = getattr(self.inner, "set_cursor_from_cell", None)
        if callable(setter):
            inner_width = max(1, width - 4) if width >= 10 else width
            inner_row = max(0, box_row - 1) if width >= 10 else box_row
            inner_col = max(0, box_col - 2) if width >= 10 else box_col
            setter(inner_row, inner_col, width=inner_width)
        else:
            basic = getattr(self.inner, "set_cursor", None)
            if callable(basic):
                basic(box_row, box_col)

    # ★ D169：`/` 补全需要的两个成员 —— 只加在**真实编辑器**上（协议不强制，见上）。
    @property
    def cursor(self) -> tuple[int, int]:
        """当前 `(行, 列)`（`BoxedEditor` 委托给内层）。"""
        getter = getattr(self.inner, "cursor", None)
        return getter if getter is not None else (0, 0)

    def apply_completion(self, text: str) -> bool:
        """把当前行的命令词替换成 ``text``（委托给内层）。"""
        applier = getattr(self.inner, "apply_completion", None)
        return bool(applier(text)) if callable(applier) else False

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

    def _tinted(self, row: Text) -> Text:
        """给输入正文一个**显式**前景色，而不是让它继承外层的边框色。

        为什么必须显式（不能靠"内容没样式就交给终端默认"）
        --------------------------------------------------
        边框那一行是 ``Text("│ ", style=self.border_style)`` —— Rich 的 base style
        作用于**该行所有没有 span 的字符**。而核心编辑器产出的正文恰好没有 span
        （``Editor.render`` 里是 ``piece = Text(prefix)``，不带 style），
        于是**输入的文字被边框色染上了**。

        实测（``.smoke/_probe_editor_style_leak.py``）：染色后是 ``border_subtle``
        = ``#313244``，它对 ``bg_base`` = ``#1e1e2e`` 只有 **1.30:1** ——
        那是给"最不抢眼的装饰线"选的值，拿它当正文必然看不清。
        **这不是"设计成灰色"，是装饰色泄漏到了内容上。**

        做法：新建一个带 base style 的容器，把 row 追加进去。
        ``Text.append_text`` 会把**被追加方的 base style 转成一个 span**，
        于是内容拿到显式颜色；而且**不修改** ``row`` 本身
        （D126 契约：``render()`` 交出的 ``Text`` 不得原地修改）。
        ``row`` 原有的 span（如 hint 的 ``text_faint``）会平移保留，
        并叠加在新 span 之后 —— 所以 hint 仍然是淡的。
        """
        if not self.text_style:
            return row
        body = Text(style=self.text_style)
        body.append_text(row)
        return body

    def render(self, width: int) -> list[Text]:
        # 当宽度过窄（< 10）时，降级为无边框，保证每行不会超宽溢出
        if width < 10:
            # 窄屏虽然没有边框（也就没有泄漏），但仍然显式上色 —— 否则
            # "拉窄终端后输入文字突然变色"会是一个很难解释的现象。
            return [self._tinted(row) for row in self.inner.render(width)]

        # 内部可用宽度：左右各留 "│ " 与 " │"（共 4 个 cell）
        inner_width = max(1, width - 4)
        inner_rows = self.inner.render(inner_width)

        # 顶边：╭───╮
        top = Text("╭" + "─" * (width - 2) + "╮", style=self.border_style)

        # 中间各行：│ <inner> │
        middle: list[Text] = []
        for row in inner_rows:
            line = Text("│ ", style=self.border_style)
            line.append_text(self._tinted(row))
            used = 2 + visible_width(row.plain)
            pad = width - used - 2
            if pad > 0:
                line.append(" " * pad)
            line.append(" │", style=self.border_style)
            middle.append(line)

        # 底边：╰───╯
        bottom = Text("╰" + "─" * (width - 2) + "╯", style=self.border_style)

        return [top, *middle, bottom]
