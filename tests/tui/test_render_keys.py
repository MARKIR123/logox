"""按键解析的测试（D80 / MODULE_tui_render §6）。

为什么这个文件必须先写
----------------------
`keys.py` 是**唯一**接触终端原始字节的地方。它错一个分支，症状是
"某个键没反应"或"某个键干了两件事"——而且**只在用户的终端上复现**。
把这些字节形态（包括"半个序列"）全部固定成用例，是整个重写里**性价比最高**的一步。

⚠️ 这些用例里的字节序列都是**真实终端会发出的形态**，不是编的：
   ↑ = ``ESC [ A``；Backspace = ``0x7F``（不是 ``0x08``）；Alt+Left = ``ESC ESC [ D``。
"""

from __future__ import annotations

import unittest

from logox.tui.render.keys import (
    DISABLE_MODIFY_OTHER_KEYS,
    ENABLE_KITTY_KEYBOARD,
    ENABLE_MODIFY_OTHER_KEYS,
    ESC_TIMEOUT_MS,
    KITTY_QUERY,
    Key,
    KeyParser,
    parse_key,
    strip_kitty_responses,
)


class SingleKeyTests(unittest.TestCase):
    """单个按键的解析。"""

    def test_printable_ascii(self) -> None:
        key = parse_key("a")
        assert key is not None
        self.assertEqual((key.name, key.char), ("a", "a"))
        self.assertTrue(key.printable)

    def test_cjk_character(self) -> None:
        """中文是多字节字符，必须整体当成一个可打印键（不是拆成三个）。"""
        key = parse_key("中")
        assert key is not None
        self.assertEqual(key.char, "中")
        self.assertTrue(key.printable)

    def test_cr_is_submit_and_lf_is_newline(self) -> None:
        """★ **CR 提交、LF 换行**（D128）—— 两者不再是同一个键。

        为什么改了（上一版把两者都当 Enter）：用户机器上的探针实测
        （``.smoke/probe_win32_enter.py``）显示 **Windows Terminal 把 ``Ctrl+Enter``
        发成 LF**，而老的映射把它当 Enter → **半句话被发了出去**。

        与参考实现 Pi 逐字节一致（``@mariozechner/pi-tui`` 的 ``editor.js``：
        单独的 LF 在**换行**分支里，``\r`` 在**提交**分支里）。

        ⚠️ 代价：若某终端把 **Enter 键**发成 LF，那种终端上要用 ``Ctrl+M``
        （它发的仍是 CR）才能发送。
        """
        self.assertEqual(parse_key("\r"), Key("enter"), "CR 仍是提交")
        newline = parse_key("\n")
        assert newline is not None
        self.assertEqual((newline.name, newline.ctrl), ("enter", True), "LF 是「带修饰的 Enter」")
        self.assertFalse(newline.printable, "换行键不能当普通字符插入")

    def test_shifted_enter_legacy_encoding(self) -> None:
        """某些终端把 ``Shift+Enter`` 发成 ``CSI 13;2~``（Pi 也认这条）。

        D129 曾随 `Shift+Enter` 键位一起去掉；**D130 随键位恢复** ——
        理由：它在 xterm 的老式功能键表里也是 ``Shift+F3``，而 F3 在本项目
        **没有任何绑定**，所以这个歧义不会让用户丢功能；反过来不认它，
        这些终端上就永远换不了行。
        """
        self.assertEqual(parse_key("\x1b[13;2~"), Key("enter", shift=True))


    def test_backspace_is_del_on_modern_terminals(self) -> None:
        """⚠️ 现代终端的 Backspace 发的是 ``0x7F``（DEL），不是 ``0x08``。

        按"Backspace = 0x08"的老常识写会得到一个"按退格没反应"的编辑器——
        这是终端程序最经典的坑之一。
        """
        self.assertEqual(parse_key("\x7f"), Key("backspace"))
        self.assertEqual(parse_key("\x08"), Key("backspace"))

    def test_escape(self) -> None:
        self.assertEqual(parse_key("\x1b"), Key("escape"))

    def test_tab(self) -> None:
        self.assertEqual(parse_key("\t"), Key("tab"))

    def test_ctrl_letters(self) -> None:
        """``Ctrl+A``..``Ctrl+Z`` 编码为 ``0x01``..``0x1A``。"""
        key = parse_key("\x01")
        assert key is not None
        self.assertEqual((key.name, key.ctrl), ("a", True))
        # 关键区分：Ctrl+字母**不是**可打印字符，编辑器据此决定"插入"还是"执行动作"
        self.assertFalse(key.printable, "Ctrl+字母不能当输入字符")

    def test_ctrl_c_is_distinguishable(self) -> None:
        """Ctrl+C 必须能被认出来——它是"中断/退出"（D69）。"""
        key = parse_key("\x03")
        assert key is not None
        self.assertEqual((key.name, key.ctrl), ("c", True))


class ArrowAndFunctionTests(unittest.TestCase):
    """CSI / SS3 序列。"""

    def test_arrows(self) -> None:
        for sequence, name in (
            ("\x1b[A", "up"),
            ("\x1b[B", "down"),
            ("\x1b[C", "right"),
            ("\x1b[D", "left"),
        ):
            with self.subTest(sequence=sequence):
                self.assertEqual(parse_key(sequence), Key(name))

    def test_home_end_and_navigation(self) -> None:
        self.assertEqual(parse_key("\x1b[H"), Key("home"))
        self.assertEqual(parse_key("\x1b[F"), Key("end"))
        self.assertEqual(parse_key("\x1b[3~"), Key("delete"))
        self.assertEqual(parse_key("\x1b[5~"), Key("pageup"))
        self.assertEqual(parse_key("\x1b[6~"), Key("pagedown"))

    def test_ss3_function_keys(self) -> None:
        """应用模式下 F1–F4 走 SS3（``ESC O P``..），不是 CSI。"""
        self.assertEqual(parse_key("\x1bOP"), Key("f1"))
        self.assertEqual(parse_key("\x1bOS"), Key("f4"))

    def test_function_keys_via_tilde(self) -> None:
        self.assertEqual(parse_key("\x1b[15~"), Key("f5"))
        self.assertEqual(parse_key("\x1b[24~"), Key("f12"))

    def test_shift_tab(self) -> None:
        self.assertEqual(parse_key("\x1b[Z"), Key("tab", shift=True))


class AltTests(unittest.TestCase):
    """Alt 组合（``ESC`` + 内容）——这是与"先 Esc 再按键"冲突的地方。"""

    def test_alt_letter(self) -> None:
        key = parse_key("\x1ba")
        assert key is not None
        self.assertEqual((key.name, key.alt), ("a", True))
        self.assertFalse(key.printable, "Alt+字母是命令，不是输入")

    def test_alt_arrow(self) -> None:
        """``Alt+Left`` = ``ESC`` + ``ESC [ D``（两个 ESC）。"""
        key = parse_key("\x1b\x1b[D")
        assert key is not None
        self.assertEqual((key.name, key.alt), ("left", True))

    def test_alt_arrow_survives_the_stateful_parser(self) -> None:
        """★★ **真实输入走的是 `KeyParser`，不是 `parse_key`。**

        这两条曾经不一致：``parse_key`` 对整串是对的，而 ``KeyParser`` 会把
        ``ESC ESC [ D`` 切成 ``Alt+Esc`` + ``[`` + ``D`` —— 症状是
        **按 Alt+方向键在输入框里打出 ``[D``，光标一动不动**。

        教训：``parse_key``（无状态）与 ``KeyParser.feed``（有状态）是**两条路**，
        只测前者等于没测用户实际走的那条。
        """
        for encoded, expected in (
            ("\x1b\x1b[D", Key("left", alt=True)),
            ("\x1b\x1b[C", Key("right", alt=True)),
            ("\x1b\x1b[A", Key("up", alt=True)),
        ):
            with self.subTest(encoded=repr(encoded)):
                self.assertEqual(KeyParser().feed(encoded), [expected])

    def test_alt_arrow_split_across_two_reads(self) -> None:
        """序列被拆成两次到达时也只能算一个键（不能把内层前缀当普通字符打出去）。"""
        parser = KeyParser()
        self.assertEqual(parser.feed("\x1b\x1b["), [], "半个序列不能产出按键")
        self.assertEqual(parser.feed("D"), [Key("left", alt=True)])
        self.assertEqual(parser.pending, "")

    def test_double_escape_alone_is_alt_escape(self) -> None:
        """``ESC ESC`` 一起到达 = ``Alt+Esc``（两个独立 Esc 之间会有一次读取的间隔）。"""
        self.assertEqual(KeyParser().feed("\x1b\x1b"), [Key("escape", alt=True)])

    def test_ctrl_alt_letter(self) -> None:
        """``Ctrl+Alt+A``：Ctrl 位已经表达了"这是命令"，所以不再叠 Alt。"""
        key = parse_key("\x1b\x01")
        assert key is not None
        self.assertEqual((key.name, key.ctrl, key.alt), ("a", True, False))


class KittyProtocolTests(unittest.TestCase):
    """Kitty 键盘协议 —— **这是唯一能区分 Shift+Enter 的途径**（§6.2）。"""

    def test_shift_enter_is_distinguishable(self) -> None:
        """★ 核心价值：传统协议下 ``Shift+Enter`` 与 ``Enter`` 是同一个 ``\\r``，
        只有 Kitty 协议能把它们分开。

        这直接关系到 UI-SPEC §7.2 的"Enter 提交 / Shift+Enter 换行"承诺。
        """
        plain = parse_key("\x1b[13u")
        shifted = parse_key("\x1b[13;2u")
        assert plain is not None and shifted is not None
        self.assertEqual(plain, Key("enter"))
        self.assertEqual(shifted, Key("enter", shift=True))
        self.assertNotEqual(plain, shifted, "Shift+Enter 必须与 Enter 可区分")

    def test_kitty_ctrl_and_alt_bits(self) -> None:
        """Kitty 的修饰是**位掩码**：真实位 = ``值 - 1``，其中 1=Shift，2=Alt，4=Ctrl。

        ⚠️ 那个 ``- 1`` 是必须的：``;1`` 表示"无修饰"，``;2`` 表示 Shift。
        不减它的话 Shift 会被当成"总有"、Alt 与 Shift 互换——**恰好把最需要区分的
        ``Shift+Enter`` 判错**（实测踩到）。
        """
        # ;5 → 位 4 → Ctrl（命令，不可插入）
        key = parse_key("\x1b[97;5u")
        assert key is not None
        self.assertEqual((key.name, key.ctrl, key.char), ("a", True, None))

        # ;3 → 位 2 → Alt（命令，不可插入）
        key = parse_key("\x1b[97;3u")
        assert key is not None
        self.assertEqual((key.name, key.alt, key.char), ("a", True, None))

        # ;2 → 位 1 → Shift。⚠️ Shift+字母**仍可插入**（码点已经是大写 A），
        # 所以它必须保持 printable —— 否则没法输入大写字母。
        key = parse_key("\x1b[65;2u")
        assert key is not None
        self.assertEqual((key.name, key.shift, key.char), ("A", True, "A"))
        self.assertTrue(key.printable)

    def test_kitty_printable_without_modifiers(self) -> None:
        key = parse_key("\x1b[97u")
        assert key is not None
        self.assertEqual(key.char, "a")
        self.assertTrue(key.printable)

    def test_kitty_named_keys_use_private_area_codepoints(self) -> None:
        """Kitty 用 Unicode 私有区码点表示方向键/功能键。"""
        self.assertEqual(parse_key("\x1b[57352u"), Key("up"))
        self.assertEqual(parse_key("\x1b[57350u"), Key("left"))


class UnrecognisedTests(unittest.TestCase):
    """认不出来时必须返回 ``None``，**不能**当成普通输入。"""

    def test_unknown_csi_returns_none(self) -> None:
        self.assertIsNone(parse_key("\x1b[99~"))

    def test_empty_returns_none(self) -> None:
        self.assertIsNone(parse_key(""))

    def test_control_characters_are_not_inserted(self) -> None:
        """终端会回一些响应（如光标位置报告）；把它们插进输入框是不可接受的。"""
        for payload in ("\x1b[6n", "\x1b[?1;2c"):
            with self.subTest(payload=payload):
                key = parse_key(payload)
                self.assertTrue(key is None or not key.printable)


class StreamingParserTests(unittest.TestCase):
    """`KeyParser`：终端给的是**流**，一次 read 可能只拿到半个序列。"""

    def test_complete_sequence_in_one_feed(self) -> None:
        parser = KeyParser()
        self.assertEqual(parser.feed("\x1b[A"), [Key("up")])
        self.assertEqual(parser.pending, "")

    def test_split_sequence_is_buffered_not_misread(self) -> None:
        """★ **核心用例**：方向键被拆成两次到达时，**不能**先报一个 Esc。

        这是"按方向键结果中断了生成"的经典成因：第一个字节 ``\\x1b`` 被当成
        Esc 处理，用户按一下方向键就把回合取消了。
        """
        parser = KeyParser()
        self.assertEqual(parser.feed("\x1b"), [], "半个序列不能产出按键")
        self.assertEqual(parser.feed("[A"), [Key("up")], "补齐后应解析为 up")

    def test_multiple_keys_in_one_feed(self) -> None:
        parser = KeyParser()
        self.assertEqual(parser.feed("abc"), [Key("a", char="a"), Key("b", char="b"), Key("c", char="c")])

    def test_bare_escape_waits_for_timeout(self) -> None:
        """单独的 ESC 不能立刻确定——因为它可能与后续字节组成 Alt 组合。

        先 ``feed("\\x1b")``（无产出），再 ``feed("a")``，应当解析成 **Alt+a**
        而不是"Esc + a"。这正是"按 Alt 被当成 Esc"的那个坑。
        """
        parser = KeyParser()
        self.assertEqual(parser.feed("\x1b"), [])
        keys = parser.feed("a")
        self.assertEqual(keys, [Key("a", alt=True)])

    def test_flush_pending_escape_confirms_escape(self) -> None:
        """超时后 ``flush_pending_escape`` 才把孤立的 ESC 确定成 Esc 键。"""
        parser = KeyParser()
        parser.feed("\x1b")
        self.assertEqual(parser.flush_pending_escape(), Key("escape"))
        self.assertEqual(parser.pending, "", "确认后缓冲必须清空")

    def test_flush_is_noop_when_buffer_empty(self) -> None:
        parser = KeyParser()
        self.assertIsNone(parser.flush_pending_escape())

    def test_cjk_character_split_across_feeds(self) -> None:
        """多字节字符也可能被拆开；缓冲要能拼回来（不能产生乱码）。"""
        parser = KeyParser()
        self.assertEqual(parser.feed("中"), [Key("中", char="中")])

    def test_esc_timeout_is_short_enough_to_feel_instant(self) -> None:
        """等待窗口必须短到用户感觉不到（这是"按 Esc 有延迟"的阈值）。"""
        self.assertLessEqual(ESC_TIMEOUT_MS, 60.0)


class KeyboardProtocolTests(unittest.TestCase):
    """键盘协议协商：**先问再开**，以及拿不到 Kitty 时的退路。

    为什么这两条都不能省：``Shift+Enter`` 在传统协议里与 ``Enter`` 是同一个字节，
    所以"输入框里换行"要么靠 Kitty 协议、要么靠 xterm 的 ``modifyOtherKeys``。
    直接发"启用 Kitty"而**不问**是不行的——不支持的终端会静默忽略，
    我们却会以为拿到了修饰位（判断依据是假的）。
    """

    def test_kitty_response_is_recognised(self) -> None:
        self.assertEqual(strip_kitty_responses("\x1b[?1u"), ("", True))
        self.assertEqual(strip_kitty_responses("\x1b[?7u"), ("", True))

    def test_keystroke_in_the_same_read_is_not_swallowed(self) -> None:
        """★ 回应与用户的第一次按键可能挤在同一次读取里。

        整包吃掉的话，用户的第一个字会**凭空消失**——这种 bug 只在
        启动瞬间出现一次，极难复现。
        """
        self.assertEqual(strip_kitty_responses("\x1b[?1ua"), ("a", True))
        self.assertEqual(strip_kitty_responses("a\x1b[?1u b"), ("a b", True))

    def test_other_input_is_not_mistaken_for_a_response(self) -> None:
        for data in ("\x1b[1;2u", "a", "\x1b[?u", ""):
            with self.subTest(data=data):
                self.assertEqual(
                    strip_kitty_responses(data), (data, False), "协议回应被误判会吃掉按键"
                )

    def test_query_itself_is_not_a_response(self) -> None:
        """``ESC[?u`` 是"我问"，``ESC[?1u`` 是"它答"。两者不能混。"""
        self.assertEqual(KITTY_QUERY, "\x1b[?u")
        self.assertEqual(strip_kitty_responses(KITTY_QUERY), (KITTY_QUERY, False))
        self.assertNotEqual(ENABLE_KITTY_KEYBOARD, KITTY_QUERY)
        self.assertNotEqual(ENABLE_MODIFY_OTHER_KEYS, DISABLE_MODIFY_OTHER_KEYS)


class ModifyOtherKeysTests(unittest.TestCase):
    """xterm ``modifyOtherKeys`` 模式 2：``CSI 27 ; <修饰> ; <码点> ~``。"""

    def test_shift_enter_is_distinguishable(self) -> None:
        """★ 这一条就是"在输入框里换行"能不能用的关键。"""
        self.assertEqual(parse_key("\x1b[27;2;13~"), Key("enter", shift=True))

    def test_plain_enter_has_no_modifiers(self) -> None:
        self.assertEqual(parse_key("\x1b[27;1;13~"), Key("enter"))

    def test_ctrl_and_alt_are_decoded(self) -> None:
        self.assertEqual(parse_key("\x1b[27;5;13~"), Key("enter", ctrl=True))
        self.assertEqual(parse_key("\x1b[27;3;13~"), Key("enter", alt=True))

    def test_printable_key_keeps_its_char(self) -> None:
        key = parse_key("\x1b[27;2;97~")
        self.assertIsNotNone(key)
        assert key is not None
        self.assertEqual(key.char, "a")
        self.assertTrue(key.shift)
        self.assertTrue(key.printable)

    def test_ctrl_printable_is_a_command_not_text(self) -> None:
        """带 Ctrl 的组合是命令，不是输入——否则会被插进编辑框。"""
        key = parse_key("\x1b[27;5;97~")
        self.assertIsNotNone(key)
        assert key is not None
        self.assertFalse(key.printable)

    def test_malformed_sequence_is_ignored(self) -> None:
        self.assertIsNone(parse_key("\x1b[27;2~"))
        self.assertIsNone(parse_key("\x1b[27;2;x~"))

    def test_parser_handles_it_as_one_sequence(self) -> None:
        """★ 不能被切碎：``ESC[27;2;13~`` 必须整体解析成**一个**按键。"""
        keys = KeyParser().feed("\x1b[27;2;13~")
        self.assertEqual(keys, [Key("enter", shift=True)])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
