"""ANSI 感知宽度的测试（D80）。

为什么这个文件值得单独存在
--------------------------
**宽度算错 7 格**这种事，症状是"整个界面错位"——极难从现象反推原因。
而转义序列的种类比想象的杂：CSI（颜色）、OSC（标题/超链接）、
APC/DCS/PM/SOS（应用自定义，**IME 光标标记就在这一类**）。

只要漏掉一类，那一类的输出就会让所有宽度计算偏大。
"""

from __future__ import annotations

import unittest

from rich.cells import cell_len
from rich.text import Text

from logox.tui.render.ansi import (
    LINE_RESET,
    split_styled_lines,
    strip_ansi,
    text_to_ansi,
    visible_width,
)
from logox.tui.render.components.text import CURSOR_MARKER


class StripAnsiTests(unittest.TestCase):
    def test_plain_text_untouched(self) -> None:
        self.assertEqual(strip_ansi("普通文本"), "普通文本")

    def test_color_sequences_are_removed(self) -> None:
        self.assertEqual(strip_ansi("\x1b[31mred\x1b[0m"), "red")

    def test_cursor_movement_is_removed(self) -> None:
        self.assertEqual(strip_ansi("\x1b[2Aup"), "up")

    def test_synchronized_output_is_removed(self) -> None:
        """同步输出序列也不占格子（它每帧都在输出里）。"""
        self.assertEqual(strip_ansi("\x1b[?2026h内容\x1b[?2026l"), "内容")

    def test_osc_with_bel_terminator(self) -> None:
        self.assertEqual(strip_ansi("\x1b]0;标题\x07正文"), "正文")

    def test_osc_with_st_terminator(self) -> None:
        self.assertEqual(strip_ansi("\x1b]8;;http://x\x1b\\链接"), "链接")

    def test_apc_sequence_is_removed(self) -> None:
        """★ APC 是 IME 光标标记用的那一类 —— 漏掉它就会多算 7 格。"""
        self.assertEqual(strip_ansi(CURSOR_MARKER + "abc"), "abc")


class VisibleWidthTests(unittest.TestCase):
    def test_matches_cell_len_for_plain_text(self) -> None:
        for text in ("abc", "中文", "a中b"):
            with self.subTest(text=text):
                self.assertEqual(visible_width(text), cell_len(text))

    def test_escape_sequences_are_zero_width(self) -> None:
        """★ 核心用例：转义序列**一格都不占**。

        `rich.cells.cell_len` 会把 ``\\x1b_Ga=p\\x1b\\\\`` 数成 7，
        这就是"宽度算错"的源头。
        """
        self.assertEqual(visible_width("\x1b[31m\x1b[0m"), 0)
        self.assertEqual(cell_len(CURSOR_MARKER), 6, "前提变了：cell_len 现在会剥序列？")
        self.assertEqual(visible_width(CURSOR_MARKER), 0)

    def test_marker_does_not_change_the_measured_width(self) -> None:
        plain = "一段文本"
        marked = "一段" + CURSOR_MARKER + "文本"
        self.assertEqual(visible_width(marked), visible_width(plain))

    def test_cjk_counts_two_cells(self) -> None:
        self.assertEqual(visible_width("中文"), 4)


class TextToAnsiTests(unittest.TestCase):
    """反向转换：把带样式的行变成**真的能写进终端的字节**。

    这一组的存在理由是一个真实缺陷：渲染器最初写的是 ``line.plain``，
    于是**主题的 53 个颜色全部失效**，界面上全是一个颜色——
    而代码里看起来一切正常（宽度断言用的是 plain，照样通过），**没有任何报错**。
    所以这里守两件事：样式**真的写出去了**，且宽度**一点没变**。
    """

    def test_plain_text_has_no_escapes(self) -> None:
        self.assertEqual(text_to_ansi(Text("普通文本")), "普通文本")

    def test_style_produces_sgr(self) -> None:
        out = text_to_ansi(Text("红", style="bold #ff0000"))
        self.assertIn("38;2;255;0;0", out)
        self.assertIn("1", out.split("m")[0], "加粗没有生效")

    def test_spans_are_applied_and_overridden_in_order(self) -> None:
        """span 是**按顺序**叠加的：后面的覆盖前面的，与 Rich 的语义一致。"""
        line = Text("abcdef", style="#111111")
        line.stylize("#ff0000", 0, 3)
        line.stylize("#00ff00", 1, 2)  # 覆盖中间那一个
        out = text_to_ansi(line)
        self.assertIn("38;2;255;0;0", out)
        self.assertIn("38;2;0;255;0", out)
        self.assertIn("38;2;17;17;17", out)

    def test_width_is_preserved(self) -> None:
        """★ 量的口径与写的口径必须一致，否则宽度不变量形同虚设。"""
        for text in ("abc", "中文 abc", "a", ""):
            with self.subTest(text=text):
                line = Text(text, style="bold #ff0000")
                line.stylize("on #000080", 0, len(text))
                self.assertEqual(visible_width(text_to_ansi(line)), visible_width(text))

    def test_empty_line_is_empty(self) -> None:
        """空行不写任何字节（清行由渲染器的 ``ESC[2K`` 负责）。"""
        self.assertEqual(text_to_ansi(Text()), "")

    def test_consecutive_equal_styles_are_merged(self) -> None:
        """同一样式的相邻字符合成**一段**：转义序列更少 = 每帧字节更少。"""
        line = Text("aaaa", style="#ff0000")
        out = text_to_ansi(line)
        self.assertEqual(out.count("38;2;255;0;0"), 1, "相同样式没有被合并成一段")
        self.assertIn("aaaa", out)

    def test_line_reset_closes_links_and_colour(self) -> None:
        self.assertIn("\x1b[0m", LINE_RESET)
        self.assertIn("\x1b]8;;\x07", LINE_RESET)


class SplitStyledLinesTests(unittest.TestCase):
    """把一段 ``Text`` 切成行、**样式逐行保留**。

    这一组来自用户报障：「我希望终端中可以显示加粗字体，和其他颜色的字体，
    就像 markdown 渲染一样」。

    实测的缺陷是：内容层渲染出了 4 段样式（加粗 / 行内代码 / 链接 / 正文），
    而组件交付给终端的行里 **0 段** —— 因为切行时写的是
    ``Text(line, style=text.style)``，把 spans 全丢了。
    症状是"界面全是同一个颜色的字"，而**没有任何报错**。
    """

    def test_spans_survive_splitting(self) -> None:
        line = Text("这是 ")
        line.append("加粗", style="bold")
        line.append(" 与 ")
        line.append("代码", style="#89dceb")
        rows = split_styled_lines(line)
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(rows[0].spans), 2, "加粗与代码色都没有留下来")

    def test_ansi_really_contains_bold_and_colour(self) -> None:
        """★ 端到端：切完行之后，真正的字节里有加粗与颜色。"""
        line = Text("普通 ")
        line.append("加粗", style="bold")
        line.append(" 彩色", style="#ff0000")
        ansi = text_to_ansi(split_styled_lines(line)[0])
        self.assertIn("\x1b[1", ansi, "没有加粗 SGR")
        self.assertIn("38;2;255;0;0", ansi, "没有 24 位颜色")

    def test_multi_line_keeps_styles_per_line(self) -> None:
        block = Text("第一行\n")
        block.append("第二行", style="bold")
        rows = split_styled_lines(block)
        self.assertEqual([row.plain for row in rows], ["第一行", "第二行"])
        self.assertEqual(len(rows[0].spans), 0)
        self.assertEqual(len(rows[1].spans), 1, "第二行的样式丢了")

    def test_base_style_is_kept_under_a_span(self) -> None:
        """★ 基样式与 span 是**叠加**的，不是覆盖。

        ``Text("x", style="bold")`` 再给一个红色 span，意图是"又粗又红"。
        写成"span 覆盖基样式"就会静默丢掉加粗——症状是"某几个字莫名不加粗了"。
        """
        line = Text("红色加粗", style="bold")
        line.stylize("#ff0000", 0, 4)
        ansi = text_to_ansi(split_styled_lines(line)[0])
        self.assertIn("\x1b[1", ansi, "基样式的加粗被 span 覆盖掉了")
        self.assertIn("38;2;255;0;0", ansi)

    def test_trailing_newline_is_a_terminator_not_a_blank_line(self) -> None:
        """``"a\\n"`` 是**一行**，不是"一行 + 一个空行"。"""
        self.assertEqual([row.plain for row in split_styled_lines(Text("a\n"))], ["a"])
        self.assertEqual(
            [row.plain for row in split_styled_lines(Text("a\n\n"))], ["a", ""],
            "两个换行才是一个真的空行",
        )

    def test_empty_text_gives_no_rows(self) -> None:
        self.assertEqual(split_styled_lines(Text("")), [])

    def test_width_is_unchanged_by_splitting(self) -> None:
        line = Text("中文 abc", style="bold #ff0000")
        joined = "".join(row.plain for row in split_styled_lines(line))
        self.assertEqual(visible_width(joined), visible_width(line.plain))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
