"""纯净终端流的测试（D80/D81）。

用 `FakeTerminal` 驱动，**不需要真终端**：断言"屏幕上是什么"
与"按键之后变成什么"。这是自研渲染器相对 Textual 的一个直接好处——
`run_test()`/`Pilot` 那一套异步脚手架不再需要，测试就是普通的同步代码。
"""

from __future__ import annotations

import asyncio
import unittest
from dataclasses import dataclass, field
from typing import Any

from rich.text import Text

from logox.kernel import events as ev
from logox.kernel.bus import EventBus
from logox.tui.content.cards import DiffHunk
from logox.tui.metrics import MetricsReducer
from logox.tui.render.app import InlineApp, StatusComponent, TimelineComponent
from logox.tui.render.components.editor import Editor
from logox.tui.render.keys import Key
from logox.tui.render.terminal import FakeTerminal
from logox.tui.theme import load_theme


@dataclass
class FakeKernel:
    """只有 `KernelPort` 三成员（与 `tests/tui/tui_support.FakeKernel` 同构）。"""

    started: list[str] = field(default_factory=list)
    cancels: int = 0

    async def start(self, text: str) -> Any:
        self.started.append(text)
        return None

    def cancel(self) -> bool:
        self.cancels += 1
        return True

    @property
    def current_turn(self) -> Any:
        return None


class _Config:
    class provider:  # noqa: N801 - 只为测试构造最小对象
        thinking_effort = "auto"


class _Runtime:
    def __init__(self) -> None:
        self.kernel = FakeKernel()
        self.bus = EventBus(session_id="test-inline")
        self.model = "test-model"
        self.config = _Config()


def make_app(
    width: int = 70, height: int = 20, session_replayer: Any | None = None
) -> tuple[InlineApp, FakeTerminal, _Runtime]:
    runtime = _Runtime()
    if session_replayer is not None:
        runtime.session_replayer = session_replayer
    terminal = FakeTerminal(columns=width, rows=height)
    app = InlineApp(runtime=runtime, terminal=terminal)
    return app, terminal, runtime


class InputTokenWiringTests(unittest.TestCase):
    """D152-a 的**接线守卫**：`InlineApp` 必须把三个 `input_*` token 装到编辑器上。

    为什么必须有这一组（它是本轮最容易被漏掉的一环）
    ----------------------------------------------
    组件层（`BoxedEditor` / `Editor`）只是**接收**样式串，它不关心谁传了什么。
    只测组件的话，"装配处仍传旧的 `border_subtle`"这种缺陷**一条用例都不会红** ——
    实测过：把 `render/app.py` 的 `border_style` 改回 `border_subtle`，全量测试
    **1467 passed、0 failed**。也就是说没有这一组用例，这次修复会被静默改回去。

    这组用例只断言**接线**（谁传给谁），不断言颜色好不好看 —— 后者由
    `tests/tui/test_theme.py` 的对比度用例负责。
    """

    def test_editor_gets_the_three_input_tokens(self) -> None:
        app, _terminal, _runtime = make_app()
        palette = app.theme.palette

        self.assertEqual(app.editor.border_style, str(palette.input_border))
        self.assertEqual(app.editor.text_style, str(palette.input_text))
        # hint 在**内层** Editor 上（BoxedEditor 是装饰器，不代理这个字段）
        self.assertEqual(app.editor.inner.hint_style, str(palette.input_hint))

    def test_switching_theme_updates_all_three(self) -> None:
        """换主题必须同步**三处**，一处不漏。

        漏掉 `input_text` 的后果最严重：切到浅色主题后会**白字白底、完全看不见**，
        而且不报错。漏掉 hint 则表现为"指路牌还是旧主题的颜色"。
        """
        app, _terminal, _runtime = make_app()
        dark = app.theme.palette
        app.apply_theme("logox-light")
        light = app.theme.palette

        self.assertNotEqual(light.input_border, dark.input_border, "该用例需要两套主题取值不同")
        self.assertEqual(app.editor.border_style, str(light.input_border))
        self.assertEqual(app.editor.text_style, str(light.input_text))
        self.assertEqual(app.editor.inner.hint_style, str(light.input_hint))

    def test_theme_dir_comes_from_runtime_paths(self) -> None:
        """用户主题目录必须来自 `runtime.paths.themes` —— 否则自定义主题无从发现。"""
        from pathlib import Path

        from logox.tui.render.app import user_themes_dir

        class _Paths:
            themes = Path("/tmp/logox-themes")

        class _WithPaths:
            paths = _Paths()

        self.assertEqual(user_themes_dir(_WithPaths()), Path("/tmp/logox-themes"))
        # 探不到时**不是报错**，而是"只有内置主题"
        self.assertIsNone(user_themes_dir(_Runtime()))


class EditorTests(unittest.TestCase):
    """输入框 —— 键位表驱动，全部可同步测试。"""

    def make(self) -> Editor:
        return Editor()

    def type_text(self, editor: Editor, text: str) -> None:
        for char in text:
            editor.handle_input(Key(char, char=char))

    def test_typing_inserts_characters(self) -> None:
        editor = self.make()
        self.type_text(editor, "你好abc")
        self.assertEqual(editor.text, "你好abc")

    def test_backspace_removes_one_character(self) -> None:
        editor = self.make()
        self.type_text(editor, "abc")
        editor.handle_input(Key("backspace"))
        self.assertEqual(editor.text, "ab")

    def test_enter_submits_and_clears(self) -> None:
        submitted: list[str] = []
        editor = Editor(on_submit=submitted.append)
        self.type_text(editor, "你好")
        editor.handle_input(Key("enter"))
        self.assertEqual(submitted, ["你好"])
        self.assertEqual(editor.text, "", "提交后应清空输入框")

    def test_shift_enter_inserts_a_newline(self) -> None:
        """★ **D130 恢复**：`Shift+Enter` 换行、且**绝不提交**。

        ⚠️ 前提是**终端能把它与 `Enter` 分开**（本机探针实测 Windows Terminal 不能：
        两者都是 `\r`）。在分不开的终端上它仍然等于提交 —— 那是**物理上限**，
        这里测的是**消费者侧**：只要拿到的 Key 带 shift，就必须换行而不是发送。
        """
        submitted: list[str] = []
        editor = Editor(on_submit=submitted.append)
        self.type_text(editor, "第一行")
        self.assertTrue(editor.handle_input(Key("enter", shift=True)))
        self.type_text(editor, "第二行")
        self.assertEqual(submitted, [], "Shift+Enter 绝不该提交")
        self.assertEqual(editor.text, "第一行\n第二行")

    def test_alt_enter_still_does_nothing(self) -> None:
        """★ **`Alt+Enter` 仍然是去掉的**（D129，未随 D130 恢复）—— 不行换行，也不许降级成提交。

        为什么必须显式吞掉而不是"不登记"：`handle_input` 的 **shift 透明回退**
        会把没登记的组合键按"没按修饰键"再查一次表，而对回车来说那就是 `_submit`
        —— 用户想换行，**半句话离手**。D129 的两个键里现在只剩它还靠这条保护。
        """
        submitted: list[str] = []
        editor = Editor(on_submit=submitted.append)
        self.type_text(editor, "第一行")
        self.assertTrue(editor.handle_input(Key("enter", alt=True)), "必须被消费掉，不能沉到别处")
        self.type_text(editor, "第二行")
        self.assertEqual(submitted, [], "Alt+Enter 把消息发出去了")
        self.assertEqual(editor.text, "第一行第二行", "Alt+Enter 不该换行")

    def test_ctrl_enter_inserts_a_newline(self) -> None:
        """★ 用户要求：`Ctrl+Enter` 换行 —— **它是唯一保留的“修饰键+Enter”换行键**（D129）。

        两条来源（都不靠猜）：① Windows Terminal 上 `Ctrl+Enter` 与所有终端上的
        `Ctrl+J` 发的都是 **LF**，而 `keys.py` 把单独的 LF 解析成“带修饰的 Enter”（D128）；
        ② 支持 Kitty（`CSI 13;5u`）或 modifyOtherKeys（`CSI 27;5;13~`）的终端直接给修饰位。

        反向守卫：不支持协议的终端上它必须**退化成普通 Enter（提交）**，
        而不是"按了没反应"——用户至少不会以为键盘坏了。
        """
        submitted: list[str] = []
        editor = Editor(on_submit=submitted.append)
        self.type_text(editor, "第一行")
        editor.handle_input(Key("enter", ctrl=True))
        self.type_text(editor, "第二行")
        self.assertEqual(submitted, [], "Ctrl+Enter 绝不应当提交")
        self.assertEqual(editor.text, "第一行\n第二行")

    def test_editor_has_no_prompt_glyph(self) -> None:
        """★ 用户裁定：输入框不再有 `❯` 提示符（圆角边框已经说明"这里是输入框"）。

        守两件事：默认值就是空串；空提示符下**渲染宽度仍然精确等于 width**
        （宽度不变量是整个渲染器的硬契约，见 `component.py`）。
        """
        from logox.tui.render.ansi import visible_width
        from logox.tui.render.components.editor import BoxedEditor

        editor = Editor()
        self.assertEqual(editor.prompt, "")
        self.type_text(editor, "你好")
        rows = BoxedEditor(editor).render(30)
        for row in rows:
            self.assertEqual(visible_width(row.plain), 30)
            self.assertNotIn("❯", row.plain)

    def test_plain_enter_still_submits(self) -> None:
        """★ D124 的反向守卫：修 `Shift+Enter` 时**不能**把 `Enter` 弄坏。"""
        submitted: list[str] = []
        editor = Editor(on_submit=submitted.append)
        self.type_text(editor, "要发出的内容")
        editor.handle_input(Key("enter"))
        self.assertEqual(submitted, ["要发出的内容"])

    def test_shift_modified_navigation_keys_do_not_regress(self) -> None:
        """★ D124 的**连带影响守卫**：`shift` 参与归一化后，带 shift 的导航键会换名字查表。

        `Shift+↑` 的编码 `CSI 1;2A` 本来就被解析成 `shift=True`，改动前它**碰巧**能
        当普通 `↑` 用（归一化把 shift 丢了）；加了 shift 之后若不处理，它会
        变成 `"shift+up"` 而**找不到 handler** = 方向键静默失效。

        **对拍口径**：同一个初始状态，一个按带 shift 的键、一个按普通键，
        之后敲同一个字符 —— 文本与光标必须**逐项相同**。
        这比逐条写死期望值可靠：光标位置是"方向键有没有生效"的真实证据。
        """
        for shifted, plain in (
            (Key("up", shift=True), Key("up")),
            (Key("down", shift=True), Key("down")),
            (Key("left", shift=True), Key("left")),
            (Key("right", shift=True), Key("right")),
            (Key("home", shift=True), Key("home")),
            (Key("end", shift=True), Key("end")),
        ):
            with self.subTest(key=str(shifted)):
                a = self.make()
                b = self.make()
                for editor in (a, b):
                    self.type_text(editor, "第一行")
                    editor.handle_input(Key("enter", ctrl=True))  # D129：换行只剩 Ctrl 系
                    self.type_text(editor, "第二行")

                self.assertTrue(a.handle_input(shifted), f"{shifted} 必须被处理，否则是静默失效")
                self.assertTrue(b.handle_input(plain))

                self.type_text(a, "X")
                self.type_text(b, "X")
                # 只比公开的 `text` 就够：光标位置不同 → 同一个 "X" 落点不同 → 文本必然不同。
                # （Editor 没有公开的光标访问器，也不该为了一条用例去暴露内部状态。）
                self.assertEqual(
                    a.text, b.text, f"{shifted} 的文本与光标结果必须与 {plain} 一致"
                )

    def test_alt_shift_navigation_still_works(self) -> None:
        """★ 回退规则的**组合键**覆盖：`Alt+Shift+←` 仍应做词跳跃。

        这条抓的是"只登记 6 条 shift+方向"这种改法的漏网之鱼：
        `Alt+Shift+Left` 归一化后是 `alt+shift+left`（没人登记），
        靠 `handle_input` 的 **shift 透明回退**才回到 `alt+left` → 词跳跃。
        """
        editor = self.make()
        self.type_text(editor, "hello world")
        editor.handle_input(Key("left", alt=True, shift=True))
        self.type_text(editor, "X")
        self.assertEqual(editor.text, "hello Xworld")

    def test_backslash_then_enter_inserts_a_newline(self) -> None:
        """★ **D131：照参考实现 Pi 搬来的\\"文本退路\\"** —— 行尾 ``\\`` + Enter = 换行。

        为什么必须有它：**相当多的终端区分不出 `Shift+Enter` 与 `Enter`**
        （两者是同一个字节 ``\\r``；本机探针实测 Windows Terminal 就是如此）。
        那种终端上，这是用户**唯一**能在输入框里换行的办法 —— 不是锦上添花。

        Pi 的原话（``editor.js`` 提交分支）：
        ``// Workaround for terminals without Shift+Enter support``。
        """
        submitted: list[str] = []
        editor = Editor(on_submit=submitted.append)
        self.type_text(editor, "abc\\")
        editor.handle_input(Key("enter"))
        self.assertEqual(submitted, [], "行尾反斜杠时 Enter 不该提交")
        self.assertEqual(editor.text, "abc\n", "反斜杠应当被吃掉、并换到下一行")

    def test_backslashes_away_from_the_cursor_are_untouched(self) -> None:
        """★ **边界**：反斜杠随便打 —— 开头、中间、路径多段都不受影响。

        用户实测提问：「``\\Pi`` 这不是可以输入反斜杠吗？」—— **对**。
        那条退路**只在「按 Enter 那一瞬间，光标前一个字符是反斜杠」时**才触发；
        只要反斜杠后面还有字符（或光标已越过它），Enter 就是普通提交。
        """
        for typed in (
            "\\Pi",
            "C:\\Windows\\System32",
            "a\\b\\c",
        ):
            with self.subTest(text=typed):
                submitted: list[str] = []
                editor = Editor(on_submit=submitted.append)
                self.type_text(editor, typed)
                editor.handle_input(Key("enter"))
                self.assertEqual(submitted, [typed], f"{typed} 被反斜杠退路劫持了")

    def test_sending_a_message_that_ends_with_a_backslash(self) -> None:
        """★ **代价的精确范围**：只有「以反斜杠结尾的消息」需要多一步，两条 recipe 都能发出。

        这是 D131 引入的唯一代价（Pi 亦然），两条路都会保留那个反斜杠：

        1. 打**两个**反斜杠再回车 —— 退路吃掉一个，留下末尾一个；再回车即发送；
        2. 打完反斜杠后按一下 **←**（光标移到它前面）再回车 —— 直接发送。
        """
        BACKSLASH = "\\"
        # recipe 1：打两个反斜杠 + 回车 + 回车
        submitted: list[str] = []
        editor = Editor(on_submit=submitted.append)
        self.type_text(editor, "C:" + BACKSLASH * 2)
        editor.handle_input(Key("enter"))
        self.assertEqual(submitted, [], "第一次回车不该提交")
        self.assertEqual(editor.text, "C:" + BACKSLASH + "\n", "应当只剩一个反斜杠并在下一行")
        editor.handle_input(Key("enter"))
        self.assertEqual(submitted, ["C:" + BACKSLASH], "第二次回车应当发出末尾带反斜杠的消息")

        # recipe 2：打完反斜杠按 ← 再回车
        submitted2: list[str] = []
        editor2 = Editor(on_submit=submitted2.append)
        self.type_text(editor2, "C:" + BACKSLASH)
        editor2.handle_input(Key("left"))
        editor2.handle_input(Key("enter"))
        self.assertEqual(submitted2, ["C:" + BACKSLASH], "光标越过反斜杠后回车应当直接发送")

    def test_plain_enter_without_backslash_still_submits(self) -> None:
        """★ 第二条反向守卫：没有反斜杠时 Enter 的语义一点没变。"""
        submitted: list[str] = []
        editor = Editor(on_submit=submitted.append)
        self.type_text(editor, "要发的消息")
        editor.handle_input(Key("enter"))
        self.assertEqual(submitted, ["要发的消息"])

    def test_empty_submit_is_ignored(self) -> None:
        submitted: list[str] = []
        editor = Editor(on_submit=submitted.append)
        editor.handle_input(Key("enter"))
        self.assertEqual(submitted, [])

    def test_arrow_navigation(self) -> None:
        editor = self.make()
        self.type_text(editor, "ab")
        editor.handle_input(Key("left"))
        self.type_text(editor, "X")
        self.assertEqual(editor.text, "aXb")

    def test_home_and_end(self) -> None:
        editor = self.make()
        self.type_text(editor, "abc")
        editor.handle_input(Key("home"))
        self.type_text(editor, "1")
        self.assertEqual(editor.text, "1abc")
        editor.handle_input(Key("end"))
        self.type_text(editor, "9")
        self.assertEqual(editor.text, "1abc9")

    def test_delete_word_back(self) -> None:
        editor = self.make()
        self.type_text(editor, "hello world")
        editor.handle_input(Key("w", ctrl=True))
        self.assertEqual(editor.text, "hello ")

    def test_ctrl_u_and_ctrl_k(self) -> None:
        editor = self.make()
        self.type_text(editor, "abcdef")
        editor.handle_input(Key("a", ctrl=True))  # 行首
        editor.handle_input(Key("k", ctrl=True))  # 删到行尾
        self.assertEqual(editor.text, "")

    def test_history_recall(self) -> None:
        editor = Editor()
        self.type_text(editor, "第一条")
        editor.handle_input(Key("enter"))
        editor.handle_input(Key("up"))
        self.assertEqual(editor.text, "第一条")
        editor.handle_input(Key("down"))
        self.assertEqual(editor.text, "", "回到最新应恢复空草稿")

    def test_cursor_marker_present_when_focused(self) -> None:
        """★ IME 光标标记：中文输入法候选窗靠它定位。"""
        from logox.tui.render.components.text import CURSOR_MARKER

        editor = self.make()
        self.type_text(editor, "你好")
        rendered = "".join(row.plain for row in editor.render(40))
        self.assertIn(CURSOR_MARKER, rendered)

    def test_lines_never_exceed_width(self) -> None:
        editor = self.make()
        self.type_text(editor, "很长的内容" * 20)
        for width in (5, 20, 70):
            with self.subTest(width=width):
                for row in editor.render(width):
                    from logox.tui.render.ansi import visible_width

                    self.assertLessEqual(visible_width(row.plain), width)

    def test_alt_b_and_alt_f_word_jump(self) -> None:
        editor = self.make()
        self.type_text(editor, "hello world python")
        self.assertEqual(editor._col, len("hello world python"))
        # Alt+B 跳到 "python" 词首
        editor.handle_input(Key("b", alt=True))
        self.assertEqual(editor._col, len("hello world "))
        # Alt+Left 跳到 "world" 词首
        editor.handle_input(Key("left", alt=True))
        self.assertEqual(editor._col, len("hello "))
        # Alt+F 跳到 "world" 词尾
        editor.handle_input(Key("f", alt=True))
        self.assertEqual(editor._col, len("hello world"))
        # Alt+Right 跳到 "python" 词尾
        editor.handle_input(Key("right", alt=True))
        self.assertEqual(editor._col, len("hello world python"))

    def test_alt_d_delete_word_forward(self) -> None:
        editor = self.make()
        self.type_text(editor, "foo bar baz")
        editor.handle_input(Key("a", ctrl=True))  # 行首
        editor.handle_input(Key("d", alt=True))   # 删掉 foo 和随后的空格
        self.assertEqual(editor.text, "bar baz")

    def test_boxed_editor_renders_rounded_border(self) -> None:
        from logox.tui.render.ansi import visible_width
        from logox.tui.render.components.editor import BoxedEditor

        core = self.make()
        self.type_text(core, "test text")
        boxed = BoxedEditor(core)
        rows = boxed.render(40)
        self.assertEqual(len(rows), 3, "圆角边框应包含顶边、内容行、底边")
        self.assertTrue(rows[0].plain.startswith("╭") and rows[0].plain.endswith("╮"))
        self.assertTrue(rows[1].plain.startswith("│ ") and rows[1].plain.endswith(" │"))
        self.assertTrue(rows[2].plain.startswith("╰") and rows[2].plain.endswith("╯"))
        for row in rows:
            self.assertEqual(visible_width(row.plain), 40)

    def test_boxed_editor_narrow_fallback(self) -> None:
        from logox.tui.render.components.editor import BoxedEditor

        core = self.make()
        self.type_text(core, "abc")
        boxed = BoxedEditor(core)
        # width < 10 自动降级为无边框
        rows = boxed.render(8)
        self.assertFalse(any("╭" in row.plain for row in rows))

    def test_boxed_editor_protocol_delegation(self) -> None:
        from logox.tui.render.components.editor import BoxedEditor

        submitted: list[str] = []
        core = Editor(on_submit=submitted.append)
        boxed = BoxedEditor(core)
        boxed.set_text("hello")
        self.assertEqual(boxed.text, "hello")
        boxed.handle_input(Key("enter"))
        self.assertEqual(submitted, ["hello"])
        self.assertEqual(boxed.text, "")
        boxed.focused = False
        self.assertFalse(core.focused)

    def test_input_text_does_not_inherit_the_border_color(self) -> None:
        """★ 输入正文必须用**正文色**，不得继承边框色（用户报障 + D151）。

        缺陷：`BoxedEditor` 构造内容行是 ``Text("│ ", style=border_style)``，
        而 Rich 的 base style 作用于该行**所有没有 span 的字符** —— 核心编辑器的
        正文恰好没有 span，于是**输入的文字被边框色染上了**。

        为什么这是真缺陷而不是"设计成灰的"：`border_subtle` 是为**装饰线**选的
        （对 `bg_base` 仅 **1.30:1**，几乎不可见），拿它当正文必然看不清。
        用户原话："目前的灰色太暗了在深色背景下可见度极低"。

        断言方式：直接看**渲染出的 ANSI 字节**里正文那一段的颜色。
        只断言 `row.style` 是不够的 —— 真正决定屏幕上是什么的是 ANSI
        （这也是本项目 `screen_emulator` 的同一口径：断言屏幕上等于渲染器以为的）。
        """
        from logox.tui.render.ansi import text_to_ansi
        from logox.tui.render.components.editor import BoxedEditor

        border = "#313244"  # border_subtle：装饰线，极暗
        text = "#cdd6f4"  # text_primary：正文白
        core = self.make()
        self.type_text(core, "你好")
        boxed = BoxedEditor(core, border_style=border, text_style=text)
        body = boxed.render(30)[1]

        ansi = text_to_ansi(body)
        # 正文那一段必须是 text_primary 的 SGR（#cdd6f4 → 38;2;205;214;244）
        self.assertIn("\x1b[38;2;205;214;244m你好", ansi)
        # 且**不得**出现"整个内容行被边框色包住"的旧形态
        self.assertNotIn("\x1b[38;2;49;50;68m│ 你好", ansi)
        # 边框本身仍然是 border_subtle（装饰线不该被一起改亮）
        self.assertIn("\x1b[38;2;49;50;68m│ ", ansi)

    def test_border_color_still_covers_the_decoration(self) -> None:
        """反向守卫：给正文上色**不能**把边框一起改掉。

        两个 token 是分离的：正文 = `text_primary`，装饰线 = `border_subtle`。
        合并它们会让输入框失去"轻量分区"的观感（UI-SPEC §3.1）。
        """
        from logox.tui.render.ansi import text_to_ansi
        from logox.tui.render.components.editor import BoxedEditor

        core = self.make()
        self.type_text(core, "x")
        boxed = BoxedEditor(core, border_style="#313244", text_style="#cdd6f4")
        rows = boxed.render(30)
        for index in (0, 2):  # 顶边与底边
            self.assertIn(
                "\x1b[38;2;49;50;68m",
                text_to_ansi(rows[index]),
                f"第 {index} 行是边框，必须仍是 border_subtle",
            )

    def test_hint_keeps_its_own_faint_color(self) -> None:
        """hint 的淡色必须**优先于**正文色 —— 否则"指路牌"会抢正文的注意力。

        机制：`_tinted` 加的正文 span 在前，hint 自己的 span 平移在后，
        Rich 渲染时**后加的优先**。这条用例就是钉住这个叠加顺序。
        """
        from logox.tui.render.ansi import text_to_ansi
        from logox.tui.render.components.editor import BoxedEditor

        core = Editor(hint="Enter 发送", hint_style="#6c7086")  # text_faint
        boxed = BoxedEditor(core, border_style="#313244", text_style="#cdd6f4")
        # hint 挂在**内容行**（`_add_hint` 作用于 inner_rows[0]），即 BoxedEditor 的 [1]；
        # [0] 是顶边框。
        body = boxed.render(30)[1]

        ansi = text_to_ansi(body)
        self.assertIn("Enter 发送", body.plain)
        # 紧挨着那段文字之前的 SGR 必须是 faint（#6c7086 → 108;112;134）
        self.assertIn("\x1b[38;2;108;112;134mEnter 发送", ansi)

    def test_narrow_fallback_also_tints_the_text(self) -> None:
        """窄屏降级（无边框）也要显式上色。

        否则"把终端拉窄之后输入文字突然变色"会是一个很难解释的现象 ——
        而它恰好发生在用户正在调整窗口的时候。
        """
        from logox.tui.render.ansi import text_to_ansi
        from logox.tui.render.components.editor import BoxedEditor

        core = self.make()
        self.type_text(core, "abc")
        boxed = BoxedEditor(core, border_style="#313244", text_style="#cdd6f4")
        rows = boxed.render(8)  # < 10 → 无边框
        self.assertFalse(any("╭" in row.plain for row in rows))
        self.assertIn("\x1b[38;2;205;214;244m", "".join(text_to_ansi(r) for r in rows))

    def test_no_text_style_keeps_the_old_behavior(self) -> None:
        """不传 `text_style` 时不得改变既有行为（组件默认值保持兼容）。

        这条守的是"新参数是**可选**的" —— 否则任何第三方直接构造
        `BoxedEditor(core)` 的地方都会静默变色。
        """
        from logox.tui.render.ansi import text_to_ansi
        from logox.tui.render.components.editor import BoxedEditor

        core = self.make()
        self.type_text(core, "abc")
        rows = BoxedEditor(core, border_style="#313244").render(30)
        self.assertEqual(rows[1].style, "#313244")
        self.assertNotIn("\x1b[38;2;205;214;244m", text_to_ansi(rows[1]))


class TimelineComponentTests(unittest.TestCase):
    def test_ingests_events_into_blocks(self) -> None:
        from logox.kernel import events as ev

        component = TimelineComponent(load_theme("logox-dark").palette)
        component.ingest(ev.UserPromptSubmit(session_id="s", text="你好", text_chars=2))
        rows = component.render(60)
        self.assertTrue(any("你好" in row.plain for row in rows))

    def test_keeps_only_the_tail_when_bounded(self) -> None:
        """主屏下"上面滚掉"是自然的，因此只需保留尾部若干行（**不需要虚拟化**）。"""
        from logox.kernel import events as ev

        component = TimelineComponent(load_theme("logox-dark").palette, max_height=5)
        for index in range(20):
            component.ingest(ev.UserPromptSubmit(session_id="s", text=f"第 {index} 行", text_chars=4))
        self.assertLessEqual(len(component.render(60)), 5)


class ToolCardDisplayTests(unittest.TestCase):
    """★ **D139**：`edit` 卡片的展开态要能看到"改了哪几行"。

    这是用户报障的原话："**目前 edit 还是无法正常显示**"。
    链路：真工具产出 `DisplayHint(kind="diff", payload={path, hunks})` →
    内核转成 `ev.ToolDisplay` 带上事件 → 界面挂成 diff 块 + 展开态显示完整参数。
    """

    ARGS = {"path": "sample.py", "old_string": "return 'later'", "new_string": "return 'for now'"}
    HUNKS = [
        {
            "header": "@@ -3,4 +3,4 @@",
            "lines": [["context", "def bye():"], ["del", "    return 'later'"], ["add", "    return 'for now'"]],
        }
    ]

    def _app(self):
        from logox.kernel import events as ev

        app, _terminal, _runtime = make_app(width=100, height=30)
        buffer = app.timeline.buffer
        buffer.ingest(ev.ToolCallRequested(session_id="s", call_id="c1", name="edit", args=self.ARGS))
        buffer.ingest(
            ev.ToolCallFinished(
                session_id="s",
                call_id="c1",
                ok=True,
                duration_ms=7,
                content="已成功编辑文件 sample.py（+1 -1 行）",
                change_stat=ev.ChangeStat(kind="modify", added=1, removed=1),
                display=ev.ToolDisplay(
                    kind="diff",
                    payload={"path": "sample.py", "hunks": self.HUNKS},
                ),
            )
        )
        return app

    def test_display_becomes_a_diff_block(self) -> None:
        app = self._app()
        blocks = app.timeline.buffer.blocks
        self.assertIn("diff", [b.kind for b in blocks], "展示提示没有变成 diff 块")
        diff_block = next(b for b in blocks if b.kind == "diff")
        self.assertEqual(diff_block.path, "sample.py")
        self.assertEqual(len(diff_block.hunks), 1)
        self.assertEqual(diff_block.change_stat.added, 1)

    def test_expanded_card_shows_args_and_the_diff(self) -> None:
        app = self._app()
        app.timeline.buffer.expand_tools = True
        app.timeline.buffer.invalidate()
        frame = app.frame_text()
        self.assertIn("old_string", frame, "展开态没显示完整参数（UI-SPEC §5.6 ①）")
        self.assertIn("return 'for now'", frame)
        self.assertIn("@@ -3,4 +3,4 @@", frame, "展开态没渲染 diff hunk")

    def test_collapsed_card_stays_one_line_only(self) -> None:
        """D132：折叠态**只是一行** —— 参数与 diff 都不许漏出来。"""
        app = self._app()
        frame = app.frame_text()
        self.assertNotIn("old_string", frame)
        self.assertNotIn("@@ -3,4 +3,4 @@", frame)

    def test_text_display_keeps_the_plain_payload(self) -> None:
        """`kind="text"` 的展示提示没有专用渲染器 ⇒ 照旧走纯文本输出。"""
        from logox.kernel import events as ev

        app, _terminal, _runtime = make_app(width=100, height=30)
        buffer = app.timeline.buffer
        buffer.ingest(ev.ToolCallRequested(session_id="s", call_id="c1", name="shell", args={"command": "ls"}))
        buffer.ingest(
            ev.ToolCallFinished(
                session_id="s",
                call_id="c1",
                ok=True,
                duration_ms=3,
                content="a.py\nb.py",
                display=ev.ToolDisplay(kind="text", payload={"text": "a.py\nb.py"}),
            )
        )
        app.timeline.buffer.expand_tools = True
        app.timeline.buffer.invalidate()
        frame = app.frame_text()
        self.assertIn("a.py", frame, "text 类展示提示不该把输出藏起来")


class ThinkingAndPayloadColourTests(unittest.TestCase):
    """★ D161：思考正文与工具结果各用**专属** token（此前都是借来的）。

    这组守的是**接线**，不是配色好不好看。

    为什么必须单独守：这两个 token 此前**根本没人读** —— 所以把它们接上之后，
    若将来有人把 `cards.py` 里的取值改回 `text_muted` / `text_primary`（旧值），
    **组件层的用例不会有任何一条变红**（组件只"接收"样式串，不关心谁传的）。
    这与 D152 的 `input_border` 是同一类陷阱，本会话已经栽过一次。
    """

    def _sentinel_ansi(self) -> str:
        """用**哨兵色**渲染（思考正文 #ff0000、工具结果 #00ff00），返回全部 ANSI。

        ⚠️ 为什么必须用哨兵色而不是主题默认值：默认值会**与别的 token 撞色** ——
        实测 `thinking_text` 默认就等于 `text_muted`，而思考*标题行*也用 `text_muted`，
        于是"ANSI 里出现了这个颜色"会被标题行满足，**正文根本没接线也照样变绿**。
        换哨兵色之后，这个颜色在别处不可能出现 ⇒ 命中即证明是**这个元素**在用。
        （第一版就是被这个坑骗过一次，见类 docstring。）
        """
        from logox.kernel import events as ev
        from logox.tui.render.ansi import text_to_ansi
        from logox.tui.theme import load_theme

        palette = load_theme("logox-dark").palette.model_copy(
            update={"thinking_text": "#ff0000", "tool_output_fg": "#00ff00"}
        )
        app, _terminal, _runtime = make_app(width=96, height=30)
        app.timeline.palette = palette
        buffer = app.timeline.buffer
        buffer.add_reasoning("思考内容", duration_ms=1)
        buffer.expand_reasoning = True
        buffer.ingest(ev.ToolCallRequested(session_id="s", call_id="c1", name="read", args={"path": "a.py"}))
        buffer.ingest(
            ev.ToolCallFinished(
                session_id="s",
                call_id="c1",
                ok=True,
                duration_ms=1,
                content="输出内容",
                display=ev.ToolDisplay(kind="text", payload={"text": "输出内容"}),
            )
        )
        buffer.expand_tools = True
        buffer.invalidate()
        return "\n".join(text_to_ansi(row) for row in app.timeline.render(96))

    def test_reasoning_body_follows_thinking_text(self) -> None:
        """展开的思考正文必须跟随 `thinking_text`（哨兵 #ff0000）。

        样式串里带 `italic`，所以 SGR 是 `[3;38;2;255;0;0m` —— 断言只看颜色部分。
        """
        ansi = self._sentinel_ansi()
        self.assertIn(
            "38;2;255;0;0",
            ansi,
            "思考正文没有跟随 thinking_text（#ff0000）—— 接线可能被改回了 text_muted",
        )

    def test_tool_payload_follows_tool_output_fg(self) -> None:
        """工具结果主体必须跟随 `tool_output_fg`（哨兵 #00ff00）。"""
        ansi = self._sentinel_ansi()
        self.assertIn(
            "38;2;0;255;0",
            ansi,
            "工具结果没有跟随 tool_output_fg（#00ff00）—— 接线可能被改回了 text_primary",
        )

    def test_both_elements_are_wired_at_once(self) -> None:
        """两个元素**同时**接上（防止"只接了一个"）。"""
        ansi = self._sentinel_ansi()
        self.assertIn("38;2;255;0;0", ansi)
        self.assertIn("38;2;0;255;0", ansi)


class ToolCardPayloadTests(unittest.TestCase):
    """★ **F-43 收尾（D136）**：工具卡 `Ctrl+O` 展开后必须**真的能看到工具输出**。

    背景：这个功能以前是"按键能用、内容空着" ——
    `ToolCallFinished.content` 一直有值（调度器填充、持久化也在用），
    但 `finish_tool` 不收它、`ingest` 也不传它，于是按 `Ctrl+O` 展开后一片空白。
    """

    OUTPUT = chr(10).join(f"第 {i} 行输出" for i in range(1, 8))

    def _app(self, payload: str, *, ok: bool = True):
        app, _terminal, _runtime = make_app(width=92, height=30)
        buffer = app.timeline.buffer
        buffer.start_tool(call_id="c1", name="shell", args_summary="pytest -q")
        buffer.finish_tool(call_id="c1", ok=ok, duration_ms=12, payload=payload)
        return app

    def test_payload_is_stored_on_the_block(self) -> None:
        """接线不变量：`finish_tool(payload=...)` 必须落到块的 ``payload`` 上。"""
        app = self._app(self.OUTPUT)
        block = next(b for b in app.timeline.buffer.blocks if b.kind == "tool")
        self.assertEqual(block.payload, self.OUTPUT)

    def test_ingest_wires_the_event_content_into_the_card(self) -> None:
        """★ 真正的断点：`ToolCallFinished.content` → 卡片内容。"""
        from logox.kernel import events as ev

        app, _terminal, _runtime = make_app(width=92, height=30)
        buffer = app.timeline.buffer
        buffer.ingest(ev.ToolCallRequested(session_id="s", call_id="c1", name="shell", args={}))
        buffer.ingest(
            ev.ToolCallFinished(session_id="s", call_id="c1", ok=True, duration_ms=5, content=self.OUTPUT)
        )
        block = next(b for b in buffer.blocks if b.kind == "tool")
        self.assertEqual(block.payload, self.OUTPUT, "工具输出没有从事件流进卡片（F-43 复发）")

    def test_collapsed_hides_and_expanded_shows_the_output(self) -> None:
        app = self._app(self.OUTPUT)
        collapsed = app.frame_text()
        self.assertNotIn("第 1 行输出", collapsed, "折叠态不该显示工具输出（D132：只一行）")

        app.press(Key("o", ctrl=True))
        expanded = app.frame_text()
        self.assertIn("第 1 行输出", expanded, "展开后必须能看到工具输出（F-43）")
        self.assertIn("第 7 行输出", expanded)

    def test_huge_output_is_rendered_fully_without_truncation(self) -> None:
        """★ D182：展开后 100% 完整展示全部输出，不再截断行数，亦不显示截断提示。"""
        many = chr(10).join(f"行 {i}" for i in range(1, 50))
        app = self._app(many)
        app.press(Key("o", ctrl=True))
        frame = app.frame_text()
        self.assertIn("行 1", frame)
        self.assertIn("行 30", frame)
        self.assertIn("行 49", frame, "D182: 超出原先 30 行上限的内容必须完整展示")
        self.assertNotIn("只显示前", frame, "D182: 不得出现截断提示")
        self.assertNotIn("未显示", frame, "D182: 不得出现未显示提示")


class TimelineCacheTests(unittest.TestCase):
    """前缀缓存（D56）：主屏下时间线渲染**全部历史**，没有缓存就会越来越慢。

    这些用例守的是一个"性能性质"，而不是"输出对不对"：
    **每帧的代价必须与变化量成正比，而不是与会话长度成正比。**
    而"变化量最大"的场景是**流式正文**——一回合几百条 delta，
    块数不变、只有最后一块在长。那正是缓存必须命中的地方。
    """

    def _component(self, count: int = 12) -> TimelineComponent:
        from logox.kernel import events as ev

        component = TimelineComponent(load_theme("logox-dark").palette)
        for index in range(count):
            component.ingest(
                ev.UserPromptSubmit(session_id="s", text=f"第 {index} 条消息", text_chars=6)
            )
        component.render(60)  # 先渲染一次，把缓存建起来
        return component

    def test_streaming_hits_the_cache(self) -> None:
        """★ 流式正文：块数不变、最后一块在长 → 前面所有块**不该**重新渲染。

        这是最热的一条路径：一回合几百条 ``ModelDelta``。
        缓存不命中意味着"每来一个字就重算整段 Markdown"。
        """
        from logox.kernel import events as ev

        component = self._component()
        before = component.prefix_hits
        for piece in ("第一段", "第二段", "第三段"):
            component.ingest(
                ev.ModelDelta(session_id="s", request_index=0, kind="text", delta=piece)
            )
            component.render(60)
        self.assertGreater(component.prefix_hits, before, "流式期间前缀缓存没有命中")

    def test_repeated_render_hits_the_cache(self) -> None:
        """内容没变时重渲染也走缓存（定时器每 16ms 就可能触发一次渲染）。"""
        component = self._component()
        before = component.prefix_hits
        for _ in range(3):
            component.render(60)
        self.assertGreater(component.prefix_hits, before)

    def test_appending_a_block_only_renders_the_new_block(self) -> None:
        """D199：新消息追加不能再使未变历史全量重排。"""
        from unittest.mock import patch

        from logox.kernel import events as ev
        from logox.tui.content.timeline import render_blocks

        component = self._component()
        component.render(60)
        touched = []
        def counted(blocks, *args, **kwargs):
            touched.extend(blocks)
            return render_blocks(blocks, *args, **kwargs)
        component.ingest(ev.UserPromptSubmit(session_id="s", text="新的一条", text_chars=4))
        with patch("logox.tui.content.timeline.render_blocks", counted):
            component.render(60)
        self.assertEqual([block.text for block in touched], ["新的一条"])

    def test_cache_is_dropped_when_the_width_changes(self) -> None:
        """宽度一变，所有块的折行位置都变——缓存必须整体作废。"""
        component = self._component()
        before = component.cache_invalidations
        component.render(40)
        self.assertGreater(component.cache_invalidations, before, "宽度变了缓存却没作废")

    def test_cache_covers_the_unchanged_last_block(self) -> None:
        """D199：缓存覆盖当前块，内容键负责发现原地变化。"""
        from logox.kernel import events as ev

        component = self._component()
        component.ingest(ev.UserPromptSubmit(session_id="s", text="最后一条", text_chars=4))
        component.render(60)
        self.assertEqual(
            component._cache.covers,  # noqa: SLF001 - 这条不变量只能直接看缓存
            len(component.buffer.visible_blocks),
        )


class FrameCostTests(unittest.TestCase):
    """D126："打字卡顿"的两个真实成因，各用一条**确定性的工作量**断言守住。

    用户报的现象是"输入有明显延迟，做什么操作都卡"。测下来是两个问题叠在一起：

    1. 每帧把**整个会话**的行重新序列化一遍（线段化），代价与会话长度成正比；
    2. 一次按键要画**两帧**（编辑器消费按键时已经画了一帧，`_on_raw_input` 又补了一帧）。

    为什么不断言耗时：耗时随机器负载抖动（M3 的教训——掐表在满负载下偶发失败）。
    "重新序列化了多少行"与"画了几帧"是同一件事，但不依赖机器。
    """

    def _app_with_history(self, messages: int) -> InlineApp:
        app, _terminal, _runtime = make_app()
        for index in range(messages):
            app.timeline.buffer.add_user(f"第 {index} 条提问")
            app.timeline.buffer.add_assistant(f"第 {index} 条回答，含 **加粗** 与 `代码`。")
        app.timeline.invalidate()
        app.screen.render_now()  # 先画一帧，把各项缓存建起来
        return app

    def test_a_keystroke_paints_exactly_one_frame(self) -> None:
        """★ 一次按键只能画一帧。（画两帧时用户感受到的延迟直接翻倍。）"""
        app, _terminal, _runtime = make_app()
        app.screen.render_now()
        before = app.screen.stats["frames"]
        app._on_raw_input("a")  # noqa: SLF001 - 要测的就是这条原始输入入口
        self.assertEqual(
            app.screen.stats["frames"] - before, 1, "一次按键画了两帧"
        )

    def test_a_keystroke_in_a_long_session_touches_only_the_tail(self) -> None:
        """★ 长会话里，敲一个字重新序列化的行数必须只与**尾部**相当。

        这一条是"输入不卡"的可执行定义：断言"重新序列化了几行"，
        而不是"花了几毫秒"。
        """
        app = self._app_with_history(80)
        hits_before = app.screen.stats["ansi_hits"]
        misses_before = app.screen.stats["ansi_misses"]
        app._on_raw_input("a")  # noqa: SLF001
        misses = app.screen.stats["ansi_misses"] - misses_before
        hits = app.screen.stats["ansi_hits"] - hits_before
        self.assertGreater(hits + misses, 300, "用例前提：会话已经足够长")
        self.assertLess(
            misses, 40, f"{hits + misses} 行里重新序列化了 {misses} 行——应当只重算尾部"
        )


class StatusComponentTests(unittest.TestCase):
    """状态行：内容由 `logox.tui.content.status` 算（与旧界面**同一套**裁剪算法）。"""

    def _status(self, width: int, **metrics: object) -> StatusComponent:
        from logox.config.schema import StatusItems

        status = StatusComponent(load_theme("logox-dark").palette, items=StatusItems())
        status.metrics = MetricsReducer().metrics
        for key, value in metrics.items():
            setattr(status.metrics, key, value)
        return status

    def test_shows_model_and_effort(self) -> None:
        status = self._status(100, model="deepseek-v4", thinking_effort="high")
        row = status.render(100)[0].plain
        self.assertIn("deepseek-v4", row)
        self.assertIn("high", row)

    def test_never_exceeds_width(self) -> None:
        """★ 硬约束：状态行永远不能超宽（超宽会让终端折行、界面错位）。"""
        from logox.tui.render.ansi import visible_width

        status = self._status(10, model="m" * 100, thinking_effort="high")
        for width in (10, 40, 120):
            with self.subTest(width=width):
                self.assertLessEqual(visible_width(status.render(width)[0].plain), width)

    def test_real_metrics_reach_the_status_line(self) -> None:
        """★ 状态行现在读的是**真实度量**：token 与费用要能显示出来。

        这一条防的是"状态行写死了几个字"——那种实现看起来正常，
        但用户永远看不到自己花了多少钱。

        ⚠️ ``usage`` 项**默认是关的**（D42：默认只开 model / context /
        throughput / cache 四项）。这里显式打开它，顺带把"开关真的有效"
        这件事也测了。
        """
        from logox.config.schema import StatusItems

        status = StatusComponent(
            load_theme("logox-dark").palette, items=StatusItems(usage=True)
        )
        status.metrics = MetricsReducer().metrics
        status.metrics.model = "m"
        status.metrics.usage_input += 10_000
        status.metrics.usage_output += 2_345
        status.metrics.cost_usd += 0.0123
        row = status.render(120)[0].plain
        self.assertIn("12.3k tok", row)
        self.assertIn("$", row)

    def test_right_hint_is_never_trimmed(self) -> None:
        """右端"在干什么"是唯一常驻线索，**宽度再紧也不能被裁掉**。"""
        status = self._status(30, model="m" * 200)
        status.metrics.generating = True
        row = status.render(30)[0].plain
        self.assertIn("interrupt", row)


class InlineAppTests(unittest.TestCase):
    """整机行为：按键 → 屏幕。"""

    def test_frame_contains_all_three_zones(self) -> None:
        """纯净终端流 = 时间线 + 输入框 + 状态行，**没有侧栏**。"""
        app, _terminal, _runtime = make_app()
        frame = app.frame_text()
        # D126：输入框**不再有 `❯` 提示符**（用户裁定），所以这里改以圆角边框为特征。
        self.assertIn("╭", frame, "缺少输入框（圆角边框）")
        self.assertNotIn("❯", frame, "提示符已按 D126 去掉，不该再出现")
        self.assertIn("Enter 发送", frame, "缺少状态行")

    def test_input_and_status_dock_in_flow_layout(self) -> None:
        """UI-SPEC §2.1 & D105：纯流式布局（Flow Layout），不人工填充空行，紧随时间线自然下挂。"""
        app, _terminal, _runtime = make_app(width=80, height=24)
        frame_lines = app.frame_text().split("\n")
        self.assertEqual(len(frame_lines), 4, "纯流式布局不填充人工空白行")
        self.assertIn("╭", frame_lines[0], "圆角输入框顶边")
        self.assertTrue(
            frame_lines[1].startswith("│ ") and frame_lines[1].endswith(" │"),
            f"输入框内容行应当是框内一行：{frame_lines[1]!r}",
        )
        self.assertIn("Enter 发送", frame_lines[1], "输入框右侧提示在同一行")
        self.assertIn("╰", frame_lines[2], "圆角输入框底边")
        self.assertIn("test-model", frame_lines[3], "状态行紧随输入框")

    def test_no_sidebar_anywhere(self) -> None:

        """★ D81 裁定：不做常驻侧栏。"""
        app, _terminal, _runtime = make_app()
        frame = app.frame_text()
        for forbidden in ("Files", "MCP", "Permissions", "Memory"):
            self.assertNotIn(forbidden, frame, f"出现了侧栏内容：{forbidden}")

    def test_typing_appears_on_screen(self) -> None:
        app, _terminal, _runtime = make_app()
        app.send("你好")
        self.assertIn("你好", app.frame_text())

    def test_enter_sends_to_the_kernel(self) -> None:
        app, _terminal, runtime = make_app()
        app.send("你好\r")
        self.assertEqual(runtime.kernel.started, ["你好"])

    def test_slash_command_is_not_sent_to_the_kernel(self) -> None:
        """斜杠命令走界面自己的命令层，**绝不进内核**（否则模型会收到 "/help"）。"""
        app, _terminal, runtime = make_app()
        app._loop = None  # noqa: SLF001
        app.send("/help\r")
        self.assertEqual(runtime.kernel.started, [], "斜杠命令不该进内核")

    def test_unknown_command_says_so_instead_of_failing_silently(self) -> None:
        app, _terminal, runtime = make_app()
        app.send("/nosuchthing\r")
        self.assertEqual(runtime.kernel.started, [])
        self.assertIn("未知命令", app.frame_text())

    def test_ctrl_t_toggles_thinking_expand(self) -> None:
        """★ D125：思考链归 `Ctrl+T`（**不再是 `Ctrl+O`**）。

        这条用例在重构前是 `test_ctrl_o_toggles_thinking_expand` —— 它固化的正是
        "一个开关管两件事"的旧语义，本轮按用户裁定把它改掉。
        """
        app, _terminal, _runtime = make_app()
        app.timeline.buffer.add_reasoning("这是深入的推导过程，应当在展开时可见")
        # 默认折叠：不显示详细推导内容
        self.assertNotIn("这是深入的推导过程", app.frame_text())
        # 按下 Ctrl+T 展开
        app.press(Key("t", ctrl=True))
        self.assertIn("这是深入的推导过程", app.frame_text())
        # 再次按下 Ctrl+T 折叠
        app.press(Key("t", ctrl=True))
        self.assertNotIn("这是深入的推导过程", app.frame_text())

    def test_ctrl_o_does_not_touch_thinking(self) -> None:
        """★ D125：`Ctrl+O` **只管工具卡与 diff**，不该顺手把思考链也展开。

        这就是"两个独立职责的开关"的验收点：用户只想核对"执行了什么"时，
        不该被迫收下冗长的内心独白。
        """
        app, _terminal, _runtime = make_app()
        app.timeline.buffer.add_reasoning("不该被 Ctrl+O 展开的推导")
        app.timeline.buffer.start_tool(call_id="c1", name="edit", args_summary='path="a.py"')
        app.timeline.buffer.finish_tool(call_id="c1", ok=True, duration_ms=3)
        app.timeline.buffer.attach_diff(
            call_id="c1",
            path="a.py",
            hunks=[DiffHunk(header="@@ -1 +1 @@", lines=(("del", "旧的"), ("add", "新的")))],
        )

        app.press(Key("o", ctrl=True))

        frame = app.frame_text()
        self.assertIn("新的", frame, "Ctrl+O 应展开 diff（Q1=A：diff 与工具卡同一键）")
        self.assertNotIn("不该被 Ctrl+O 展开的推导", frame, "Ctrl+O 不该展开思考链")

    def test_two_expand_switches_are_orthogonal(self) -> None:
        """★ D125：两个开关**互不影响** —— 先 O 再 T，两者同时展开。"""
        app, _terminal, _runtime = make_app()
        app.timeline.buffer.add_reasoning("思考链正文")
        app.timeline.buffer.start_tool(call_id="c1", name="edit", args_summary='path="a.py"')
        app.timeline.buffer.finish_tool(call_id="c1", ok=True, duration_ms=3)
        app.timeline.buffer.attach_diff(
            call_id="c1",
            path="a.py",
            hunks=[DiffHunk(header="@@ -1 +1 @@", lines=(("del", "旧的"), ("add", "新的")))],
        )

        app.press(Key("o", ctrl=True))
        app.press(Key("t", ctrl=True))

        frame = app.frame_text()
        self.assertIn("新的", frame, "工具侧已展开")
        self.assertIn("思考链正文", frame, "思考侧也已展开")
        # 各自再按一次 → 回到全折叠
        app.press(Key("o", ctrl=True))
        app.press(Key("t", ctrl=True))
        frame = app.frame_text()
        self.assertNotIn("新的", frame)
        self.assertNotIn("思考链正文", frame)

    def _card_with_diff(self, ok: bool = True):
        """脚手架：一张 edit 卡 + 一份 diff（两条都用同一个形状，便于对照）。"""
        app, _terminal, _runtime = make_app()
        buffer = app.timeline.buffer
        buffer.start_tool(call_id="c1", name="edit", args_summary='path="a.py"')
        buffer.finish_tool(call_id="c1", ok=ok, duration_ms=3)
        buffer.attach_diff(
            call_id="c1",
            path="a.py",
            hunks=[DiffHunk(header="@@ -1 +1 @@", lines=(("add", "新的"),))],
        )
        return app

    def test_collapsed_tool_call_is_exactly_one_line(self) -> None:
        """★ **D132（用户裁定）：折叠态只有一行** —— 操作 + 参数 + 状态色，不多占一块。

        用户原话："虽然没完全展开，但 diff 和工具调用还是有一块在。"
        旧行为是折叠态**仍输出第二行** `└ diff +1 -1`（外加 `Ctrl+O 展开` 提示），
        而那正是被否掉的"折叠了却还占一块"的中间态。
        """
        app = self._card_with_diff()
        frame = app.frame_text()
        track_lines = [line for line in frame.split("\n") if "▎" in line]

        self.assertEqual(len(track_lines), 1, "折叠态的工具调用必须**只有一行**")
        self.assertIn("edit", track_lines[0], "那一行要能看到操作（工具名）")
        self.assertIn("a.py", track_lines[0], "那一行要能看到参数")
        self.assertNotIn("└", frame, "折叠态不该再有第二行徽标")
        self.assertNotIn("Ctrl+O", frame, "折叠态保持干净：提示只在展开态出现")
        self.assertNotIn("新的", frame, "折叠态绝不显示 diff 正文")

    def test_expanded_card_tells_you_how_to_collapse_it(self) -> None:
        """★ **D132：展开态必须写出"按哪个键收回"。**

        用户原话："我发现 diff 结果我无法关闭，没有这个快捷键" ——
        根因不是没有快捷键，而是**提示只写在折叠态**：展开之后一个字都不写，
        用户于是合理地以为关不掉。（折叠 + 展开两态都要有出口。）
        """
        app = self._card_with_diff()
        app.press(Key("o", ctrl=True))
        frame = app.frame_text()
        self.assertIn("新的", frame, "Ctrl+O 要能展开看到 diff 正文")
        self.assertIn("Ctrl+O 折叠", frame, "展开态必须写出怎么收回")

    def test_a_failed_card_can_be_collapsed_with_ctrl_o(self) -> None:
        """★ **F-40（D132 修）**：失败卡会自动展开一次，但按 `Ctrl+O` **必须收得起来**。

        旧行为：`finish_tool(ok=False)` 写死 `block.expanded=True`，
        而块级覆盖**优先级高于全局开关** → 那张卡永远收不起来
        （当时看不出来，是因为工具输出从未被填充 —— F-43）。
        """
        app = self._card_with_diff(ok=False)
        # ⚠️ 取**工具块**而不是 `blocks[-1]`：`attach_diff` 会在它后面再追加一个 diff 块
        block = next(b for b in app.timeline.buffer.blocks if b.kind == "tool")
        block.payload = "错误详情正文"
        app.timeline.invalidate()

        self.assertIn("错误详情正文", app.frame_text(), "失败卡应当自动展开（D40）")

        app.press(Key("o", ctrl=True))
        self.assertNotIn("错误详情正文", app.frame_text(), "Ctrl+O 必须能收起自动展开的失败卡")

        app.press(Key("o", ctrl=True))
        self.assertIn("错误详情正文", app.frame_text(), "再按一次应当又展开")

    def test_dual_track_user_and_assistant_rendering(self) -> None:
        app, _terminal, _runtime = make_app()
        app.timeline.buffer.add_user("用户提问内容")
        app.timeline.buffer.add_assistant("助手回复正文")
        frame = app.frame_text()
        self.assertIn("▌", frame, "用户消息应包含粗竖线标记")
        self.assertIn("✦ Logox", frame, "助手消息应包含角色标识")
        self.assertIn("▎", frame, "助手消息应包含细竖线标记")

    def test_all_model_operations_grouped_in_green_track(self) -> None:
        """验证模型的思考、工具调用与正文全量归入翡翠绿细轨（▎），且全回合仅有一个 ✦ Logox。"""
        app, _terminal, _runtime = make_app()
        app.timeline.buffer.add_user("帮我看看 main.py")
        app.timeline.buffer.add_reasoning("首先需要分析项目结构与入口文件")
        app.timeline.buffer.start_tool(call_id="c1", name="read_file", args_summary='path="main.py"')
        app.timeline.buffer.finish_tool(call_id="c1", ok=True, duration_ms=15)
        app.timeline.buffer.add_assistant("这是 main.py 的主要逻辑")

        frame = app.frame_text()
        # 1. 验证整个回合只有一个 ✦ Logox 头部
        self.assertEqual(frame.count("✦ Logox"), 1, "同一回合内所有模型操作仅有一个角色头部")
        # 2. 验证用户轨（粗竖线）和模型轨（细竖线）均存在
        self.assertIn("▌", frame)
        self.assertIn("▎", frame)
        # 3. 验证思考摘要和工具卡片都紧随在绿轨下
        self.assertIn("▎ ▸ 思考", frame)
        self.assertIn("▎ ✓ read_file", frame)
        self.assertIn("▎ 这是 main.py 的主要逻辑", frame)

        # 4. 按下 Ctrl+T 展开思考（D125：思考链归 Ctrl+T，Ctrl+O 只管工具与 diff），
        #    验证展开后的内容同样在前缀绿轨下
        app.press(Key("t", ctrl=True))
        expanded_frame = app.frame_text()
        self.assertIn("▎ │ 首先需要分析项目结构与入口文件", expanded_frame)

        # 5. 用户发起第二回合，新回合开始时再次输出 ✦ Logox
        app.timeline.buffer.add_user("第二问")
        app.timeline.buffer.add_assistant("第二答")
        second_frame = app.frame_text()
        self.assertEqual(second_frame.count("✦ Logox"), 2, "跨回合应分别输出角色标识")

    def test_commands_that_need_a_popup_explain_themselves_without_a_loop(self) -> None:
        """★ 没有事件循环时，交互命令必须**报错**而不是永久挂起。

        这条是实测踩出来的：`push_overlay` 在没有循环时 `await` 一个永远
        不会被填的 Future，症状是"界面完全没反应"——比一条明确的错误难查得多。
        """
        app, _terminal, _runtime = make_app()
        app._loop = None  # noqa: SLF001
        app.send("/help\r")
        self.assertIn("事件循环", app.frame_text(), "应当明确说明原因")

    def test_unimplemented_commands_are_named_honestly(self) -> None:
        """还没实现的命令必须说清，而不是假装可用（D47）。"""
        app, _terminal, _runtime = make_app()
        app.send("/files\r")
        frame = app.frame_text()
        self.assertIn("还没有实现", frame)
        self.assertIn("/files", frame, "提示里要带上命令名")

    def test_events_reach_the_timeline(self) -> None:
        """总线事件必须出现在屏幕上（这是"接了内核"的可执行证明）。"""
        import asyncio

        from logox.kernel import events as ev

        app, _terminal, runtime = make_app()

        async def publish() -> None:
            await runtime.bus.publish(
                ev.SessionStart(
                    session_id=runtime.bus.session_id,
                    cwd=".",
                    provider="test",
                    model="test-model",
                    shell_backend="unknown",
                    memory_sources=[],
                )
            )

        asyncio.run(publish())
        self.assertIn("会话开始", app.frame_text())

    def test_resumed_session_does_not_show_initial_startup_info(self) -> None:
        """★ 恢复的历史会话不展示突兀的'会话开始'初始信息。"""
        import asyncio

        from logox.kernel import events as ev

        app, _terminal, runtime = make_app(
            session_replayer=lambda tl: tl.buffer.add_user("历史提问内容")
        )

        async def publish() -> None:
            await runtime.bus.publish(
                ev.SessionStart(
                    session_id=runtime.bus.session_id,
                    cwd=".",
                    provider="test",
                    model="test-model",
                    shell_backend="powershell",
                    memory_sources=[],
                    resumed=True,
                )
            )

        asyncio.run(publish())
        frame = app.frame_text()
        self.assertIn("历史提问内容", frame)
        self.assertNotIn("会话开始", frame)

    def test_ctrl_c_interrupts_when_busy(self) -> None:
        app, _terminal, runtime = make_app()
        app._busy = True  # noqa: SLF001
        app.send("\x03")
        self.assertEqual(runtime.kernel.cancels, 1)
        self.assertIn("已中断", app.frame_text())

    def test_never_exceeds_terminal_width(self) -> None:
        """★ 硬约束：整屏每一行都不能超宽（超宽会让终端折行、界面错位）。"""
        app, _terminal, _runtime = make_app(width=40)
        app.send("一边很长的中文指令" * 5 + "\r")
        app.frame_text()
        app.screen.assert_fits(40)

    def test_status_bar_updates_with_session_and_model_finished(self) -> None:
        """★ 状态行度量绑定：SessionStart 发生后，ModelRequestFinished 的用量与缓存必须实时反映在状态栏。"""
        app, _terminal, runtime = make_app(width=120, height=20)

        async def publish_events() -> None:
            await runtime.bus.publish(
                ev.SessionStart(
                    session_id="test-inline",
                    cwd="/workspace",
                    provider="deepseek",
                    model="deepseek-v4.1-flash",
                    thinking_effort="medium",
                    shell_backend="powershell",
                    memory_sources=[],
                    context_window=1_000_000,
                )
            )
            await runtime.bus.publish(
                ev.ModelRequestFinished(
                    session_id="test-inline",
                    turn=0,
                    usage=ev.Usage(
                        input_tokens=2500,
                        output_tokens=150,
                        cached_input_tokens=1000,
                    ),
                    duration_ms=1000,
                    first_token_ms=200,
                    stop_reason="end_turn",
                )
            )

        asyncio.run(publish_events())
        frame = app.frame_text()
        self.assertIn("deepseek-v4.1-flash · medium", frame)
        self.assertIn("ctx 2.5k/1.0M", frame)
        self.assertIn("cache 40%", frame)



class ReaderThreadIsolationTests(unittest.IsolatedAsyncioTestCase):
    """★ D133：**读线程绝不渲染**（渲染只能发生在事件循环线程上）。

    为什么这条值一条用例：读线程一旦被渲染（写终端 + flush）占住，控制台的输入缓冲就会
    积压 —— 症状是"松开 `a` 之后还会继续打一会儿""退格多删几个"。这条用例直接断言
    "渲染发生在哪个线程"，比断言"耗时"稳得多（不会因机器快慢而谎报）。
    """

    async def test_input_from_the_reader_thread_is_marshalled_to_the_loop(self) -> None:
        import asyncio as _asyncio
        import threading

        app, _terminal, _runtime = make_app()
        app._loop = _asyncio.get_running_loop()  # noqa: SLF001 - 模拟 run() 里做的事
        loop_thread = threading.get_ident()
        app._loop_thread_id = loop_thread  # noqa: SLF001

        rendered_on: list[int] = []
        real_render = app.screen.render_now

        def counting_render() -> None:
            rendered_on.append(threading.get_ident())  # noqa: SLF001
            real_render()

        app.screen.render_now = counting_render  # type: ignore[method-assign]

        thread = threading.Thread(target=lambda: app._on_raw_input("a" * 5))  # noqa: SLF001
        thread.start()
        thread.join()
        await _asyncio.sleep(0.05)  # 让 call_soon_threadsafe 排的回调跑完

        self.assertEqual(app.editor.text, "aaaaa", "读线程交上来的字节没有被处理")
        self.assertTrue(rendered_on, "一帧都没画（合并/转投把它弄丢了）")
        self.assertTrue(
            all(tid == loop_thread for tid in rendered_on),
            f"渲染跑到了读线程上（线程 {rendered_on}，事件循环是 {loop_thread}）",
        )


class KeyboardProtocolTests(unittest.TestCase):
    """键盘协议的协商与**收尾**（对齐 Pi 的 ``queryAndEnableKittyProtocol``）。

    为什么这两头都得测：协商错了只是"某些键用不了"，但**收尾漏了会污染用户的 shell**
    ——退出之后用户按 ``Shift+Enter``，shell 收到的是 ``CSI 27;2;13~`` 这串乱码。
    """

    def test_startup_asks_before_enabling(self) -> None:
        """★ 必须先问（``CSI ? u``）再开。

        直接开是不行的：不支持的终端会**静默忽略**，我们却会以为拿到了修饰位。
        """
        from logox.tui.render.keys import ENABLE_KITTY_KEYBOARD, ENABLE_MODIFY_OTHER_KEYS, KITTY_QUERY

        app, terminal, _runtime = make_app()
        app._enable_keyboard_protocols()  # noqa: SLF001
        written = terminal.output
        self.assertIn(KITTY_QUERY, written, "没有发查询")
        self.assertIn("\x1b[?2004h", written, "没有开启括号粘贴")
        self.assertNotIn(ENABLE_KITTY_KEYBOARD, written, "还没确认支持就直接开了")
        self.assertNotIn(ENABLE_MODIFY_OTHER_KEYS, written, "还没等到超时就开了退路")

    def test_a_response_enables_kitty_and_is_swallowed(self) -> None:
        """终端回应之后启用协议，且**回应本身不能进输入框**。"""
        from logox.tui.render.keys import ENABLE_KITTY_KEYBOARD

        app, terminal, _runtime = make_app()
        app.send("\x1b[?7u")
        self.assertIn(ENABLE_KITTY_KEYBOARD, terminal.output)
        self.assertEqual(app.editor.text, "", "协议回应被当成了输入")
        self.assertTrue(app._kitty_active)  # noqa: SLF001

    def test_no_response_falls_back_to_modify_other_keys(self) -> None:
        """★ 没有 Kitty 回应 → 退到 xterm 的 ``modifyOtherKeys``。

        这条退路是 ``Shift+Enter``（换行）在多数终端上唯一的希望。
        """
        import asyncio

        from logox.tui.render.keys import ENABLE_MODIFY_OTHER_KEYS

        app, terminal, _runtime = make_app()

        async def scenario() -> None:
            app._loop = asyncio.get_running_loop()  # noqa: SLF001
            app._enable_keyboard_protocols()  # noqa: SLF001
            await asyncio.sleep(0.3)

        asyncio.run(scenario())
        self.assertIn(ENABLE_MODIFY_OTHER_KEYS, terminal.output, "没有启用退路")

    def test_an_early_response_cancels_the_fallback(self) -> None:
        """已经确认支持 Kitty 就不该再开退路（两个都开会互相打架）。"""
        import asyncio

        from logox.tui.render.keys import DISABLE_MODIFY_OTHER_KEYS, ENABLE_MODIFY_OTHER_KEYS

        app, terminal, _runtime = make_app()

        async def scenario() -> None:
            app._loop = asyncio.get_running_loop()  # noqa: SLF001
            app._enable_keyboard_protocols()  # noqa: SLF001
            app._on_raw_input("\x1b[?1u")  # noqa: SLF001
            await asyncio.sleep(0.3)

        asyncio.run(scenario())
        self.assertNotIn(ENABLE_MODIFY_OTHER_KEYS, terminal.output)
        self.assertNotIn(DISABLE_MODIFY_OTHER_KEYS, terminal.output)

    def test_ctrl_enter_reaches_the_editor_through_real_bytes(self) -> None:
        """★ D126 的**端到端**用例：字节 → 解析 → 编辑器。

        前面那条 `test_ctrl_enter_inserts_a_newline` 合成的是 `Key(ctrl=True)`，
        只覆盖了**消费者侧**；这里补上真正的字节形态（两条协议各一条），
        否则"解析器认识它、编辑器却没接上"这种断链测不出来。
        """
        for encoded, note in (
            # ★ 第三条是**用户终端上真实发生的那个字节**（探针 `.smoke/probe_win32_enter.py`
            # 实测：Windows Terminal 把 Ctrl+Enter 发成 LF）。前两条是"终端配合时"的形态。
            ("\n", "单独一个 LF（Windows Terminal 上 Ctrl+Enter 的真实形态，D128）"),
            ("\x1b[13;5u", "Kitty 协议（CSI 13;5u）"),
            ("\x1b[27;5;13~", "xterm modifyOtherKeys（CSI 27;5;13~）"),
        ):
            with self.subTest(protocol=note):
                app, _terminal, runtime = make_app()
                app.send("第一行")
                app.send(encoded)
                app.send("第二行")
                self.assertEqual(runtime.kernel.started, [], f"{note}：Ctrl+Enter 把消息发出去了")
                self.assertEqual(app.editor.text, "第一行\n第二行", f"{note}：没有换行")

    def test_windows_console_record_reaches_the_editor_as_a_newline(self) -> None:
        """★★ **D127 的全链路用例**：控制台按键记录 → 字节 → 解析 → 编辑器。

        为什么必须有一条跨到这么远的用例：D126 的用例停在"解析器 + 键位表"，
        而真实缺陷在**更上游** —— Windows 上 `Ctrl+Enter` 与 `Enter` 的字符
        都是 `\r`，"按了 Ctrl"只写在 `dwControlKeyState` 里，而它没被读。
        于是三层的任意一层断掉，用户看到的都是同一句话："Ctrl+Enter 发出去了"。
        """
        from logox.tui.render.terminal import decode_input_records

        class _Key:
            bKeyDown = True
            UnicodeChar = 0x0D  # 与普通 Enter **完全相同**
            dwControlKeyState = 0x0008  # LEFT_CTRL_PRESSED —— 唯一的信息来源

        class _Record:
            EventType = 0x0001
            Event = type("_Event", (), {"KeyEvent": _Key})()

        chars, _resized = decode_input_records([_Record()], 1, 0x0001, 0x0004)
        app, _terminal, runtime = make_app()
        app.send("第一行")
        app.send(chars)
        app.send("第二行")
        self.assertEqual(runtime.kernel.started, [], "Ctrl+Enter 把消息发出去了")
        self.assertEqual(app.editor.text, "第一行\n第二行", "Ctrl+Enter 没有换行")

    def test_everything_is_turned_off_on_exit(self) -> None:
        """★ 收尾：不关掉的话会**污染用户的 shell**。"""
        app, terminal, _runtime = make_app()
        app.send("\x1b[?1u")  # 假装终端支持 Kitty
        terminal.reset()
        app._restore_screen()  # noqa: SLF001
        written = terminal.output
        self.assertIn("\x1b[<u", written, "Kitty 协议没有关闭")
        self.assertIn("\x1b[?2004l", written, "括号粘贴没有关闭")
        self.assertIn("\x1b[?25h", written, "退出后光标必须可见")

    def test_exit_turns_off_the_fallback_when_it_was_used(self) -> None:
        from logox.tui.render.keys import DISABLE_MODIFY_OTHER_KEYS

        app, terminal, _runtime = make_app()
        app._modify_other_keys_active = True  # noqa: SLF001
        terminal.reset()
        app._restore_screen()  # noqa: SLF001
        self.assertIn(DISABLE_MODIFY_OTHER_KEYS, terminal.output)


class InputWiringTests(unittest.TestCase):
    """按键从终端到组件的整条链路。"""

    def test_screen_starts_the_terminal_with_its_own_callbacks(self) -> None:
        app, terminal, _runtime = make_app()
        app.screen.start()
        self.assertTrue(terminal.started, "`Screen.start()` 没有启动终端")
        app.stop()

    def test_terminal_input_reaches_the_editor(self) -> None:
        """★ 端到端：终端推来的字节最终出现在输入框里。

        这一条覆盖的正是最容易"整块缺失"的一环——渲染器写好了、
        按键解析写好了，但**没有人去读终端**，界面看起来就是卡住的。
        """
        app, terminal, _runtime = make_app()
        app.screen.start()
        terminal.send("你好")
        self.assertIn("你好", app.frame_text())
        app.stop()

    def test_ctrl_d_exits(self) -> None:
        app, terminal, _runtime = make_app()
        app.screen.start()
        terminal.send("\x04")
        self.assertFalse(app.screen._running, "Ctrl+D 没有退出")  # noqa: SLF001

    def test_stop_is_idempotent(self) -> None:
        """`stop()` 会被"退出命令"与"read 循环结束"两条路各调一次。"""
        app, _terminal, _runtime = make_app()
        app.stop()
        app.stop()

    def test_bus_unsubscribe_happens_on_stop(self) -> None:
        app, _terminal, _runtime = make_app()
        app.stop()
        self.assertEqual(app._unsubscribe, [])  # noqa: SLF001


class RunInlineTests(unittest.TestCase):
    """`run_inline()`：CLI 真正调用的那个同步入口。

    这一条覆盖的是"最后一公里"——启动路径里唯一没被其它用例走过的地方：
    真的起一个事件循环、真的 start/stop 终端、退出时**真的把终端恢复原状**。
    """

    def test_full_cycle_stops_on_ctrl_d(self) -> None:
        import threading

        from logox.tui.render.app import run_inline

        runtime = _Runtime()
        terminal = FakeTerminal(columns=60, rows=12)
        # Ctrl+D 通过"终端"送达（`FakeTerminal.send` 走的就是 Screen 注册的回调），
        # 所以这一条同时也证明了输入链路是通的。
        timer = threading.Timer(0.2, lambda: terminal.send("\x04"))
        timer.start()
        try:
            code = run_inline(runtime, terminal=terminal)
        finally:
            timer.cancel()

        self.assertEqual(code, 0)
        self.assertTrue(terminal.stopped, "退出时没有停掉终端")
        self.assertIn("╭", terminal.output, "一帧都没画出来")
        self.assertIn("\x1b[?25h", terminal.output, "退出后没有恢复光标")

    def test_terminal_warnings_are_shown_to_the_user(self) -> None:
        """★ 终端降级（旧版 Windows 不支持 VT 输入）必须**说出来**。

        `Win32Terminal` 在这种情况下的设计是"不抛异常、记一条警告继续"，
        所以"方向键为什么没反应"只能靠这条警告解释。
        """
        app, terminal, _runtime = make_app(width=120, height=20)
        terminal.warnings.append("本控制台不支持 VT 输入")
        app._report_terminal_warnings()  # noqa: SLF001
        self.assertIn("不支持 VT 输入", app.frame_text())


class InteractiveCommandTests(unittest.IsolatedAsyncioTestCase):
    """命令的浮层在**真实接线**下跑一遍（按键 → 浮层 → 结果 → 关掉）。

    与 `test_render_commands.py` 的分工：那边测"流程对不对"（假 host），
    这边测"接线通不通"——浮层真的被放到屏幕上、按键真的送进了它、
    关掉之后焦点真的回到了输入框。
    """

    async def asyncSetUp(self) -> None:
        self.app, self.terminal, self.runtime = make_app()
        self.app._loop = asyncio.get_running_loop()  # noqa: SLF001

    async def _press_until(self, key: str, *, ticks: int = 20) -> None:
        """等浮层出现，然后按一个键。"""
        for _ in range(ticks):
            await asyncio.sleep(0)
            if self.app.screen.has_overlay():
                break
        self.app.press(key)

    async def test_help_panel_opens_and_escape_closes_it(self) -> None:
        task = asyncio.create_task(self.app._run_command("/help"))  # noqa: SLF001
        await asyncio.sleep(0)
        self.assertTrue(self.app.screen.has_overlay(), "浮层没有出现")
        self.assertIn("Logox 帮助", self.app.frame_text())
        await self._press_until("escape")
        await task
        self.assertFalse(self.app.screen.has_overlay(), "Esc 没有关掉浮层")
        self.assertIs(self.app.screen.focus, self.app.editor, "焦点没有回到输入框")

    async def test_escape_does_not_leave_the_app_stuck(self) -> None:
        """★ 关掉浮层之后，按键必须继续进输入框。

        漏掉"把焦点还回去"这一步的症状是：浮层关了，但**打字没反应**——
        用户会以为程序死了。
        """
        task = asyncio.create_task(self.app._run_command("/help"))  # noqa: SLF001
        await self._press_until("escape")
        await task
        self.app.send("hi")
        self.assertIn("hi", self.app.frame_text())

    async def test_exit_command_stops_the_app(self) -> None:
        self.app.screen.start()
        await self.app._run_command("/exit")  # noqa: SLF001
        self.assertFalse(self.app.screen._running)  # noqa: SLF001

    async def test_clear_command_empties_the_timeline(self) -> None:
        from logox.kernel import events as ev

        self.app.timeline.ingest(ev.UserPromptSubmit(session_id="s", text="旧内容", text_chars=3))
        self.assertIn("旧内容", self.app.frame_text())
        await self.app._run_command("/clear")  # noqa: SLF001
        self.assertNotIn("旧内容", self.app.frame_text())


class PasteTests(unittest.TestCase):
    """括号粘贴：**一次粘贴 = 一次插入**（否则粘贴 10 行会发出 10 条消息）。"""

    def test_multiline_paste_does_not_submit(self) -> None:
        app, _terminal, runtime = make_app()
        app.send("\x1b[200~第一行\n第二行\n第三行\x1b[201~")
        self.assertEqual(runtime.kernel.started, [], "粘贴不该提交")
        self.assertIn("第三行", app.frame_text())
        # 再按一次回车才提交，而且提交的是**完整的三行**
        app.send("\r")
        self.assertEqual(runtime.kernel.started, ["第一行\n第二行\n第三行"])

    def test_paste_of_a_single_line_is_inserted_verbatim(self) -> None:
        app, _terminal, _runtime = make_app()
        app.send("\x1b[200~sk-abc123\x1b[201~")
        self.assertEqual(app.editor.text, "sk-abc123")

    def test_normal_keys_still_work_after_a_paste(self) -> None:
        app, _terminal, _runtime = make_app()
        app.send("\x1b[200~粘贴\x1b[201~")
        app.send("!")
        self.assertEqual(app.editor.text, "粘贴!")


class TestHelperTests(unittest.TestCase):
    """`press()` 是测试与预览脚本的入口——它静默失效过一次（预览脚本因此卡死）。"""

    def test_press_synthesises_char_for_single_printable_keys(self) -> None:
        """★ ``press("3")`` 必须等于"按了字符 3"。

        ``Key("3")`` 既不是"字符 3"（``char`` 是 ``None``）也不是任何具名键，
        所以它**什么都不会发生**——而这个组件（选择器）恰恰只认带 ``char`` 的
        数字键。症状是"测试挂起"，离原因（少了一个 char）很远。
        """
        app, _terminal, _runtime = make_app()
        received: list[Key] = []
        app.screen.set_focus(_Grabber(received))
        app.press("3")
        self.assertEqual(received, [Key("3", char="3")])

    def test_press_keeps_named_keys(self) -> None:
        app, _terminal, _runtime = make_app()
        received: list[Key] = []
        app.screen.set_focus(_Grabber(received))
        app.press("escape", Key("enter"))
        self.assertEqual(received, [Key("escape"), Key("enter")])


class _Grabber:
    """只记录按键的假组件。"""

    def __init__(self, sink: list[Key]) -> None:
        self.sink = sink
        self.focused = False

    def render(self, width: int) -> list[Text]:
        return []

    def handle_input(self, key: Key) -> bool:
        self.sink.append(key)
        return True

    def invalidate(self) -> None:
        return None


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
