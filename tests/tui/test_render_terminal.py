"""终端抽象与 `FakeTerminal` 的测试（D80 / MODULE_tui_render §8.4）。

`Win32Terminal` / `PosixTerminal` **无法自动化测试**（需要真控制台），
所以这个文件测的是"`FakeTerminal` 本身是否忠实"，以及**协议形状**是否正确——
后者能挡住"忘记实现某个成员"这类错误（``runtime_checkable`` 只检查成员存在）。
"""

from __future__ import annotations

import unittest

from logox.tui.render.terminal import (
    FakeTerminal,
    Terminal,
    decode_input_records,
    make_terminal,
)


class FakeTerminalContractTests(unittest.TestCase):
    """`FakeTerminal` 必须满足 `Terminal` 协议，行为与真实驱动一致。"""

    def test_satisfies_protocol(self) -> None:
        self.assertIsInstance(FakeTerminal(), Terminal)

    def test_reports_size(self) -> None:
        terminal = FakeTerminal(columns=120, rows=40)
        self.assertEqual((terminal.columns, terminal.rows), (120, 40))

    def test_records_writes(self) -> None:
        terminal = FakeTerminal()
        terminal.write("hello")
        terminal.write(" world")
        self.assertEqual(terminal.output, "hello world")
        self.assertEqual(len(terminal.writes), 2)

    def test_records_size_at_write_time(self) -> None:
        """每次写都要记下**当时的**尺寸——尺寸变化是差分渲染的关键输入。"""
        terminal = FakeTerminal(columns=80, rows=24)
        terminal.write("a")
        terminal.resize(100, 30)
        terminal.write("b")
        self.assertEqual(terminal.writes[0][1:], (80, 24))
        self.assertEqual(terminal.writes[1][1:], (100, 30))

    def test_start_and_stop_are_tracked(self) -> None:
        terminal = FakeTerminal()
        terminal.start(lambda _data: None, lambda: None)
        self.assertTrue(terminal.started)
        terminal.stop()
        self.assertTrue(terminal.stopped)

    def test_input_is_forwarded_to_callback(self) -> None:
        received: list[str] = []
        terminal = FakeTerminal()
        terminal.start(received.append, lambda: None)
        terminal.send("\x1b[A")
        self.assertEqual(received, ["\x1b[A"])

    def test_resize_triggers_callback(self) -> None:
        calls: list[int] = []
        terminal = FakeTerminal()
        terminal.start(lambda _data: None, lambda: calls.append(1))
        terminal.resize(120, 30)
        self.assertEqual(len(calls), 1, "尺寸变化必须触发一次 resize 回调")

    def test_input_before_start_is_ignored_not_crashing(self) -> None:
        """还没 start 就收到输入：忽略，不能抛异常（驱动线程启动顺序存在竞态）。"""
        FakeTerminal().send("x")

    def test_clear_emits_the_expected_sequence(self) -> None:
        terminal = FakeTerminal()
        terminal.clear()
        self.assertEqual(terminal.output, "\x1b[H\x1b[0J")


class SyncOutputTrackingTests(unittest.TestCase):
    """同步输出（``CSI ?2026h/l``）的成对性 —— 不闪的关键。"""

    def test_sync_depth_balances(self) -> None:
        terminal = FakeTerminal()
        terminal.write("\x1b[?2026h")
        self.assertEqual(terminal.sync_depth, 1)
        terminal.write("\x1b[?2026l")
        self.assertEqual(terminal.sync_depth, 0)

    def test_last_frame_returns_content_after_the_last_begin(self) -> None:
        """`last_frame()` 让"只重画变化行"的断言可以精确到**这一帧的字节**。"""
        terminal = FakeTerminal()
        terminal.write("first frame")
        terminal.write("\x1b[?2026h")
        terminal.write("second")
        terminal.write("\x1b[?2026l")
        self.assertIn("second", terminal.last_frame())
        self.assertNotIn("first frame", terminal.last_frame())


class FactoryTests(unittest.TestCase):
    """`make_terminal()` 必须只**构造**，不在 import/构造期产生副作用。"""

    def test_returns_a_terminal_on_this_platform(self) -> None:
        terminal = make_terminal()
        # 只断言它满足协议；不去 start()（那会真的改控制台模式）
        for member in ("columns", "rows", "write", "start", "stop", "clear"):
            self.assertTrue(hasattr(terminal, member), f"终端缺少成员 {member}")


class Win32RecordLayoutTests(unittest.TestCase):
    """Windows 输入记录的结构体布局。

    为什么值得测：`Win32Terminal` 整体**无法自动化测试**（要真控制台），
    但"能不能读到按键"取决于一个 ctypes 结构体的**字节偏移**。
    偏移算错的症状是"按键变成乱码"或"按什么都没反应"——在真终端上极难定位。
    而结构体布局本身**不需要控制台就能验证**：构造一条记录，读回来看看。
    """

    def setUp(self) -> None:
        import sys

        if sys.platform != "win32":
            self.skipTest("Win32 结构体只在 Windows 上有意义")
        from logox.tui.render.terminal import Win32Terminal

        self.record_type = Win32Terminal._input_record_type()  # noqa: SLF001

    def test_input_record_is_twenty_bytes(self) -> None:
        """``sizeof(INPUT_RECORD)`` == 20：2 字节类型 + 2 字节对齐 + 16 字节联合体。

        算错的话 ``ReadConsoleInputW`` 会把一批记录**错位**解析，
        于是按键内容整个乱掉。
        """
        import ctypes

        self.assertEqual(ctypes.sizeof(self.record_type), 20)

    def test_key_event_fields_round_trip(self) -> None:
        record = self.record_type()
        record.EventType = 0x0001
        record.Event.KeyEvent.bKeyDown = 1
        record.Event.KeyEvent.UnicodeChar = ord("A")
        record.Event.KeyEvent.wVirtualKeyCode = 0x41
        self.assertEqual(record.Event.KeyEvent.UnicodeChar, ord("A"))
        self.assertEqual(record.Event.KeyEvent.wVirtualKeyCode, 0x41)
        self.assertEqual(record.EventType, 0x0001)


class DecodeInputRecordsTests(unittest.TestCase):
    """把一批输入记录解成字符——这是"按键能不能用"的最后一步。"""

    KEY_EVENT = 0x0001
    RESIZE_EVENT = 0x0004

    def _record(
        self,
        *,
        down: bool = True,
        char: str = "a",
        kind: int = KEY_EVENT,
        state: int = 0,
    ):
        record = _FakeRecord(kind, down, char, state)
        return record

    def _decode(self, records: list[object]) -> tuple[str, bool]:
        return decode_input_records(
            records, len(records), self.KEY_EVENT, self.RESIZE_EVENT
        )

    def test_typed_characters_are_joined(self) -> None:
        chars, resized = self._decode([self._record(char=c) for c in "abc"])
        self.assertEqual(chars, "abc")
        self.assertFalse(resized)

    def test_key_up_is_ignored(self) -> None:
        """★ 只要"按下"。不过滤的话每个键会被处理**两遍**（方向键会跳两格）。"""
        chars, _resized = self._decode([self._record(down=True, char="a"), self._record(down=False, char="a")])
        self.assertEqual(chars, "a")

    def test_function_keys_without_a_character_are_skipped(self) -> None:
        """功能键的记录里 ``UnicodeChar`` 是 0（真正的编码在 VT 序列里）。"""
        chars, _resized = self._decode([self._record(char="\x00")])
        self.assertEqual(chars, "")

    def test_resize_event_is_reported(self) -> None:
        chars, resized = self._decode([self._record(kind=self.RESIZE_EVENT)])
        self.assertEqual(chars, "")
        self.assertTrue(resized)

    def test_escape_sequence_arrives_as_separate_characters(self) -> None:
        """开了 VT 输入之后，方向键就是 ``ESC`` ``[`` ``A`` 三条字符记录。

        这正是"Windows 与 POSIX 只需一套按键解析"的原因。
        """
        chars, _resized = self._decode([self._record(char=c) for c in "\x1b[A"])
        self.assertEqual(chars, "\x1b[A")

    def test_only_the_first_count_records_are_read(self) -> None:
        """缓冲区里可能有上一轮的残留 —— 只能读 ``count`` 条。"""
        records = [self._record(char=c) for c in "abcdef"]
        chars, _resized = decode_input_records(records, 3, self.KEY_EVENT, self.RESIZE_EVENT)
        self.assertEqual(chars, "abc")


class ModifiedKeyTests(unittest.TestCase):
    """``dwControlKeyState`` → 修饰键编码（D127）。

    背景是一次用户实测：**Ctrl+Enter 还是把消息发了出去**。
    根因不在键位表（那一层早就有用例了），也不在解析层，而在**读控制台记录时
    把修饰键状态丢掉了**：``Ctrl+Enter`` 与 ``Enter`` 的 ``UnicodeChar`` 都是 ``\r``，
    而"按了 Ctrl"这件事只写在 ``dwControlKeyState`` 里。

    所以这一组用例钉的是**同一个字节在两种修饰下的不同结果** —— 而"两种输入必须
    产生不同输出"正是当初 ``Shift+Enter``（D124）与 ``Ctrl+Enter``（D126）两次
    修复都想要而没拿全的东西。
    """

    KEY_EVENT = 0x0001
    RESIZE_EVENT = 0x0004
    SHIFT = 0x0010
    CTRL = 0x0008  # LEFT_CTRL_PRESSED
    RIGHT_CTRL = 0x0004
    ALT = 0x0002  # LEFT_ALT_PRESSED

    def _decode(self, *records: object, shift_override: bool | None = None) -> str:
        chars, _resized = decode_input_records(
            list(records),
            len(records),
            self.KEY_EVENT,
            self.RESIZE_EVENT,
            shift_override=shift_override,
        )
        return chars

    def _record(self, char: str, state: int = 0) -> object:
        return _FakeRecord(self.KEY_EVENT, True, char, state)

    def _keys(self, *records: object, shift_override: bool | None = None) -> list[object]:
        """**端到端**：控制台记录 → 字符 → 有状态解析器 → 按键。

        为什么要一路到 ``KeyParser``：中间任何一环断掉，用户看到的都是
        "Ctrl+Enter 把消息发出去了"，而单看某一层是查不出来的。
        """
        from logox.tui.render.keys import KeyParser

        return KeyParser().feed(self._decode(*records, shift_override=shift_override))

    # -- 核心：Enter 家族的四个修饰组合 ---------------------------------- #

    def test_ctrl_enter_is_no_longer_a_plain_enter(self) -> None:
        """★ 用户报的那一条：**Ctrl+Enter 必须不是 Enter**。"""
        from logox.tui.render.keys import Key

        self.assertEqual(self._keys(self._record("\r", self.CTRL)), [Key("enter", ctrl=True)])
        self.assertEqual(self._keys(self._record("\r", self.RIGHT_CTRL)), [Key("enter", ctrl=True)])

    def test_shift_enter_is_no_longer_a_plain_enter(self) -> None:
        """D124 的键位表在 Windows 上真正生效的前提 —— 字符层必须先把两者分开。"""
        from logox.tui.render.keys import Key

        self.assertEqual(self._keys(self._record("\r", self.SHIFT)), [Key("enter", shift=True)])

    def test_native_shift_detection_when_conpty_strips_modifiers(self) -> None:
        """★ CHANGE-054：对齐 Pi 的原生 Shift 探查。

        Windows Terminal / ConPTY 会把 Shift+Enter 的 dwControlKeyState 置零并只发 \\r。
        当物理 Shift 探查结果为 True 时，必须将其升级为 Shift+Enter 换行；
        当物理 Shift 探查结果为 False 时，仍作为普通 Enter 提交。
        """
        from logox.tui.render.keys import Key

        # state=0（ConPTY 剥离修饰位现象），但物理 Shift 正被按住
        self.assertEqual(
            self._keys(self._record("\r", state=0), shift_override=True),
            [Key("enter", shift=True)],
        )
        # state=0，物理 Shift 未按住 -> 保持普通 Enter 提交
        self.assertEqual(
            self._keys(self._record("\r", state=0), shift_override=False),
            [Key("enter")],
        )

    def test_alt_enter_is_no_longer_a_plain_enter(self) -> None:
        """Alt+Enter 有时是控制台翻译好的 ``ESC`` + ``\r``，有时只是一条 ``\r`` + Alt 位。

        两条路都得到 **一个** ``Alt+Enter``，不多不少 ——
        多出来的那一个就是"按一次换两行"，属于"修好了但这个更难受"。
        """
        from logox.tui.render.keys import Key

        self.assertEqual(self._keys(self._record("\r", self.ALT)), [Key("enter", alt=True)])
        # 控制台已经发了 ESC 前缀，且两条记录都带 Alt 位 → 仍然只算一个键
        self.assertEqual(
            self._keys(self._record("\x1b", self.ALT), self._record("\r", self.ALT)),
            [Key("enter", alt=True)],
        )

    def test_ctrl_j_is_left_to_the_line_feed_rule(self) -> None:
        """``Ctrl+J`` 的字符形态就是 LF —— 因此不需要为它额外造一条编码规则。

        （D128 把"单独的 LF"定成了换行，所以 ``Ctrl+J`` 与 ``Ctrl+Enter`` 在
        字符层是同一样东西，而在消费者侧都指向换行。）
        """
        from logox.tui.render.keys import Key

        self.assertEqual(self._decode(self._record("\n", self.CTRL)), "\n")
        self.assertEqual(self._keys(self._record("\n", self.CTRL)), [Key("enter", ctrl=True)])

    # -- 逆向守卫：没有修饰时一个字都不该变 -------------------------------- #

    def test_plain_enter_is_still_a_plain_enter(self) -> None:
        """★ **反向守卫**：修 Ctrl+Enter 时不能把普通 Enter 弄坏（那是最常用的键）。"""
        from logox.tui.render.keys import Key

        self.assertEqual(self._decode(self._record("\r")), "\r")
        self.assertEqual(self._keys(self._record("\r")), [Key("enter")])

    def test_a_bare_line_feed_is_a_newline_not_a_submit(self) -> None:
        """★ **用户报的那一条的最终形态**（D128）：单独一个 LF = **换行**。

        探针实测（``.smoke/probe_win32_enter.py``）：Windows Terminal 上
        ``Ctrl+Enter`` 发的就是 LF，而老映射把 LF 当 Enter → **消息被发了出去**。
        参考实现 Pi 把单独的 LF 放在**换行**分支里（``editor.js``），逐字节一致。

        ⚠️ 代价：若某终端把 **Enter 键**发成 LF，那种终端上要用 ``Ctrl+M``（发 CR）发送。
        """
        self.assertEqual(self._decode(self._record("\n")), "\n")  # 字符层不改写
        newline = self._keys(self._record("\n"))
        self.assertEqual(len(newline), 1)
        self.assertEqual((newline[0].name, newline[0].ctrl), ("enter", True))

    def test_ctrl_c_is_left_alone(self) -> None:
        """``Ctrl+C`` 现在是 ``\x03``，中断逻辑就靠它。别把它卷进编码里。"""
        self.assertEqual(self._decode(self._record("\x03", self.CTRL)), "\x03")

    def test_shifted_arrow_sequence_is_not_double_encoded(self) -> None:
        """★★ **最容易被搞坏的一条**：开了 VT 输入后，``Shift+↑`` 是控制台
        **已经翻译好**的六条字符记录（``ESC`` ``[`` ``1`` ``;`` ``2`` ``A``），
        而那六条记录上**同样**带着 Shift 位。

        如果按"带修饰就补编码"去处理，``Shift+↑`` 会变成一串垃圾字符
        （每个字符都被包一层 ``CSI 27;…~``）—— 这就是为什么本函数只认
        ``\r`` / ``\n`` 两个字符。
        """
        from logox.tui.render.keys import Key

        records = [self._record(c, self.SHIFT) for c in "\x1b[1;2A"]
        self.assertEqual(self._decode(*records), "\x1b[1;2A", "方向键序列被改写了")
        self.assertEqual(self._keys(*records), [Key("up", shift=True)])

    def test_every_record_that_isnt_a_modified_enter_passes_through(self) -> None:
        """回归网：一般字符带修饰位时也不能被改写。"""
        self.assertEqual(self._decode(self._record("a", self.CTRL)), "a")
        self.assertEqual(self._decode(self._record("A", self.SHIFT)), "A")


class _FakeRecord:
    """``INPUT_RECORD`` 的最小替身（只保留 `decode_input_records` 用到的字段）。"""

    def __init__(self, kind: int, down: bool, char: str, state: int = 0) -> None:
        self.EventType = kind
        self.Event = type("_Event", (), {"KeyEvent": _FakeKeyEvent(down, char, state)})()


class _FakeKeyEvent:
    def __init__(self, down: bool, char: str, state: int = 0) -> None:
        self.bKeyDown = down
        self.UnicodeChar = ord(char)
        #: ``dwControlKeyState``：**修饰键唯一的信息来源**（D127）
        self.dwControlKeyState = state


class ReaderThreadShutdownTests(unittest.TestCase):
    """退出时**不能在读线程里 join 自己**。

    ⚠️ 这不是理论问题：用户按 ``Ctrl+D`` 时，按键回调是在**读线程**里执行的，
    于是退出流程会从读线程走一遍 ``stop()``。
    ``Thread.join()`` 对当前线程会抛 ``RuntimeError``，而它抛出的位置正好在
    "恢复控制台模式"**之前**——结果是**用户的 shell 被留在原始模式**：
    没有回显、回车不换行。一个 bug 把用户的终端搞坏，是这里最坏的失败方式。
    """

    def test_joining_from_the_reader_thread_is_a_no_op(self) -> None:
        import threading

        from logox.tui.render.terminal import Win32Terminal

        terminal = Win32Terminal()
        terminal._reader = threading.current_thread()  # noqa: SLF001 - 模拟"从读线程退出"
        terminal._join_reader()  # noqa: SLF001 - 不抛异常即通过
        self.assertIsNone(terminal._reader)  # noqa: SLF001

    def test_joining_without_a_reader_is_safe(self) -> None:
        from logox.tui.render.terminal import Win32Terminal

        Win32Terminal()._join_reader()  # noqa: SLF001


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
