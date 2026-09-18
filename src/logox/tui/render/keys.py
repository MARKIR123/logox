"""按键解析：原始字节 → :class:`Key`（D80，MODULE_tui_render §6）。

为什么需要这一层
----------------
终端在"原始模式"下交给我们的是**字节流**，而不是"按键"。按下 ``↑`` 得到的是
``\\x1b[A`` 三个字节；按下 ``Ctrl+A`` 得到 ``\\x01`` 一个字节。而且：

* **不同平台形态不同**（Windows 的 ``ReadConsoleInputW`` 与 POSIX 的 ``read``
  在 Alt/功能键上并不完全一致）；
* **同一按键可能有多种编码**（``Esc`` 可以是单独的 ``\\x1b``，也可以是某个序列的开头）。

**没有这一层会怎样**：每个组件都要自己判断 ``data == "\\x1b[A"``，
于是"平台差异"散落到十几个地方，而且**测试必须伪造转义序列**才能测一个按钮。

**有了它**：解析只有一处实现、可整体单测；组件只面对 :class:`Key`，
测试可以**直接合成按键**（``Key("up")``）而不必构造字节。

与 Pi 的差异（刻意的）
----------------------
Pi 的组件收原始字节、自己用 ``matchesKey()`` 判断。我们改成**已解析的 `Key` 对象**。
理由：解析集中 ⇒ 可测、且平台差异只在一处。

诚实登记：这些地方**做不到 100% 可靠**
--------------------------------------
============================ ====================================================
``Shift+Enter``              传统终端协议里与 ``Enter`` **是同一个字节**。
                             本模块只在终端支持 **Kitty 键盘协议**时才能区分
                             （见 :data:`ENABLE_KITTY_KEYBOARD`）。
                             UI-SPEC §12.4 已承诺"不承诺 Shift+Enter"，保持一致。
``Ctrl+字母``                 只对 ``a``–``z`` 可靠（``\\x01``–``\\x1a``）。
                             其余组合在多数终端上根本没有独立编码。
``Alt+字母``                  编码为 ``ESC`` + 字母，与"先按 Esc 再按字母"无法区分。
                             因此**收到 ESC 后要等一下**（见 :data:`ESC_TIMEOUT_MS`）。
============================ ====================================================
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = [
    "DISABLE_KITTY_KEYBOARD",
    "DISABLE_MODIFY_OTHER_KEYS",
    "ENABLE_KITTY_KEYBOARD",
    "ENABLE_MODIFY_OTHER_KEYS",
    "ESC_TIMEOUT_MS",
    "KITTY_QUERY",
    "PASTE_END",
    "PASTE_START",
    "Key",
    "KeyParser",
    "parse_key",
    "strip_kitty_responses",
]


#: **先问**终端支不支持 Kitty 键盘协议（``CSI ? u``）。
#:
#: 为什么要先问、而不是直接开：直接发 ``CSI > 7 u`` 时，不支持的终端会**忽略**它，
#: 于是我们"以为"拿到了修饰位，其实拿到的是旧的字节编码——**判断依据是假的**。
#: 先问再开，就变成"有回应才认为支持"（Pi 也是这个顺序）。
KITTY_QUERY = "\x1b[?u"

#: 请求终端启用 Kitty 键盘协议（``CSI > 7 u``）。
#:
#: 为什么需要它：启用后终端会给**每个按键**附带修饰位，于是 ``Shift+Enter``
#: 与 ``Enter`` 可以区分（传统协议里它们是同一个 ``\\r``）。
#: ⚠️ 支持的终端：kitty / WezTerm / Ghostty / Windows Terminal（较新版本）。
ENABLE_KITTY_KEYBOARD = "\x1b[>7u"

#: 关闭 Kitty 键盘协议（退出时恢复，避免污染终端状态）
DISABLE_KITTY_KEYBOARD = "\x1b[<u"

#: Kitty 查询的**回应**形态：``CSI ? <数字> u``
#:
#: 刻意**不加行尾锚点**：终端可能把回应与用户的第一次按键放在**同一次读取**里
#: （``ESC[?1u`` 紧跟一个 ``a``）。带 ``$`` 匹配时 ``\\n`` 之前也算匹配，
#: 于是"回应 + 换行"会被整段吃掉——那等于吞掉用户的一次回车。
_KITTY_RESPONSE = re.compile(r"\x1b\[\?\d+u")


def strip_kitty_responses(data: str) -> tuple[str, bool]:
    """剥掉"终端说它支持 Kitty 键盘协议"的回应。

    返回 ``(剩下的字节, 是否见到回应)``。

    为什么返回两样东西：这段回应**既不是用户按的键，也不能直接丢掉整段输入**——
    它可能和真正的按键挤在同一次 ``read`` 里。所以是"从里面挑出来删掉"，
    不是"整段吃掉"。这与 Pi 在 ``stdin-buffer`` 里"先切序列再判断"是同一个效果。
    """
    if "\x1b[?" not in data:
        return data, False  # 快路径：绝大多数输入没有这个前缀
    cleaned, count = _KITTY_RESPONSE.subn("", data)
    return cleaned, count > 0

#: 没有 Kitty 协议时的退路：xterm 的 ``modifyOtherKeys`` 模式 2（``CSI > 4 ; 2 m``）。
#:
#: 为什么需要退路：``Shift+Enter`` 在传统协议里**和 Enter 是同一个字节**，
#: 于是"换行"这个动作在大多数终端上无法完成。xterm / tmux / Windows Terminal
#: 都实现了这个模式：开了之后带修饰的键会变成 ``CSI 27 ; <修饰> ; <码点> ~``。
#: 这就是 Pi 在没有收到 Kitty 回应时做的事（150ms 后开这个）。
ENABLE_MODIFY_OTHER_KEYS = "\x1b[>4;2m"

#: 关闭 ``modifyOtherKeys``（退出时必须恢复，否则会**污染用户的 shell**）
DISABLE_MODIFY_OTHER_KEYS = "\x1b[>4;0m"

#: 收到单独 ``ESC`` 后等待多久才断定"用户就是要按 Esc"（毫秒）。
#:
#: 为什么必须等：``Alt+字母`` 与 ``Esc`` 然后 ``字母`` 的字节完全一样
#: （都是 ``ESC`` + 字母）。不等的话，用户按 ``Alt+Left`` 会被解析成
#: "Esc 然后 Left"，于是**中途退出生成**——这个误伤很难查。
#: 30ms 是"人不会在 30ms 内按两次键"的量级。
ESC_TIMEOUT_MS = 30.0


#: 括号粘贴的**开始**与**结束**标记（``CSI 200 ~`` / ``CSI 201 ~``）。
#:
#: 为什么需要它：终端把"粘贴"与"手打"区分开的唯一手段就是给粘贴内容套一层标记。
#: **不处理它会怎样**（实测会踩）：粘贴进来的一段文本里的换行会被当成"逐条回车"，
#: 于是一次粘贴发出十几条消息；粘贴一个 API Key 时，标记本身还会混进密钥里
#: （``ESC[200~sk-xxxESC[201~``），报错是"密钥无效"——离原因很远。
PASTE_START = "\x1b[200~"
PASTE_END = "\x1b[201~"


@dataclass(frozen=True, slots=True)
class Key:
    """一个**已解析**的按键。

    ``name`` 是规范化名字（``"enter"`` / ``"escape"`` / ``"up"`` / ``"a"`` / ``"f1"``）。
    可打印字符同时带上 ``char``，方便编辑器直接插入。

    有一个**特殊名字** ``"paste"``：它的 ``char`` 是整段粘贴内容（可含换行）。
    组件应当把它当成"一次性插入这一整段"，而不是逐字符处理——
    这正是括号粘贴要解决的问题。
    """

    name: str
    ctrl: bool = False
    alt: bool = False
    shift: bool = False
    char: str | None = None

    @property
    def printable(self) -> bool:
        """是否是"可以插进文本"的字符（不含 Ctrl/Alt 组合）。

        ``Ctrl+字母`` **不算**可打印——它是编辑命令，不是输入。
        这条判断是编辑器区分"插入字符"与"执行动作"的唯一依据。
        """
        return self.char is not None and not self.ctrl and not self.alt

    def __str__(self) -> str:  # pragma: no cover - 仅用于调试输出
        parts = []
        if self.ctrl:
            parts.append("ctrl")
        if self.alt:
            parts.append("alt")
        if self.shift:
            parts.append("shift")
        parts.append(self.name)
        return "+".join(parts)


def _modifiers(*, ctrl: bool = False, alt: bool = False, shift: bool = False) -> dict[str, bool]:
    return {"ctrl": ctrl, "alt": alt, "shift": shift}


#: 方向键 / 导航键的 CSI 末位字母 → 名字
_CSI_FINAL = {
    "A": "up",
    "B": "down",
    "C": "right",
    "D": "left",
    "H": "home",
    "F": "end",
    "Z": "tab",  # Shift+Tab（CBT / back-tab）
}

#: ``CSI <数字> ~`` 形式的功能键与导航键
_CSI_TILDE = {
    "1": "home",
    "2": "insert",
    "3": "delete",
    "4": "end",
    "5": "pageup",
    "6": "pagedown",
    "7": "home",
    "8": "end",
    "11": "f1",
    "12": "f2",
    "13": "f3",
    "14": "f4",
    "15": "f5",
    "17": "f6",
    "18": "f7",
    "19": "f8",
    "20": "f9",
    "21": "f10",
    "23": "f11",
    "24": "f12",
}

#: 单字节控制字符 → 名字（``Ctrl+字母`` 与常见控制键）
_CONTROL = {
    0x00: ("space", True),  # Ctrl+Space / Ctrl+@
    0x08: ("backspace", False),
    0x09: ("tab", False),
    0x0D: ("enter", False),
    0x0A: ("enter", False),  # 部分终端把 Enter 发成 LF
    0x1B: ("escape", False),
    0x1F: ("_", True),
    0x7F: ("backspace", False),  # 绝大多数终端上 Backspace 发的是 DEL
}


def parse_key(data: str) -> Key | None:
    """把一个**完整的**按键序列解析成 :class:`Key`；无法识别时返回 ``None``。

    ``None`` 的语义是"这串字节不是一个能识别的按键"——调用方**应当忽略它**
    （而不是当成普通输入插进编辑器），否则终端返回的杂散响应会被写进输入框。
    """
    if not data:
        return None

    # ⚠️ **顺序至关重要**：CSI/SS3 必须**先**判断。
    # 它们都以 ESC 开头，若先走"Alt 组合"分支，``\x1b[A`` 会被解析成
    # ``Alt+[`` 再跟一个 ``A`` —— 症状是"方向键变成了输入字符"（实测踩到）。
    if data.startswith("\x1b["):
        return _parse_csi(data)
    if data.startswith("\x1bO") and len(data) >= 3:
        final = data[2]
        if final in _CSI_FINAL:
            return Key(_CSI_FINAL[final], **_modifiers(shift=(final == "Z")))
        if final in "PQRS":
            return Key(f"f{ord(final) - ord('P') + 1}")
        return None

    # Alt 组合：ESC + **一个** 字符（传统协议里 Alt 就是这么编码的）。
    #
    # ⚠️ 这里必须排除 ``ESC ESC`` 的情况：``Alt+方向键`` 的编码是
    # ``ESC ESC [ D``（两个 ESC —— 外层是 Alt，内层是 CSI 的开头）。
    # 若不排除，``data[1]`` 又是一个 ESC，会解析出 ``Alt+Escape``（实测踩到）。
    if data.startswith("\x1b") and len(data) > 1 and not data.startswith("\x1b\x1b"):
        rest = parse_key(data[1])
        if rest is None:
            return None
        return Key(
            rest.name,
            ctrl=rest.ctrl,
            alt=not rest.ctrl,  # Ctrl 已经表达了"这是命令"，不必再叠 Alt
            shift=rest.shift,
            char=None,  # Alt 组合是命令，不是输入
        )

    # ``ESC ESC [ D`` = Alt + 方向键：剥掉一个 ESC，把 Alt 位加到里面那串上
    if data.startswith("\x1b\x1b"):
        rest = parse_key(data[1:])
        if rest is None:
            return None
        return Key(rest.name, ctrl=rest.ctrl, alt=True, shift=rest.shift, char=None)

    # 单字节控制字符
    if len(data) == 1:
        code = ord(data)
        if code in (0x0D, 0x0A):
            return Key("enter")
        if code == 0x09:
            return Key("tab")
        if code in (0x08, 0x7F):
            return Key("backspace")
        if code == 0x1B:
            return Key("escape")
        if 0x01 <= code <= 0x1A:
            # Ctrl+A..Ctrl+Z（0x01–0x1A）；0x09/0x0D 已被上面接走
            letter = chr(ord("a") + code - 1)
            return Key(letter, **_modifiers(ctrl=True), char=letter)
        if code == 0x00:
            return Key("space", **_modifiers(ctrl=True), char=" ")

    # 普通可打印字符（含中文等多字节字符）
    if data.isprintable():
        return Key(data, char=data)

    return None


def _parse_csi(data: str) -> Key | None:
    """解析 ``CSI`` 序列（``\\x1b[`` 之后的部分）。"""
    body = data[2:]
    if not body:
        return None

    # ``CSI Z`` 是 Shift+Tab 的**专用**序列（back-tab），没有参数段
    if body == "Z":
        return Key("tab", **_modifiers(shift=True))

    # xterm ``modifyOtherKeys`` 模式 2：``CSI 27 ; <修饰> ; <码点> ~``
    # 这是拿不到 Kitty 协议时的退路，也是 Shift+Enter 在多数终端上唯一的希望。
    if body.startswith("27;") and body.endswith("~"):
        return _parse_modify_other_keys(body[:-1])

    # Kitty 键盘协议：``CSI <码> ; <修饰> u``
    if body.endswith("u"):
        return _parse_kitty(body[:-1])

    # ``CSI 1 ; 5 A`` 这类（修饰参数 + 方向键）
    if body[-1] in _CSI_FINAL:
        name = _CSI_FINAL[body[-1]]
        params = body[:-1].split(";")
        # ⚠️ 修饰参数在**终止字母之前**。少看这一段的话，Shift+Up 会被
        # 当成普通 Up —— 这类"修饰被静默忽略"的 bug 很难从症状反推。
        shift = len(params) > 1 and params[1].split(":")[0] in ("2", "4", "6", "8")
        return Key(name, **_modifiers(shift=shift))

    # ``CSI 3 ~`` 这类
    if body.endswith("~"):
        params = body[:-1].split(";")
        name = _CSI_TILDE.get(params[0])
        if name is None:
            return None
        shift = len(params) > 1 and params[1].split(":")[0] in ("2", "4", "6", "8")
        return Key(name, **_modifiers(shift=shift))

    return None


#: Kitty 协议的修饰位（``CSI <码>;<修饰>u`` 里那个数）
#:
#: ⚠️ **该值是从 1 开始的**：``1`` = 无修饰，``2`` = Shift，``3`` = Alt，``5`` = Ctrl。
#: 也就是说真实位掩码是 ``值 - 1``。不减这个 1 的话，Shift 会被当成"总有"、
#: Alt 与 Shift 互换——**恰好把最需要区分的 Shift+Enter 判错**（实测踩到）。
#:
#: 好消息：xterm 的 ``modifyOtherKeys`` 用的是**同一套**编号，
#: 所以 :func:`_parse_modify_other_keys` 可以复用这张表和减 1 的规则。
_KITTY_MODIFIERS = {
    1: "shift",
    2: "alt",
    4: "ctrl",
}

#: Kitty 协议里特殊功能键的码点（Unicode 私有区）
_KITTY_FUNCTIONAL = {
    0x0D: "enter",
    0x1B: "escape",
    0x7F: "backspace",
    0x09: "tab",
    57344: "escape",
    57345: "enter",
    57346: "tab",
    57347: "backspace",
    57348: "insert",
    57349: "delete",
    57350: "left",
    57351: "right",
    57352: "up",
    57353: "down",
    57354: "pageup",
    57355: "pagedown",
    57356: "home",
    57357: "end",
}


def _decode_modifiers(raw: int) -> dict[str, bool]:
    """把 Kitty / xterm 的**修饰编号**翻译成布尔位。

    两家的编号规则一样：``1`` = 无修饰，真实位掩码 = ``值 - 1``。
    集中成一个函数是为了让"减 1"这条规则**只写一遍**——它是本项目踩过的坑之一。
    """
    flags = max(0, raw - 1)
    return {name: bool(flags & bit) for bit, name in _KITTY_MODIFIERS.items()}


def _parse_kitty(body: str) -> Key | None:
    """解析 Kitty 协议的 ``CSI ... u`` 形式（这是能区分 Shift+Enter 的唯一途径）。"""
    # 形式一：``CSI <码点> ; <修饰> u``   形式二：``CSI <码点> u``
    if ";" in body:
        code_part, _, mod_part = body.partition(";")
    else:
        code_part, mod_part = body, ""

    # 码点与修饰都可能带子参数（``<值>:<事件类型>``），只取冒号前的数字
    code_part = code_part.split(":")[0]
    mod_digits = mod_part.split(":")[0]
    if not code_part.isdigit():
        return None
    code = int(code_part)
    raw = int(mod_digits) if mod_digits.isdigit() else 1

    return _key_from_codepoint(code, _decode_modifiers(raw))


def _parse_modify_other_keys(body: str) -> Key | None:
    """解析 xterm ``modifyOtherKeys`` 的 ``CSI 27 ; <修饰> ; <码点>``。"""
    parts = body.split(";")
    if len(parts) != 3:  # noqa: PLR2004 - 协议固定三段（27;修饰;码点）
        return None
    _, mod_digits, code_digits = parts
    if not code_digits.isdigit():
        return None
    raw = int(mod_digits) if mod_digits.isdigit() else 1
    return _key_from_codepoint(int(code_digits), _decode_modifiers(raw))


def _key_from_codepoint(code: int, modifiers: dict[str, bool]) -> Key | None:
    """码点 + 修饰位 → :class:`Key`（Kitty 与 modifyOtherKeys 共用这一段）。"""
    if code in _KITTY_FUNCTIONAL:
        return Key(_KITTY_FUNCTIONAL[code], **modifiers)

    if 0x20 <= code <= 0x10FFFF:
        char = chr(code)
        # ⚠️ `char` 的语义是"**可插入的字符**"。带 Ctrl/Alt 的是命令，
        # 一律不给 char —— 否则 `Key("a", shift=True, char="a")` 与
        # `Key("a", shift=True)` 会不相等，测试与去重都会莫名其妙地失败。
        insertion = char if not modifiers.get("ctrl") and not modifiers.get("alt") else None
        return Key(char.lower() if modifiers.get("ctrl") else char, char=insertion, **modifiers)

    return None


class KeyParser:
    """**有状态**的按键解析器：把字节流切成一个个完整按键。

    为什么需要状态：终端给的是**流**，一次 ``read`` 可能拿到半个序列
    （``\\x1b`` 到了、``[A`` 还没到）。没有缓冲就会把方向键解析成"Esc"。
    做法与 Pi 的 ``stdin-buffer.ts`` 一致：攒着，直到能确定一个完整按键。
    """

    def __init__(self) -> None:
        self._buffer = ""
        #: 括号粘贴状态：``True`` 表示"正在收集一段粘贴内容"
        self._pasting = False
        self._paste_content = ""

    def feed(self, data: str) -> list[Key]:
        """投喂一段字节，返回**这次能确定的**全部按键。

        ⚠️ 末尾可能是半个序列，会被留在缓冲里等下一次 ``feed``。
        调用方**不要**把返回值当成"这次输入的全部"——不完整时它会少一个。
        """
        self._buffer += data
        keys: list[Key] = []
        while self._buffer:
            if self._pasting:
                if not self._consume_paste(keys):
                    break
                continue
            if self._buffer.startswith(PASTE_START):
                self._buffer = self._buffer[len(PASTE_START) :]
                self._pasting = True
                self._paste_content = ""
                continue
            # 可能只到了粘贴开始标记的一半 —— 等下一次
            if len(self._buffer) < len(PASTE_START) and PASTE_START.startswith(self._buffer):
                break
            if not self._consume_one(keys):
                break
        return keys

    def _consume_paste(self, keys: list[Key]) -> bool:
        """收集粘贴内容，直到看到结束标记。返回 ``False`` 表示"还没收全"。"""
        end = self._buffer.find(PASTE_END)
        if end == -1:
            # 还没看到结束标记。**保留最后 len(END)-1 个字符**，因为结束标记可能
            # 正好被这一次读取从中间切开——不保留的话标记会被当成正文插进去。
            hold = len(PASTE_END) - 1
            if len(self._buffer) > hold:
                self._paste_content += self._buffer[:-hold]
                self._buffer = self._buffer[-hold:]
            return False
        self._paste_content += self._buffer[:end]
        self._buffer = self._buffer[end + len(PASTE_END) :]
        keys.append(Key("paste", char=self._paste_content))
        self._paste_content = ""
        self._pasting = False
        return True

    def _consume_one(self, keys: list[Key]) -> bool:
        """尝试从缓冲里取走一个按键。返回 ``False`` 表示"还需要更多字节"。"""
        buffer = self._buffer

        if buffer.startswith("\x1b"):
            length = _sequence_length(buffer)
            if length is None:
                return False  # 还没收全（例如只到了 ``\x1b[``）
            if length <= 1:
                # 独立的 ESC：**不能立刻确定**——它可能是某个序列的开头。
                # 这是"按方向键结果中断了生成"的成因：第一个字节就被当成了 Esc。
                # 真按了 Esc 的情况由调用方在超时后调 `flush_pending_escape()` 确认。
                return False
            chunk, self._buffer = buffer[:length], buffer[length:]
            key = parse_key(chunk)
            if key is not None:
                keys.append(key)
            return True

        # 普通字符（可能是一个多字节的 CJK 字符）
        chunk, self._buffer = buffer[0], buffer[1:]
        key = parse_key(chunk)
        if key is not None:
            keys.append(key)
        return True

    def flush_pending_escape(self) -> Key | None:
        """超时后调用：把缓冲里那个"孤零零的 ESC"确定成 Esc 键。

        这是 ``Alt+字母`` 与 ``Esc`` + ``字母`` 无法区分的**唯一出路**：
        等一小会儿（:data:`ESC_TIMEOUT_MS`），没人接着来就当成 Esc。
        """
        if self._buffer == "\x1b":
            self._buffer = ""
            return Key("escape")
        return None

    @property
    def pending(self) -> str:
        """当前还没解析完的缓冲（测试与调试用）。"""
        return self._buffer

    @property
    def pasting(self) -> bool:
        """是否正处在"收集中"的括号粘贴里（调试用）。"""
        return self._pasting


def _sequence_length(buffer: str) -> int | None:
    """判断 ``buffer`` 开头这个转义序列有多长（``None`` = 还没收全，``0`` = 就是个 ESC）。

    只处理我们认识的形式；认识不了的按"最短可判定"返回，避免把用户的输入卡在缓冲里。
    """
    if len(buffer) == 1:
        return 0  # 可能就是个 Esc，也可能是序列开头 —— 由调用方结合长度决定
    if not buffer.startswith("\x1b"):
        return None

    second = buffer[1]

    # CSI：ESC [ ... 终止字节在 0x40–0x7E
    if second == "[":
        for index in range(2, len(buffer)):
            if "@" <= buffer[index] <= "~":
                return index + 1
        return None  # 还没到终止字节

    # SS3：ESC O <一个字符>
    if second == "O":
        return 3 if len(buffer) >= 3 else None

    # Alt + 单字符（多字节 CJK 的 Alt 组合会走这里，按"一个字符"算）
    return 2
