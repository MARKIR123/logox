"""渲染保真度：**把字节真的执行一遍**，看屏幕上到底是什么。

本文件与 `test_render_screen.py` 的分工
--------------------------------------
``test_render_screen.py`` 管"**写了哪些字节**"（省不省、闪不闪）。
本文件管"**那些字节最终让屏幕长什么样**"——用一个二维模拟器
（`screen_emulator.ScreenEmulator`）执行转义序列，然后断言：

    模拟器里的内容  ==  ``Screen.frame_text()``

这条等式是"界面不会错位"最严格的可执行定义。它能抓到的东西，
只检查"输出里有没有某段文字"是**永远抓不到**的：

* 光标移错了行 → 内容写到别处 / 互相覆盖；
* 少写了一个清行 → 上一帧的长内容留下尾巴（"幽灵字符"）；
* 滚动算错了 → 老内容被覆盖，或者屏幕上出现重复；
* 视口行号记错了 → 内容一长就开始错位。

这几类问题的共同点是：**代码看起来完全正常，输出里也"有"那段文字**，
只有在真正的二维屏幕上才看得见。这正是我们前面反复踩坑的地方。
"""

from __future__ import annotations

import unittest

from rich.text import Text

from logox.tui.render.component import Container
from logox.tui.render.components.text import TextComponent
from logox.tui.render.screen import Screen
from logox.tui.render.terminal import FakeTerminal

from .screen_emulator import ScreenEmulator


class FidelityCase(unittest.TestCase):
    """公共脚手架：一个 Screen + 一个 FakeTerminal + 一个模拟器。"""

    columns = 40
    rows = 10

    def setUp(self) -> None:
        self.terminal = FakeTerminal(columns=self.columns, rows=self.rows)
        self.screen = Screen(self.terminal)
        self.emulator = ScreenEmulator(columns=self.columns, rows=self.rows)
        self.container = Container()
        self.screen.add(self.container)

    def frame(self, *, expect_ok: bool = True) -> None:
        """渲染一帧，把**这一次写出去的字节**喂给模拟器，然后校验等式。"""
        before = len(self.terminal.writes)
        self.screen.render_now()
        for data, _columns, _rows in self.terminal.writes[before:]:
            self.emulator.feed(data)
        if expect_ok:
            self.assertScreenMatchesFrame()

    def assertScreenMatchesFrame(self) -> None:
        """★ 核心断言：模拟器上的内容 == 渲染器以为自己画的内容。"""
        expected = _trim([line for line in self.screen.frame_text().split("\n")])
        actual = self.emulator.trimmed_history()
        self.assertEqual(
            actual,
            expected,
            "屏幕内容与渲染器认为的内容不一致（这就是「界面错位」）\n"
            f"屏幕上：{actual}\n"
            f"渲染器：{expected}",
        )


def _trim(lines: list[str]) -> list[str]:
    out = list(lines)
    while out and not out[0].strip():
        out.pop(0)
    while out and not out[-1].strip():
        out.pop()
    return out


class AppendFidelityTests(FidelityCase):
    """流式追加是 AI 对话最常见的形态，也是主屏滚动最容易出错的地方。"""

    def test_growing_past_the_screen_keeps_everything(self) -> None:
        """内容长到超过一屏之后：屏幕不错位，**且旧内容仍在回滚缓冲里**。

        这一条同时证明了两件事：增量重画的行号是对的，以及"滚上去的行"
        真的进了终端的回滚缓冲（用户能滚回去看）——后者是主屏方案的全部意义。
        """
        for index in range(30):  # 30 行 > 10 行高的屏幕
            self.container.add(TextComponent(f"第 {index} 行"))
            self.frame()
        self.assertGreater(len(self.emulator.scrollback), 0, "超过一屏后应当有内容滚进回滚缓冲")
        self.assertIn("第 0 行", "\n".join(self.emulator.scrollback), "最早的内容必须还能滚回去看")

    def test_streaming_append_inside_one_line(self) -> None:
        """流式输出的真实形态：**同一行内容不断变长**（不是一个字一行）。"""
        block = TextComponent("")
        self.container.add(block)
        for piece in ["我", "正在", "读取", "文件"]:
            block.set_text(block.text + piece)
            self.frame()
        self.assertEqual(self.emulator.trimmed_history()[-1], "我正在读取文件")

    def test_long_line_then_short_line_leaves_no_ghost(self) -> None:
        """★ 长内容变短必须把**残留的尾巴**清掉。

        漏清的症状是"新内容后面跟着几个旧字符"——用户会以为是乱码。
        """
        block = TextComponent("这是一条很长很长的内容，长到会留下尾巴")
        self.container.add(block)
        self.frame()
        block.set_text("短")
        self.frame()
        self.assertEqual(self.emulator.trimmed_history()[-1], "短")


class EditFidelityTests(FidelityCase):
    """输入框每一帧都在变——它是最"动态"的那一行。"""

    def test_typing_one_char_at_a_time(self) -> None:
        editor_line = TextComponent("")
        self.container.add(TextComponent("固定的第一行"))
        self.container.add(editor_line)
        self.frame()
        typed = ""
        for char in "hello 你好":
            typed += char
            editor_line.set_text(typed)
            self.frame()

    def test_deleting_lines_clears_them_from_screen(self) -> None:
        """内容整体变短（删掉若干行）时，屏幕上不能留下"幽灵行"。"""
        blocks = [TextComponent(f"行 {index}") for index in range(6)]
        for block in blocks:
            self.container.add(block)
        self.frame()
        for block in blocks[:3]:
            self.container.remove(block)
        self.frame()
        self.assertNotIn("行 0", self.emulator.visible)

    def test_change_in_the_middle_does_not_disturb_neighbours(self) -> None:
        blocks = [TextComponent(f"行 {index}") for index in range(5)]
        for block in blocks:
            self.container.add(block)
        self.frame()
        blocks[2].set_text("行 2 改了")
        self.frame()
        self.assertEqual(
            self.emulator.trimmed_history(),
            ["行 0", "行 1", "行 2 改了", "行 3", "行 4"],
        )


class ResizeFidelityTests(FidelityCase):
    def test_width_change_reflows_and_stays_correct(self) -> None:
        """宽度变化会改变折行位置，只能整屏重画——重画之后也必须对得上。"""
        for index in range(5):
            self.container.add(TextComponent(f"第 {index} 行的一些内容"))
        self.frame()

        self.terminal.resize(30, self.rows)
        self.emulator = ScreenEmulator(columns=30, rows=self.rows)
        self.frame()

    def test_height_growth_then_shrink(self) -> None:
        for index in range(4):
            self.container.add(TextComponent(f"行 {index}"))
        self.frame()
        self.terminal.resize(self.columns, 16)
        self.frame()
        self.terminal.resize(self.columns, self.rows)
        self.frame()


class CjkFidelityTests(FidelityCase):
    """中文占 2 格：宽度算错会**同时**造成超宽折行与错位。"""

    def test_wide_characters_align_on_screen(self) -> None:
        for index in range(6):
            self.container.add(TextComponent(f"中文内容第 {index} 行"))
        self.frame()
        self.assertTrue(all("中文内容" in line for line in self.emulator.trimmed_history() if line))

    def test_mixed_width_append(self) -> None:
        block = TextComponent("abc")
        self.container.add(block)
        self.frame()
        block.set_text("abc 中文 def")
        self.frame()
        self.assertEqual(self.emulator.trimmed_history()[-1], "abc 中文 def")


class EmptyAndEdgeTests(FidelityCase):
    def test_empty_frame(self) -> None:
        self.frame()

    def test_single_line_taller_than_screen(self) -> None:
        """屏幕只有 1 行时不能崩，也不能错位（病态但必须不炸）。"""
        self.terminal.resize(self.columns, 1)
        self.emulator = ScreenEmulator(columns=self.columns, rows=1)
        self.container.add(TextComponent("第一行"))
        self.container.add(TextComponent("第二行"))
        self.frame()

    def test_no_write_when_nothing_changes(self) -> None:
        """空闲时**零字节**：这条性质必须与光标记账一起成立。"""
        self.container.add(TextComponent("稳定的内容"))
        self.frame()
        before = len(self.terminal.writes)
        for _ in range(5):
            self.frame()
        self.assertEqual(len(self.terminal.writes), before, "内容没变却又写字节了")


class StyleFidelityTests(FidelityCase):
    """样式必须真的写进终端——漏掉这一步界面上会**完全没有颜色**。"""

    def test_styles_reach_the_terminal(self) -> None:
        self.container.add(TextComponent("红色", style="bold #ff0000"))
        self.frame()
        written = self.terminal.output
        self.assertIn("\x1b[", written, "样式没有写进终端（界面会全无颜色）")
        self.assertIn("38;2;255;0;0", written, "红色没有出现")

    def test_style_does_not_affect_width(self) -> None:
        """带上颜色之后宽度**一点都不能变**，否则所有宽度计算全废。"""
        plain = Text("中文 abc")
        styled = Text("中文 abc", style="bold #ff0000")
        from logox.tui.render.ansi import visible_width

        self.assertEqual(visible_width(plain.plain), visible_width(styled.plain))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
