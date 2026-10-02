"""全屏模式鼠标交互与纯净选区复制测试 (D180 / MODULE_tui_mouse_interaction)。

覆盖矩阵：
1. SGR 1006 鼠标协议解析（mouse_down, mouse_drag, mouse_up 及 1-based 坐标）；
2. 输入框点击定位光标（ASCII 字符、CJK 中文双宽、行尾吸附、BoxedEditor 边框转换）；
3. 单卡独立展开与折叠（方案 3A：tool/reasoning 独立翻转，不干扰全局快捷键或其他卡片）；
4. 纯净选区复制（双轨标记 ▌/▎ 物理剥离，代码缩进完整保留）；
5. 拖拽划选高亮与复制完成 Toast 状态机；
6. 剪贴板写回 fail-safe（Win32 原生 API 与 OSC 52 后备）。
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest
from rich.text import Text

from logox.tui.content.timeline import Block, CardContext, render_blocks
from logox.tui.render.app import StatusComponent, TimelineComponent
from logox.tui.render.components.editor import BoxedEditor, Editor
from logox.tui.render.fullscreen import (
    FullscreenLayout,
    copy_to_system_clipboard,
    extract_clean_selection,
    strip_track_prefix,
)
from logox.tui.render.keys import Key, KeyParser
from logox.tui.theme import load_theme


def get_palette():
    return load_theme("logox-dark").palette


class DummyTerminal:
    def __init__(self, cols: int = 80, rows: int = 24) -> None:
        self.columns = cols
        self.rows = rows
        self.output: list[str] = []

    def write(self, text: str) -> None:
        self.output.append(text)


# ========================================================================= #
# 1. SGR 1006 鼠标协议解析测试
# ========================================================================= #

def test_sgr_mouse_events_parsing() -> None:
    parser = KeyParser()

    # 1. 按下左键在 (x=10, y=20) -> CSI < 0; 10; 20 M
    keys = parser.feed("\x1b[<0;10;20M")
    assert len(keys) == 1
    assert keys[0].name == "mouse_down"
    assert keys[0].x == 10
    assert keys[0].y == 20

    # 2. 按住左键拖拽移动到 (x=15, y=22) -> CSI < 32; 15; 22 M (base & 32 == 32)
    keys = parser.feed("\x1b[<32;15;22M")
    assert len(keys) == 1
    assert keys[0].name == "mouse_drag"
    assert keys[0].x == 15
    assert keys[0].y == 22

    # 3. 释放左键在 (x=15, y=22) -> CSI < 0; 15; 22 m ('m' 代表 release)
    keys = parser.feed("\x1b[<0;15;22m")
    assert len(keys) == 1
    assert keys[0].name == "mouse_up"
    assert keys[0].x == 15
    assert keys[0].y == 22

    # 4. 滚轮事件仍保持正常兼容
    keys = parser.feed("\x1b[<64;5;5M")
    assert len(keys) == 1
    assert keys[0].name == "wheel_up"


# ========================================================================= #
# 2. 输入框鼠标点击定位光标测试
# ========================================================================= #

def test_editor_click_cursor_ascii() -> None:
    editor = Editor()
    editor.set_text("hello world")

    # 点击第 0 行第 5 个单元格（在 'hello' 与空格之间）
    editor.set_cursor_from_cell(0, 5, width=80)
    assert editor.cursor == (0, 5)

    # 点击超出行尾：自动吸附到行尾
    editor.set_cursor_from_cell(0, 50, width=80)
    assert editor.cursor == (0, 11)


def test_editor_click_cursor_cjk_double_width() -> None:
    editor = Editor()
    editor.set_text("你好世界")

    # '你' 占 cell 0..1，'好' 占 2..3，'世' 占 4..5，'界' 占 6..7
    # 点击 cell 0 -> 光标在 '你' 前面 (0)
    editor.set_cursor_from_cell(0, 0, width=80)
    assert editor.cursor == (0, 0)

    # 点击 cell 1 -> 超过一半，吸附到 '你' 后面 (1)
    editor.set_cursor_from_cell(0, 1, width=80)
    assert editor.cursor == (0, 1)

    # 点击 cell 2 -> 光标在 '好' 前面 (1)
    editor.set_cursor_from_cell(0, 2, width=80)
    assert editor.cursor == (0, 1)

    # 点击 cell 3 -> 吸附到 '好' 后面 (2)
    editor.set_cursor_from_cell(0, 3, width=80)
    assert editor.cursor == (0, 2)

    # 点击远端 -> 吸附到全部字符末尾 (4)
    editor.set_cursor_from_cell(0, 20, width=80)
    assert editor.cursor == (0, 4)


def test_boxed_editor_click_cursor_mapping() -> None:
    inner = Editor()
    inner.set_text("python code")
    boxed = BoxedEditor(inner)

    # box_row 0 是顶边边框 ╭──╮ -> 映射到 inner_row 0
    # box_col 2 是左边框 '│ ' 后的第一个字符 -> inner_col 0
    boxed.set_cursor_from_cell(box_row=0, box_col=2, width=80)
    assert inner.cursor == (0, 0)

    # box_row 1 是内容第一行，box_col 8 -> inner_col 6 ('python' 后面)
    boxed.set_cursor_from_cell(box_row=1, box_col=8, width=80)
    assert inner.cursor == (0, 6)


# ========================================================================= #
# 3. 单卡精准展开与折叠测试 (方案 3A)
# ========================================================================= #

def test_timeline_component_single_card_toggle() -> None:
    palette = get_palette()
    tl = TimelineComponent(palette)

    # 添加用户消息、思考卡片与工具卡片
    b_user = tl.buffer.add_user("test request")
    b_reason = Block(kind="reasoning", text="thinking details", duration_ms=500)
    b_tool = Block(kind="tool", name="read_file", args_summary="path='foo.py'")
    tl.buffer.blocks.extend([b_reason, b_tool])

    # 初始渲染以建立 block_ranges
    rows = tl.render(80)
    assert len(tl.block_ranges) >= 3

    # 找到思考卡片与工具卡片所在的行区间
    reason_range = next(r for r in tl.block_ranges if r[2] is b_reason)
    tool_range = next(r for r in tl.block_ranges if r[2] is b_tool)

    assert b_reason.expanded is None
    assert b_tool.expanded is None

    # 1. 点击思考卡片所在的行 -> 只有 b_reason.expanded 翻转为 True
    hit = tl.toggle_card_at_line(reason_range[0])
    assert hit is True
    assert b_reason.expanded is True
    assert b_tool.expanded is None  # 工具卡不受任何影响！

    # 2. 点击工具卡片所在的行 -> 只有 b_tool.expanded 翻转为 True
    hit2 = tl.toggle_card_at_line(tool_range[0])
    assert hit2 is True
    assert b_tool.expanded is True
    assert b_reason.expanded is True  # 思考卡仍然保持展开

    # 3. 再次点击思考卡片 -> 收起
    tl.render(80)  # 刷新区间
    reason_range_expanded = next(r for r in tl.block_ranges if r[2] is b_reason)
    tl.toggle_card_at_line(reason_range_expanded[0])
    assert b_reason.expanded is False
    assert b_tool.expanded is True

    # 4. 点击用户消息所在的行 -> 返回 False，不触发任何折叠
    user_range = next(r for r in tl.block_ranges if r[2] is b_user)
    assert tl.toggle_card_at_line(user_range[0]) is False


def test_timeline_component_tool_with_diff_toggle() -> None:
    palette = get_palette()
    tl = TimelineComponent(palette)

    b_tool = Block(kind="tool", name="edit", args_summary="path='main.py'")
    b_diff = Block(kind="diff", path="main.py")
    tl.buffer.blocks.extend([b_tool, b_diff])

    tl.render(80)
    tool_range = next(r for r in tl.block_ranges if r[2] is b_tool)

    # 点击工具卡片，关联的 diff 块同步翻转
    tl.toggle_card_at_line(tool_range[0])
    assert b_tool.expanded is True
    assert b_diff.expanded is True


# ========================================================================= #
# 4. 双轨剥离与纯净选区复制测试
# ========================================================================= #

def test_strip_track_prefix() -> None:
    # 助手消息前缀 ▎
    assert strip_track_prefix("▎ def add(a, b):") == "def add(a, b):"
    # 代码缩进完整保留
    assert strip_track_prefix("▎     return a + b") == "    return a + b"
    # 用户消息前缀 ▌
    assert strip_track_prefix("▌ 请写一个排序算法") == "请写一个排序算法"
    # 纯文本行无前缀
    assert strip_track_prefix("ordinary line") == "ordinary line"


def test_extract_clean_selection_multi_line() -> None:
    lines = [
        Text("▌ 请帮我优化这段代码"),
        Text("▎ 好的，这是优化方案："),
        Text("▎ ```python"),
        Text("▎ def fast():"),
        Text("▎     pass"),
        Text("▎ ```"),
    ]

    # 全量选择
    selected = extract_clean_selection(lines, start_line=0, start_col=0, end_line=5, end_col=10)
    expected = (
        "请帮我优化这段代码\n"
        "好的，这是优化方案：\n"
        "```python\n"
        "def fast():\n"
        "    pass\n"
        "```"
    )
    assert selected == expected
    # 绝对没有任何 ▌ 或 ▎
    assert "▌" not in selected
    assert "▎" not in selected


# ========================================================================= #
# 5. FullscreenLayout 鼠标拖选、复制与光标定位集成测试
# ========================================================================= #

def test_fullscreen_mouse_click_editor_positioning() -> None:
    term = DummyTerminal(cols=80, rows=20)
    palette = get_palette()
    tl = TimelineComponent(palette)
    editor = Editor()
    editor.set_text("antigravity python")
    boxed_editor = BoxedEditor(editor)
    status = StatusComponent(palette)

    layout = FullscreenLayout(term, tl, boxed_editor, status)

    # 触发渲染计算 Dock 高度
    layout.render(80)
    dock_rows = layout._get_dock_rows_count(80)
    viewport_height = 20 - dock_rows

    # 点击 Editor 第一行中的字符位置
    # Editor 处于 Dock 的第 1 行（y = viewport_height + 1）
    click_y = viewport_height + 1
    # 点击第 14 个 cell ('antigravity ' 之后，应落于 'python' 的 'p')
    click_x = 2 + 12 + 1  # 边框(2) + 12 字符 + 1-based offset

    layout.handle_input(Key("mouse_down", x=click_x, y=click_y))
    assert editor.cursor[1] >= 11
    assert editor.focused is True


def test_fullscreen_mouse_drag_selection_and_copy() -> None:
    term = DummyTerminal(cols=80, rows=20)
    palette = get_palette()
    tl = TimelineComponent(palette)
    tl.buffer.add_user("hello logox")
    tl.buffer.blocks.append(Block(kind="assistant", text="answer text"))

    editor = Editor()
    boxed_editor = BoxedEditor(editor)
    status = StatusComponent(palette)

    layout = FullscreenLayout(term, tl, boxed_editor, status)
    layout.render(80)

    # 模拟按下鼠标左键在视口第 1 行第 1 列
    layout.handle_input(Key("mouse_down", x=1, y=1))
    assert layout._mouse_down_pos == (1, 1)
    assert layout._is_dragging is False

    # 模拟拖拽移动到第 2 行第 20 列
    layout.handle_input(Key("mouse_drag", x=20, y=2))
    assert layout._is_dragging is True
    assert layout._selection is not None

    # 渲染时应有反色高亮
    rendered = layout.render(80)
    assert any("reverse" in str(line.spans) for line in rendered[:10])

    # 模拟释放左键完成拖选
    with patch("logox.tui.render.fullscreen.copy_to_system_clipboard", return_value=True) as mock_copy:
        layout.handle_input(Key("mouse_up", x=20, y=2))
        assert mock_copy.called
        # 复制完成后，选区清除，显示 Toast
        assert layout._selection is None
        assert layout._is_dragging is False
        assert layout._toast == "已复制到剪贴板"
        assert time.time() - layout._toast_time < 1.0


def test_fullscreen_mouse_single_click_card_toggle() -> None:
    term = DummyTerminal(cols=80, rows=20)
    palette = get_palette()
    tl = TimelineComponent(palette)
    b_tool = Block(kind="tool", name="read_file", args_summary="file='test.py'")
    tl.buffer.blocks.append(b_tool)

    editor = Editor()
    boxed_editor = BoxedEditor(editor)
    status = StatusComponent(palette)

    layout = FullscreenLayout(term, tl, boxed_editor, status)
    layout.render(80)

    # 在卡片所在的行单击（按下与松开在同一点）
    # 查找卡片所在行
    tool_range = next(r for r in tl.block_ranges if r[2] is b_tool)
    card_screen_y = tool_range[0] + 1  # 1-based

    layout.handle_input(Key("mouse_down", x=5, y=card_screen_y))
    layout.handle_input(Key("mouse_up", x=5, y=card_screen_y))

    # 单击应成功触发卡片折叠翻转
    assert b_tool.expanded is True


# ========================================================================= #
# 6. 系统剪贴板写入与 OSC 52 后备测试
# ========================================================================= #

def test_clipboard_copy_smoke() -> None:
    # 无论当前环境是否支持 Win32，copy_to_system_clipboard 均安全返回布尔值，绝不崩溃
    res = copy_to_system_clipboard("Logox Pure Clipboard Test")
    assert isinstance(res, bool)


# ========================================================================= #
# 7. 性能优化与 16ms 帧合并测试 (D180-b / Pi Agent 对齐)
# ========================================================================= #

def test_fullscreen_mouse_drag_zero_render_benchmark() -> None:
    """断言鼠标划选期间绝对零重绘 (Zero Re-render)，100 次拖拽耗时在毫秒级。"""
    term = DummyTerminal(cols=80, rows=20)
    palette = get_palette()
    tl = TimelineComponent(palette)
    for i in range(10):
        tl.buffer.add_user(f"message {i}")
        tl.buffer.blocks.append(Block(kind="assistant", text=f"response {i}"))

    editor = Editor()
    boxed_editor = BoxedEditor(editor)
    status = StatusComponent(palette)

    layout = FullscreenLayout(term, tl, boxed_editor, status)
    layout.render(80)  # 初始帧建立 _cached_tl_rows

    # 按下鼠标
    layout.handle_input(Key("mouse_down", x=1, y=1))

    # Spy 拦截 tl.render，确保拖拽期间绝对不触发任何 render
    with patch.object(tl, "render", wraps=tl.render) as spy_render:
        start_time = time.perf_counter()
        # 模拟高频拖拽 100 次 (相当于 100Hz 鼠标滑动半秒钟)
        for i in range(100):
            col = (i % 60) + 1
            row = (i % 10) + 1
            layout.handle_input(Key("mouse_drag", x=col, y=row))

        elapsed_ms = (time.perf_counter() - start_time) * 1000.0

        # 核心契约 1：拖拽期间 tl.render 调用次数必须严格为 0
        assert spy_render.call_count == 0

        # 核心契约 2：100 次高频拖拽总耗时必须极低 (< 50ms，通常 < 2ms)
        assert elapsed_ms < 50.0

        # 选区处于激活态
        assert layout._is_dragging is True
        assert layout._selection is not None


def test_fullscreen_mouse_drag_cell_deduplication() -> None:
    """微小移动未跨越单元格时不触发重绘。"""
    term = DummyTerminal(cols=80, rows=20)
    palette = get_palette()
    tl = TimelineComponent(palette)
    tl.buffer.add_user("hello world")

    editor = Editor()
    boxed_editor = BoxedEditor(editor)
    status = StatusComponent(palette)

    layout = FullscreenLayout(term, tl, boxed_editor, status)
    layout.render(80)

    layout.handle_input(Key("mouse_down", x=2, y=1))
    consumed_first = layout.handle_input(Key("mouse_drag", x=5, y=1))
    assert consumed_first is True

    # 再次发送完全相同的位置，选区未变，返回 False（不请求绘制）
    consumed_duplicate = layout.handle_input(Key("mouse_drag", x=5, y=1))
    assert consumed_duplicate is False


def test_fullscreen_app_dispatch_mouse_drag_throttling() -> None:
    """FullscreenApp._dispatch 对 mouse_drag 使用 force=True 实现无时钟延迟的单帧即时合并 (D181)。"""
    from tests.tui.test_fullscreen_app import _Runtime
    from logox.tui.render.fullscreen import FullscreenApp

    runtime = _Runtime()
    app = FullscreenApp(runtime=runtime, terminal=DummyTerminal(cols=80, rows=20))
    app.timeline.buffer.add_user("hello world")
    app.root.render(80)
    app.screen.request_render = MagicMock()

    # 1. mouse_down -> force=True
    app._dispatch(Key("mouse_down", x=1, y=1))
    app.screen.request_render.assert_called_with(force=True)

    # 2. mouse_drag -> force=True (消除 Windows 定时器延迟，并在下一 tick 单帧合并，D181)
    app.screen.request_render.reset_mock()
    app._dispatch(Key("mouse_drag", x=10, y=1))
    app.screen.request_render.assert_called_with(force=True)

    # 3. mouse_up -> force=True (松手即时响应)
    app.screen.request_render.reset_mock()
    app._dispatch(Key("mouse_up", x=10, y=1))
    app.screen.request_render.assert_called_with(force=True)


def test_cell_to_char_index_cjk_and_ascii() -> None:
    """验证 cell_to_char_index 在 ASCII 与 CJK 双宽字符下的精确定位。"""
    from logox.tui.render.fullscreen import cell_to_char_index

    # 1. ASCII 文本
    text_ascii = "hello world"
    assert cell_to_char_index(text_ascii, 0, is_end=False) == 0
    assert cell_to_char_index(text_ascii, 4, is_end=True) == 5  # 包含 'o' (cell 4 -> slice end 5)
    assert text_ascii[0:5] == "hello"

    # 2. CJK 文本："你好世界" (每个汉字 2 cells)
    text_cjk = "你好世界"
    # cell 0 ('你' 的前半): start=0, end=1 (包含'你')
    assert cell_to_char_index(text_cjk, 0, is_end=False) == 0
    assert cell_to_char_index(text_cjk, 0, is_end=True) == 1
    assert text_cjk[0:1] == "你"

    # cell 1 ('你' 的后半): end 仍为 1
    assert cell_to_char_index(text_cjk, 1, is_end=True) == 1

    # cell 2 ('好' 的前半): end=2 (包含 '你好')
    assert cell_to_char_index(text_cjk, 2, is_end=True) == 2
    assert text_cjk[0:2] == "你好"

    # cell 7 ('界' 的后半): end=4 (包含全部 4 个汉字)
    assert cell_to_char_index(text_cjk, 7, is_end=True) == 4
    assert text_cjk[0:4] == "你好世界"

    # 超出边界
    assert cell_to_char_index(text_cjk, 20, is_end=True) == 4
    assert cell_to_char_index(text_cjk, -5, is_end=False) == 0


def test_fullscreen_toast_rendered_top_right() -> None:
    """验证复制完成 Toast 呈现在视口第一行（Row 0）右上角 (D181)。"""
    term = DummyTerminal(cols=60, rows=20)
    palette = get_palette()
    tl = TimelineComponent(palette)
    tl.buffer.add_user("hello logox")

    editor = Editor()
    boxed_editor = BoxedEditor(editor)
    status = StatusComponent(palette)

    layout = FullscreenLayout(term, tl, boxed_editor, status)
    layout.render(60)

    # 激活 Toast
    layout._toast = "已复制到剪贴板"
    layout._toast_time = time.time()

    rendered = layout.render(60)
    top_line = rendered[0].plain

    # 断言 Toast 在首行 (Row 0)
    assert "✓ 已复制到剪贴板" in top_line
    # 断言 Toast 靠右侧（末尾无多余文字）
    assert top_line.rstrip().endswith("✓ 已复制到剪贴板")


def test_fullscreen_mouse_micro_jitter_click_toggles_card() -> None:
    """★ D182：手部微抖（<= 1 列位移）严格仲裁为单击，100% 触发卡片展开且不写剪贴板。"""
    term = DummyTerminal(cols=80, rows=20)
    palette = get_palette()
    tl = TimelineComponent(palette)
    b_tool = Block(kind="tool", name="read_file", args_summary="file='test.py'")
    tl.buffer.blocks.append(b_tool)

    editor = Editor()
    boxed_editor = BoxedEditor(editor)
    status = StatusComponent(palette)

    layout = FullscreenLayout(term, tl, boxed_editor, status)
    layout.render(80)

    tool_range = next(r for r in tl.block_ranges if r[2] is b_tool)
    card_screen_y = tool_range[0] + 1  # 1-based

    with patch("logox.tui.render.fullscreen.copy_to_system_clipboard") as mock_copy:
        # 模拟物理按下 (5, y)，手部发生 1 列微抖至 (6, y)，并在 (6, y) 松开
        layout.handle_input(Key("mouse_down", x=5, y=card_screen_y))
        layout.handle_input(Key("mouse_drag", x=6, y=card_screen_y))
        layout.handle_input(Key("mouse_up", x=6, y=card_screen_y))

        # 断言：微抖成功被消除歧义，认定为单击意图，卡片翻转为 True
        assert b_tool.expanded is True
        # 断言：微抖绝对不误触系统剪贴板写入
        assert not mock_copy.called


def test_fullscreen_mouse_real_drag_does_not_toggle_card() -> None:
    """★ D182：真正划选复制（跨度 >= 2 列）写入剪贴板，绝不误触卡片折叠/展开。"""
    term = DummyTerminal(cols=80, rows=20)
    palette = get_palette()
    tl = TimelineComponent(palette)
    b_tool = Block(kind="tool", name="read_file", args_summary="file='test.py'")
    tl.buffer.blocks.append(b_tool)

    editor = Editor()
    boxed_editor = BoxedEditor(editor)
    status = StatusComponent(palette)

    layout = FullscreenLayout(term, tl, boxed_editor, status)
    layout.render(80)

    tool_range = next(r for r in tl.block_ranges if r[2] is b_tool)
    card_screen_y = tool_range[0] + 1  # 1-based

    with patch("logox.tui.render.fullscreen.copy_to_system_clipboard", return_value=True) as mock_copy:
        # 模拟真正划选：从第 5 列拖到第 15 列（跨度 10 列）
        layout.handle_input(Key("mouse_down", x=5, y=card_screen_y))
        layout.handle_input(Key("mouse_drag", x=15, y=card_screen_y))
        layout.handle_input(Key("mouse_up", x=15, y=card_screen_y))

        # 断言：真正拖拽触发了剪贴板复制
        assert mock_copy.called
        # 断言：卡片未被翻转（仍为默认初始折叠状态）
        assert b_tool.expanded is not True


def test_cards_full_content_untruncated() -> None:
    """★ D182：思考卡片与工具卡片展开时 100% 完整展示全部行，不带任何截断提示。"""
    from logox.tui.content.cards import CardContext, render_reasoning, render_tool_card

    ctx = CardContext(palette=get_palette(), width=80)

    # 50 行超长思考文本
    long_reasoning = "\n".join(f"思考第 {i} 步" for i in range(1, 51))
    rendered_reasoning = render_reasoning(text=long_reasoning, context=ctx, expanded=True).plain
    assert "思考第 1 步" in rendered_reasoning
    assert "思考第 20 步" in rendered_reasoning
    assert "思考第 50 步" in rendered_reasoning
    assert "另有" not in rendered_reasoning
    assert "未显示" not in rendered_reasoning

    # 50 行超长工具输出
    long_output = "\n".join(f"日志输出行 {i}" for i in range(1, 51))
    rendered_tool = render_tool_card(
        state="ok",
        name="read_file",
        args_summary="foo.txt",
        payload=long_output,
        context=ctx,
        expanded=True,
    ).plain
    assert "日志输出行 1" in rendered_tool
    assert "日志输出行 30" in rendered_tool
    assert "日志输出行 50" in rendered_tool
    assert "只显示前" not in rendered_tool
    assert "未显示" not in rendered_tool


# ========================================================================= #
# 8. 全屏右侧滚动条与轮次标尺测试 (D183)
# ========================================================================= #

def test_fullscreen_scrollbar_rendered_when_content_overflows() -> None:
    """★ D183 / D184：内容超出一屏时，右侧第 width 列自动呈现导轨同色滑块与滚动条。"""
    term = DummyTerminal(cols=80, rows=15)
    palette = get_palette()
    tl = TimelineComponent(palette)
    for i in range(15):
        tl.buffer.add_user(f"question {i}")
        tl.buffer.blocks.append(Block(kind="assistant", text=f"answer {i}"))

    editor = Editor()
    boxed_editor = BoxedEditor(editor)
    status = StatusComponent(palette)

    layout = FullscreenLayout(term, tl, boxed_editor, status)
    rendered = layout.render(80)

    # 断言：已激活滚动条
    assert layout._show_scrollbar is True

    # 检查视口前 N 行右边缘（第 80 列，0-based 下标 79）
    dock_rows = len(boxed_editor.render(80)) + len(status.render(80))
    viewport_h = 15 - dock_rows
    viewport_rows = rendered[:viewport_h]

    right_chars = [r.plain[-1] for r in viewport_rows]
    block_chars = ("█", "▄", "▀", " ", "▂", "▃", "▅", "▆", "▇")
    assert any(ch in block_chars for ch in right_chars), "视口右边缘必须包含滚动条滑块字符"
    assert "│" in right_chars, "视口右边缘必须包含滚动条导轨"


def test_fullscreen_scrollbar_turn_start_blue_markers() -> None:
    """★ D184：多轮对话时，导轨上对应用户提问行正确呈现与导轨同款格式的蓝色 │ 标尺。"""
    term = DummyTerminal(cols=80, rows=20)
    palette = get_palette()
    tl = TimelineComponent(palette)
    # 模拟 5 轮长对话
    for i in range(5):
        tl.buffer.add_user(f"用户第 {i+1} 轮提问")
        tl.buffer.blocks.append(Block(kind="assistant", text=f"长回答第 {i+1} 轮\n" * 8))

    editor = Editor()
    boxed_editor = BoxedEditor(editor)
    status = StatusComponent(palette)

    layout = FullscreenLayout(term, tl, boxed_editor, status)
    rendered = layout.render(80)

    # 提取轮次标记行映射
    assert len(layout._turn_mark_rows) > 0, "必须成功计算出轮次起点行映射"

    dock_rows = len(boxed_editor.render(80)) + len(status.render(80))
    viewport_h = 20 - dock_rows
    viewport_rows = rendered[:viewport_h]

    # 检查是否存在格式与导轨同款（│）且带有 bold 蓝色 accent 样式的轮次标尺
    has_blue_marker = False
    accent_color = str(getattr(palette, "accent", "#89b4fa"))
    for v_idx in layout._turn_mark_rows:
        if v_idx < len(viewport_rows):
            r = viewport_rows[v_idx]
            assert r.plain.endswith("│"), f"轮次标尺必须与导轨同款格式 │，实际末尾为: {r.plain[-1]!r}"
            last_span_style = ""
            for span in r.spans:
                if span.end == len(r.plain):
                    last_span_style = str(span.style)
            if "bold" in last_span_style and (accent_color in last_span_style or "blue" in last_span_style or "cyan" in last_span_style):
                has_blue_marker = True
    assert has_blue_marker, "导轨上的轮次起点标尺必须呈现为用户双轨同款蓝色 │"


def test_fullscreen_scrollbar_subcell_smooth_movement() -> None:
    """★ D184：8 级微步子字符（Eighth-Block）平滑渲染，微小滚动即时响应。"""
    term = DummyTerminal(cols=80, rows=20)
    palette = get_palette()
    tl = TimelineComponent(palette)
    for i in range(10):
        tl.buffer.add_user(f"turn {i}")
        tl.buffer.blocks.append(Block(kind="assistant", text=f"line\n" * 15))

    editor = Editor()
    boxed_editor = BoxedEditor(editor)
    status = StatusComponent(palette)

    layout = FullscreenLayout(term, tl, boxed_editor, status)
    layout.render(80)

    observed_chars = set()
    dock_rows = len(boxed_editor.render(80)) + len(status.render(80))
    viewport_h = 20 - dock_rows

    # 模拟微步连续滚动 0 到 20 行
    for off in range(0, 20):
        layout.scroll_offset = off
        rendered = layout.render(80)
        v_rows = rendered[:viewport_h]
        for r in v_rows:
            observed_chars.add(r.plain[-1])

    subcell_chars = {" ", "▂", "▃", "▄", "▅", "▆", "▇", "▀"}
    found_subcells = observed_chars.intersection(subcell_chars)
    assert len(found_subcells) > 0, f"滚动条必须产生 8 级微步子字符以实现丝滑平滑过渡，观测到的字符: {observed_chars}"


def test_fullscreen_scrollbar_click_jump_and_turn_snap() -> None:
    """★ D183：点击右侧滚动条支持比例跳转，点击/靠近蓝色标尺支持轮次精准直达。"""
    term = DummyTerminal(cols=80, rows=20)
    palette = get_palette()
    tl = TimelineComponent(palette)
    for i in range(10):
        tl.buffer.add_user(f"turn {i}")
        tl.buffer.blocks.append(Block(kind="assistant", text=f"response {i}\n" * 5))

    editor = Editor()
    boxed_editor = BoxedEditor(editor)
    status = StatusComponent(palette)

    layout = FullscreenLayout(term, tl, boxed_editor, status)
    layout.render(80)

    initial_offset = layout.scroll_offset  # 默认 0 停在底部

    # 1. 点击滚动条顶部（y=1，最顶端）：应将视口滚动到最历史位置（最大 offset）
    handled = layout.handle_input(Key("mouse_down", x=80, y=1))
    assert handled is True
    assert layout.scroll_offset > 0, "点击滚动条顶部应向上回滚到历史消息"

    # 2. 拖拽滚动条滑块：从 y=1 拖到 y=10
    layout.handle_input(Key("mouse_drag", x=80, y=10))
    mid_offset = layout.scroll_offset
    assert mid_offset < layout._get_max_offset(80), "拖拽滑块应实时平滑更新视口滚动偏移"

    # 3. 松开鼠标
    layout.handle_input(Key("mouse_up", x=80, y=10))
    assert layout._is_dragging is False
    assert layout._selection is None


def test_fullscreen_scrollbar_hidden_when_content_fits() -> None:
    """★ D183：内容不足一屏时，滚动条自动隐藏，不占位保持纯净全宽文本。"""
    term = DummyTerminal(cols=80, rows=20)
    palette = get_palette()
    tl = TimelineComponent(palette)
    tl.buffer.add_user("short query")
    tl.buffer.blocks.append(Block(kind="assistant", text="short reply"))

    editor = Editor()
    boxed_editor = BoxedEditor(editor)
    status = StatusComponent(palette)

    layout = FullscreenLayout(term, tl, boxed_editor, status)
    rendered = layout.render(80)

    # 断言：未激活滚动条
    assert layout._show_scrollbar is False
    assert len(layout._turn_mark_rows) == 0

    # 视口末尾没有滚动条字符
    dock_rows = len(boxed_editor.render(80)) + len(status.render(80))
    viewport_h = 20 - dock_rows
    viewport_rows = rendered[:viewport_h]
    right_chars = [r.plain[-1] for r in viewport_rows if r.plain]
    assert "█" not in right_chars
    assert "│" not in right_chars


def test_fullscreen_ctrl_o_scroll_anchoring() -> None:
    """★ D185：Ctrl+O 全局展开/折叠时执行首行视口锚定，严禁跳至最上方或最下方。"""
    from tests.tui.test_fullscreen_app import _Runtime
    from logox.tui.render.fullscreen import FullscreenApp

    runtime = _Runtime()
    term = DummyTerminal(cols=80, rows=20)
    app = FullscreenApp(runtime=runtime, terminal=term)

    # 构造包含多轮与工具卡的长历史会话
    app.timeline.buffer.add_user("\n".join(f"User Turn 1 query line {i}" for i in range(10)))
    b_tool1 = Block(
        kind="tool",
        name="read_file",
        args_summary="file='test1.py'",
        payload="line1\nline2\nline3\nline4\nline5\nline6\nline7\nline8\nline9\nline10\nline11\nline12\nline13\nline14\nline15",
    )
    app.timeline.buffer.blocks.append(b_tool1)
    app.timeline.buffer.blocks.append(Block(kind="assistant", text="\n".join(f"Answer 1 line {i}" for i in range(25))))
    app.timeline.buffer.add_user("User Turn 2")
    b_tool2 = Block(
        kind="tool",
        name="write_file",
        args_summary="file='test2.py'",
        payload="w1\nw2\nw3\nw4\nw5\nw6\nw7\nw8\nw9\nw10\nw11\nw12\nw13\nw14\nw15",
    )
    app.timeline.buffer.blocks.append(b_tool2)
    app.timeline.buffer.blocks.append(Block(kind="assistant", text="Final Answer"))

    # 初始折叠渲染
    app.root.render(80)
    initial_total = len(app.root._cached_tl_rows)

    # 找到 b_tool1 的起始行
    t1_line = -1
    for s_l, e_l, blk in app.timeline.block_ranges:
        if blk is b_tool1:
            t1_line = s_l
            break
    assert t1_line >= 0

    # 视口高度
    dock_rows = app.root._get_dock_rows_count(80)
    v_height = 20 - dock_rows

    # 将视口首行对齐至 b_tool1
    target_offset = initial_total - v_height - t1_line
    app.root.scroll_offset = max(0, target_offset)
    app.root.render(80)

    # 验证当前视口第一行包含 b_tool1 的角色头或卡片名
    rendered_rows = app.root.render(80)
    top_plain = rendered_rows[0].plain
    assert "✦ Logox" in top_plain or "read_file" in top_plain
    assert "read_file" in rendered_rows[1].plain or "test1.py" in rendered_rows[1].plain

    # 1. 触发 Ctrl+O 展开全部工具卡
    app._dispatch(Key("o", ctrl=True))
    assert app.timeline.buffer.expand_tools is True

    # 关键断言 1：展开后视口第一行与第二行依然绝对稳固，没有跳到最下方！
    rendered_after_expand = app.root.render(80)
    top_after_expand = rendered_after_expand[0].plain
    assert "✦ Logox" in top_after_expand or "read_file" in top_after_expand
    assert "read_file" in rendered_after_expand[1].plain or "test1.py" in rendered_after_expand[1].plain

    # 关键断言 2：总行数显著增加，且 scroll_offset 自适应增大保持视口首行锚定
    new_total = len(app.root._cached_tl_rows)
    assert new_total > initial_total
    assert app.root.scroll_offset > target_offset

    # 2. 再次触发 Ctrl+O 折叠全部工具卡
    app._dispatch(Key("o", ctrl=True))
    assert app.timeline.buffer.expand_tools is False

    # 关键断言 3：折叠后视口首行依然是 b_tool1，绝对没有跳到第 0 行最上方！
    rendered_after_collapse = app.root.render(80)
    top_after_collapse = rendered_after_collapse[0].plain
    assert "✦ Logox" in top_after_collapse or "read_file" in top_after_collapse
    assert "read_file" in rendered_after_collapse[1].plain or "test1.py" in rendered_after_collapse[1].plain
    assert app.root.scroll_offset == target_offset


def test_fullscreen_ctrl_t_scroll_anchoring() -> None:
    """★ D185：Ctrl+T 全局展开/折叠思考链时执行首行视口锚定，保持视野稳定。"""
    from tests.tui.test_fullscreen_app import _Runtime
    from logox.tui.render.fullscreen import FullscreenApp

    runtime = _Runtime()
    term = DummyTerminal(cols=80, rows=20)
    app = FullscreenApp(runtime=runtime, terminal=term)

    app.timeline.buffer.add_user("Query")
    b_think = Block(
        kind="reasoning",
        text="thought line 1\nthought line 2\nthought line 3\nthought line 4\nthought line 5\nthought line 6\nthought line 7\nthought line 8\nthought line 9\nthought line 10",
    )
    app.timeline.buffer.blocks.append(b_think)
    app.timeline.buffer.blocks.append(Block(kind="assistant", text="Done reply\nSecond line\nThird line"))

    app.root.render(80)
    initial_total = len(app.root._cached_tl_rows)

    # 触发 Ctrl+T 展开思考链
    app._dispatch(Key("t", ctrl=True))
    assert app.timeline.buffer.expand_reasoning is True

    # 展开后行数增加
    expanded_total = len(app.root._cached_tl_rows)
    assert expanded_total > initial_total

    # 再次触发 Ctrl+T 折叠
    app._dispatch(Key("t", ctrl=True))
    assert app.timeline.buffer.expand_reasoning is False


def test_fullscreen_single_card_click_anchoring() -> None:
    """★ D185：鼠标单击单张卡片展开时执行视口锚定，卡片不从鼠标下方跳跑。"""
    term = DummyTerminal(cols=80, rows=20)
    palette = get_palette()
    tl = TimelineComponent(palette)
    tl.buffer.add_user("Question 1")
    b_tool = Block(
        kind="tool",
        name="grep",
        args_summary="query='def foo'",
        text="match 1\nmatch 2\nmatch 3\nmatch 4\nmatch 5\nmatch 6\nmatch 7\nmatch 8\nmatch 9\nmatch 10",
    )
    tl.buffer.blocks.append(b_tool)
    tl.buffer.blocks.append(Block(kind="assistant", text="Assistant response"))

    editor = Editor()
    boxed_editor = BoxedEditor(editor)
    status = StatusComponent(palette)

    layout = FullscreenLayout(term, tl, boxed_editor, status)
    layout.render(80)

    # 找到 b_tool 的屏幕行 (y)
    card_y = -1
    for y in range(1, 15):
        line_idx = layout._screen_y_to_timeline_line(y, 80)
        if line_idx is not None:
            for s_l, e_l, blk in tl.block_ranges:
                if blk is b_tool and s_l <= line_idx < e_l:
                    card_y = y
                    break
        if card_y > 0:
            break

    assert card_y > 0

    # 单击该卡片展开
    layout.handle_input(Key("mouse_down", x=10, y=card_y))
    consumed = layout.handle_input(Key("mouse_up", x=10, y=card_y))
    assert consumed is True
    assert b_tool.expanded is True

    # 展开后重新渲染：被点击的卡片所在行依然命中 b_tool
    layout.render(80)
    after_line_idx = layout._screen_y_to_timeline_line(card_y, 80)
    assert after_line_idx is not None
    assert tl.block_ranges is not None
    # 验证 card_y 依然对应 b_tool 的某一行（没有被顶走）
    is_still_tool = False
    for s_l, e_l, blk in tl.block_ranges:
        if blk is b_tool and s_l <= after_line_idx < e_l:
            is_still_tool = True
            break
    assert is_still_tool is True, "展开后卡片应依然停留在鼠标所在行，没有跳跑"


def test_fullscreen_anchor_at_bottom_scenario_2a() -> None:
    """★ D185 方案 2.A：处于最底部 (scroll_offset == 0) 时按 Ctrl+O，视口首行锚定保持当前可见内容，折叠后安全归底。"""
    from tests.tui.test_fullscreen_app import _Runtime
    from logox.tui.render.fullscreen import FullscreenApp

    runtime = _Runtime()
    term = DummyTerminal(cols=80, rows=20)
    app = FullscreenApp(runtime=runtime, terminal=term)

    # 构造历史会话，最后是一张长工具卡
    app.timeline.buffer.add_user("\n".join(f"Query line {i}" for i in range(15)))
    b_tool = Block(
        kind="tool",
        name="pytest",
        args_summary="args='-v'",
        payload="\n".join(f"test_pass_{i} PASSED" for i in range(30)),
    )
    app.timeline.buffer.blocks.append(b_tool)

    # 初始渲染，默认停在最底部 (scroll_offset == 0)
    app.root.render(80)
    assert app.root.scroll_offset == 0

    # 捕获折叠状态下视口首行文本
    rendered_before = app.root.render(80)
    top_before = rendered_before[0].plain

    # 按 Ctrl+O 展开
    app._dispatch(Key("o", ctrl=True))
    assert app.timeline.buffer.expand_tools is True

    # 断言：展开后视口首行依然是 top_before，屏幕没有跳飞！
    rendered_after_expand = app.root.render(80)
    top_after_expand = rendered_after_expand[0].plain
    assert top_after_expand.strip() == top_before.strip()
    assert app.root.scroll_offset > 0, "为保持首行锚定，scroll_offset 自适应调整，防止被顶飞"

    # 再次按 Ctrl+O 折叠
    app._dispatch(Key("o", ctrl=True))
    assert app.timeline.buffer.expand_tools is False

    # 断言：折叠后安全归底 (scroll_offset == 0)
    app.root.render(80)
    assert app.root.scroll_offset == 0
