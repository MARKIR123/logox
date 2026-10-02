"""终端驱动（D80 / MODULE_tui_render §3.2）。

本模块是**整个重写里唯一与平台相关、且无法自动化测试**的部分。因此它的设计原则是
**做到最薄**：只负责"读原始字节 / 写字节 / 问尺寸 / 进出原始模式"四件事，
所有**逻辑**（按键解析、渲染差分、组件）都在别处，用 `FakeTerminal` 就能测。

三个实现
--------
============================ ==========================================================
``FakeTerminal``             **测试用**：把"写出去的字节"记在内存里。
                             于是"差分渲染只重画变化行"这类断言可以精确到字节序列。
``Win32Terminal``            生产（Windows）：``ctypes`` 调 Win32 控制台 API。
``PosixTerminal``            生产（Linux/macOS）：``termios`` + ``SIGWINCH``。
============================ ==========================================================

为什么 Windows 要开 ``ENABLE_VIRTUAL_TERMINAL_INPUT``
----------------------------------------------------
Windows 控制台默认把按键**先翻译成 Win32 输入记录**（``KEY_EVENT``），
我们拿到的是"虚拟键码"而不是终端字节流。打开这个标志后，控制台改为投递
**VT 序列**（``\\x1b[A`` 这种），与 Linux/macOS 完全一致——于是 `keys.py`
只需处理一种形态，**这也是 Pi 的做法**。

⚠️ 优雅降级：旧版 Windows（< 10 1511）不支持该标志。此时 ``SetConsoleMode`` 失败，
我们**不抛异常**，而是记一条警告并继续——最坏情况是方向键不工作，
但程序仍然能跑（比"启动就崩"好得多）。
"""

from __future__ import annotations

import contextlib
import sys
import threading
from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

from logox.tui.render.ansi import CLEAR_VIEWPORT

__all__ = [
    "FakeTerminal",
    "PosixTerminal",
    "Terminal",
    "Win32Terminal",
    "decode_input_records",
    "make_terminal",
]

#: 读线程每轮最多等多久（毫秒）。见 `Win32Terminal._read_loop` 的说明。
READ_POLL_MS = 20


@runtime_checkable
class Terminal(Protocol):
    """终端的能力（刻意只有 6 个成员）。"""

    @property
    def columns(self) -> int:
        """当前列数。"""
        ...

    @property
    def rows(self) -> int:
        """当前行数。"""
        ...

    def write(self, data: str) -> None:
        """写一段字节（**不自动 flush**，由调用方决定刷新时机）。"""
        ...

    def start(self, on_input: Callable[[str], None], on_resize: Callable[[], None]) -> None:
        """进入"应用模式"：进原始模式、启动读线程、注册 resize 回调。"""
        ...

    def stop(self) -> None:
        """退出应用模式并**恢复终端原状**（这一步绝不能漏）。"""
        ...

    def clear(self) -> None:
        """清屏并把光标归位。"""
        ...


# --------------------------------------------------------------------------- #
# 测试用实现
# --------------------------------------------------------------------------- #


class FakeTerminal:
    """**测试用**终端：把写出去的字节记下来，把输入交给我们自己投喂。

    为什么它是整个测试方案的核心：有了它，"渲染器只重画变化行"这种断言
    可以精确到**字节序列**，不需要真终端、也不需要掐表。
    """

    def __init__(self, *, columns: int = 80, rows: int = 24) -> None:
        self._columns = columns
        self._rows = rows
        #: 依次记录每一次 ``write`` 的内容**以及当时的尺寸**（尺寸变化要能看出）
        self.writes: list[tuple[str, int, int]] = []
        self.started = False
        self.stopped = False
        #: 与真实驱动同名：降级警告由上层显示给用户（见 `InlineApp._report_terminal_warnings`）
        self.warnings: list[str] = []
        self._on_input: Callable[[str], None] | None = None
        self._on_resize: Callable[[], None] | None = None
        #: 同步输出的开关状态（渲染器应当成对使用）
        self.sync_depth = 0

    @property
    def columns(self) -> int:
        return self._columns

    @property
    def rows(self) -> int:
        return self._rows

    def write(self, data: str) -> None:
        if data == "\x1b[?2026h":
            self.sync_depth += 1
        elif data == "\x1b[?2026l":
            self.sync_depth -= 1
        self.writes.append((data, self._columns, self._rows))

    def start(self, on_input: Callable[[str], None], on_resize: Callable[[], None]) -> None:
        self.started = True
        self._on_input = on_input
        self._on_resize = on_resize

    def stop(self) -> None:
        self.stopped = True

    def clear(self) -> None:
        self.write(CLEAR_VIEWPORT)

    # -- 测试辅助 ------------------------------------------------------- #

    @property
    def output(self) -> str:
        """到目前为止写出的**全部**内容拼起来（断言用）。"""
        return "".join(data for data, _, _ in self.writes)

    def last_frame(self) -> str:
        """最近一次"从上一次同步输出结束到现在"的内容。"""
        joined = self.output
        start = joined.rfind("\x1b[?2026h")
        return joined[start:] if start >= 0 else joined

    def resize(self, columns: int, rows: int) -> None:
        """模拟终端被改变尺寸，并触发回调（真实驱动由信号/事件触发）。"""
        self._columns, self._rows = columns, rows
        if self._on_resize is not None:
            self._on_resize()

    def send(self, data: str) -> None:
        """模拟用户按键（原始字节）。"""
        if self._on_input is not None:
            self._on_input(data)

    def reset(self) -> None:
        self.writes.clear()


# --------------------------------------------------------------------------- #
# Windows
# --------------------------------------------------------------------------- #


class Win32Terminal:
    """Windows 控制台驱动（``ctypes``，无第三方依赖）。

    ⚠️ **本类无法自动化测试**（需要真控制台）。因此它只做四件事，
    任何判断逻辑都不放在这里。手工验收清单见 `MODULE_tui_render.md` §8.4。

    输入是怎么读到的（这一段是"界面能不能用"的关键）
    ------------------------------------------------
    控制台不会像 POSIX 那样"往 stdin 里推字节"。要用
    ``ReadConsoleInputW`` 从**控制台输入缓冲区**取 *输入记录*。
    打开 ``ENABLE_VIRTUAL_TERMINAL_INPUT`` 之后，方向键在缓冲区里就是
    ``\\x1b`` ``[`` ``A`` 三个字符的记录——与 Linux/macOS 完全一致，
    于是 `keys.py` 只需要面对一种形态。

    为什么读线程要用 ``WaitForSingleObject`` 轮询、而不是直接阻塞在
    ``ReadConsoleInputW`` 上：阻塞式读取在 ``stop()`` 时**唤不醒**
    （线程会永远卡在系统调用里），于是要么留下一个僵死线程、
    要么得往输入缓冲区里塞一个假按键（那个假按键可能漏到用户的 shell 里）。
    等 50ms、超时就回头看一眼"还该不该活着"，两个问题都没有了。
    """

    #: 输入模式：``ENABLE_VIRTUAL_TERMINAL_INPUT`` 让方向键以 VT 序列形式到达
    _ENABLE_VIRTUAL_TERMINAL_INPUT = 0x0200
    #: 输出模式：``ENABLE_VIRTUAL_TERMINAL_PROCESSING`` 让 ANSI 转义被解释而不是打印出来
    _ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
    #: 输入模式：``ENABLE_PROCESSED_INPUT`` —— 关掉后 ``Ctrl+C`` 作为**按键**交给我们，
    #: 而不是变成操作系统的中断信号。这与 POSIX 的"原始模式"语义一致。
    _ENABLE_PROCESSED_INPUT = 0x0001
    #: 输入模式：``ENABLE_LINE_INPUT`` —— **必须关掉**（Win32 官方值为 0x0002），
    #: 否则输入被操作系统行缓冲与输入法防抖拦截，无法即时交出单字符。
    _ENABLE_LINE_INPUT = 0x0002
    #: 输入模式：``ENABLE_ECHO_INPUT`` —— **必须关掉**，否则用户每按一个键屏幕上都重复一遍
    _ENABLE_ECHO_INPUT = 0x0004
    #: 输入模式：``ENABLE_WINDOW_INPUT`` —— 开启后窗口尺寸变化会生成 WINDOW_BUFFER_SIZE_EVENT
    _ENABLE_WINDOW_INPUT = 0x0008
    #: 输入模式：``ENABLE_MOUSE_INPUT`` —— **必须关掉**，否则鼠标移动会产生大量 MOUSE_EVENT 挤占读线程
    _ENABLE_MOUSE_INPUT = 0x0010

    #: 输入记录类型（``INPUT_RECORD.EventType``）
    _KEY_EVENT = 0x0001
    _WINDOW_BUFFER_SIZE_EVENT = 0x0004

    #: 一次最多取多少条输入记录
    _RECORD_BATCH = 32

    def __init__(self) -> None:
        import ctypes

        self._ctypes = ctypes
        self._kernel32 = ctypes.windll.kernel32
        self._bind_api()
        self._stdin = self._kernel32.GetStdHandle(-10)  # STD_INPUT_HANDLE
        self._stdout = self._kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        self._saved_in: int | None = None
        self._saved_out: int | None = None
        #: 无法启用 VT 输入时的降级警告（界面启动后显示给用户）
        self.warnings: list[str] = []
        self._on_input: Callable[[str], None] | None = None
        self._on_resize: Callable[[], None] | None = None
        self._active = False
        self._reader: threading.Thread | None = None

    # -- ctypes 函数签名 ------------------------------------------------- #
    #
    # ⚠️ 为什么必须声明签名：不声明的话 ctypes 会把返回值当成 **32 位 int**，
    # 而 ``HANDLE`` 是**指针宽度**（64 位下 8 字节）。句柄值一旦被截断，
    # 后续所有控制台调用都会失败——而症状只是"什么都没发生"，极难定位。

    def _bind_api(self) -> None:
        ctypes = self._ctypes
        k32 = self._kernel32
        k32.GetStdHandle.restype = ctypes.c_void_p
        k32.GetStdHandle.argtypes = [ctypes.c_uint32]
        k32.GetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        k32.GetConsoleMode.restype = ctypes.c_int
        k32.SetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        k32.SetConsoleMode.restype = ctypes.c_int
        k32.GetConsoleScreenBufferInfo.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        k32.GetConsoleScreenBufferInfo.restype = ctypes.c_int
        k32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        k32.WaitForSingleObject.restype = ctypes.c_uint32
        k32.ReadConsoleInputW.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(self._input_record_type()),
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        k32.ReadConsoleInputW.restype = ctypes.c_int

    @classmethod
    def _input_record_type(cls) -> Any:
        """构造 ``INPUT_RECORD`` 的 ctypes 镜像（只需一次，构造在类上缓存）。

        ``INPUT_RECORD`` 是 ``{ WORD EventType; union {...} Event; }``，
        总长 20 字节（2 字节类型 + 2 字节对齐 + 16 字节联合体）。
        联合体里我们只关心 ``KEY_EVENT_RECORD``（16 字节）与
        ``WINDOW_BUFFER_SIZE_EVENT``——其余用一块 16 字节的裸内存占位即可。
        """
        import ctypes

        cached = getattr(cls, "_record_type_cache", None)
        if cached is not None:
            return cached

        class KeyEventRecord(ctypes.Structure):
            #: ``BOOL bKeyDown`` —— Win32 的 ``BOOL`` 是 4 字节，不是 1 字节
            _fields_ = [
                ("bKeyDown", ctypes.c_int32),
                ("wRepeatCount", ctypes.c_uint16),
                ("wVirtualKeyCode", ctypes.c_uint16),
                ("wVirtualScanCode", ctypes.c_uint16),
                #: ``WCHAR`` 是 2 字节；用 c_uint16 而不是 c_wchar（后者宽度随平台变）
                ("UnicodeChar", ctypes.c_uint16),
                ("dwControlKeyState", ctypes.c_uint32),
            ]

        class _Event(ctypes.Union):
            _fields_ = [
                ("KeyEvent", KeyEventRecord),
                ("_padding", ctypes.c_byte * 16),
            ]

        class InputRecord(ctypes.Structure):
            _fields_ = [("EventType", ctypes.c_uint16), ("Event", _Event)]

        cls._record_type_cache = InputRecord
        return InputRecord

    # -- 尺寸 ----------------------------------------------------------- #

    def _size(self) -> tuple[int, int]:
        class Coord(self._ctypes.Structure):  # type: ignore[misc]
            _fields_ = [("X", self._ctypes.c_short), ("Y", self._ctypes.c_short)]

        class Rect(self._ctypes.Structure):  # type: ignore[misc]
            _fields_ = [
                ("Left", self._ctypes.c_short),
                ("Top", self._ctypes.c_short),
                ("Right", self._ctypes.c_short),
                ("Bottom", self._ctypes.c_short),
            ]

        class Info(self._ctypes.Structure):  # type: ignore[misc]
            _fields_ = [
                ("dwSize", Coord),
                ("dwCursorPosition", Coord),
                ("wAttributes", self._ctypes.c_ushort),
                ("srWindow", Rect),
                ("dwMaximumWindowSize", Coord),
            ]

        info = Info()
        if not self._kernel32.GetConsoleScreenBufferInfo(self._stdout, self._ctypes.byref(info)):
            return 80, 24
        width = info.srWindow.Right - info.srWindow.Left + 1
        height = info.srWindow.Bottom - info.srWindow.Top + 1
        return max(1, width), max(1, height)

    @property
    def columns(self) -> int:
        return self._size()[0]

    @property
    def rows(self) -> int:
        return self._size()[1]

    # -- 读写 ----------------------------------------------------------- #

    def write(self, data: str) -> None:
        # 用 ``sys.stdout`` 而不是 ``WriteConsoleW``：前者已经处理了编码，
        # 而且与我们在别处（如 --version）的输出走同一条路。
        sys.stdout.write(data)
        sys.stdout.flush()

    def clear(self) -> None:
        self.write(CLEAR_VIEWPORT)

    # -- 模式 ----------------------------------------------------------- #

    def _read_mode(self, handle: Any) -> int | None:
        mode = self._ctypes.c_uint32()
        if not self._kernel32.GetConsoleMode(handle, self._ctypes.byref(mode)):
            return None
        return int(mode.value)

    def _write_mode(self, handle: Any, value: int) -> bool:
        return bool(self._kernel32.SetConsoleMode(handle, value))

    def _set_mode(self, handle: Any, flag: int, *, enable: bool) -> bool:
        mode = self._read_mode(handle)
        if mode is None:
            return False
        new_mode = (mode | flag) if enable else (mode & ~flag)
        return self._write_mode(handle, new_mode)

    def start(self, on_input: Callable[[str], None], on_resize: Callable[[], None]) -> None:
        self._on_input = on_input
        self._on_resize = on_resize
        self._saved_in = self._read_mode(self._stdin)
        self._saved_out = self._read_mode(self._stdout)

        if not self._set_mode(self._stdout, self._ENABLE_VIRTUAL_TERMINAL_PROCESSING, enable=True):
            # 旧版 Windows：ANSI 转义会被当作普通字符打印出来（屏幕上满屏 \x1b[）
            self.warnings.append(
                "本控制台不支持 ANSI 转义（需要 Windows 10 1511+）；"
                "界面可能无法正确显示，建议升级或改用 --chat 文本模式"
            )

        # 原始输入：彻底切入 Raw 模式，消除操作系统缓冲与干扰。
        #
        # ⚠️ 每一项都有一个用户可见的直接症状：
        # 关 ECHO_INPUT → 否则按键在屏幕上重复一遍；
        # 关 LINE_INPUT (0x0002) → 必须关掉！否则操作系统行缓冲与输入法防抖会产生严重击键延迟；
        # 关 PROCESSED_INPUT → 否则 Ctrl+C 由操作系统处理，我们的"中断"收不到；
        # 关 MOUSE_INPUT → 避免鼠标移动产生海量 MOUSE_EVENT 挤占读线程；
        # 开 WINDOW_INPUT → 保证窗口大小调整事件能够正常被读线程捕获；
        # 开 VIRTUAL_TERMINAL_INPUT → VT 序列由控制台直接解码投递。
        if self._saved_in is not None:
            raw = self._saved_in
            raw |= self._ENABLE_VIRTUAL_TERMINAL_INPUT
            raw |= self._ENABLE_WINDOW_INPUT
            raw &= ~self._ENABLE_ECHO_INPUT
            raw &= ~self._ENABLE_LINE_INPUT
            raw &= ~self._ENABLE_PROCESSED_INPUT
            raw &= ~self._ENABLE_MOUSE_INPUT
            if not self._write_mode(self._stdin, raw):
                self.warnings.append(
                    "本控制台不支持 VT 输入；方向键与功能键可能无法识别（字母与回车仍可用）"
                )

        self._active = True
        self.write("\x1b[?25l")  # 隐藏硬件光标（未定位到输入框前先藏起来，避免乱跳）
        self._reader = threading.Thread(target=self._read_loop, name="logox-input", daemon=True)
        self._reader.start()

    def stop(self) -> None:
        if not self._active:
            return
        self._active = False
        self._join_reader()
        self.write("\x1b[?25h")  # 恢复硬件光标 —— **漏掉这一步终端会"没有光标"**
        if self._saved_in is not None and self._stdin:
            self._write_mode(self._stdin, self._saved_in)
        if self._saved_out is not None and self._stdout:
            self._write_mode(self._stdout, self._saved_out)

    def _join_reader(self) -> None:
        """等读线程退出——但**不能从读线程自己调用**。

        ⚠️ 这不是理论问题：用户按 ``Ctrl+D`` 时，回调是在**读线程**里跑的，
        于是退出流程会走到这里。``Thread.join()`` 对当前线程会抛
        ``RuntimeError: cannot join current thread``，而它抛出的位置正好在
        "恢复控制台模式"**之前**——结果是**用户的 shell 被留在原始模式**：
        没有回显、回车不换行。一个 bug 把终端搞坏，是这里最坏的失败方式。
        """
        reader = self._reader
        if reader is None or reader is threading.current_thread():
            self._reader = None
            return
        reader.join(timeout=READ_POLL_MS / 1000.0 + 0.5)
        self._reader = None

    # -- 读线程 --------------------------------------------------------- #

    def _read_loop(self) -> None:
        """后台读按键，直到 :meth:`stop` 把 ``_active`` 置假。"""
        ctypes = self._ctypes
        handle = self._stdin
        if not handle:
            return
        record_type = self._input_record_type()
        records = (record_type * self._RECORD_BATCH)()
        count = ctypes.c_uint32()
        while self._active:
            # ``WAIT_OBJECT_0`` == 0；超时返回 258（``WAIT_TIMEOUT``）
            if self._kernel32.WaitForSingleObject(handle, READ_POLL_MS) != 0:
                continue
            if not self._kernel32.ReadConsoleInputW(
                handle, records, self._RECORD_BATCH, ctypes.byref(count)
            ):
                return  # 句柄不是控制台（例如 stdin 被重定向）——静默退出，不阻塞程序
            chars, resized = decode_input_records(
                records, count.value, self._KEY_EVENT, self._WINDOW_BUFFER_SIZE_EVENT
            )
            if chars and self._on_input is not None:
                self._on_input(chars)
            if resized and self._on_resize is not None:
                self._on_resize()
        return


#: Win32 ``dwControlKeyState`` 的修饰位（``wincon.h``）。
#:
#: ⚠️ 为什么必须读它：控制台把**修饰键状态放在这里**，而 ``UnicodeChar`` 只给
#: "不带修饰时那个字符"。于是 ``Ctrl+Enter`` 与 ``Enter`` 拿到的**都是** ``\r`` ——
#: 在 Windows 上"Ctrl+Enter 换行"曾经**必然退化成提交**（用户实测：消息发出去了）。
#: 注意左右各一位（左右 Alt / 左右 Ctrl 是四个不同的位），少读一半就是"按住左边那个有效、
#: 按住右边那个没反应"。
WIN32_SHIFT_PRESSED = 0x0010
WIN32_ALT_PRESSED = 0x0002 | 0x0001
WIN32_CTRL_PRESSED = 0x0008 | 0x0004


def _is_shift_pressed_native() -> bool:
    """探查物理键盘 Shift 键是否正处于按下状态。

    借鉴参考实现 Pi（`isNativeModifierPressed("shift")`）：
    Windows Terminal (ConPTY) 在传输 Shift+Enter 时会剥离修饰位，把 `dwControlKeyState` 置零并只发送 `\r`。
    当收到 `\r` 的瞬间通过 Win32 `GetAsyncKeyState(VK_SHIFT)` 异步探查物理按键状态，
    若用户正按住 Shift，则将其升级为 `Shift+Enter`。
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        # 0x10 == VK_SHIFT。最高位 (0x8000) 置 1 表示按键当前处于按压状态
        return bool(ctypes.windll.user32.GetAsyncKeyState(0x10) & 0x8000)
    except Exception:
        return False


def _encode_modified_key(char: str, state: int, *, shift_override: bool | None = None) -> str:
    """带修饰的按键 → xterm ``modifyOtherKeys`` 形式（``CSI 27;<修饰>;<码点>~``）。

    只对**字符本身无法表达修饰**的少数几个键补编码：

    ================= ============================== ==========================
    按下               控制台给的字符                  不补编码的后果
    ================= ============================== ==========================
    ``Ctrl+Enter``    ``\r``（与 Enter 完全相同）     退化成**提交**
    ``Shift+Enter``   ``\r``（同上）                  退化成**提交**
    ``Alt+Enter``     ``\r`` 或 ``ESC`` + ``\r``     退化成**提交**（视终端而定）
    ================= ============================== ==========================

    ⚠️ ``\n`` **不在这里补编码**：D128 已把"单独的 LF"定成**换行**（``keys.py``
    把 LF 解析成"带修饰的 Enter"），于是 ``Ctrl+J`` / ``ConPTY 上的 Ctrl+Enter``
    在字符层保持原样就能得到正确行为 —— 少一条规则就少一处能改动的地方。

    补出来的形式是 ``keys.py`` **正式支持**的形态之一（``CSI 27;mod;code~``），
    所以解析层一个字都不用改 —— 与 Kitty / modifyOtherKeys 两条路复用同一段代码。

    ⚠️ **不要**给所有带修饰的键都补编码：开了 VT 输入之后，方向键等功能键本来就是
    控制台**已经翻译好**的 ``CSI`` 序列（``ESC`` ``[`` ``1`` ``;`` ``2`` ``A`` 六条字符记录），
    而那六条记录上**同样带着修饰位** —— 逐个补编码会把 ``Shift+↑`` 变成一串垃圾字符。
    这也是为什么这里只认 ``\r`` / ``\n`` 这两个"控制台不会翻译、只会原样给"的字符。
    """
    shift = bool(state & WIN32_SHIFT_PRESSED) or (
        char == "\r" and (shift_override if shift_override is not None else _is_shift_pressed_native())
    )
    ctrl = bool(state & WIN32_CTRL_PRESSED)
    alt = bool(state & WIN32_ALT_PRESSED)

    if char != "\r" or not (ctrl or shift or alt):
        return char
    code = 0x0D  # Enter

    # xterm / Kitty 的修饰编号是**从 1 开始**的位掩码：1=Shift，2=Alt，4=Ctrl
    # （``raw - 1`` 才是真实位）。这里反过来合成，规则必须与 `keys.py` 的
    # `_decode_modifiers` 严格一致 —— 两边差 1 就会把 Shift 判成 Alt。
    modifier = 1 + (1 if shift else 0) + (2 if alt else 0) + (4 if ctrl else 0)
    return f"\x1b[27;{modifier};{code}~"


def decode_input_records(
    records: Any,
    count: int,
    key_event: int,
    resize_event: int,
    *,
    shift_override: bool | None = None,
) -> tuple[str, bool]:
    """把一批 ``INPUT_RECORD`` 解成 ``(字符, 是否发生了窗口尺寸变化)``。

    抽成**模块级函数**而不是留在读线程里，是为了让它可测：解析控制台记录
    依赖三个字段的偏移（``bKeyDown`` / ``UnicodeChar`` / ``EventType``），
    而 ctypes 的结构体对齐一旦算错，症状是"按键变成乱码"或"按什么都没反应"——
    在真终端上极难定位，而在测试里只要构造几条记录就能验证。

    ``dwControlKeyState`` 这一维（D127）：不读它的话，**任何依赖修饰位的按键在
    Windows 上都不成立**，而症状是"按了变成另一个动作"（Ctrl+Enter 变成了发送），
    比"没反应"更难联想到成因。
    """
    chars: list[str] = []
    resized = False
    for index in range(count):
        record = records[index]
        if record.EventType == resize_event:
            resized = True
            continue
        if record.EventType != key_event:
            continue
        key = record.Event.KeyEvent
        if not key.bKeyDown:
            continue  # 只要按下，不要抬起（否则每个键会被处理两遍）
        code = key.UnicodeChar
        if code == 0:
            continue  # 功能键的"虚拟键码"记录里没有字符，交给 VT 序列那条路
        state = int(getattr(key, "dwControlKeyState", 0) or 0)
        chars.append(_encode_modified_key(chr(code), state, shift_override=shift_override))
    return "".join(chars), resized


# --------------------------------------------------------------------------- #
# POSIX
# --------------------------------------------------------------------------- #


class PosixTerminal:
    """Linux / macOS 驱动（``termios`` + ``SIGWINCH``）。

    与 Windows 的差别只有"字节从哪来"：这里是**真的往 stdin 里推字节**，
    所以一个 ``os.read`` 循环就够了。**不要**用 ``sys.stdin.read(1)``：
    前者可能阻塞到读满，后者会把多字节的 UTF-8 字符拆成半个。
    """

    def __init__(self) -> None:
        self._saved: list[Any] | None = None
        self._on_input: Callable[[str], None] | None = None
        self._on_resize: Callable[[], None] | None = None
        self._active = False
        self._reader: threading.Thread | None = None

    def _size(self) -> tuple[int, int]:
        import shutil

        size = shutil.get_terminal_size(fallback=(80, 24))
        return max(1, size.columns), max(1, size.lines)

    @property
    def columns(self) -> int:
        return self._size()[0]

    @property
    def rows(self) -> int:
        return self._size()[1]

    def write(self, data: str) -> None:
        sys.stdout.write(data)
        sys.stdout.flush()

    def clear(self) -> None:
        self.write(CLEAR_VIEWPORT)

    def start(self, on_input: Callable[[str], None], on_resize: Callable[[], None]) -> None:
        import signal
        import termios
        import tty

        self._on_input = on_input
        self._on_resize = on_resize
        self._saved = termios.tcgetattr(sys.stdin.fileno())
        tty.setraw(sys.stdin.fileno())
        self._active = True
        self.write("\x1b[?25l")
        # 窗口大小变化由信号通知（Windows 那边是控制台事件，见 `_read_loop`）
        try:
            self._previous_sigwinch = signal.signal(signal.SIGWINCH, self._handle_sigwinch)
        except ValueError:  # pragma: no cover - 不在主线程时无法注册信号
            self._previous_sigwinch = None
        self._reader = threading.Thread(target=self._read_loop, name="logox-input", daemon=True)
        self._reader.start()

    def _handle_sigwinch(self, signum: int, frame: Any) -> None:  # pragma: no cover - 信号
        if self._on_resize is not None:
            self._on_resize()

    def _read_loop(self) -> None:
        import os

        fd = sys.stdin.fileno()
        while self._active:
            try:
                chunk = os.read(fd, 4096)
            except (OSError, ValueError):
                return  # 终端关掉了 / fd 被复用 —— 安静退出
            if not chunk:
                return  # EOF
            if self._on_input is not None:
                # ``errors="replace"``：半个 UTF-8 字符宁可显示成替换符，也不要抛异常
                self._on_input(chunk.decode("utf-8", errors="replace"))

    def stop(self) -> None:
        if not self._active:
            return
        import signal
        import termios

        self._active = False
        self.write("\x1b[?25h")
        previous = getattr(self, "_previous_sigwinch", None)
        if previous is not None:
            with contextlib.suppress(ValueError):
                signal.signal(signal.SIGWINCH, previous)
        self._join_reader()
        if self._saved is not None:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self._saved)

    def _join_reader(self) -> None:
        """与 :meth:`Win32Terminal._join_reader` 同样的保护：不能在读线程里 join 自己。"""
        reader = self._reader
        if reader is None or reader is threading.current_thread():
            self._reader = None
            return
        reader.join(timeout=READ_POLL_MS / 1000.0 + 0.5)
        self._reader = None


# --------------------------------------------------------------------------- #
# 工厂
# --------------------------------------------------------------------------- #


def make_terminal() -> Terminal:
    """按当前平台选一个真实驱动。

    ⚠️ 这里**不做**"是否能 import ctypes"之类的探测——探测失败要等到
    ``start()`` 才知道。构造一个对象不该有副作用，也不该在 import 期抛异常。
    """
    if sys.platform == "win32":
        return Win32Terminal()
    if sys.platform == "emscripten":  # pragma: no cover - 非目标平台
        raise RuntimeError("Logox 不支持 WebAssembly 终端")
    return PosixTerminal()
