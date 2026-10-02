"""差分渲染器的测试（D80 / MODULE_tui_render §4）。

这个文件守的是"流畅"与"不闪"两件事，而它们都可以用 `FakeTerminal` 精确断言：

* **流畅** = "内容没变时**一个字节都不写**" + "只有尾部变化时只重画尾部"
* **不闪** = "每一帧的字节都成对包在同步输出序列里"
* **不错位** = "交给终端的每一行都不超宽"（超宽会让其下所有行错位）

⚠️ 这些断言都是**字节级**的。这正是 `FakeTerminal` 存在的理由——
不用真终端、不用掐表，也能证明渲染策略是对的。
"""

from __future__ import annotations

import unittest

from rich.text import Text

from logox.tui.render.ansi import visible_width
from logox.tui.render.component import Container, Spacer
from logox.tui.render.components.editor import Editor
from logox.tui.render.components.text import CURSOR_MARKER, Box, TextComponent
from logox.tui.render.keys import Key
from logox.tui.render.screen import (
    CLEAR_ALL,
    SYNC_BEGIN,
    SYNC_END,
    Screen,
)
from logox.tui.render.terminal import FakeTerminal


def _screen(columns: int = 40, rows: int = 10) -> tuple[Screen, FakeTerminal]:
    terminal = FakeTerminal(columns=columns, rows=rows)
    screen = Screen(terminal)
    screen.request_render(force=True)
    terminal.reset()
    return screen, terminal


class FirstFrameTests(unittest.TestCase):
    def test_first_frame_prints_without_clearing(self) -> None:
        """首帧不清屏（假定屏幕干净）——清屏会造成一次可见的闪。"""
        screen, terminal = _screen()
        screen.add(TextComponent("第一行"))
        screen.request_render(force=True)
        self.assertIn("第一行", terminal.output)
        self.assertNotIn(CLEAR_ALL, terminal.output, "首帧不该清屏")

    def test_frame_is_wrapped_in_sync_output(self) -> None:
        """★ 不闪的关键：整帧包在 ``CSI ?2026h ... l`` 之间。"""
        screen, terminal = _screen()
        screen.add(TextComponent("x"))
        screen.request_render(force=True)
        frame = terminal.last_frame()
        self.assertTrue(frame.startswith(SYNC_BEGIN), f"帧没有以同步开始：{frame[:20]!r}")
        self.assertTrue(frame.endswith(SYNC_END), f"帧没有以同步结束：{frame[-20:]!r}")
        self.assertEqual(terminal.sync_depth, 0, "同步输出没有成对")


class DiffTests(unittest.TestCase):
    """只重画变化的部分 —— "流畅"的核心。"""

    def test_unchanged_content_writes_nothing(self) -> None:
        """★ **内容没变就一个字节都不写**。

        这是"流畅"最直接的体现：定时器可以随便触发，
        只要内容没变，终端就完全不受打扰。
        """
        screen, terminal = _screen()
        screen.add(TextComponent("不变的内容"))
        screen.request_render(force=True)
        before = terminal.output
        for _ in range(5):
            screen.request_render(force=True)
        self.assertEqual(terminal.output, before, "内容没变却又写了一次")

    def test_appending_only_redraws_the_tail(self) -> None:
        """★ 尾部追加时，**前面的行不该重新写**。

        这条用"写出的字节里不含前面那些行的内容"来断言——
        比掐表可靠，也比看截图可靠。
        """
        container = Container()
        for index in range(8):
            container.add(TextComponent(f"固定的第 {index} 行"))
        screen, terminal = _screen()
        screen.add(container)
        screen.request_render(force=True)

        terminal.reset()
        container.add(TextComponent("新追加的一行"))
        screen.request_render(force=True)

        frame = terminal.last_frame()
        self.assertIn("新追加的一行", frame)
        self.assertNotIn("固定的第 0 行", frame, "前面的行被无谓地重画了")

    def test_changing_a_middle_line_redraws_from_there(self) -> None:
        container = Container()
        first = TextComponent("第一行")
        middle = TextComponent("中间行")
        container.add(first)
        container.add(middle)
        container.add(TextComponent("第三行"))

        screen, terminal = _screen()
        screen.add(container)
        screen.request_render(force=True)

        terminal.reset()
        middle.set_text("中间行改了")
        screen.request_render(force=True)

        frame = terminal.last_frame()
        self.assertIn("中间行改了", frame)
        self.assertNotIn("第一行", frame, "变化之前的行不该重画")

    def test_shrinking_content_does_not_wipe_scrollback(self) -> None:
        """内容变短时**擦掉多余的行**，但不整屏重画。

        为什么这条比"变短就清屏"更好：``CLEAR_ALL`` 里的 ``ESC[3J`` 会
        **连回滚缓冲一起清掉**——用户往上滚就什么都没有了。
        主屏方案的意义正是"历史归终端管"，所以变短要走"逐行擦除"这条路。
        屏幕到底长什么样由 `test_render_fidelity.py` 的模拟器负责校验。
        """
        container = Container()
        for index in range(6):
            container.add(TextComponent(f"第 {index} 行"))
        screen, terminal = _screen()
        screen.add(container)
        screen.request_render(force=True)

        terminal.reset()
        container.clear()
        container.add(TextComponent("只剩一行"))
        screen.request_render(force=True)
        self.assertNotIn(CLEAR_ALL, terminal.output, "变短不该清屏（那会连回滚缓冲一起清掉）")
        self.assertIn("\x1b[2K", terminal.output, "多余的行必须被逐行擦掉")

    def test_incremental_shrink_clears_lines_without_crlf_scroll(self) -> None:
        """当行内被改写且总行数变短（如动子消失、文本合并），走 _write_incremental 时不能输出 \\r\\n 擦除多余行。"""
        line0 = TextComponent("第 0 行")
        line1 = TextComponent("第 1 行旧")
        line2 = TextComponent("第 2 行")
        container = Container()
        container.add(line0)
        container.add(line1)
        container.add(line2)

        screen, terminal = _screen()
        screen.add(container)
        screen.request_render(force=True)

        terminal.reset()
        # 改写第 1 行并删掉第 2 行：触发 first = 1 < len(new_lines) = 2, shrunk = True
        line1.set_text("第 1 行新")
        container.remove(line2)
        screen.request_render(force=True)

        frame = terminal.last_frame()
        self.assertIn("第 1 行新", frame)
        self.assertIn("\x1b[2K", frame, "多余的行必须被擦除")
        self.assertIn("\x1b[1B", frame, "必须使用下移光标而不是 \\r\\n 清理行，避免底行物理上滚")
        self.assertIn("\x1b[1A", frame, "清理后光标必须上移回内容末尾")

    def test_width_change_forces_full_redraw(self) -> None:
        """宽度变了，折行位置全变，无法增量。"""
        screen, terminal = _screen(columns=40)
        screen.add(TextComponent("一些内容"))
        screen.request_render(force=True)

        terminal.reset()
        terminal.resize(60, 10)
        screen.request_render(force=True)
        self.assertIn("\x1b[H\x1b[0J", terminal.output)
        self.assertNotIn("\x1b[3J", terminal.output)


class WidthGuardTests(unittest.TestCase):
    """渲染器的**最后一道防线**：交给终端的行必须不超宽。"""

    def test_overwide_component_output_is_clipped(self) -> None:
        """故意造一个"不守规矩"的组件，渲染器必须兜住。"""

        class Rogue:
            def render(self, width: int) -> list[Text]:
                return [Text("x" * (width * 3))]  # 严重超宽

            def handle_input(self, key: Key) -> bool:
                return False

            def invalidate(self) -> None:
                return None

        screen, _terminal = _screen(columns=30)
        screen.add(Rogue())
        screen.request_render(force=True)
        screen.assert_fits(30)  # 不抛异常即通过

    def test_cjk_is_measured_in_cells(self) -> None:
        """中文占 2 格：按字符数算会低估一半宽度。"""
        screen, _terminal = _screen(columns=21)
        screen.add(TextComponent("中文" * 20))
        screen.request_render(force=True)
        screen.assert_fits(21)


class OverlayTests(unittest.TestCase):
    """浮层合成（替代 Textual 的 CSS 定位）。"""

    def test_overlay_is_composited_over_content(self) -> None:
        screen, _terminal = _screen(columns=40, rows=10)
        screen.add(TextComponent("背景内容"))
        dialog = Box(padding_x=1, padding_y=0, background="#111111")
        dialog.add(TextComponent("弹窗"))
        screen.show_overlay(dialog, width=20, max_height=3)
        screen.request_render(force=True)
        self.assertIn("弹窗", screen.frame_text())

    def test_overlay_hidden_by_visible_predicate(self) -> None:
        """``visible`` 是**回调**，每帧调用——用于"窄屏时自动隐藏"。"""
        screen, _terminal = _screen(columns=40, rows=10)
        screen.add(TextComponent("背景"))
        screen.show_overlay(TextComponent("浮层"), visible=lambda w, h: w >= 100)
        screen.request_render(force=True)
        self.assertNotIn("浮层", screen.frame_text(), "窄屏时浮层应当隐藏")

    def test_hide_overlay_removes_it(self) -> None:
        screen, _terminal = _screen()
        screen.add(TextComponent("背景"))
        screen.show_overlay(TextComponent("浮层"))
        screen.request_render(force=True)
        self.assertTrue(screen.has_overlay())
        screen.hide_overlay()
        screen.request_render(force=True)
        self.assertFalse(screen.has_overlay())
        self.assertNotIn("浮层", screen.frame_text())

    def test_overlay_does_not_exceed_width(self) -> None:
        """浮层更危险：它被贴在**已经写好的行**上，超宽会同时破坏两侧。"""
        screen, _terminal = _screen(columns=30)
        screen.add(TextComponent("背景内容" * 5))
        screen.show_overlay(TextComponent("很长的浮层内容" * 6), width="90%")
        screen.request_render(force=True)
        screen.assert_fits(30)


class OverlayLineTests(unittest.TestCase):
    """浮层贴到含中文的行上时，必须**按 cell** 对齐（按字符切会错位）。"""

    def test_overlay_aligns_on_cjk_background(self) -> None:
        from logox.tui.render.screen import _drop_cells, _take_cells

        self.assertEqual(_take_cells("中文字", 4), "中文")
        self.assertEqual(_drop_cells("中文字", 4), "字")
        self.assertEqual(_take_cells("abcdef", 3), "abc")
        self.assertEqual(_drop_cells("abcdef", 3), "def")

    def test_take_cells_never_splits_a_wide_char(self) -> None:
        """取 3 格时不能把"中文"的"文"切一半（那样会留下半个乱码）。"""
        from logox.tui.render.screen import _take_cells

        self.assertEqual(_take_cells("中文", 3), "中")


class InputRoutingTests(unittest.TestCase):
    def test_handle_key_reaches_focused_component(self) -> None:
        received: list[Key] = []

        class Grabber:
            def render(self, width: int) -> list[Text]:
                return []

            def handle_input(self, key: Key) -> bool:
                received.append(key)
                return True

            def invalidate(self) -> None:
                return None

        screen, _terminal = _screen()
        grabber = Grabber()
        screen.set_focus(grabber)
        screen.handle_key(Key("a", char="a"))
        self.assertEqual(len(received), 1)

    def test_spacer_is_inert(self) -> None:
        spacer = Spacer(2)
        self.assertFalse(spacer.handle_input(Key("a", char="a")))


class StatsTests(unittest.TestCase):
    def test_stats_count_frames_and_redraws(self) -> None:
        screen, _terminal = _screen()
        screen.add(TextComponent("x"))
        screen.request_render(force=True)
        self.assertGreaterEqual(screen.stats["frames"], 1)
        self.assertGreaterEqual(screen.stats["full_redraws"], 1, "首帧应当算一次全量重画")


class ImeCursorTests(unittest.TestCase):
    """IME 光标：中文输入法的候选窗位置全靠它。

    编辑器**自己不画光标**（只发一个零宽标记），真正决定候选窗出现在哪里的
    是硬件光标。所以这里守两件事：标记被摘干净（不能写进终端），
    以及**位置算对**（算错的话候选窗会弹在屏幕角落）。
    """

    def _screen_with_editor(self, text: str = "ab", columns: int = 20) -> Screen:
        editor = Editor()
        editor.set_text(text)
        terminal = FakeTerminal(columns=columns, rows=10)
        screen = Screen(terminal)
        screen.add(editor)
        screen.set_focus(editor)
        return screen

    def test_marker_never_reaches_the_terminal(self) -> None:
        """标记是零宽 APC 序列，写出去会让终端显示出乱码。"""
        screen = self._screen_with_editor()
        screen.request_render(force=True)
        for data, _columns, _rows in screen.terminal.writes:  # type: ignore[attr-defined]
            self.assertNotIn(CURSOR_MARKER, data, "光标标记被写进了终端")

    def test_cursor_sits_at_the_text_end(self) -> None:
        """光标列 = 提示符宽度 + 已输入字符的**格数**（中文算 2 格）。

        D126 之后提示符宽度是 **0**（用户裁定去掉 `❯`），所以期望值就是文本本身的格宽。
        """
        screen = self._screen_with_editor("中文")
        screen.request_render(force=True)
        screen.assert_fits(20)
        self.assertIsNotNone(screen._cursor_pos)  # noqa: SLF001 - 测试内部状态
        row, col = screen._cursor_pos  # type: ignore[misc]  # noqa: SLF001
        self.assertEqual(row, len(screen._lines) - 1, "光标应当落在最后一行（输入框）")
        self.assertEqual(col, 0 + 4, "无提示符 + 两个中文字 4 格")

    def test_cursor_is_shown_with_the_hardware_cursor(self) -> None:
        """不显示硬件光标的话，用户会看到"输入框里没有光标"，以为卡住了。"""
        screen = self._screen_with_editor()
        screen.request_render(force=True)
        self.assertTrue(screen._cursor_visible)  # noqa: SLF001
        self.assertIn("\x1b[?25h", screen.terminal.output)  # type: ignore[attr-defined]

    def test_removing_the_marker_keeps_the_style(self) -> None:
        """摘标记时**必须保留样式**——否则打字的一瞬间输入框会突然变白。"""
        from logox.tui.render.screen import _remove_marker

        line = Text("红色字", style="#ff0000")
        marked = Text("红色", style="#ff0000")
        marked.append(CURSOR_MARKER)
        marked.append("字")
        cleaned = _remove_marker(marked, 2, len(CURSOR_MARKER))
        self.assertEqual(cleaned.plain, "红色字")
        self.assertEqual(visible_width(cleaned.plain), visible_width(line.plain))
        self.assertEqual(cleaned.style, "#ff0000", "基样式丢了")

    def test_span_across_the_marker_is_split(self) -> None:
        """跨过标记的 span 要拆成左右两段，不能整段丢掉。"""
        from logox.tui.render.screen import _remove_marker

        marked = Text("abc")
        marked.append(CURSOR_MARKER)
        marked.append("def")
        marked.stylize("#00ff00", 0, len(marked.plain))
        cleaned = _remove_marker(marked, 3, len(CURSOR_MARKER))
        self.assertEqual(cleaned.plain, "abcdef")
        self.assertEqual(len(cleaned.spans), 2, "跨标记的样式没有被拆开保留")


class DeferredRenderTests(unittest.TestCase):
    """节流只能**推迟**一帧，不能**丢掉**它。

    这一组来自用户报障：「prompt 窗口我无法一次输入多个字符，如果我同时输入了
    "你好"，它只会出现"你"，当我再输入"吗"，就会出现"你好吗"」。

    实测根因不是按键丢了（编辑器里三个字都在），而是**被节流挡下的那次重绘
    再也没有人补画**：`request_render()` 在 16ms 窗口内直接返回，
    而应用里没有任何定时器会回头再画一次。症状是"打快一点就会吞字"——
    而终端里的用户一定会打快。

    ⚠️ 这一组测试**故意把节流窗口调到 60 秒**（而不是用默认的 16ms）。
    为什么：默认窗口下"两次调用间隔 < 16ms"这个前提**是拿真实时钟去赌的**——
    这台机器上光是构造一个 ``TextComponent`` + ``FakeTerminal.reset()``
    就可能花掉几十毫秒（冷启动、GC、CI 上更慢），于是 ``request_render()``
    走的是"已经够久了，直接画"那条分支，测试就会以"没排进待办"的样子失败，
    而被测代码其实是对的。这正是本项目早就总结过的教训：
    **用时间做断言的测试，会在机器变慢时变成谎报**。
    把窗口放大到 60 秒，被测的那条分支就由**配置**决定，与机器快慢无关；
    同时延迟依然是个有界值（≤ 窗口长度），断言照样检查得到。
    """

    # 大到任何真实耗时都不可能跨过去的节流窗口（毫秒）。
    THROTTLED_MS = 60_000.0

    def _screen(self) -> tuple[Screen, FakeTerminal, list[tuple[float, object]]]:
        terminal = FakeTerminal(columns=40, rows=10)
        screen = Screen(terminal, min_interval_ms=self.THROTTLED_MS)
        scheduled: list[tuple[float, object]] = []
        # 假调度器：把"稍后再画"记下来，由测试决定什么时候真的触发
        screen.on_defer = lambda delay, callback: (
            scheduled.append((delay, callback)) or _FakeHandle()
        )
        screen.add(TextComponent("第一行"))
        screen.render_now()
        return screen, terminal, scheduled

    def test_a_throttled_request_is_rescheduled_not_dropped(self) -> None:
        """★ 16ms 内的第二次请求必须被**排进待办**。"""
        screen, terminal, scheduled = self._screen()
        terminal.reset()
        screen.add(TextComponent("第二行"))
        screen.request_render()  # 距离上一帧几乎 0ms → 会被节流

        self.assertEqual(len(scheduled), 1, "被节流的那一帧没有排进待办（就是吞字的根因）")
        delay, callback = scheduled[0]
        self.assertGreaterEqual(delay, 0.0)
        self.assertLessEqual(delay, screen.min_interval_ms / 1000.0)

        self.assertEqual(terminal.output, "", "节流窗口内不该立刻画")
        callback()  # 定时器到点
        self.assertIn("第二行", terminal.output, "补画时应当把这一帧画出来")

    def test_a_burst_of_keystrokes_paints_exactly_one_frame(self) -> None:
        """★★ **D133 的主用例**：一个输入批次里的 N 个按键 → **只画一帧**。

        这条直接钉住用户报的那个症状：按住 `a` 时终端一次会交上来一个批次
        （最多 32 条按键记录），旧语义下每个按键都 `force=True` 同步画一帧 ⇒
        32 帧；读线程被渲染拖住 ⇒ 控制台输入缓冲积压 ⇒ **松开手还在继续打**、
        退格**多删几个**、中文输入法一个字一个字蹦。
        """
        screen, terminal, scheduled = self._screen()
        terminal.reset()
        for index in range(32):
            screen.add(TextComponent(f"第 {index} 行"))
            screen.request_render(force=True)  # 每次按键都请求一帧（与按键路径一致）

        self.assertEqual(len(scheduled), 1, f"32 个按键排了 {len(scheduled)} 帧，应当只有 1 帧")
        self.assertEqual(terminal.output, "", "不该在调用栈里同步画")

        scheduled[0][1]()  # 下一轮兑现
        self.assertIn("第 31 行", terminal.output, "最后一帧应当反映全部按键的结果")

    def test_repeated_requests_do_not_queue_up(self) -> None:
        """连打十个字符只该排**一次**待办——排十次就是十次重绘。"""
        screen, _terminal, scheduled = self._screen()
        for index in range(10):
            screen.add(TextComponent(f"第 {index} 行"))
            screen.request_render()
        self.assertEqual(len(scheduled), 1)

    def test_force_render_promotes_the_pending_frame(self) -> None:
        """★ D133：`force` **不再多画一帧**，而是把排队中的那一帧**提前到延迟 0**。

        旧语义是"撤销待办 + 立刻同步画"；新语义下"立刻"= 排到事件循环的下一轮，
        所以做法变成"取消旧定时器（它带着一个更长的延迟）+ 用 0 延迟重排"。
        两种做法的**渲染次数都是 1**，但旧做法会在**调用栈里**画 —— 那正是
        "一次输入 32 个按键画 32 帧"的来源。
        """
        screen, _terminal, scheduled = self._screen()
        handle = _FakeHandle()
        screen.on_defer = lambda delay, callback: (scheduled.append((delay, callback)) or handle)
        screen.add(TextComponent("x"))
        screen.request_render()          # 被节流 → 排了一帧（延迟 > 0）
        self.assertEqual(len(scheduled), 1)
        screen.request_render(force=True)

        self.assertTrue(handle.cancelled, "强制重绘时旧定时器要撤掉（否则会画两帧）")
        self.assertEqual(len(scheduled), 2, "提前 = 取消旧帧 + 重排一帧")
        self.assertEqual(scheduled[1][0], 0.0, "重排的那一帧必须是'立刻'那一档")

    def test_without_a_scheduler_it_renders_immediately(self) -> None:
        """★ 没有调度器时**立刻画**——宁可多画一帧，也不能静默丢帧。

        同样把窗口放大：这样"立刻画"就**只能**是因为缺少调度器，
        不可能是因为恰好等够了 16ms（否则这条测试在某些机器上会
        因为走错分支而假装通过）。
        """
        terminal = FakeTerminal(columns=40, rows=10)
        screen = Screen(terminal, min_interval_ms=self.THROTTLED_MS)
        screen.add(TextComponent("第一行"))
        screen.render_now()
        terminal.reset()
        screen.add(TextComponent("第二行"))
        screen.request_render()  # on_defer 是 None
        self.assertIn("第二行", terminal.output, "没有调度器时丢了一帧")

    def test_stop_cancels_a_pending_render(self) -> None:
        """退出时撤掉待办：否则事件循环关闭后回调还会被触发。"""
        screen, _terminal, _scheduled = self._screen()
        handle = _FakeHandle()
        screen.on_defer = lambda delay, callback: handle
        screen.add(TextComponent("x"))
        screen.request_render()
        screen.stop()
        self.assertTrue(handle.cancelled)


class _FakeHandle:
    """``loop.call_later`` 返回的句柄的最小替身。"""

    def __init__(self) -> None:
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
