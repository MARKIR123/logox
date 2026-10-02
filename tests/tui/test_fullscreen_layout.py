"""全屏备用屏视口布局单元测试（D179 / MODULE_tui_fullscreen）。

测试目标
--------
1. Viewport 剪裁与 Dock 底端固定停靠（时间线短于视口 / 超过一屏）；
2. 历史消息向上翻看时首行滚动指示器（Scroll Indicator）；
3. 边界约束（Clamping）：scroll_offset 严格限制在 [0, max_offset]；
4. 鼠标滚轮（wheel_up / wheel_down，含 Alt 加速）；
5. 键盘翻页（PageUp / PageDown、Home / End）；
6. 打字与 Esc 快捷回底；
7. 独占模态浮层替换 Dock 时的空间重算。
"""

from __future__ import annotations

import unittest
from typing import Any

from rich.text import Text

from logox.kernel import events as ev
from logox.tui.content.status import StatusContext
from logox.tui.content.timeline import Block
from logox.tui.render.app import StatusComponent, TimelineComponent
from logox.tui.render.component import Component
from logox.tui.render.components.editor import BoxedEditor, Editor
from logox.tui.render.fullscreen import FullscreenLayout
from logox.tui.render.keys import Key
from logox.tui.render.screen import OverlayHandle, Screen
from logox.tui.render.terminal import FakeTerminal
from logox.tui.theme import load_theme


class DummyOverlayComponent:
    """供测试用的固定行数浮层组件。"""

    def __init__(self, rows: int = 5) -> None:
        self.rows = rows

    def render(self, width: int) -> list[Text]:
        return [Text(f"Overlay line {i}") for i in range(self.rows)]

    def handle_input(self, key: Key) -> bool:
        return False

    def invalidate(self) -> None:
        pass


def make_layout(
    *, columns: int = 80, rows: int = 24
) -> tuple[FullscreenLayout, FakeTerminal, TimelineComponent, BoxedEditor, StatusComponent]:
    theme = load_theme("logox-dark")
    palette = theme.palette
    terminal = FakeTerminal(columns=columns, rows=rows)
    timeline = TimelineComponent(palette)
    editor = BoxedEditor(Editor())
    status = StatusComponent(palette)
    # mock metrics
    status.metrics = lambda: None
    layout = FullscreenLayout(terminal, timeline, editor, status)
    return layout, terminal, timeline, editor, status


class FullscreenLayoutTests(unittest.TestCase):
    def test_initial_state(self) -> None:
        layout, terminal, timeline, editor, status = make_layout()
        self.assertEqual(layout.scroll_offset, 0)
        self.assertTrue(layout.auto_scroll_to_bottom)
        self.assertIs(layout.terminal, terminal)
        self.assertIs(layout.timeline, timeline)
        self.assertIs(layout.editor, editor)
        self.assertIs(layout.status, status)

    def test_render_fewer_lines_than_viewport_pads_and_pins_dock(self) -> None:
        """消息较少（不足一屏）时，顶部放消息，中间补空白，输入框和状态栏稳固钉在底部。"""
        layout, terminal, timeline, editor, status = make_layout(columns=80, rows=24)
        # 添加 5 行消息
        for i in range(5):
            timeline.buffer.add_notice(f"Notice message {i}")

        rendered = layout.render(80)
        self.assertEqual(len(rendered), 24, "总输出行数必须精确等于 terminal.rows (24)")

        # 底部 dock 占用行数 = editor (3) + status (1) = 4
        ed_rows = editor.render(80)
        st_rows = status.render(80)
        dock_rows = len(ed_rows) + len(st_rows)
        self.assertEqual(dock_rows, 4)

        viewport_height = 24 - 4
        # 视口 20 行：5 行消息 + 15 行空白
        all_tl_rows = timeline.render(80)
        self.assertEqual(len(all_tl_rows), 5)
        for i in range(5):
            self.assertIn(f"Notice message {i}", rendered[i].plain)

        for i in range(5, viewport_height):
            self.assertEqual(rendered[i].plain.strip(), "", f"第 {i} 行应当为空白 padding")

        # 最后 4 行是 editor 和 status
        dock_rendered = rendered[viewport_height:]
        self.assertEqual(len(dock_rendered), 4)

    def test_render_more_lines_than_viewport_at_offset_zero(self) -> None:
        """消息超过一屏且 scroll_offset=0 时，显示最新生成的尾部消息。"""
        layout, terminal, timeline, editor, status = make_layout(columns=80, rows=24)
        for i in range(40):
            timeline.buffer.add_notice(f"Message line {i:02d}")

        rendered = layout.render(80)
        self.assertEqual(len(rendered), 24)

        # 视口高度 = 20 行，由于总共有 40 行，scroll_offset=0 应切出最后 20 行（20..39）
        view_rows = rendered[:20]
        self.assertIn("Message line 20", view_rows[0].plain)
        self.assertIn("Message line 39", view_rows[-1].plain)
        # 正常状态下第一行不应包含历史指示器横幅
        self.assertNotIn("历史消息", view_rows[0].plain)

    def test_render_with_scroll_offset_pure_messages(self) -> None:
        """当 scroll_offset > 0 时，首行不再显示遮挡横幅，保持纯净对话内容 (D181 方案 A)。"""
        layout, terminal, timeline, editor, status = make_layout(columns=80, rows=24)
        for i in range(40):
            timeline.buffer.add_notice(f"Message line {i:02d}")

        layout.scroll_offset = 6
        rendered = layout.render(80)
        self.assertEqual(len(rendered), 24)

        # 首行不再包含历史横幅，直接呈现真实消息行 14 (40 - 20 - 6 = 14)
        first_line = rendered[0].plain
        self.assertNotIn("历史消息", first_line)
        self.assertIn("Message line 14", first_line)

        # view_rows[1] 对应原本第 15 行
        self.assertIn("Message line 15", rendered[1].plain)

    def test_scroll_offset_clamping(self) -> None:
        """scroll_offset 超出边界时自动收敛至合法区间 [0, max_offset]。"""
        layout, terminal, timeline, editor, status = make_layout(columns=80, rows=24)
        for i in range(30):
            timeline.buffer.add_notice(f"Line {i}")

        # viewport = 20, total = 30, max_offset = 10
        layout.scroll_offset = 999
        rendered = layout.render(80)
        self.assertEqual(layout.scroll_offset, 10)

        layout.scroll_offset = -50
        rendered = layout.render(80)
        self.assertEqual(layout.scroll_offset, 0)

    def test_mouse_wheel_navigation(self) -> None:
        """滚轮向上向上翻 3 行（Alt 为 15 行），滚轮向下递减。"""
        layout, terminal, timeline, editor, status = make_layout(columns=80, rows=24)
        for i in range(50):
            timeline.buffer.add_notice(f"Message {i}")
        # viewport = 20, max_offset = 30

        consumed = layout.handle_input(Key("wheel_up"))
        self.assertTrue(consumed)
        self.assertEqual(layout.scroll_offset, 3)

        layout.handle_input(Key("wheel_up", alt=True))
        self.assertEqual(layout.scroll_offset, 18)

        layout.handle_input(Key("wheel_down"))
        self.assertEqual(layout.scroll_offset, 15)

        layout.handle_input(Key("wheel_down", alt=True))
        self.assertEqual(layout.scroll_offset, 0)

        # 滚到底部继续滚轮向下保持 0
        layout.handle_input(Key("wheel_down"))
        self.assertEqual(layout.scroll_offset, 0)

    def test_pageup_and_pagedown(self) -> None:
        """PageUp 向上翻半屏，PageDown 向下翻半屏。"""
        layout, terminal, timeline, editor, status = make_layout(columns=80, rows=24)
        for i in range(50):
            timeline.buffer.add_notice(f"Message {i}")
        # viewport = 20, step = 10

        self.assertTrue(layout.handle_input(Key("pageup")))
        self.assertEqual(layout.scroll_offset, 10)

        self.assertTrue(layout.handle_input(Key("pageup")))
        self.assertEqual(layout.scroll_offset, 20)

        self.assertTrue(layout.handle_input(Key("pagedown")))
        self.assertEqual(layout.scroll_offset, 10)

        self.assertTrue(layout.handle_input(Key("pagedown")))
        self.assertEqual(layout.scroll_offset, 0)

    def test_home_and_end(self) -> None:
        """Ctrl+Home 直达历史起点，Ctrl+End 瞬间回底。"""
        layout, terminal, timeline, editor, status = make_layout(columns=80, rows=24)
        for i in range(50):
            timeline.buffer.add_notice(f"Message {i}")

        self.assertTrue(layout.handle_input(Key("home", ctrl=True)))
        self.assertEqual(layout.scroll_offset, 30)

        self.assertTrue(layout.handle_input(Key("end", ctrl=True)))
        self.assertEqual(layout.scroll_offset, 0)

    def test_escape_resets_scroll_offset(self) -> None:
        """当处于历史查看状态时，按 Esc 归零回底。"""
        layout, terminal, timeline, editor, status = make_layout(columns=80, rows=24)
        for i in range(50):
            timeline.buffer.add_notice(f"Message {i}")

        layout.scroll_offset = 12
        self.assertTrue(layout.handle_input(Key("escape")))
        self.assertEqual(layout.scroll_offset, 0)

    def test_typing_in_editor_preserves_scroll_offset(self) -> None:
        """用户在输入框打字与编辑时，scroll_offset 保持不变，便于参照历史上下文。"""
        layout, terminal, timeline, editor, status = make_layout(columns=80, rows=24)
        for i in range(50):
            timeline.buffer.add_notice(f"Message {i}")

        layout.scroll_offset = 15
        consumed = layout.handle_input(Key("a", char="a"))
        self.assertTrue(consumed)
        self.assertEqual(layout.scroll_offset, 15, "打字不应强制回底")
        self.assertEqual(editor.text, "a")

        # 继续输入和退格删除，scroll_offset 依然保持
        layout.handle_input(Key("b", char="b"))
        layout.handle_input(Key("backspace"))
        self.assertEqual(layout.scroll_offset, 15, "退格编辑不应强制回底")
        self.assertEqual(editor.text, "a")

    def test_modal_overlay_replaces_dock(self) -> None:
        """激活模态浮层时，Dock 让位给浮层，视口高度精确预留给浮层。"""
        layout, terminal, timeline, editor, status = make_layout(columns=80, rows=24)
        for i in range(30):
            timeline.buffer.add_notice(f"Message {i}")

        screen = Screen(terminal)
        layout.screen = screen

        # 显示一个 6 行高的浮层
        overlay_comp = DummyOverlayComponent(rows=6)
        screen.show_overlay(overlay_comp, width=80, anchor="bottom", push=True)

        rendered = layout.render(80)
        # layout.render 返回给 Screen._composite_overlays 的基础视口行数应为 24 - 6 = 18 行
        self.assertEqual(len(rendered), 18)

        # 检查经过 Screen composite 后整帧高度依然是 24 行
        composited = screen._composite_overlays(rendered, 80, 24)
        self.assertEqual(len(composited), 24)
        self.assertIn("Overlay line 0", composited[18].plain)
        self.assertIn("Overlay line 5", composited[23].plain)


if __name__ == "__main__":
    unittest.main()
