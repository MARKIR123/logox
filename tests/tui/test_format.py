"""格式化纯函数的测试（MODULE_tui.md §8 的 ``test_format.py``）。

这些函数是状态栏与工具卡片全部显示文本的来源，因此**每个分支都要覆盖**——
尤其是 CJK 宽度与 ``None`` vs ``0`` 的区分。
"""

from __future__ import annotations

import unittest

from rich.cells import cell_len

from logox.kernel.events import ChangeStat
from logox.tui.format import (
    EMPTY,
    _is_structured,
    clip,
    diff_badge,
    format_bytes,
    format_clock,
    format_cost,
    format_duration,
    format_ratio,
    format_tokens,
    pad_right,
    short_path,
    wrap_cells,
)




class FormatArgsTests(unittest.TestCase):
    """D139：展开态第一项「完整参数」（UI-SPEC §5.6 ①）。

    为什么值得一条用例：`edit` 的 `old_string` / `new_string` 必然超过折叠行
    44 字的摘要长度 —— 完整参数是**唯一**能看到它们的地方。
    """

    def test_json_pretty_with_two_space_indent(self) -> None:
        from logox.tui.format import format_args

        text = format_args({"path": "a.py", "limit": 10})
        self.assertEqual(text, '{\n  "path": "a.py",\n  "limit": 10\n}')

    def test_chinese_is_not_escaped(self) -> None:
        from logox.tui.format import format_args

        self.assertIn("中文路径", format_args({"path": "中文路径/x.py"}))

    def test_empty_args_yield_empty_string(self) -> None:
        from logox.tui.format import format_args

        self.assertEqual(format_args(None), "")
        self.assertEqual(format_args({}), "")

    def test_unserializable_value_falls_back_instead_of_raising(self) -> None:
        from logox.tui.format import format_args

        text = format_args({"weird": object()})
        self.assertIn("weird", text)
class CellWidthTests(unittest.TestCase):
    """UI-SPEC §4：宽度一律按 cell 算，中文占 2 cell。"""

    def test_ascii_clip(self) -> None:
        self.assertEqual(clip("hello world", 5), "hell…")
        self.assertEqual(clip("hello", 5), "hello")

    def test_cjk_clip_does_not_split_characters(self) -> None:
        """中文不能被截半个字，且截断后**实际 cell 宽度**不得超过预算。"""
        text = "把 config.py 里的超时改成 30 秒"
        for width in range(1, 30):
            result = clip(text, width)
            self.assertLessEqual(cell_len(result), width, f"width={width} 时超宽：{result!r}")
        self.assertNotIn("\ufffd", clip(text, 8))

    def test_clip_zero_or_negative_width(self) -> None:
        self.assertEqual(clip("abc", 0), "")
        self.assertEqual(clip("abc", -3), "")

    def test_pad_right_uses_cell_width(self) -> None:
        self.assertEqual(pad_right("abc", 6), "abc   ")
        padded = pad_right("中文", 8)
        self.assertEqual(cell_len(padded), 8)

    def test_short_path_keeps_filename(self) -> None:
        long_path = r"G:\hz\codes\Logox\src\logox\kernel\bus.py"
        result = short_path(long_path, 20)
        self.assertLessEqual(cell_len(result), 20)
        self.assertIn("bus.py", result)


class WrapCellsTests(unittest.TestCase):
    """``wrap_cells`` **重排软换行**（两轮实测反馈的产物）。

    这两组用例分别对应两次真实报障：

    * 第一次报 **"内容凭空不见"**——`clip` 把多行回答从中间截断（已修）；
    * 第二次报 **"还是有奇怪的换行"**——模型给的换行被当成硬换行原样保留，
      于是"一个孤零零的单词占一整行"（本组用例就是它的回归防线）。
    """

    # -- 不变量：任何输入、任何宽度，都不能超宽、不能丢内容 ------------------ #

    def test_never_exceeds_width_and_never_loses_text(self) -> None:
        sample = (
            "Here is what I found.\n"
            "The configuration file lives in the project directory.\n"
            "\n"
            "Steps:\n"
            "1. Open the file\n"
            "- 列表项，中文\n"
            "\n"
            "    def f(x):\n"
            "        return x + 1\n"
        )
        for width in range(8, 90):
            with self.subTest(width=width):
                lines = wrap_cells(sample, width).split("\n")
                for line in lines:
                    self.assertLessEqual(cell_len(line), width, f"超宽：{line!r}")

    def test_zero_width_returns_empty(self) -> None:
        self.assertEqual(wrap_cells("abc", 0), "")
        self.assertEqual(wrap_cells("abc", -5), "")

    # -- 重排：模型给的换行是软换行 ----------------------------------------- #

    def test_prose_lines_are_reflowed_not_kept(self) -> None:
        """**核心用例**：句子中间的硬换行必须被合并，而不是留成半截行。

        报障原文的现象是"一个孤零零的单词占一整行"，成因就是这条换行被当成硬换行。
        """
        text = "It is important\none.\n"
        self.assertEqual(wrap_cells(text, 66), "It is important one.")

    def test_reflowed_lines_are_packed_full(self) -> None:
        """重排的**可观测目的**：除了最后一行，每行都要填满。

        断言"没有短行"才是这个功能真正的验收口径——只断言"合并了没有"
        挡不住"合并不彻底、留下半截行"的回归。
        """
        text = (
            "It contains three sections, and the second one is the important\n"
            "one, because the first two are only defaults.\n"
        )
        lines = wrap_cells(text, 40).split("\n")
        self.assertGreater(len(lines), 1)
        for line in lines[:-1]:
            self.assertGreater(cell_len(line), 40 - 12, f"这一行没填满，像是退回了硬换行：{line!r}")

    def test_words_are_never_split_across_lines(self) -> None:
        """★ **英文单词不得被切成两半**（CHANGE-052，用户第三次报"奇怪的换行"）。

        实测报障原文：摘要被折成
        ``…阶段 5–7（删 turn_lines_of／docstrin`` + ``g／文档收尾）`` ——
        **`docstring` 被从中间切开**，读起来像排版坏了。

        成因：折行器的"优先断点"只有**标点与空格**，而它的**回溯窗口**只有
        `max(8, 行宽//4)`；窗口内一个断点都没有时就硬断 —— 落点落在哪个字符
        纯属巧合，**切进单词内部完全可能**。中文没有这个问题（逐字可断），
        但本项目正文里到处是**英文标识符**（文件名、函数名、命令）。

        ⟹ 修法：硬断前先检查落点是否在单词内部；是则**退到词首**
        （这一行会短一点，但比切开一个词好得多）。

        ⚠️ 一整个词比一行还长时（长 URL、超长标识符）**该切还得切** —— 否则
        每轮不前进会死循环。本用例同时守这一条。
        """
        text = "阶段 5–7（删 turn_lines_of／docstring／文档收尾）待做"
        for width in (20, 30, 40, 60):
            with self.subTest(width=width):
                out = wrap_cells(text, width)
                for line in out.split("\n"):
                    self.assertLessEqual(cell_len(line), width)
                # 重新拼起来必须**逐字等于**原文（不丢字、不多字）
                self.assertEqual("".join(out.split("\n")).replace(" ", ""),
                                 text.replace(" ", ""), "折行不得改动内容")
                # 任何**单词**都不许被切断：检查每个标识符是否完整出现在某一行里
                for word in ("turn_lines_of", "docstring"):
                    self.assertTrue(
                        any(word in line for line in out.split("\n")),
                        f"宽 {width}：单词 {word!r} 被切开了 → {out.splitlines()}",
                    )

    def test_an_overlong_word_is_still_hard_broken(self) -> None:
        """**反例守卫**：整词长于一行时仍须硬断（否则折行器不再前进 = 死循环）。

        没有这一条的话，"退到词首"那行代码可以被写成"一直退到 start"，
        于是每一轮都不前进 —— 症状是**界面卡死**，而普通用例（文本短）
        根本跑不出来。
        """
        long_word = "a" * 50
        out = wrap_cells(long_word, 20).split("\n")
        self.assertGreaterEqual(len(out), 3, "50 个字符在 20 格里必须折成多行")
        self.assertEqual("".join(out), long_word, "不能丢字符")

    def test_lines_are_naturally_packed_without_artificial_mid_split(self) -> None:
        """★ **排版回归自然贪心满行，无末行平分副作用**（CHANGE-053 / D177）。

        用户裁定方案 A：彻底删除 `_balance_tail`。
        旧版 `_balance_tail` 把倒数第二行与末行强制对半分（target = combined // 2），
        导致 110 列宽下倒数第二行在 60 列处突兀截断、留白 50 列。
        修后：除末行外，每行都应尽量填满预算。
        """
        text = (
            "smoke/compaction-preview/compaction/*.md；注意预览用的是磁盘"
            "新代码而运行中的进程仍是旧代码，需 /status 确认并重启才一致。"
        )
        lines = wrap_cells(text, 110).split("\n")
        # 110 宽下合计 120 格，原本两行各 60 格；修后首行应接近满行（> 100 格）
        self.assertGreater(cell_len(lines[0]), 95, f"首行未自然填满，疑似仍有强制平分：{lines[0]!r}")

    def test_trailing_text_that_fits_is_not_pushed_onto_an_orphan_line(self) -> None:
        """★ **末行本来放得下，就不得被切下来**（2026-09-29）。

        用户第四次报"摘要末尾有奇怪的换行"，这次的形态是::

            …并行会话 D199 缓存契约与旧断言冲突，未擅改），需重启 logox
            生效                                            ← 孤字行

        根因与前面几次**都不同**：`_scan_preferred` 在"剩余内容整体放得下"时
        仍然去找"更漂亮的断点"，把最后一个空格当断点，于是 95 格的内容
        被切成 90 + 4。修法是在扫描前先判断"这一段本行吃得下"，是则一次吃完。

        ⚠️ 这个缺陷**只在整段超宽时才出现**：整体放得下会命中
        `_wrap_single_line` 的快速路径（`cell_len(text) <= width` 直接返回一行）。
        所以复现必须让**总宽超过预算**，否则用例会"通过得毫无意义"
        —— 我第一次就是这么写的，红检时它没变红。
        """
        cases = [
            ("A" * 150 + " " + "生效", 2),  # 纯 ASCII 尾
            ("中文内容" * 30 + "，需重启 logox 生效", 3),  # 中文尾
        ]
        for text, expected_lines in cases:
            with self.subTest(head=text[:12]):
                lines = wrap_cells(text, 100).split("\n")
                for line in lines:
                    self.assertLessEqual(cell_len(line), 100, "不得超宽")
                self.assertEqual(
                    "".join(lines).replace(" ", ""), text.replace(" ", ""), "不得丢字"
                )
                self.assertNotIn(
                    "生效", [line.strip() for line in lines],
                    f"末行被切成孤字行（应为 {expected_lines} 行）：{lines!r}",
                )
                self.assertTrue(
                    lines[-1].endswith("生效"), f"尾部内容应留在末行：{lines!r}"
                )

    def test_thousands_separator_is_never_split(self) -> None:
        """★ **千分位数字绝不得在逗号处被截断**（CHANGE-053 / D177）。

        实测报障：`1,048,576` 被切成 `1,048,` 换行 `576`，`32,768` 被切成 `32,` 换行 `768`。
        断点回溯在遇到夹在两数字间的 `,` 时，不得将其视为标点断开。
        """
        samples = [
            "deepseek-flash 窗口 1,048,576−reserve 32,768）；测试",
            "当前 848,649 / 触发线 1,015,808，下一次自动压缩还差 167,159 tokens",
            "数字序列：10,000 与 20,000 与 30,000 与 40,000 与 50,000",
        ]
        for width in (20, 30, 40, 60, 80, 100, 110):
            for sample in samples:
                with self.subTest(width=width, head=sample[:16]):
                    out = wrap_cells(sample, width)
                    lines = out.split("\n")
                    for line in lines:
                        self.assertLessEqual(cell_len(line), width)
                    # 检查千分位数字是否完整保留在同一行中
                    for num in ("1,048,576", "32,768", "848,649", "1,015,808", "167,159"):
                        if num in sample:
                            self.assertTrue(
                                any(num in line for line in lines),
                                f"宽 {width} 下数字 {num} 被腰斩：{lines}",
                            )

    def test_filename_and_extensions_are_never_split(self) -> None:
        """★ **文件名与扩展名绝不得在点号处被截断**（CHANGE-053 / D177）。

        实测报障：`*.md` 被切成 `*.` 换行 `md`，`.smoke` 被切成 `.` 换行 `smoke`。
        点号后紧随字母数字时属于文件名/路径/版本号，不得作为断点。
        """
        text = "现场在 .smoke/compaction/*.md；脚本 preview-next.py 正常"
        for width in (35, 40, 60, 80, 100):
            with self.subTest(width=width):
                lines = wrap_cells(text, width).split("\n")
                for line in lines:
                    self.assertLessEqual(cell_len(line), width)
                # 检查 *.md 与 .py 是否没有被在点号处拆分
                self.assertFalse(any(line.endswith("*.") for line in lines), f"*. 被拆开：{lines}")
                self.assertTrue(any("*.md" in line for line in lines), f"*.md 丢失：{lines}")

    def test_kinsoku_shori_no_closing_punctuation_at_line_start(self) -> None:
        """★ **避头尾法则：行首绝不得出现句末标点符号**（CHANGE-053 / D177）。

        实测报障：90 宽下上一行停在“旧代码”，下一行开头单独出现 `，需 /status...`。
        标点必须连同前置文字一同换行，保证行首无孤立标点。
        """
        text = (
            "注意预览用的是磁盘新代码而运行中的进程仍是旧代码，"
            "需 /status 确认并重启才一致。"
        )
        # 挑选若干可能在逗号附近截断的临界宽度
        for width in (20, 25, 30, 35, 40, 50, 60, 70, 80, 90, 100):
            with self.subTest(width=width):
                lines = wrap_cells(text, width).split("\n")
                for i, line in enumerate(lines):
                    self.assertLessEqual(cell_len(line), width)
                    if not line:
                        continue
                    # 行首不得出现句末标点（中文逗号、句号、分号等）
                    self.assertNotIn(
                        line[0], "，。！？；：、）】》,.!?;:]}",
                        f"宽 {width} 第 {i} 行为标点开篇：{line!r}（全量={lines}）",
                    )


    def test_balancing_never_loses_or_duplicates_text(self) -> None:
        """均衡是**重新分配**，不是增删 —— 折出来的内容必须与原文一致。

        ⚠️ 这条是本项目**血淋淋的教训**：折行器第一版"折完再拼回去重折"，
        用 `" ".join(rest.split(" ")[1:])` 把整段正文丢掉了
        （实测 157 字只剩 25 字）。均衡同样在动行的划分，所以必须守这一条。
        """
        samples = [
            "前者要改文档，后者要改行为 —— 而区分它们只需一次阅读。我这次跳过了那一步。",
            "a b c d e f g h i j k l m n o p q r s t u v w x y z 0 1 2 3 4 5 6 7 8 9",
            "中文没有空格所以逐字可断，而 English identifiers 要整体保留不切开。",
        ]
        for width in (30, 47, 78):
            for sample in samples:
                with self.subTest(width=width, head=sample[:16]):
                    lines = wrap_cells(sample, width).split("\n")
                    for line in lines:
                        self.assertLessEqual(cell_len(line), width, f"超宽：{line!r}")
                    # 去掉折行引入的空白后必须逐字相等（中文不补空格，英文补回原空格）
                    joined = "".join(lines)
                    self.assertEqual(
                        "".join(joined.split()), "".join(sample.split()),
                        "折行不得丢字或重复",
                    )

    def test_interline_code_is_not_split(self) -> None:
        """★ 行内代码 `` `/status` `` **不得被拆开**（CHANGE-052）。

        实测：摘要被折成 ``…但很可能未生效——`/`` + ``status` 的「代码」行…`` ——
        反引号与标识符分居两行，比切开单词更难看（它是 markdown 语法）。
        修法：回退时把反引号也一起吞进下一个 token。
        """
        text = "查明契约确实改过（长度口径，非位置）但很可能未生效——`/status` 的「代码」行有内建过期警告"
        for width in (30, 40, 60):
            with self.subTest(width=width):
                lines = wrap_cells(text, width).split("\n")
                for line in lines[:-1]:
                    self.assertFalse(
                        line.endswith("`/"),
                        f"反引号与斜杠被留在行尾：{line!r} → {lines}",
                    )
                joined = "".join(lines)
                self.assertEqual("".join(joined.split()), "".join(text.split()))

    def test_lines_never_end_with_a_space(self) -> None:
        """折行处残留行尾空格 → 该行看着比别的行短一格，复制出来还带尾随空白。

        这是第二轮报障的**直接成因**（第一版把空格先拼上、再判断要不要换行）。
        """
        text = ("word " * 40) + "\n"
        for width in (20, 33, 47, 66):
            with self.subTest(width=width):
                out = wrap_cells(text, width)
                for line in out.split("\n"):
                    self.assertEqual(line, line.rstrip(), f"行尾有空白：{line!r}")

    def test_chinese_reflow_does_not_inject_spaces(self) -> None:
        """中文之间补空格会满屏空隙 —— 拼接要按**相邻字符是否 CJK** 决定。"""
        text = "这一段中文在中间被模型断开，\n重排之后不该出现空格。\n"
        out = wrap_cells(text, 80)
        self.assertEqual(out, "这一段中文在中间被模型断开，重排之后不该出现空格。")

    def test_english_reflow_keeps_the_space(self) -> None:
        """英文换行处原本是空格，合并时必须补回来，否则 ``importantone``。"""
        self.assertEqual(wrap_cells("important\none", 80), "important one")

    def test_paragraph_breaks_survive(self) -> None:
        out = wrap_cells("first para\nstill first\n\nsecond para\n", 80)
        self.assertEqual(out, "first para still first\n\nsecond para")

    def test_single_newline_does_not_split_a_paragraph(self) -> None:
        """**关键**：模型每写完一句就换行，单换行**不能**当成段落边界。

        否则会出现"开头一句独占一行、没有填满"的怪现象——
        这是在真 app 的屏幕上实测抓到的（第一版按 ``\\n\\n`` 切块，
        第一句后面没有空行时就被切成了独立块）。
        """
        text = (
            "I looked at the file and here is what I found.\n"
            "The configuration lives in the project directory,\n"
            "and it has three sections where the second one matters.\n"
            "\n"
            "Steps to change it:\n"
            "1. Open the file\n"
        )
        lines = wrap_cells(text, 74).split("\n")
        self.assertGreater(cell_len(lines[0]), 60, f"第一行没填满，说明段落被切碎了：{lines[0]!r}")
        self.assertIn("", lines, "段落之间的空行必须保留")

    def test_blank_line_run_is_collapsed(self) -> None:
        """连续多个空行压成一个，避免屏幕上出现大段空白。"""
        self.assertEqual(wrap_cells("a\n\n\n\nb", 40), "a\n\nb")

    def test_single_space_indent_is_not_structure(self) -> None:
        """一个空格**不算**"结构化"（阈值是 2 cell），但作为段落首行缩进会保留。"""
        self.assertFalse(_is_structured(" one space"))
        self.assertTrue(_is_structured("  two spaces"))
        self.assertEqual(wrap_cells("a\n\n b", 40), "a\n\n b")

    # -- 结构化内容：宁可少合并，也不能搅坏 --------------------------------- #

    def test_list_items_are_not_merged_into_each_other(self) -> None:
        """列表项一旦被合并，屏幕上会出现 ``1. Open the file2. Change ...``。"""
        text = "1. Open the file\n2. Change the value\n3. Restart\n"
        self.assertEqual(wrap_cells(text, 80), "1. Open the file\n2. Change the value\n3. Restart")

    def test_bullet_items_are_not_merged(self) -> None:
        text = "- 第一项\n- 第二项\n- 第三项\n"
        self.assertEqual(wrap_cells(text, 80), "- 第一项\n- 第二项\n- 第三项")

    def test_code_block_keeps_its_line_breaks(self) -> None:
        """缩进行 = 代码，**不参与重排**（否则代码会被拼成一行）。"""
        text = "    def f(x):\n        return x + 1\n"
        self.assertEqual(wrap_cells(text, 80), "    def f(x):\n        return x + 1")

    def test_heading_and_fence_are_kept(self) -> None:
        text = "# 标题\n\n```python\nx = 1\n```\n"
        out = wrap_cells(text, 80)
        self.assertIn("# 标题", out)
        self.assertIn("```python", out)

    # -- 悬挂缩进：换行后要对齐到正文 --------------------------------------- #

    def test_long_list_item_hangs_under_its_text(self) -> None:
        """列表项换行后顶到最左边，看起来像另起了一段 —— 必须悬挂缩进。"""
        text = "- " + "很长的内容" * 12 + "\n"
        lines = wrap_cells(text, 40).split("\n")
        self.assertGreater(len(lines), 1)
        self.assertTrue(lines[0].startswith("- "), lines[0])
        for line in lines[1:]:
            self.assertTrue(line.startswith("  "), f"续行没有悬挂缩进：{line!r}")
            self.assertFalse(line.startswith("- "), f"续行重复了列表标记：{line!r}")

    def test_ordered_list_hangs_under_its_text(self) -> None:
        text = "10. " + "abcdefghij " * 12 + "\n"
        lines = wrap_cells(text, 40).split("\n")
        self.assertTrue(lines[0].startswith("10. "))
        for line in lines[1:]:
            self.assertTrue(line.startswith("    "), f"续行缩进应为 4：{line!r}")

    # -- 不重排模式：用户自己敲的换行 --------------------------- #

    def test_reflow_disabled_keeps_user_line_breaks(self) -> None:
        """``/model`` 之类用户输入的换行是**他有意打的**，重排会篡改原话。"""
        text = "line one\nline two\n"
        self.assertEqual(wrap_cells(text, 80, reflow=False), "line one\nline two")
        self.assertEqual(wrap_cells(text, 80), "line one line two")

    def test_reflow_disabled_still_enforces_width(self) -> None:
        out = wrap_cells("中文" * 30, 20, reflow=False)
        for line in out.split("\n"):
            self.assertLessEqual(cell_len(line), 20)

    # -- 极端输入 ----------------------------------------------------------- #

    def test_oversized_token_is_hard_split(self) -> None:
        """无空格的长串（base64、长 URL）必须硬切，否则整行超宽。"""
        out = wrap_cells("x" * 200, 30)
        for line in out.split("\n"):
            self.assertLessEqual(cell_len(line), 30)

    def test_empty_input(self) -> None:
        self.assertEqual(wrap_cells("", 40), "")

    def test_blank_lines_only(self) -> None:
        self.assertEqual(wrap_cells("\n\n", 40).strip(), "")

    # -- 丢字 / 重复字：本文件最该守住的底线 ------------------------------- #

    def test_never_loses_or_duplicates_characters(self) -> None:
        """★ **底线回归**：折行只能改换行位置，**不能增删任何非空白字符**。

        这条用例的来历值得记：这里前后写过三版折行实现，前两版都在"当前行里
        已经有什么"这件事上记错账，症状是**丢字**与**重复字**交替出现，
        而且只在特定的"文本 × 宽度"组合下复现——手写用例全部漏过。
        用户看到的就是"**话说到一半就断了**"。

        现在用固定种子的随机文本 × 多档宽度来守它：这是能自动化的部分，
        比"盯着屏幕看"可靠得多。
        """
        import random

        def non_space(value: str) -> str:
            return "".join(char for char in value if not char.isspace())

        alphabet = "这是一个测试中文句子，包含标点。还有English words and numbers 123，以及**md**标记。"
        rng = random.Random(20260915)
        for _ in range(120):
            text = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 300)))
            for width in (20, 33, 50, 72, 94):
                with self.subTest(width=width, text=text[:24]):
                    out = wrap_cells(text, width)
                    self.assertEqual(
                        non_space(out),
                        non_space(text),
                        "折行后字符序列变了（丢字或重复字）",
                    )
                    for line in out.split("\n"):
                        self.assertLessEqual(cell_len(line), width, f"超宽：{line!r}")

    def test_break_prefers_punctuation(self) -> None:
        """优先断在标点之后——中文硬断会把词组劈开（用户报的"错误的断句"）。"""
        text = "这是一个很长的句子，它包含逗号，所以应该断在逗号后面而不是随便一个位置。"
        lines = wrap_cells(text, 24).split("\n")
        self.assertGreater(len(lines), 1)
        # 第一行应当以标点结尾，而不是断在一个词中间
        self.assertIn(lines[0][-1], "，。！？；：、）", f"首行没有断在标点处：{lines[0]!r}")

    def test_long_latin_word_is_split_not_dropped(self) -> None:
        """超长无空格 token 必须**硬切**（不是丢掉）——base64、长 URL 就是这种。"""
        text = "prefix " + "x" * 200 + " suffix"
        out = wrap_cells(text, 30)
        self.assertEqual(
            "".join(char for char in out if not char.isspace()),
            "".join(char for char in text if not char.isspace()),
        )
        for line in out.split("\n"):
            self.assertLessEqual(cell_len(line), 30)


class DurationTests(unittest.TestCase):
    def test_short_durations_keep_one_decimal(self) -> None:
        self.assertEqual(format_duration(0), "0.0s")
        self.assertEqual(format_duration(400), "0.4s")
        self.assertEqual(format_duration(9_900), "9.9s")

    def test_ten_seconds_drops_decimal(self) -> None:
        """≥10 秒后小数对判断"是否卡住"没有帮助，去掉以减少状态栏抖动。"""
        self.assertEqual(format_duration(12_300), "12s")

    def test_minutes_and_hours(self) -> None:
        self.assertEqual(format_duration(63_000), "1m03s")
        self.assertEqual(format_duration(3_723_000), "1h02m")

    def test_none_is_em_dash(self) -> None:
        self.assertEqual(format_duration(None), EMPTY)

    def test_clock_format(self) -> None:
        self.assertEqual(format_clock(754_000), "12:34")
        self.assertEqual(format_clock(3_723_000), "1:02:03")
        self.assertEqual(format_clock(None), EMPTY)


class NumberFormatTests(unittest.TestCase):
    def test_tokens(self) -> None:
        self.assertEqual(format_tokens(999), "999")
        self.assertEqual(format_tokens(1_200), "1.2k")
        self.assertEqual(format_tokens(12_400), "12.4k")
        self.assertEqual(format_tokens(1_200_000), "1.2M")
        self.assertEqual(format_tokens(None), EMPTY)

    def test_cost_keeps_precision_for_tiny_amounts(self) -> None:
        """$0.0031 若按 2 位显示会变成 $0.00，看起来像免费——必须保留 4 位。"""
        self.assertEqual(format_cost(0.0031), "$0.0031")
        self.assertEqual(format_cost(0.42), "$0.420")
        self.assertEqual(format_cost(3.5), "$3.50")
        self.assertEqual(format_cost(0), "$0")
        self.assertEqual(format_cost(None), EMPTY)

    def test_ratio_distinguishes_none_from_zero(self) -> None:
        """D39：未上报（None）与真的 0% 必须显示不同。"""
        self.assertEqual(format_ratio(None), EMPTY)
        self.assertEqual(format_ratio(0.0), "0%")
        self.assertEqual(format_ratio(0.4237), "42%")

    def test_bytes(self) -> None:
        self.assertEqual(format_bytes(512), "512 B")
        self.assertEqual(format_bytes(1536), "1.5 KB")
        self.assertEqual(format_bytes(None), EMPTY)


class DiffBadgeTests(unittest.TestCase):
    """D40：五类徽标逐个覆盖。"""

    def test_modify(self) -> None:
        stat = ChangeStat(kind="modify", added=8, removed=3)
        self.assertEqual(diff_badge(stat), "diff +8 -3")

    def test_new(self) -> None:
        stat = ChangeStat(kind="new", added=42, removed=0)
        self.assertEqual(diff_badge(stat), "diff +42 -0  (new)")

    def test_rewrite(self) -> None:
        stat = ChangeStat(kind="rewrite", added=120, removed=118)
        self.assertEqual(diff_badge(stat), "diff +120 -118  (rewrite)")

    def test_large(self) -> None:
        stat = ChangeStat(kind="large", added=312, removed=48)
        self.assertEqual(diff_badge(stat), "diff +312 -48  (large)")

    def test_binary_shows_size_change(self) -> None:
        stat = ChangeStat(kind="binary", added=0, removed=0, bytes_before=1229, bytes_after=1434)
        self.assertEqual(diff_badge(stat), "binary changed  1.2 KB → 1.4 KB")

    def test_no_stat_is_em_dash_not_zero_diff(self) -> None:
        """没有变更统计时**不能**显示 ``diff +0 -0``——那会让人以为改了东西。"""
        self.assertEqual(diff_badge(None), EMPTY)


if __name__ == "__main__":
    unittest.main()
