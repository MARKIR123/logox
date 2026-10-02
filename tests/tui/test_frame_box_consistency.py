"""提示框（圆角框）的跨组件一致性守卫（D170 / D171）。

用户报障过三件事，这里逐条钉住：

1. **边框颜色**：提示框要与**输入框**同色（`input_border`），且**四边（含页脚）都一致**
   —— 最初是页脚用更暗的 `border_subtle`，改完头/身后页脚漏改又回退过一次（被探针当场抓到）。
2. **选中高亮**：选中行用主题的 `selected_bg` 刷底（用户裁定「提示框统一这样设计」），
   而且**恰好只有那一行** —— 用户原话是「这个高亮色会**溢出到下一个选项**」。
3. **跨组件一致**：`/` 补全、`/model`（picker）、确认框用的是同一套高亮与边框。

技术要点（为什么不会溢出）：`frame_box` 把背景样式**只加在内边距那一行**上，
**换行符在样式区间之外** —— 于是"溢出"在机制上不可能发生，而不是靠调坐标。
"""

from __future__ import annotations

import unittest

from logox.tui.content.completion import completion_for, render_completion
from logox.tui.content.overlay import Choice, PickerState, render_confirm, render_picker
from logox.tui.render.ansi import text_to_ansi
from tests.tui.test_render_inline_app import make_app

ACCENT_SEQ = ""  # 运行时按主题算出（见 _seq）
BG_SEQ = "48;2;"  # 真彩色背景 —— **不该出现**（D81：不用背景色）


def _seq(color: str) -> str:
    """十六进制色值 → ANSI 真彩前景前缀（`38;2;r;g;b`）。"""
    value = str(color).lstrip("#")
    r, g, b = (int(value[i : i + 2], 16) for i in (0, 2, 4))
    return f"38;2;{r};{g};{b}"


def _border_seq(palette) -> str:
    """输入框边框色（提示框要用的那个）。"""
    return _seq(palette.input_border)


def _accent_seq(palette) -> str:
    """选中行的强调色（`bold accent`）。"""
    return _seq(palette.accent)


def _rows(box) -> list:
    return list(box.split("\n", include_separator=False, allow_blank=True))


def _ansi_by_row(box) -> list[str]:
    return [text_to_ansi(row) for row in _rows(box)]


class FrameBoxConsistencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app, _terminal, _runtime = make_app(width=90, height=24)
        self.palette = self.app.theme.palette

    def _picker(self, *, index: int = 0):
        choices = [
            Choice(value="1", label="[1] 轮次 3 · 2 个文件", hint="改了解析逻辑"),
            Choice(value="2", label="[2] 轮次 2 · 纯对话", hint="只讨论了方案"),
            Choice(value="3", label="[3] 轮次 1 · 1 个文件", hint="初次实现"),
        ]
        state = PickerState(
            title="时空穿梭检查点回滚", choices=list(choices), index=index, all_choices=list(choices)
        )
        box, _rows_map = render_picker(state, self.palette, width=90)
        return box

    # ---------------------------------------------------------------- 边框色
    def test_t01_all_four_sides_use_the_input_box_color(self) -> None:
        """★ 四边（含**页脚**）都用 `input_border` —— 页脚漏改过一次，这条就是为它写的。

        ⚠️ 例外：**选中行**的左右竖线用 accent（整行一起强调，视觉上才是一条完整的选择条）。
        """
        expected = _border_seq(self.palette)
        ansi_rows = _ansi_by_row(self._picker(index=1))
        for index in (0, 1, len(ansi_rows) - 1):  # 上框线、非选中正文行、下框线
            with self.subTest(row=index):
                self.assertIn(
                    expected, ansi_rows[index], "边框没有用输入框的颜色（input_border）"
                )

    def test_t01b_no_background_color_anywhere(self) -> None:
        """D81：**不用任何背景色** —— 选中强调走前景（accent），不走底色。"""
        for row in _ansi_by_row(self._picker()):
            self.assertNotIn("48;2;", row, "提示框里出现了背景色（D81 不允许）")

    # ---------------------------------------------------------------- 高亮不溢出
    def test_t02_selected_row_highlight_does_not_leak(self) -> None:
        """★ 高亮恰好只落在选中那一行（用户报障：会溢出到下一个选项）。"""
        for index in (0, 1, 2):
            with self.subTest(index=index):
                ansi_rows = _ansi_by_row(self._picker(index=index))
                highlighted = [i for i, row in enumerate(ansi_rows) if _accent_seq(self.palette) in row]
                self.assertEqual(
                    highlighted,
                    [1 + index],
                    "高亮的行不对（1 = 第一条正文；上框线是第 0 行）",
                )

    def test_t03_highlight_only_covers_one_row_even_at_the_last_item(self) -> None:
        """最后一项选中时也不能"多出一行"（边界）。"""
        ansi_rows = _ansi_by_row(self._picker(index=2))
        self.assertEqual(sum(_accent_seq(self.palette) in row for row in ansi_rows), 1)

    # ---------------------------------------------------------------- 跨组件一致
    def test_t04_completion_uses_the_same_highlight_and_border(self) -> None:
        lines = render_completion(completion_for("/m"), self.palette, width=90)
        from rich.text import Text

        box = Text("\n").join([line if isinstance(line, Text) else Text(str(line)) for line in lines])
        ansi_rows = _ansi_by_row(box)
        highlighted = [i for i, row in enumerate(ansi_rows) if _accent_seq(self.palette) in row]
        self.assertEqual(highlighted, [1], "补全的选中行强调位置不对")
        self.assertIn(_border_seq(self.palette), ansi_rows[0], "补全的边框色与输入框不一致")

    def test_t05_confirm_dialog_highlights_its_focused_option(self) -> None:
        box = render_confirm("确认", "是否继续？", self.palette, width=90)
        ansi_rows = _ansi_by_row(box)
        self.assertGreaterEqual(
            sum(_accent_seq(self.palette) in row for row in ansi_rows),
            1,
            "确认框的焦点项没有强调",
        )


if __name__ == "__main__":
    unittest.main()
