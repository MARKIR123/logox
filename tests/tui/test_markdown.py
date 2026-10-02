"""Markdown 渲染的测试（D79：对齐 Pi 的 ``Markdown`` 组件）。

为什么这个文件重要
------------------
模型回答**默认就是 Markdown**。之前我们把正文当纯文本画，屏幕上出现的是字面量
``1. **终端主题/配色问题**``——用户直接指出了这个问题。所以这里的用例同时守住两件事：

1. **语法符号不能出现在屏幕上**（``**`` / ``#`` / 反引号都要吃掉）；
2. **每一行都不超宽**——Textual 遇到超宽行是**裁掉**（不是折行），
   而"话说到一半没了"这个症状我们已经被咬过三次（PROJECT-REVIEW §5.21）。
"""

from __future__ import annotations

import random
import unittest
from collections import Counter

from rich.cells import cell_len
from rich.text import Text

from logox.tui.content.cards import CardContext
from logox.tui.content.markdown import parse_markdown, render_inline, render_markdown
from logox.tui.theme import load_theme

PALETTE = load_theme("logox-dark").palette

#: 框线表的"骨架"字符：断言内容时要把它们（连同补白空格）全部去掉。
_BOX_CHARS = " \n┌┬┐├┼┤└┴┘│─"


def _ctx(width: int = 72) -> CardContext:
    return CardContext(palette=PALETTE, width=width)


def _plain(lines: list[Text]) -> str:
    return "\n".join(line.plain for line in lines)


def _strip_box(rendered: str) -> str:
    """去掉框线与补白，只留内容字符。"""
    return "".join(char for char in rendered if char not in _BOX_CHARS)


class ParseTests(unittest.TestCase):
    """块级解析：每种元素都要被认出来。"""

    def test_heading_levels(self) -> None:
        blocks = parse_markdown("# 一级\n\n### 三级")
        self.assertEqual([(b.kind, b.level) for b in blocks], [("heading", 1), ("heading", 3)])

    def test_hash_without_space_is_not_a_heading(self) -> None:
        """``#tag`` 不是标题——这是 Markdown 的规矩，踩错会把正文吃掉。"""
        blocks = parse_markdown("#标签不是标题")
        self.assertEqual(blocks[0].kind, "para")

    def test_code_fence_keeps_content_verbatim(self) -> None:
        """代码块内容**原样保留**：里面的 ``#`` 不能变成标题、缩进不能动。"""
        blocks = parse_markdown("```python\n# 注释\n    print(1)\n```")
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0].kind, "code")
        self.assertEqual(blocks[0].lang, "python")
        self.assertEqual(blocks[0].lines, ["# 注释", "    print(1)"])

    def test_fence_without_closing_still_parses(self) -> None:
        """流式输出里代码块可能**还没收尾**——不能因此报错或吞掉内容。"""
        blocks = parse_markdown("```python\nprint(1)")
        self.assertEqual(blocks[0].kind, "code")
        self.assertEqual(blocks[0].lines, ["print(1)"])

    def test_lists_and_quote_and_hr(self) -> None:
        blocks = parse_markdown("- 甲\n- 乙\n\n1. 丙\n\n> 引\n\n---")
        kinds = [b.kind for b in blocks]
        self.assertEqual(kinds, ["bullet", "bullet", "ordered", "quote", "hr"])

    def test_hr_is_not_a_bullet(self) -> None:
        """``---`` 必须在无序列表**之前**判断，否则会被当成列表项。"""
        self.assertEqual(parse_markdown("---")[0].kind, "hr")

    def test_paragraph_joins_consecutive_lines(self) -> None:
        blocks = parse_markdown("第一句。\n第二句。")
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0].text, "第一句。\n第二句。")


class InlineTests(unittest.TestCase):
    """行内语法：符号要被吃掉，样式要贴上。"""

    def test_bold_markers_are_consumed(self) -> None:
        text = render_inline("这是 **重点** 内容", PALETTE)
        self.assertEqual(text.plain, "这是 重点 内容")
        self.assertNotIn("**", text.plain)

    def test_inline_code_is_literal(self) -> None:
        """反引号里的内容**不做任何解析**：`` `a*b*c` `` 的星号不该当斜体。"""
        text = render_inline("看 `a*b*c` 这里", PALETTE)
        self.assertEqual(text.plain, "看 a*b*c 这里")
        self.assertNotIn("`", text.plain)

    def test_link_shows_text_and_url(self) -> None:
        text = render_inline("[文档](https://example.com)", PALETTE)
        self.assertIn("文档", text.plain)
        self.assertIn("https://example.com", text.plain)
        self.assertNotIn("](", text.plain)

    def test_italic_markers_are_consumed(self) -> None:
        self.assertEqual(render_inline("这是 *斜体* 字", PALETTE).plain, "这是 斜体 字")

    def test_plain_text_is_untouched(self) -> None:
        self.assertEqual(render_inline("普通一句话。", PALETTE).plain, "普通一句话。")

    def test_underscore_in_identifier_is_not_italic(self) -> None:
        """``snake_case_name`` 不能被当成斜体——代码里到处都是下划线。"""
        self.assertEqual(render_inline("foo_bar_baz", PALETTE).plain, "foo_bar_baz")


class RenderTests(unittest.TestCase):
    """整块渲染：屏幕上看得到什么。"""

    def test_no_markdown_syntax_leaks_to_screen(self) -> None:
        """★ **核心用例**：渲染结果里不该出现任何 Markdown 标记。

        这条直接对应用户的报障："屏幕上看到的是 ``**`` 和序号"。
        """
        source = (
            "# 标题\n\n"
            "1. **粗体** 与 `代码`\n"
            "2. [链接](https://a.example)\n\n"
            "> 引用\n\n"
            "```python\nprint(1)\n```\n"
        )
        rendered = _plain(render_markdown(source, 72, _ctx()))
        for marker in ("**", "`", "](", "```"):
            self.assertNotIn(marker, rendered, f"Markdown 标记 {marker!r} 漏到了屏幕上")
        self.assertNotIn("# 标题", rendered, "标题的 # 不该显示")

    def test_never_exceeds_width(self) -> None:
        """★ 宽度不变量：**任何**宽度、**任何**内容都不能超宽。

        为什么要固定这句话：Textual 对超宽行是**裁掉**而不是折行，
        症状是"话说到一半没了"——我们已经为它返工过三次（§5.21）。
        """
        source = (
            "普通中文段落，长度足够触发折行，包含 English words and 123 numbers。\n\n"
            "## 一个标题\n\n- 列表项 " + "很长" * 40 + "\n\n"
            "```text\n" + "x" * 200 + "\n```\n\n> 引用 " + "内容" * 30 + "\n"
        )
        for width in (16, 20, 33, 50, 72, 120):
            with self.subTest(width=width):
                for line in render_markdown(source, width, _ctx(width)):
                    self.assertLessEqual(
                        cell_len(line.plain), width, f"超宽：{line.plain!r}"
                    )

    def test_code_block_keeps_its_own_line_breaks(self) -> None:
        """代码的换行是**语义**，不能被重排（与正文的处理刚好相反）。"""
        source = "```python\ndef f():\n    return 1\n```"
        rendered = _plain(render_markdown(source, 60, _ctx(60)))
        self.assertIn("def f():", rendered)
        self.assertIn("    return 1", rendered, "代码缩进被吃掉了")

    def test_code_block_long_line_is_hard_split_not_dropped(self) -> None:
        """代码超长行必须**硬切**（不丢内容），不能截断。"""
        source = "```\n" + "x" * 100 + "\n```"
        rendered = _plain(render_markdown(source, 30, _ctx(30)))
        self.assertEqual(rendered.count("x"), 100, "超长代码行丢了内容")

    def test_blank_lines_still_get_a_blank_line(self) -> None:
        """代码块里的空行必须保留（否则代码看起来被压扁了）。"""
        source = "```\ndef a():\n\n    pass\n```"
        rendered = _plain(render_markdown(source, 40, _ctx(40)))
        self.assertIn("│", rendered)
        self.assertIn("def a():", rendered)
        self.assertIn("    pass", rendered)

    def test_empty_source_renders_nothing(self) -> None:
        self.assertEqual(render_markdown("", 40, _ctx(40)), [])
        self.assertEqual(render_markdown("   \n\n  ", 40, _ctx(40)), [])

    def test_zero_width_is_safe(self) -> None:
        self.assertEqual(render_markdown("有内容", 0, _ctx(0)), [])

    def test_unknown_fence_language_still_renders(self) -> None:
        rendered = _plain(render_markdown("```brainfuck\n+++\n```", 40, _ctx(40)))
        self.assertIn("+++", rendered)
        self.assertIn("brainfuck", rendered, "语言标签应当显示出来")

    def test_headings_are_bold_and_colored(self) -> None:
        """标题靠**粗体 + 暖色**表达层级（终端没有字号）。"""
        lines = render_markdown("## 标题", 40, _ctx(40))
        self.assertEqual(lines[0].plain, "标题")
        self.assertTrue(lines[0].spans, "标题没有任何样式")
        rendered_styles = " ".join(str(span.style) for span in lines[0].spans)
        self.assertIn("bold", rendered_styles.lower())

    def test_code_block_borders_never_exceed_width(self) -> None:
        """★ 代码块的边框**不能超过 width**（多一格会被终端裁掉，右边框就消失了）。

        这条是被真机抓出来的：``footer = "╰" + "─" * (width - 1)`` 算出来是
        ``width + 1``——屏幕上一行捅出去一格，而 Textual 对超宽行是**裁掉**。

        ⚠️ D81 之后**不再补到整宽**：补白是"整块底色"的配套要求，
        去掉底色后它只剩坏处（行尾一长串空格，复制出来带尾随空白）。
        因此现在断言的是"**不超过**"而不是"恰好等于"。
        """
        for width in (20, 33, 50, 84):
            with self.subTest(width=width):
                lines = render_markdown("```python\nx = 1\n```", width, _ctx(width))
                for line in lines:
                    self.assertLessEqual(cell_len(line.plain), width, f"超宽：{line.plain!r}")

    def test_code_block_has_no_trailing_padding(self) -> None:
        """代码块行尾不留补白（D81）——否则复制出来带一长串尾随空格。"""
        lines = render_markdown("```python\nx = 1\n```", 40, _ctx(40))
        for line in lines:
            self.assertEqual(line.plain, line.plain.rstrip(), f"行尾有补白：{line.plain!r}")

    def test_blank_line_inside_code_block_survives(self) -> None:
        """代码块里的空行必须保留（否则代码看起来被压扁了）。"""
        lines = render_markdown("```\na\n\nb\n```", 30, _ctx(30))
        blanks = [line for line in lines if line.plain.strip("│╭╰╮╯─ ") == ""]
        # 至少有一行是"只有边框和空白"（也就是那块空行）
        self.assertGreaterEqual(len(blanks), 3, "代码块的空行被吃掉了")

    def test_code_block_box_is_closed_on_right_side(self) -> None:
        """代码块四周边框必须完全闭合：右上 ╮、右侧 │、右下 ╯。"""
        width = 40
        lines = render_markdown("```python\nprint('hello world')\n```", width, _ctx(width))
        self.assertEqual(len(lines), 3)

        top = lines[0].plain
        self.assertTrue(top.startswith("╭"), f"顶边应以 ╭ 开始：{top!r}")
        self.assertTrue(top.endswith("╮"), f"顶边应以 ╮ 闭合：{top!r}")
        self.assertEqual(cell_len(top), width)

        mid = lines[1].plain
        self.assertTrue(mid.startswith("│ "), f"代码行应以 │ 开始：{mid!r}")
        self.assertTrue(mid.endswith(" │"), f"代码行右侧应以 │ 闭合：{mid!r}")
        self.assertEqual(cell_len(mid), width)

        bottom = lines[2].plain
        self.assertTrue(bottom.startswith("╰"), f"底边应以 ╰ 开始：{bottom!r}")
        self.assertTrue(bottom.endswith("╯"), f"底边应以 ╯ 闭合：{bottom!r}")
        self.assertEqual(cell_len(bottom), width)


class TableTests(unittest.TestCase):
    """表格（D115）：``| 表头 |`` + ``|---|`` → 闭合框线表。

    为什么单开一组：改动前这里**没有表格分支**，``| 方案 | 优点 |`` 被当成普通段落，
    屏幕上出现的是字面量竖线；窄终端下还会在**格子中间**折行，一行数据被劈成两行。
    所以下面同时守住三件事：竖线不漏到屏幕上、框线闭合且等宽、内容一个字都不丢。
    """

    SOURCE = (
        "| 方案 | 优点 | 缺点 |\n"
        "|---|---|---|\n"
        "| 事件总线 | 解耦彻底、可观测 | 调试链路长 |\n"
        "| 直接调用 | 简单直观 | 容易耦合 |\n"
    )

    # -- 块级解析 ------------------------------------------------------- #

    def test_parses_header_rows_and_alignment(self) -> None:
        blocks = parse_markdown("| a | b | c |\n|:--|:-:|--:|\n| 1 | 2 | 3 |")
        self.assertEqual(len(blocks), 1)
        block = blocks[0]
        self.assertEqual(block.kind, "table")
        self.assertEqual(block.header, ["a", "b", "c"])
        self.assertEqual(block.align, ["left", "center", "right"])
        self.assertEqual(block.rows, [["1", "2", "3"]])

    def test_leading_and_trailing_pipes_are_optional(self) -> None:
        """``a | b`` + ``--- | ---`` 也是合法表格（GFM 允许省略首尾竖线）。"""
        blocks = parse_markdown("a | b\n--- | ---\n1 | 2")
        self.assertEqual(blocks[0].kind, "table")
        self.assertEqual(blocks[0].header, ["a", "b"])

    def test_delimiter_row_is_required(self) -> None:
        """★ 没有分隔行就不是表格——否则正文里随便一个 ``|`` 都会被吞掉。"""
        blocks = parse_markdown("a | b\nc | d")
        self.assertEqual([b.kind for b in blocks], ["para"])

    def test_delimiter_column_count_must_match(self) -> None:
        """分隔行列数与表头不一致 → 不是表格（只是一行恰好带竖线的正文）。"""
        blocks = parse_markdown("| a | b |\n|---|---|---|\n| 1 | 2 |")
        self.assertNotEqual(blocks[0].kind, "table")

    def test_escaped_pipe_stays_inside_the_cell(self) -> None:
        blocks = parse_markdown("| a \\| b | c |\n|---|---|\n| 1 | 2 |")
        self.assertEqual(blocks[0].header, ["a | b", "c"])

    def test_table_stops_at_a_blank_line(self) -> None:
        blocks = parse_markdown("| a | b |\n|---|---|\n| 1 | 2 |\n\n正文段落")
        self.assertEqual(blocks[0].kind, "table")
        self.assertEqual(blocks[0].rows, [["1", "2"]])
        self.assertEqual(blocks[-1].kind, "para")

    def test_table_inside_a_fence_is_not_a_table(self) -> None:
        """代码块优先：围栏里的 ``|`` 是代码，不是表格。"""
        blocks = parse_markdown("```\n| a | b |\n|---|---|\n```")
        self.assertEqual([b.kind for b in blocks], ["code"])

    def test_ragged_rows_are_padded_and_truncated(self) -> None:
        """行内单元格数与表头不一致时：少的补空、多的丢掉（GFM 的规矩）。"""
        blocks = parse_markdown("| a | b | c |\n|---|---|---|\n| 1 |\n| 1 | 2 | 3 | 4 |")
        self.assertEqual(blocks[0].kind, "table")
        rendered = _plain(render_markdown("| a | b | c |\n|---|---|---|\n| 1 |\n| 1 | 2 | 3 | 4 |", 60, _ctx(60)))
        # 多出来的第 4 格不渲染；缺格的那一行不因此错位（各列仍然对齐）
        self.assertNotIn("4", rendered)
        self.assertIn("1", rendered)
        sizes = {cell_len(line.plain) for line in render_markdown(
            "| a | b | c |\n|---|---|---|\n| 1 |\n| 1 | 2 | 3 | 4 |", 60, _ctx(60)
        )}
        self.assertEqual(len(sizes), 1, "缺格的行让表格宽度不一致了")

    # -- 渲染 ----------------------------------------------------------- #

    def test_no_literal_pipe_leaks_to_screen(self) -> None:
        """★ **核心用例**：屏幕上不该出现字面量竖线（框线用的是 ``│``）。"""
        rendered = _plain(render_markdown(self.SOURCE, 60, _ctx(60)))
        self.assertNotIn("|", rendered)
        self.assertIn("方案", rendered)

    def test_box_is_closed_and_every_row_is_the_same_width(self) -> None:
        """框线表：四角闭合，且**每一行等宽**（差一格就会在屏幕上歪掉）。"""
        width = 60
        lines = render_markdown(self.SOURCE, width, _ctx(width))
        sizes = {cell_len(line.plain) for line in lines}
        self.assertEqual(len(sizes), 1, f"表格各行宽度不一致：{sizes}")
        self.assertLessEqual(sizes.pop(), width)
        self.assertTrue(lines[0].plain.startswith("┌"))
        self.assertTrue(lines[0].plain.endswith("┐"))
        self.assertTrue(lines[-1].plain.startswith("└"))
        self.assertTrue(lines[-1].plain.endswith("┘"))
        for line in lines[1:-1]:
            # 表头分隔线是 ├─┼─┤，内容行是 │ … │，两种都必须两侧收口
            self.assertIn(line.plain[0], "│├", line.plain)
            self.assertIn(line.plain[-1], "│┤", line.plain)

    def test_alignment_is_honoured(self) -> None:
        """``:--`` / ``:-:`` / ``--:`` 三种对齐都要落地。"""
        lines = _plain(
            render_markdown(
                "| 左 | 中 | 右 |\n|:--|:-:|--:|\n| a | b | c |\n", 40, _ctx(40)
            )
        )
        body = [line for line in lines.split("\n") if line.startswith("│")][-1]
        # 右对齐：内容紧贴右侧（"c" 与右竖线之间只有一格空隙）
        self.assertTrue(body.endswith("c │"), body)
        # 左对齐：内容紧贴左侧（"a" 前面只有一格空隙）
        self.assertIn("│ a ", body)

    def test_header_is_bold_and_separated(self) -> None:
        lines = render_markdown(self.SOURCE, 60, _ctx(60))
        styles = " ".join(str(span.style) for span in lines[1].spans)
        self.assertIn("bold", styles.lower(), "表头没有加粗")
        self.assertTrue(any(line.plain.startswith("├") for line in lines), "缺少表头分隔线")

    def test_content_is_never_truncated(self) -> None:
        """★ 折行**不截断**：屏幕上出现的字符必须与表格内容**一模一样**（不多不少）。

        只比对"字符多重集"而不是"连续子串"——多列表格里各列是**逐行交错**排布的
        （第 1 行放各列的第一段、第 2 行放各列的第二段），所以一个单元格被折开后，
        它的两段中间隔着别的列。字符集相等足以证明"一个字都没丢、也没凭空多"。

        宽度只取 ≥22 的：三列表格放不下任何一列时会走**记录式降级**，
        那条路径会额外加上 ``•`` 与 ``表头：`` 这些本来没有的字符
        （降级路径由 :meth:`test_very_narrow_falls_back_to_records` 单独守）。
        """
        cells = "方案优点缺点事件总线解耦彻底、可观测调试链路长直接调用简单直观容易耦合"
        for width in (22, 28, 40, 60):
            with self.subTest(width=width):
                rendered = _plain(render_markdown(self.SOURCE, width, _ctx(width)))
                self.assertEqual(Counter(_strip_box(rendered)), Counter(cells), f"width={width} 内容对不上")

    def test_wrapped_cell_keeps_its_characters_in_order(self) -> None:
        """单列表格里没有跨列交错，所以可以断言"字**按原顺序**连成一片"。"""
        text = "解耦彻底、可观测，这是一段会被折成好几行的长文本"
        source = f"| 内容 |\n|---|\n| {text} |\n"
        for width in (12, 16, 20, 40):
            with self.subTest(width=width):
                rendered = _plain(render_markdown(source, width, _ctx(width)))
                self.assertIn(text, _strip_box(rendered))

    def test_very_narrow_falls_back_to_records(self) -> None:
        """放不下列就退化成记录式列表：表头当字段名，**不画框线**、也不丢内容。"""
        rendered = _plain(render_markdown(self.SOURCE, 10, _ctx(10)))
        self.assertNotIn("┌", rendered)
        self.assertNotIn("│", rendered)
        self.assertIn("方案：", rendered)
        flat = _strip_box(rendered)
        for cell in ("事件总线", "解耦彻底、可观测", "调试链路长", "简单直观", "容易耦合"):
            self.assertIn(cell, flat, f"记录式降级丢了 {cell}")

    def test_inline_markdown_in_cells_is_rendered(self) -> None:
        """单元格里也支持行内语法，**标记不漏到屏幕上，样式也不许丢**。"""
        lines = render_markdown("| a | b |\n|---|---|\n| **粗** | `code` |\n", 40, _ctx(40))
        rendered = _plain(lines)
        self.assertNotIn("**", rendered)
        self.assertNotIn("`", rendered)
        self.assertIn("粗", rendered)

        styles = " ".join(str(span.style) for line in lines for span in line.spans)
        self.assertIn(PALETTE.md_code, styles, "行内代码的样式丢了")
        self.assertIn("bold", styles, "单元格里的粗体样式丢了")

    def test_inline_styles_survive_the_narrow_fallback(self) -> None:
        """降级为记录式列表时，单元格里的行内样式同样不许丢。"""
        lines = render_markdown("| a | b |\n|---|---|\n| **粗** | `code` |\n", 9, _ctx(9))
        styles = " ".join(str(span.style) for line in lines for span in line.spans)
        self.assertIn(PALETTE.md_code, styles, "降级路径丢了行内代码样式")
        self.assertIn("bold", styles, "降级路径丢了粗体样式")
        self.assertIn(PALETTE.md_heading, styles, "降级路径丢了字段名颜色")

    def test_cjk_column_width_uses_cells_not_characters(self) -> None:
        """★ 中文按 **2 cell** 计宽：用 ``len()`` 会让右边框捅出屏幕一格。"""
        width = 30
        for line in render_markdown("| 中文表头 | b |\n|---|---|\n| 中文内容 | 2 |\n", width, _ctx(width)):
            self.assertLessEqual(cell_len(line.plain), width, line.plain)

    def test_table_never_exceeds_width(self) -> None:
        source = (
            "| 列一 | 列二 | 列三 | 列四 |\n|---|---|---|---|\n"
            "| " + "很长的中文内容" * 3 + " | x | " + "y" * 40 + " | z |\n"
        )
        for width in (8, 9, 13, 16, 20, 33, 50, 72, 120):
            with self.subTest(width=width):
                for line in render_markdown(source, width, _ctx(width)):
                    self.assertLessEqual(cell_len(line.plain), width, f"超宽：{line.plain!r}")

    def test_header_only_table_does_not_invent_labels(self) -> None:
        """只有表头、没有正文行时不该凭空造出 ``方案：`` 这样的空字段。"""
        rendered = _plain(render_markdown("| a | b |\n|---|---|\n", 10, _ctx(10)))
        self.assertNotIn("：", rendered)
        self.assertIn("a", rendered)

    def test_random_tables_never_overflow_and_never_lose_content(self) -> None:
        """★ 随机表格的**不变量**测试：任意宽度都不超宽、内容一个字符都不丢。

        为什么要有这条：表格是**唯一**一种"列宽互相牵制"的块——总宽不够时每一列
        都得同时让步，而这种牵制最容易写出两类错：``sum(列宽) > 可用宽度``
        （右边框被终端裁掉）或把某一列饿到 1 格、中文被劈成半个。
        随机内容用**固定种子**，所以每次跑的都是同一批表格（失败可复现）。

        内容核对用"**去掉框线与空白后的字符多重集**"：框线是渲染加上去的，
        补白是空格；剩下字符必须与 ``render_inline(单元格)`` 的正文逐字符相等
        ——多一个字符（凭空加）或少一个（被裁掉）都会当场失败。
        """
        rng = random.Random(20260917)
        alphabet = "abc 中文，。xyz`*-_[]() "
        widths = (5, 8, 13, 21, 34, 55, 89, 144)

        def random_cells(count: int) -> list[str]:
            # 刻意不含 `|`：否则生成出来的"一行"会被切成更多列，测的就不是表格了
            return ["".join(rng.choice(alphabet) for _ in range(rng.randint(0, 10))) for _ in range(count)]

        for case in range(120):
            ncols = rng.randint(1, 4)
            header = random_cells(ncols)
            body = [random_cells(ncols) for _ in range(rng.randint(0, 3))]
            delims = "|".join(rng.choice(["-", "---", ":-", "-:", ":-:"]) for _ in range(ncols))
            source = (
                "| " + " | ".join(header) + " |\n"
                f"|{delims}|\n" + "".join("| " + " | ".join(row) + " |\n" for row in body)
            )

            block = parse_markdown(source)[0]
            expected = Counter(
                "".join(render_inline(cell, PALETTE).plain for cell in [*block.header, *sum(block.rows, [])]).replace(
                    " ", ""
                )
            )

            for width in widths:
                with self.subTest(case=case, width=width):
                    lines = render_markdown(source, width, _ctx(width))
                    for line in lines:
                        self.assertLessEqual(cell_len(line.plain), width, f"超宽：{line.plain!r}")
                    # 只对框线表比对内容（记录式降级会额外加上 `•` 与 `表头：`）
                    if any(line.plain.startswith("┌") for line in lines):
                        got = Counter(_strip_box(_plain(lines)))
                        self.assertEqual(got, expected, f"case={case} width={width} 内容对不上：{source!r}")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
