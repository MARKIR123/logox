"""浮层组件的测试（D80 §9 第 6 步）。

为什么这些组件值得单独测
========================

`/login`、`/model`、`/theme` 都靠它们。它们出错的方式**全都在用户面前**：

* 选项筛选失灵 → 用户以为"这个模型不存在"；
* 确认框默认焦点在"是" → 一次误按就把密钥写进磁盘；
* 输入框没屏蔽密钥 → 明文留在回滚缓冲、截图、屏幕共享里；
* 面板不滚动 → `/help` 在 24 行终端上被砍掉一截，"看不到的命令"比"排版难看"严重得多。

而且它们**完全可以同步测**：给一个 :class:`~logox.tui.render.keys.Key`，
检查渲染出来的行 —— 不需要终端、不需要事件循环、不需要 Textual。
"""

from __future__ import annotations

import unittest

from rich.text import Text

from logox.tui.content.overlay import Choice, PickerState
from logox.permission_types import PermissionAsk, PermissionChoice
from logox.tui.render.ansi import visible_width
from logox.tui.render.components.overlay import (
    ConfirmComponent,
    PanelComponent,
    PermissionComponent,
    PickerComponent,
    PromptComponent,
)
from logox.tui.render.components.text import CURSOR_MARKER
from logox.tui.render.keys import Key
from logox.tui.theme import load_theme

PALETTE = load_theme("logox-dark").palette
WIDTH = 60


def picker(*labels: str) -> PickerComponent:
    state = PickerState(
        title="选择",
        choices=[Choice(value=label, label=label) for label in labels],
        footer="Esc 取消",
    )
    return PickerComponent(state, PALETTE)


def plain(component: object, width: int = WIDTH) -> list[str]:
    return [row.plain for row in component.render(width)]  # type: ignore[attr-defined]


class WidthInvariantTests(unittest.TestCase):
    """★ 所有浮层在**任何**宽度下都不能超宽（超宽 → 终端折行 → 界面错位）。"""

    def _components(self) -> list[object]:
        long_text = Text("\n".join("内容" * 20 for _ in range(30)) + "\n")
        return [
            picker("alpha", "beta"),
            PromptComponent(
                title="密钥", label="KEY", palette=PALETTE, on_done=lambda _r: None
            ),
            ConfirmComponent(
                title="确认", question="要记住吗？", palette=PALETTE, on_done=lambda _r: None
            ),
            PanelComponent(long_text, palette=PALETTE, max_rows=6),
        ]

    def test_never_exceed_width(self) -> None:
        for width in (24, 40, 60, 72, 100):
            for component in self._components():
                with self.subTest(width=width, kind=type(component).__name__):
                    for row in component.render(width):  # type: ignore[attr-defined]
                        self.assertLessEqual(visible_width(row.plain), width)


class PickerTests(unittest.TestCase):
    def test_arrow_keys_move(self) -> None:
        component = picker("a", "b", "c")
        component.handle_input(Key("down"))
        self.assertEqual(component.state.current.label, "b")  # type: ignore[union-attr]
        component.handle_input(Key("up"))
        self.assertEqual(component.state.current.label, "a")  # type: ignore[union-attr]

    def test_enter_returns_the_selected_choice(self) -> None:
        result: list[object] = []
        component = PickerComponent(
            PickerState(choices=[Choice(value="a", label="a"), Choice(value="b", label="b")]),
            PALETTE,
            on_done=result.append,
        )
        component.handle_input(Key("down"))
        component.handle_input(Key("enter"))
        self.assertEqual([choice.value for choice in result], ["b"])  # type: ignore[union-attr]

    def test_number_key_selects_directly(self) -> None:
        """数字直达：用户说"我要第三个"时不用按两次方向键。"""
        result: list[object] = []
        component = PickerComponent(
            PickerState(choices=[Choice(value=str(i), label=str(i)) for i in range(5)]),
            PALETTE,
            on_done=result.append,
        )
        component.handle_input(Key("3", char="3"))
        self.assertEqual([choice.value for choice in result], ["2"])  # type: ignore[union-attr]

    def test_typing_filters(self) -> None:
        """★ 打字即筛选（Pi 的手感）。这一条也顺带证明了 j/k **没有**被当成导航键。"""
        component = picker("deepseek-flash", "jamba-1.5", "gpt-4o")
        for char in "jam":
            component.handle_input(Key(char, char=char))
        self.assertEqual(component.state.query, "jam")
        self.assertEqual([c.label for c in component.state.choices], ["jamba-1.5"])

    def test_picker_occupies_full_terminal_width(self) -> None:
        """选择器提示框不使用自适应窄宽度，而是和输入框一样占据终端全宽（如 120 列）。"""
        component = picker("alpha", "beta")
        rows = component.render(120)
        self.assertGreater(len(rows), 0)
        self.assertEqual(visible_width(rows[0].plain), 120)
        self.assertEqual(visible_width(rows[-1].plain), 120)

    def test_escape_clears_the_filter_before_cancelling(self) -> None:
        """有筛选时 Esc 先清筛选——否则用户一按就把整个选择取消了。"""
        result: list[object] = []
        component = PickerComponent(
            PickerState(choices=[Choice(value="a", label="a")]), PALETTE, on_done=result.append
        )
        component.handle_input(Key("a", char="a"))
        component.handle_input(Key("escape"))
        self.assertEqual(result, [], "第一次 Esc 不该取消整个选择")
        self.assertEqual(component.state.query, "")
        component.handle_input(Key("escape"))
        self.assertEqual(result, [None])

    def test_disabled_items_are_skipped_and_not_selectable(self) -> None:
        result: list[object] = []
        component = PickerComponent(
            PickerState(
                choices=[Choice(value="a", label="a"), Choice(value="b", label="b", disabled=True)]
            ),
            PALETTE,
            on_done=result.append,
        )
        component.handle_input(Key("down"))
        self.assertEqual(component.state.index, 0, "光标不该停在禁用项上")
        component.handle_input(Key("1", char="1"))
        self.assertEqual([c.value for c in result], ["a"])
        component.handle_input(Key("2", char="2"))
        self.assertEqual(len(result), 1, "禁用项不该被选中")

    def test_finish_is_idempotent(self) -> None:
        """★ 一次按键可能被两条路径处理；重复回调会让 `/login` 跑两遍流程。"""
        result: list[object] = []
        component = PickerComponent(
            PickerState(choices=[Choice(value="a", label="a")]), PALETTE, on_done=result.append
        )
        component.handle_input(Key("enter"))
        component.handle_input(Key("enter"))
        self.assertEqual(len(result), 1)

    def test_picker_delete_with_ctrl_d_and_confirm(self) -> None:
        deleted: list[Choice] = []
        state = PickerState(
            choices=[Choice(value="sess_1", label="会话 1"), Choice(value="sess_2", label="会话 2")],
            allow_delete=True,
        )
        component = PickerComponent(
            state,
            PALETTE,
            on_delete=lambda c: (deleted.append(c) or True),
        )
        # 按 Ctrl+D 进入二次确认态
        component.handle_input(Key("d", ctrl=True))
        self.assertTrue(state.confirming_delete)
        rendered = "\n".join(plain(component))
        self.assertIn("移入回收站", rendered)

        # 按 n 取消确认
        component.handle_input(Key("n", char="n"))
        self.assertFalse(state.confirming_delete)
        self.assertEqual(len(deleted), 0)

        # 再次按 Ctrl+D 并按 y 确认删除
        component.handle_input(Key("d", ctrl=True))
        self.assertTrue(state.confirming_delete)
        component.handle_input(Key("y", char="y"))
        self.assertFalse(state.confirming_delete)
        self.assertEqual(len(deleted), 1)
        self.assertEqual(deleted[0].value, "sess_1")
        # 列表中应该只剩 sess_2
        self.assertEqual(len(state.choices), 1)
        self.assertEqual(state.choices[0].value, "sess_2")


class PromptTests(unittest.TestCase):
    def _prompt(self, **kwargs: object) -> tuple[PromptComponent, list[object]]:
        result: list[object] = []
        component = PromptComponent(
            title="密钥", label="KEY", palette=PALETTE, on_done=result.append, **kwargs
        )
        return component, result

    def test_typed_characters_are_masked(self) -> None:
        """★ 密钥**绝不能**明文出现在终端里（回滚缓冲 / 截图 / 屏幕共享都留得住）。"""
        component, _result = self._prompt()
        for char in "sk-secret":
            component.handle_input(Key(char, char=char))
        rendered = "\n".join(plain(component))
        self.assertNotIn("sk-secret", rendered)
        self.assertIn("•" * 9, rendered)

    def test_mask_can_be_turned_off(self) -> None:
        """非密钥字段（如自建端点 URL）要能看见内容。"""
        component, _result = self._prompt(mask=False)
        for char in "http://x":
            component.handle_input(Key(char, char=char))
        self.assertIn("http://x", "\n".join(plain(component)))

    def test_enter_returns_the_trimmed_value(self) -> None:
        component, result = self._prompt()
        for char in "abc":
            component.handle_input(Key(char, char=char))
        component.handle_input(Key("enter"))
        self.assertEqual(result, ["abc"])

    def test_paste_drops_newlines(self) -> None:
        """★ 粘贴的密钥常常带一个换行；它进了 `.env` 就变成坏密钥。

        症状是"鉴权失败"，离原因（多了一个换行）很远。
        """
        component, result = self._prompt()
        component.handle_input(Key("paste", char="sk-abc123\r\n"))
        component.handle_input(Key("enter"))
        self.assertEqual(result, ["sk-abc123"])

    def test_escape_cancels_with_none(self) -> None:
        component, result = self._prompt()
        component.handle_input(Key("escape"))
        self.assertEqual(result, [None])

    def test_cursor_marker_is_present_for_ime(self) -> None:
        """★ 中文输入法的候选窗跟硬件光标走 —— 标记必须在。"""
        component, _result = self._prompt()
        rendered = "".join(row.plain for row in component.render(WIDTH))
        self.assertIn(CURSOR_MARKER, rendered)

    def test_editing_keys_work(self) -> None:
        component, result = self._prompt(mask=False)
        for char in "abc":
            component.handle_input(Key(char, char=char))
        component.handle_input(Key("left"))
        component.handle_input(Key("backspace"))  # 光标在 c 前，删掉的是 b
        component.handle_input(Key("enter"))
        self.assertEqual(result, ["ac"])

    def test_typing_clears_a_previous_error(self) -> None:
        component, _result = self._prompt(error="密钥无效")
        component.handle_input(Key("a", char="a"))
        self.assertEqual(component.error, "")


class ConfirmTests(unittest.TestCase):
    def _confirm(self) -> tuple[ConfirmComponent, list[object]]:
        result: list[object] = []
        component = ConfirmComponent(
            title="记住这个密钥？",
            question="要写入文件吗？",
            palette=PALETTE,
            yes="记住",
            no="仅本次",
            on_done=result.append,
        )
        return component, result

    def test_default_focus_is_the_safe_option(self) -> None:
        """★ 默认焦点在"否"：误按一次 Enter 不该把密钥写进磁盘。"""
        component, result = self._confirm()
        self.assertEqual(component.focus_index, 1)
        component.handle_input(Key("enter"))
        self.assertEqual(result, [False])

    def test_arrow_keys_switch_the_focus(self) -> None:
        component, result = self._confirm()
        component.handle_input(Key("left"))
        component.handle_input(Key("enter"))
        self.assertEqual(result, [True])

    def test_y_and_n_shortcuts(self) -> None:
        component, result = self._confirm()
        component.handle_input(Key("y", char="y"))
        self.assertEqual(result, [True])
        component2, result2 = self._confirm()
        component2.handle_input(Key("n", char="n"))
        self.assertEqual(result2, [False])

    def test_escape_returns_none_not_false(self) -> None:
        """★ ``None``（取消）与 ``False``（明确选"否"）**语义不同**：

        `/login` 对这两者的处理不一样——取消是"别问了、密钥只放内存"，
        选"否"是"我明确不要写盘"。合并成一个值会让提示文案撒谎。
        """
        component, result = self._confirm()
        component.handle_input(Key("escape"))
        self.assertEqual(result, [None])


class PanelTests(unittest.TestCase):
    def _panel(self, lines: int, max_rows: int = 6) -> PanelComponent:
        body = Text("\n".join(f"第 {index} 行" for index in range(lines)) + "\n")
        return PanelComponent(body, palette=PALETTE, max_rows=max_rows)

    def test_short_content_has_no_scroll_hint(self) -> None:
        """内容放得下时**不加**滚动提示（提示是"还能翻"的信号，没有就不该出现）。"""
        rows = plain(self._panel(3, max_rows=10))
        self.assertEqual(rows, ["第 0 行", "第 1 行", "第 2 行"])

    def test_long_content_is_scrollable_with_a_hint(self) -> None:
        """★ 内容比面板高时必须能滚，而且**要告诉用户还能滚**。"""
        panel = self._panel(30, max_rows=6)
        rows = plain(panel)
        self.assertLessEqual(len(rows), 6)
        self.assertIn("共 30 行", rows[-1])
        panel.handle_input(Key("down"))
        self.assertEqual(panel.offset, 1)
        panel.handle_input(Key("pagedown"))
        self.assertGreater(panel.offset, 1)
        panel.handle_input(Key("home"))
        self.assertEqual(panel.offset, 0)

    def test_offset_is_clamped(self) -> None:
        panel = self._panel(10, max_rows=5)
        for _ in range(50):
            panel.handle_input(Key("down"))
        panel.render(WIDTH)
        self.assertLessEqual(panel.offset, 10 - 4)

    def test_escape_closes(self) -> None:
        result: list[object] = []
        panel = self._panel(3)
        panel.on_done = result.append
        panel.handle_input(Key("escape"))
        self.assertEqual(result, [None])


class PermissionDialogTests(unittest.TestCase):
    """权限弹窗（UI-SPEC §5.8）。

    这是**最高危**的一个浮层：它决定一条命令能不能跑。所以这里的断言比别处更严：

    * 参数**绝不截断**（截断后的命令是**另一条**命令，用户会据此做出错误判断）；
    * 默认焦点在**拒绝**（误按 Enter 不能等于放行）；
    * 弹窗再高也不能把自己的按钮挤出屏幕（否则用户**没法回答**）。
    """

    def _ask(self, **kwargs: object) -> PermissionAsk:
        base: dict[str, object] = {
            "tool": "shell",
            "detail": "npm install left-pad",
            "rule": "builtin.shell 非白名单",
            "rule_scope": "内置默认",
            "cwd": "G:/hz/codes/Logox",
        }
        base.update(kwargs)
        return PermissionAsk(**base)  # type: ignore[arg-type]

    def _dialog(self, **kwargs: object) -> tuple[PermissionComponent, list[object]]:
        result: list[object] = []
        component = PermissionComponent(
            self._ask(**kwargs), PALETTE, on_done=result.append
        )
        return component, result

    # -- 安全默认 -------------------------------------------------------- #

    def test_default_focus_is_deny(self) -> None:
        """★ 默认焦点必须是**拒绝**：误按一次 Enter 不该放行一条命令。"""
        component, result = self._dialog()
        label = component.options[component.focus_index][1]
        self.assertEqual(label, "拒绝")
        component.handle_input(Key("enter"))
        self.assertEqual(result, [PermissionChoice.DENY])

    def test_escape_denies(self) -> None:
        """Esc 是**拒绝**，不是"取消"——这个弹窗的默认答案只能是非放行的那一个。"""
        component, result = self._dialog()
        component.handle_input(Key("escape"))
        self.assertEqual(result, [PermissionChoice.DENY])

    def test_deny_is_highlighted_in_the_rendered_rows(self) -> None:
        """焦点必须**看得见**（唯一的焦点指示就是那一行的样式）。"""
        component, _result = self._dialog()
        rows = component.render(72)
        styled = [row for row in rows if row.spans]
        self.assertTrue(styled, "没有一行被高亮，用户看不出焦点在哪")
        self.assertIn("拒绝", styled[-1].plain)

    # -- 四个选项 -------------------------------------------------------- #

    def test_four_options_map_to_the_four_choices(self) -> None:
        component, _result = self._dialog()
        self.assertEqual(
            [choice for choice, _label in component.options],
            [
                PermissionChoice.ONCE,
                PermissionChoice.SESSION,
                PermissionChoice.DENY,
                PermissionChoice.PROJECT,
            ],
        )
        for index, expected in enumerate(
            [
                PermissionChoice.ONCE,
                PermissionChoice.SESSION,
                PermissionChoice.DENY,
                PermissionChoice.PROJECT,
            ],
            start=1,
        ):
            fresh, result = self._dialog()
            fresh.handle_input(Key(str(index), char=str(index)))
            self.assertEqual(result, [expected], f"[{index}] 选项对应错了")

    def test_session_option_names_the_scope(self) -> None:
        """★ "本会话总是允许"的**范围**必须写在按钮上。

        它记住的是工具级放行（"以后所有 shell 都不问了"），所以标签里要出现工具名
        ——藏起来的话，用户按下去的是一张他不知道多大的空白支票。
        """
        component, _result = self._dialog(tool="shell")
        labels = [label for _choice, label in component.options]
        self.assertTrue(any("shell" in label for label in labels))

    def test_arrow_keys_move_the_focus(self) -> None:
        component, result = self._dialog()
        component.handle_input(Key("up"))  # 拒绝 → 本会话总是允许
        self.assertEqual(component.options[component.focus_index][0], PermissionChoice.SESSION)
        component.handle_input(Key("enter"))
        self.assertEqual(result, [PermissionChoice.SESSION])

    def test_unavailable_options_are_not_shown(self) -> None:
        """兑现不了的选项**不出现在屏幕上**——画一个按了没用的按钮比不画更糟。"""
        component, _result = self._dialog(allow_session=False, allow_project=False)
        self.assertEqual(
            [choice for choice, _label in component.options],
            [PermissionChoice.ONCE, PermissionChoice.DENY],
        )

    # -- 参数不截断 ------------------------------------------------------ #

    def test_long_command_is_wrapped_not_truncated(self) -> None:
        """★★ 参数**绝不截断**。

        截断的后果不是"少看几个字"：``npm install a-b-c`` 裁成 ``npm install a``
        之后，用户看到的是**另一条命令**——一条看起来更安全的命令，
        然后他会按"允许"。**显示错误的信息比不显示更危险。**
        """
        long_command = "npm install " + "very-long-package-name-" * 4
        component = PermissionComponent(self._ask(detail=long_command), PALETTE)
        rendered = "\n".join(row for row in plain(component, width=40))
        for piece in ("very-long-package-name-", "name-very", "long-package"):
            with self.subTest(piece=piece):
                self.assertIn(piece, rendered.replace("\n", ""), "参数被截断了")
        self.assertGreater(len(plain(component, width=40)), 8, "长命令应当被折成多行")

    def test_detail_scrolls_when_longer_than_the_window(self) -> None:
        detail = "\n".join(f"第 {index} 行" for index in range(12))
        component = PermissionComponent(self._ask(detail=detail), PALETTE)
        first = plain(component, width=60)
        self.assertIn("第 0 行", "\n".join(first))
        self.assertNotIn("第 11 行", "\n".join(first), "超出窗口的行不该一次全显示")
        self.assertTrue(any("PgUp" in row for row in first), "要告诉用户还能滚")
        component.handle_input(Key("pagedown"))
        component.handle_input(Key("pagedown"))
        self.assertIn("第 11 行", "\n".join(plain(component, width=60)))

    def test_options_stay_visible_when_the_dialog_is_squeezed(self) -> None:
        """★★ 弹窗被压矮时，**选项必须还在屏幕上**。

        浮层比终端还高时多出来的行会被推到屏幕外——连选项一起推出去的话，
        用户看到一张"要你授权"的弹窗却没有任何按钮可按，那一回合就**卡死**了。
        所以压矮时优先牺牲的是参数区与"工作目录 / 命中规则"。
        """
        detail = "\n".join(f"第 {index} 行" for index in range(30))
        for max_rows in (14, 16, 20, 40):
            with self.subTest(max_rows=max_rows):
                component = PermissionComponent(
                    self._ask(detail=detail, risk="high", risk_note="高风险"), PALETTE,
                    max_rows=max_rows,
                )
                rendered = plain(component, width=60)
                self.assertLessEqual(len(rendered), max_rows, "弹窗超过了允许的高度")
                joined = "\n".join(rendered)
                self.assertIn("[3] 拒绝", joined, "最关键的选项被挤出屏幕了")
                self.assertIn("工具：shell", joined, "工具名不该被挤掉")

    def test_minimum_rows_is_reported_honestly(self) -> None:
        """★ 组件要**说得出**自己最小需要多高，界面才能决定"要不要问"。

        终端比这个还矮时，正确的做法是**不问**（拒绝 + 说明原因），
        而不是画一张按钮看不见的弹窗——那时用户无法回答，回合会一直卡着。
        """
        component = PermissionComponent(self._ask(risk="high"), PALETTE)
        minimum = component.minimum_rows(60)
        self.assertGreater(minimum, 8, "四个选项 + 工具名 + 参数至少需要这么多行")
        # 请求一个不可能的矮度：组件仍给出最小可用版本（而不是挤掉按钮）
        component.max_rows = 5
        self.assertGreaterEqual(len(plain(component, width=60)), minimum)

    # -- 高危警示 -------------------------------------------------------- #

    def test_high_risk_gets_a_banner(self) -> None:
        """高危操作要有警示，而且**不能只靠颜色**（D81 的教训）。"""
        component = PermissionComponent(
            self._ask(risk="high", risk_note="这个工具不是只读的"), PALETTE
        )
        joined = "\n".join(plain(component, width=60))
        self.assertIn("▶▶", joined, "警示条缺少结构信号")
        self.assertIn("不是只读的", joined)

    def test_normal_risk_has_no_banner(self) -> None:
        component = PermissionComponent(self._ask(risk="normal"), PALETTE)
        self.assertNotIn("▶▶", "\n".join(plain(component, width=60)))

    # -- 契约 ------------------------------------------------------------ #

    def test_never_exceeds_width(self) -> None:
        ask = self._ask(
            detail="x" * 400 + "\n第二行" * 20, risk="high", risk_note="很长的警示" * 10
        )
        for width in (24, 40, 60, 72, 100):
            with self.subTest(width=width):
                for row in PermissionComponent(ask, PALETTE, max_rows=20).render(width):
                    self.assertLessEqual(visible_width(row.plain), width)

    def test_finish_is_idempotent(self) -> None:
        """一次按键可能被两条路径处理；重复回调会让同一次授权被记两次。"""
        component, result = self._dialog()
        component.handle_input(Key("1", char="1"))
        component.handle_input(Key("2", char="2"))
        self.assertEqual(result, [PermissionChoice.ONCE])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
