"""一个**极小的终端模拟器**（测试用）。

为什么需要它
------------
差分渲染器的正确性没法靠"看输出里有没有某段文字"来证明。真正要证明的是：

    终端屏幕上最终显示的内容 == 渲染器以为自己画出来的内容

上面这句话里的"屏幕上"是**二维**的——光标移到哪一行、清了哪几列、
滚动了几行，都会影响结果。只看字节流是看不出来的。

所以这里实现一个**够用的**模拟器：把渲染器写出的转义序列**真的执行一遍**，
得到一块 2D 字符网格 + 一份"滚出去的历史"（回滚缓冲）。
于是测试可以断言：

* 当前屏逐行等于渲染器的可见帧；只追加场景再检查 ``回滚缓冲 + 屏幕``，
  历史重排按 D199 保留旧快照，不能要求整份历史等于最新帧；
* 内容变长时，旧内容**仍然在回滚缓冲里**（这是主屏方案的意义所在：
  用户能滚回去看）。

刻意**不**实现的部分：颜色（SGR 直接忽略）、宽字符的第二次覆盖、
DECSTBM 滚动区域、括号粘贴。这些要么与本模块要证明的事无关，
要么我们的渲染器根本不发。**模拟器只认渲染器真实会写的那批序列**，
多认一种就多一分"模拟器与实现一起错"的风险。
"""

from __future__ import annotations

import re

from logox.tui.render.ansi import visible_width

__all__ = ["ScreenEmulator"]

#: 形如 ``ESC [ <参数> <终止字节>``；``?`` 开头的是私有序列（同步输出）
_CSI = re.compile(r"\x1b\[(\??)([0-9;]*)([A-Za-z])")
#: OSC：``ESC ] ... BEL``（超链接、设标题）
_OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")


class ScreenEmulator:
    """执行帧字节；可选模拟 Windows Terminal ED(2) 的旧页归档。

    该模式只刻画 ED(2) 的副作用，不模拟原生 ConPTY、缓冲上限与 resize reflow。
    """

    def __init__(self, columns: int = 80, rows: int = 24, *, erase_all_to_scrollback: bool = False) -> None:
        # Windows Terminal ED(2) archives the old page; xterm-style tests stay unchanged.
        self.erase_all_to_scrollback = erase_all_to_scrollback
        self.columns = columns
        self.rows = rows
        self.grid: list[list[str]] = [self._blank_row() for _ in range(rows)]
        #: 滚出屏幕顶部的行（终端交给用户滚动查看的那部分）
        self.scrollback: list[str] = []
        self.cursor_row = 0
        self.cursor_col = 0

    def _blank_row(self) -> list[str]:
        return [" "] * self.columns

    # -- 输入 ----------------------------------------------------------- #

    def feed(self, data: str) -> None:
        """把一段字节喂给模拟器（就地执行，不返回任何东西）。"""
        index = 0
        length = len(data)
        while index < length:
            char = data[index]
            if char == "\x1b":
                index = self._handle_escape(data, index)
                continue
            if char == "\r":
                self.cursor_col = 0
            elif char == "\n":
                self._line_feed()
            elif char == "\b":
                self.cursor_col = max(0, self.cursor_col - 1)
            else:
                self._put(char)
            index += 1

    def _handle_escape(self, data: str, index: int) -> int:
        """处理 ``index`` 处的 ``ESC``，返回**下一个未处理字符**的下标。"""
        rest = data[index:]
        osc = _OSC.match(rest)
        if osc is not None:
            return index + osc.end()
        match = _CSI.match(rest)
        if match is not None:
            self._apply_csi(match.group(1), match.group(2), match.group(3))
            return index + match.end()
        # 不认识的转义：跳过 ESC 与紧随的一个字符，避免死循环
        return min(index + 2, len(data))

    def _apply_csi(self, private: str, params: str, final: str) -> None:
        if private == "?":  # 同步输出 / 光标可见性 / 括号粘贴：对画面没有影响
            return
        numbers = [int(part) if part else 0 for part in params.split(";")] if params else []
        first = numbers[0] if numbers else 1 if final != "J" and final != "K" else 0
        if final == "A":
            self.cursor_row = max(0, self.cursor_row - max(1, first))
        elif final == "B":
            self.cursor_row = min(self.rows - 1, self.cursor_row + max(1, first))
        elif final == "C":
            self.cursor_col = min(self.columns - 1, self.cursor_col + max(1, first))
        elif final == "D":
            self.cursor_col = max(0, self.cursor_col - max(1, first))
        elif final == "G":
            self.cursor_col = max(0, min(self.columns - 1, max(1, first) - 1))
        elif final == "H":
            row = numbers[0] if numbers else 1
            col = numbers[1] if len(numbers) > 1 else 1
            self.cursor_row = max(0, min(self.rows - 1, row - 1))
            self.cursor_col = max(0, min(self.columns - 1, col - 1))
        elif final == "K":
            mode = numbers[0] if numbers else 0
            if mode == 2:
                self.grid[self.cursor_row] = self._blank_row()
            elif mode == 0:
                for col in range(self.cursor_col, self.columns):
                    self.grid[self.cursor_row][col] = " "
        elif final == "J":
            mode = numbers[0] if numbers else 0
            if mode == 2:
                if self.erase_all_to_scrollback:
                    used = max((i + 1 for i, row in enumerate(self.visible) if row), default=0)
                    self.scrollback.extend(self.visible[:used])
                self.grid = [self._blank_row() for _ in range(self.rows)]
            elif mode == 3:
                self.scrollback.clear()  # 清回滚缓冲
            elif mode == 0:
                for col in range(self.cursor_col, self.columns):
                    self.grid[self.cursor_row][col] = " "
                for row in range(self.cursor_row + 1, self.rows):
                    self.grid[row] = self._blank_row()
        # SGR（``m``）与其他序列：对画面无影响，忽略

    # -- 屏幕操作 -------------------------------------------------------- #

    def _put(self, char: str) -> None:
        width = visible_width(char)
        if width <= 0:
            return
        if self.cursor_col >= self.columns:
            # 自动折行：终端在写到行尾后会自动换到下一行
            self.cursor_col = 0
            self._line_feed()
        self.grid[self.cursor_row][self.cursor_col] = char
        if width > 1:
            # 宽字符占两格：第二格记成空串，这样拼起来仍然是原来那个字
            for extra in range(1, width):
                if self.cursor_col + extra < self.columns:
                    self.grid[self.cursor_row][self.cursor_col + extra] = ""
        self.cursor_col = min(self.columns, self.cursor_col + width)

    def _line_feed(self) -> None:
        self.cursor_row += 1
        if self.cursor_row >= self.rows:
            self.scrollback.append(self.row_text(0))
            self.grid.pop(0)
            self.grid.append(self._blank_row())
            self.cursor_row = self.rows - 1

    # -- 读取 ------------------------------------------------------------ #

    def row_text(self, row: int) -> str:
        """第 ``row`` 行的可见文本（去掉右侧填充的空格）。"""
        return "".join(self.grid[row]).rstrip()

    @property
    def visible(self) -> list[str]:
        """当前屏幕上的所有行（去掉了每行右侧的填充）。"""
        return [self.row_text(row) for row in range(self.rows)]

    @property
    def history(self) -> list[str]:
        """回滚缓冲 + 当前屏幕 = 这个终端里**存在过的全部内容**。"""
        return [*self.scrollback, *self.visible]

    def trimmed_history(self) -> list[str]:
        """去掉首尾的空行——用于与"渲染器以为的内容"比较。"""
        lines = list(self.history)
        while lines and not lines[0].strip():
            lines.pop(0)
        while lines and not lines[-1].strip():
            lines.pop()
        return lines
