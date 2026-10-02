"""组件协议与基础组件的测试（D80 / MODULE_tui_render §8.3）。

这个文件守的是**那条唯一的硬约束**：``render(width)`` 的每一行都不超宽、且不丢内容。
超宽行在终端里会**折行**，导致其下所有行错位——症状是"整个界面花了"。

因此这里用**边界宽度扫描**（1 / 5 / 20 / 80 / 200）而不是挑几个宽度试试：
宽度算错往往只在极端值上暴露（1 格宽、或刚好差 1 格）。
"""

from __future__ import annotations

import unittest

from rich.cells import cell_len
from rich.text import Text

from logox.tui.render.ansi import visible_width
from logox.tui.render.component import Container, Spacer, fit_lines
from logox.tui.render.components.text import CURSOR_MARKER, Box, TextComponent, TruncatedText
from logox.tui.render.keys import Key

#: 边界宽度：极端窄 / 常见 / 很宽
WIDTHS = (1, 5, 20, 40, 80, 200)

#: 一段"什么都有"的文本：中文、英文、长单词、已有换行
SAMPLE = (
    "普通中文段落，包含 English words 与 123 数字。\n"
    "很长的无空格串：" + "x" * 60 + "\n"
    "多行长文本，用来触发折行与重排逻辑，长度超过一般终端的一半宽度。"
)


def non_space(value: str) -> str:
    return "".join(char for char in value if not char.isspace())


class FitLinesTests(unittest.TestCase):
    """`fit_lines` 是渲染器的兜底：超宽就硬切，**但绝不丢内容**。"""

    def test_wide_text_is_split_not_dropped(self) -> None:
        text = "a" * 100
        lines = fit_lines([Text(text)], 30)
        self.assertEqual("".join(line.plain for line in lines), text, "硬切丢了内容")
        for line in lines:
            self.assertLessEqual(cell_len(line.plain), 30)

    def test_already_fitting_text_is_untouched(self) -> None:
        lines = fit_lines([Text("短")], 30)
        self.assertEqual(lines[0].plain, "短")

    def test_zero_width_is_safe(self) -> None:
        self.assertEqual([line.plain for line in fit_lines([Text("abc")], 0)], [""])

    def test_styles_survive_splitting(self) -> None:
        """切行时样式必须跟着字符走 —— 否则高亮/着色会在断点处丢失。

        两种样式都要验：``spans``（局部样式）**和** ``style``（基样式）。
        只搬 spans 的话，``Text("x", style="on #282832")`` 这种"整行底色"
        会在切分后消失——而我们的内容分层全靠它。
        """
        spanned = Text("a" * 10)
        spanned.stylize("bold red", 0, 5)
        lines = fit_lines([spanned], 4)
        self.assertTrue(any(line.spans for line in lines), "spans 样式丢了")

        based = Text("b" * 10, style="on #282832")
        based_lines = fit_lines([based], 4)
        for line in based_lines:
            self.assertEqual(line.style, "on #282832", "基样式（整行底色）丢了")


class BoxTests(unittest.TestCase):
    """`Box` = 内边距 + 底色（Pi 的 ``Box``）。"""

    def test_padding_and_full_width(self) -> None:
        box = Box(padding_x=1, padding_y=0, background="#282832")
        box.add(TextComponent("你好"))
        # 从 3 起：padding 1×2 + 一个中文字符 2 格 = 4，宽度 < 4 时物理上放不下
        for width in (4, 5, 20, 40, 80, 200):
            with self.subTest(width=width):
                rows = box.render(width)
                for row in rows:
                    self.assertLessEqual(cell_len(row.plain), width, f"超宽：{row.plain!r}")
    def test_background_covers_the_whole_row(self) -> None:
        """底纹必须铺满整行（分区靠"面"，不能只盖到文字那么宽）。"""
        box = Box(padding_x=1, padding_y=0, background="#282832")
        box.add(TextComponent("hi"))
        rows = box.render(20)
        self.assertEqual(rows[0].style, "on #282832")
        self.assertEqual(cell_len(rows[0].plain), 20)

    def test_padding_y_adds_blank_rows(self) -> None:
        box = Box(padding_x=0, padding_y=1, background="#111111")
        box.add(TextComponent("x"))
        self.assertEqual(len(box.render(10)), 3, "上下各应有一个空行")


class TextComponentTests(unittest.TestCase):
    """`TextComponent` 折行 —— 复用 `format.wrap_cells` 的成果。"""

    def test_never_exceeds_width(self) -> None:
        """★ 硬约束：任何宽度下都不能超宽。

        ⚠️ 从 2 开始扫：宽度 1 时**一个中文字符（2 格）就放不下**，
        那是物理上不可能满足的（见 `component._split_hard` 的说明——
        我们把"宽度不变量"排在"保住每个字符"之前，与终端自身的处理方向一致）。
        """
        for width in (2, 5, 20, 40, 80, 200):
            with self.subTest(width=width):
                rows = TextComponent(SAMPLE).render(width)
                for row in rows:
                    self.assertLessEqual(cell_len(row.plain), width, f"超宽：{row.plain!r}")

    def test_never_loses_content(self) -> None:
        """★ 折行只能改换行位置。**丢内容是比超宽更严重的错**（D74 的教训）。"""
        for width in (20, 40, 80):
            with self.subTest(width=width):
                rows = TextComponent(SAMPLE).render(width)
                got = non_space("".join(row.plain for row in rows))
                self.assertEqual(got, non_space(SAMPLE), "折行丢了或重复了字符")

    def test_empty_text_renders_nothing(self) -> None:
        self.assertEqual(TextComponent("").render(40), [])

    def test_padding_x_is_applied(self) -> None:
        rows = TextComponent("hi", padding_x=2).render(20)
        self.assertTrue(rows[0].plain.startswith("  hi"))


class TruncatedTextTests(unittest.TestCase):
    """状态行用的单行文本：**可以**截断（它是摘要，不是正文）。"""

    def test_always_exactly_one_row(self) -> None:
        for width in WIDTHS:
            with self.subTest(width=width):
                rows = TruncatedText(SAMPLE.replace("\n", " ")).render(width)
                self.assertEqual(len(rows), 1, "状态行必须只占一行")
                self.assertLessEqual(cell_len(rows[0].plain), width)

    def test_short_text_is_not_truncated(self) -> None:
        self.assertEqual(TruncatedText("短").render(20)[0].plain.rstrip(), "短")


class ContainerTests(unittest.TestCase):
    """垂直堆叠。"""

    def test_stacks_children_in_order(self) -> None:
        container = Container()
        container.add(TextComponent("第一行"))
        container.add(TextComponent("第二行"))
        rows = container.render(40)
        self.assertEqual(rows[0].plain, "第一行")
        self.assertEqual(rows[1].plain, "第二行")

    def test_remove_and_clear(self) -> None:
        container = Container()
        child = TextComponent("x")
        container.add(child)
        container.remove(child)
        self.assertEqual(container.render(40), [])
        container.add(child)
        container.clear()
        self.assertEqual(container.render(40), [])

    def test_spacer_adds_blank_rows(self) -> None:
        container = Container()
        container.add(TextComponent("上"))
        container.add(Spacer(2))
        container.add(TextComponent("下"))
        rows = container.render(40)
        self.assertEqual([row.plain for row in rows], ["上", "", "", "下"])

    def test_input_goes_to_last_child_that_handles_it(self) -> None:
        """按键从后往前派发：输入区在下面，应当优先拿到按键。"""

        class Grabber:
            def __init__(self) -> None:
                self.seen: list[Key] = []

            def render(self, width: int) -> list[Text]:
                return []

            def handle_input(self, key: Key) -> bool:
                self.seen.append(key)
                return True

            def invalidate(self) -> None:
                return None

        container = Container()
        first, second = Grabber(), Grabber()
        container.add(first)
        container.add(second)
        self.assertTrue(container.handle_input(Key("a", char="a")))
        self.assertEqual(len(second.seen), 1, "应当由最后一个子组件处理")
        self.assertEqual(len(first.seen), 0)

    def test_invalidate_propagates_to_children(self) -> None:
        class Counter:
            def __init__(self) -> None:
                self.calls = 0

            def render(self, width: int) -> list[Text]:
                return []

            def handle_input(self, key: Key) -> bool:
                return False

            def invalidate(self) -> None:
                self.calls += 1

        container = Container()
        child = Counter()
        container.add(child)
        container.invalidate()
        self.assertEqual(child.calls, 1)


class CursorMarkerTests(unittest.TestCase):
    """IME 光标标记（§10 第 2 项：中文输入法候选窗定位）。"""

    def test_marker_is_zero_width(self) -> None:
        """★ 零宽是硬要求：它会被插进渲染输出，多占一格就会让整行错位。

        ⚠️ 必须用 `visible_width` 而不是 `cell_len`：后者按**原始字节**数，
        会把 APC 序列的 6–7 个字节也算成宽度——**这正是要防的那个错误**。
        """
        self.assertEqual(visible_width(CURSOR_MARKER), 0)

    def test_marker_is_an_apc_sequence(self) -> None:
        """APC 序列（``ESC _ ... ESC \\``）是终端规范里"应用自定义"的通道。"""
        self.assertTrue(CURSOR_MARKER.startswith("\x1b_"))
        self.assertTrue(CURSOR_MARKER.endswith("\x1b\\"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
