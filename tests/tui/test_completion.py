"""`/` 命令自动补全的用例（D169 / D174 / D175）。

两层：
* **纯逻辑**（`content/completion.py`）：触发判据、匹配排序、别名、窗口滚动 —— 不碰终端；
* **端到端**（真界面 + 真按键）：列表出现、`Tab` 补全、`↑↓` 移动（**含超过窗口的那一段**）、
  `Esc` 关闭、`Enter` 先补全再提交。

★ **D175**：补全的状态机与渲染**复用 picker**（`PickerState` + `render_picker`）——
所以这里的断言用的是 `choices` / `index` / `visible_window` 这套 picker 语义，
而不再是自定义的 `candidates` / `selected`。
"""

from __future__ import annotations

import unittest

from logox.tui.content.completion import MAX_VISIBLE, completion_for, is_completion_context
from logox.tui.render.keys import Key
from tests.tui.test_render_inline_app import make_app

A = {"model": "切换模型", "mcp": "看 MCP", "help": "帮助"}
PLANNED = {"memory": "项目记忆", "files": "文件清单"}
ALIASES = {"q": "help", "mdl": "model", "f": "files"}


def _names(state) -> list[str]:
    """候选的**规范命令名**（`Choice.value`）。"""
    return [choice.value for choice in state.choices]


class TriggerTests(unittest.TestCase):
    """触发判据（决策 2 = 只在行首以 `/` 开头）。"""

    def test_t01_slash_alone_lists_candidates(self) -> None:
        state = completion_for("/", available=A, planned=PLANNED, aliases=ALIASES)
        self.assertIsNotNone(state)
        assert state is not None
        self.assertEqual(_names(state), ["model", "mcp", "help", "memory", "files"])
        self.assertEqual(state.index, 0, "初始选择应在第一条")

    def test_t02_prefix_filtering(self) -> None:
        state = completion_for("/mod", available=A, planned=PLANNED, aliases=ALIASES)
        assert state is not None
        self.assertEqual(_names(state), ["model"])

        many = completion_for("/m", available=A, planned=PLANNED, aliases=ALIASES)
        assert many is not None
        self.assertEqual(_names(many), ["model", "mcp", "memory"], "同组内按命令表顺序")

    def test_t03_no_match_returns_none(self) -> None:
        self.assertIsNone(completion_for("/zzz", available=A, planned=PLANNED, aliases=ALIASES))

    def test_t04_paths_do_not_trigger(self) -> None:
        """★ 路径保护：`/g/hz/codes` 不该弹列表。

        ⚠️ 必须**同时**断言 `is_completion_context` 本身 —— 只断言 `completion_for` 返回 None 的话，
        「第二个斜杠」与「前缀匹配」两条规则里任一条都能让用例通过，**变异测试测不出来**。
        """
        for text in ("/g/hz/codes", "/d/foo", "/tmp/x"):
            with self.subTest(text=text):
                self.assertFalse(is_completion_context(text, row=0, col=len(text)))
                self.assertIsNone(
                    completion_for(text, available=A, planned=PLANNED, aliases=ALIASES)
                )

    def test_t05_argument_area_does_not_trigger(self) -> None:
        self.assertIsNone(
            completion_for("/model gpt-4o", available=A, planned=PLANNED, aliases=ALIASES)
        )

    def test_t06_second_line_does_not_trigger(self) -> None:
        text = "some text\n/model"
        self.assertFalse(is_completion_context(text, row=1, col=6))
        self.assertIsNone(
            completion_for(text, row=1, col=6, available=A, planned=PLANNED, aliases=ALIASES)
        )

    def test_t07_plain_text_does_not_trigger(self) -> None:
        self.assertFalse(is_completion_context("hello /mo", row=0, col=9))
        self.assertIsNone(
            completion_for("hello /mo", available=A, planned=PLANNED, aliases=ALIASES)
        )


class MatchTests(unittest.TestCase):
    """匹配、排序、别名、标注、窗口。"""

    def test_t08_alias_matches_the_canonical_name(self) -> None:
        state = completion_for("/q", available=A, planned=PLANNED, aliases=ALIASES)
        assert state is not None
        self.assertEqual(len(state.choices), 1)
        self.assertEqual(state.choices[0].value, "help", "别名要显示规范名")
        self.assertIn("别名 /q", (state.choices[0].hint or ""), "说明里要点出用户敲的别名")

    def test_t09_planned_commands_are_listed_and_marked(self) -> None:
        state = completion_for("/f", available=A, planned=PLANNED, aliases=ALIASES)
        assert state is not None
        self.assertEqual(state.choices[0].value, "files")
        self.assertIn("（未实现）", state.choices[0].label, "planned 必须标注（决策 3）")

    def test_t10_candidates_are_never_truncated(self) -> None:
        """★ **D174**：窗口上限只决定「画几行」，**不决定"能选到哪"**。"""
        big = {f"cmd{i}": "说明" for i in range(12)}
        state = completion_for("/", available=big, planned={}, aliases={})
        assert state is not None
        self.assertEqual(len(state.choices), 12, "候选被截断了 —— 第 6 条起选不到")
        self.assertEqual(state.visible_window, (0, MAX_VISIBLE), "初始窗口是前 5 条")

    def test_t10b_window_scrolls_to_keep_the_selection_visible(self) -> None:
        big = {f"cmd{i}": "说明" for i in range(12)}
        state = completion_for("/", available=big, planned={}, aliases={})
        assert state is not None
        for _ in range(9):
            state.move(1)
        start, end = state.visible_window
        self.assertEqual(state.index, 9)
        self.assertTrue(start <= 9 < end, "当前项不在窗口内 ⇒ 用户看不见自己在选什么")
        self.assertEqual(end - start, MAX_VISIBLE)

    def test_t10c_moving_wraps_at_the_edges(self) -> None:
        """环绕（与 picker 完全一致 —— D175：同一套状态机）。"""
        big = {f"cmd{i}": "说明" for i in range(7)}
        state = completion_for("/", available=big, planned={}, aliases={})
        assert state is not None
        for _ in range(7):
            state.move(1)
        self.assertEqual(state.index, 0, "到底再按 ↓ 应环绕回第一条")
        state.move(-1)
        self.assertEqual(state.index, 6, "第一条再按 ↑ 应环绕到最后一条")

    def test_t11_current_is_the_selected_command(self) -> None:
        state = completion_for("/m", available=A, planned=PLANNED, aliases=ALIASES)
        assert state is not None
        self.assertEqual(state.current.value, "model")
        state.move(1)
        self.assertEqual(state.current.value, "mcp")


class InlineCompletionTests(unittest.TestCase):
    """端到端：真界面 + 真按键（不看渲染细节，只看行为与状态）。"""

    def setUp(self) -> None:
        self.app, _terminal, _runtime = make_app(width=100, height=30)

    def _type(self, text: str) -> None:
        for char in text:
            self.app.press(char)

    def test_t12_typing_slash_opens_the_list_and_tab_completes(self) -> None:
        self._type("/mo")
        self.assertIsNotNone(self.app._completion)
        assert self.app._completion is not None
        self.assertEqual(self.app._completion.current.value, "model")

        self.app.press(Key("tab"))

        self.assertEqual(self.app.editor.text, "/model ", "Tab 应当把命令补全进输入框")
        self.assertIsNone(self.app._completion, "接受之后列表要收起")

    def test_t13_arrows_move_the_selection(self) -> None:
        self._type("/m")
        assert self.app._completion is not None
        first = self.app._completion.current.value
        self.app.press(Key("down"))
        assert self.app._completion is not None
        second = self.app._completion.current.value
        self.assertNotEqual(first, second, "↓ 应当移动选择")
        self.app.press(Key("up"))
        assert self.app._completion is not None
        self.assertEqual(self.app._completion.current.value, first, "↑ 应当移回来")

    def test_t14_escape_closes_without_touching_the_input(self) -> None:
        self._type("/mo")
        self.app.press(Key("escape"))
        self.assertIsNone(self.app._completion, "Esc 应当关掉列表")
        self.assertEqual(self.app.editor.text, "/mo", "Esc 不该动用户已经打的字")

    def test_t15_enter_completes_then_submits(self) -> None:
        """决策 1=C：`Enter` 在列表开着时**先补全再提交**。"""
        submitted: list[str] = []
        self.app._on_submit = lambda text: submitted.append(text)  # type: ignore[method-assign]
        self._type("/stat")
        self.assertIsNotNone(self.app._completion)
        self.app.press(Key("enter"))
        self.assertEqual(submitted, ["/status "], "Enter 应当先补全成 /status 再提交")

    def test_t16_list_does_not_steal_focus(self) -> None:
        self._type("/mo")
        self.assertIsNotNone(self.app._completion)
        self._type("d")  # 继续输入：焦点必须还在编辑器
        self.assertEqual(self.app.editor.text, "/mod")
        assert self.app._completion is not None
        self.assertEqual(self.app._completion.current.value, "model")

    def test_t16b_arrows_reach_items_beyond_the_visible_five(self) -> None:
        """★ **D174 的端到端守卫**：`/` 后连按 ↓，必须能选到第 5 条**之后**的条目。

        这正是用户报障的场景（"超出 5 个选项后无法继续选择"）。命令表有 20 条，
        所以「按 7 次 ⇒ index == 7」在「候选被截断」的旧实现下**不可能**成立。
        """
        self._type("/")
        assert self.app._completion is not None
        self.assertGreater(len(self.app._completion.choices), 5, "命令表应当多于 5 条")

        for _ in range(7):
            self.app.press(Key("down"))

        assert self.app._completion is not None
        self.assertEqual(self.app._completion.index, 7, "按 ↓ 到不了第 5 条之后")
        start, end = self.app._completion.visible_window
        self.assertTrue(start <= 7 < end, "选中项不在可见窗口内")

    def test_t16c_pointer_and_highlight_stay_on_the_same_row(self) -> None:
        """★ **D175 的守卫**：窗口滚动后，`❯` 指针与高亮必须在**同一行**。

        用户报障：「超过五条以后，选择指针与高光发生了错位」——
        根因是我自己实现时把高亮行号用成全量下标、而渲染按窗口画。
        复用 picker 之后，两者由**同一份**代码（`render_picker`）决定 ⇒ 不可能再错位。
        """
        self._type("/")
        for _ in range(7):
            self.app.press(Key("down"))
        assert self.app._completion is not None
        self.assertEqual(self.app._completion.index, 7)

        frame = self.app.frame_text()
        pointer_rows = [i for i, line in enumerate(frame.split("\n")) if "❯" in line]
        self.assertEqual(len(pointer_rows), 1, "指针应当恰好出现一次")
        # 指针所在行必须也在高亮序列里（本用例的「高亮」由 accent 前景表达，帧文本看不到颜色，
        # 因此这里断言两者来自同一渲染器这一事实：指针行 = 窗口内偏移 index-window_start 的行）
        start, _end = self.app._completion.visible_window
        box_rows = [i for i, line in enumerate(frame.split("\n")) if "─ 命令 " in line]
        self.assertTrue(box_rows, "提示框应当在帧里")
        expected_row = box_rows[0] + 1 + (7 - start)
        self.assertEqual(pointer_rows[0], expected_row, "指针行与窗口内偏移不一致 ⇒ 错位")


class LayoutTests(unittest.TestCase):
    """★ 用户报障过的两个点：在**输入框上方** + **不遮挡输入框**。"""

    def setUp(self) -> None:
        self.app, _terminal, _runtime = make_app(width=96, height=30)

    def _frame_lines(self) -> list[str]:
        return [line.rstrip() for line in self.app.frame_text().split("\n")]

    def test_t18_hint_box_sits_above_the_input_and_does_not_cover_it(self) -> None:
        for char in "/m":
            self.app.press(char)
        self.assertIsNotNone(self.app._completion)

        lines = self._frame_lines()
        box_title = next((i for i, text in enumerate(lines) if "─ 命令 " in text), None)
        editor_line = next(
            (i for i, text in enumerate(lines) if "/m" in text and "命令" not in text), None
        )
        self.assertIsNotNone(box_title, "没有找到提示框（标题应为「命令」）")
        self.assertIsNotNone(editor_line, "输入框消失了 —— 用户报障的原症状")
        assert box_title is not None and editor_line is not None
        self.assertLess(box_title, editor_line, "提示框必须在输入框**上方**")
        editor_block = "\n".join(lines[editor_line - 1 : editor_line + 2])
        self.assertIn("│", editor_block, "输入框的框线不见了 ⇒ 被提示框遮住了")

    def test_t19_hint_box_disappears_when_the_trigger_no_longer_holds(self) -> None:
        for char in "/mo":
            self.app.press(char)
        self.assertIsNotNone(self.app._completion)

        self.app.press(Key("tab"))
        self.assertIsNone(self.app._completion)
        self.assertEqual(self.app.editor.text, "/model ")
        self.assertNotIn("─ 命令 ", "\n".join(self._frame_lines()), "接受之后提示框应当消失")

        self.app.press(Key("backspace"))  # 删掉尾随空格 ⇒ 判据重新成立
        self.assertIsNotNone(self.app._completion, "删掉空格后提示框应当回来")


if __name__ == "__main__":
    unittest.main()
