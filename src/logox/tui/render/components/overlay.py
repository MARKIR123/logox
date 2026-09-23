"""浮层组件：选择器 / 输入框 / 确认框 / 文本面板（D80 §9 第 6 步）。

它们解决什么问题
================

`/login`、`/model`、`/theme` 这些命令需要**在命令行里选东西**——选供应商、
选模型、粘贴密钥、确认"要不要记住"。用纯文本命令也能做（``/model gpt-4o``），
但有两个实际代价：

1. **名字要背下来**：``deepseek-v4.1-flash-expires-on-0910`` 记错一个字符
   就是一次失败请求；
2. **有的选项要额外信息才敢选**（"这个供应商需要密钥"）。

所以做成浮层。

与旧界面的关系（重要）
====================

**外观与手感是同一套**：这些组件把内容交给 :mod:`logox.tui.content.overlay` 与
:mod:`logox.tui.content.help` 渲染——那两个模块是从旧 Textual 组件里原样搬出来的
纯逻辑。所以"选择器长什么样"只有一处定义，两条界面路径不会走样。

差别只在**驱动方式**：

============================ ==================================================
旧（Textual）                ``push_screen_wait`` + ``ModalScreen.dismiss``
新（本模块）                 ``on_done(结果)`` 回调 + ``asyncio.Future``（见 `app.py`）
============================ ==================================================

**为什么用回调而不是抛异常/返回值**：按键处理是同步的（一行一次），而等待用户
选择是异步的。回调是这两者之间最小的接口：组件只负责"我得出结果了"，
至于谁来等、等到之后干什么，组件完全不需要知道（它连 asyncio 都不 import）。

浮层为什么用**边框**而不是底色
============================

D81 撤销了全部背景色——理由是"差一点才看得见"的对比度只在校准终端上成立
（详见决策记录）。边框、字形、缩进是**结构信号**，不依赖任何颜色参数，
在任何终端配色下都成立。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from rich.text import Text

from logox.tui.content.overlay import (
    BOX_WIDTH,
    Choice,
    PickerState,
    render_confirm,
    render_picker,
    render_prompt_form,
)
from logox.permission_types import PermissionAsk, PermissionChoice
from logox.tui.format import clip
from logox.tui.render.ansi import (
    slice_styled,
    split_styled_lines,
    styles_per_char,
    visible_width,
)
from logox.tui.render.components.text import CURSOR_MARKER
from logox.tui.render.keys import Key

__all__ = [
    "ConfirmComponent",
    "PanelComponent",
    "PermissionComponent",
    "PickerComponent",
    "PromptComponent",
]

#: 权限弹窗里参数区最多显示多少行（超过就内部滚动）
PERMISSION_DETAIL_ROWS = 5


def _split(text: Text) -> list[Text]:
    """把渲染出的 ``Text``（含换行）切成帧要用的行列表，**样式逐行保留**。

    ⚠️ 每行都要带上原来的样式：`:mod:`logox.tui.content.overlay` 的渲染结果里，
    边框、高亮、错误行各有各的颜色，丢了样式浮层就变成一片灰。

    实现委托给 :func:`logox.tui.render.ansi.split_styled_lines` —— **全项目只有那
    一处做"切行且保样式"**。这里曾经自己写了一份，而它用的是"span 覆盖基样式"
    的语义（会把基样式丢掉），与 ``text_to_ansi`` 的"叠加"不一致——
    同一件事两种口径，早晚会有一边显示得不对。
    """
    return split_styled_lines(text)


def _insert_text(row: Text, index: int, text: str) -> Text:
    """在 ``row`` 的第 ``index`` 个字符处插入 ``text``，**样式原样保留**。

    为什么不用 ``Text.insert``：rich 的 ``Text`` **没有**这个方法（实测踩到
    ``AttributeError: 'Text' object has no attribute 'insert'``）。
    而"直接改 plain 再重建"会丢掉 spans —— 对输入框来说就是那一行突然变色。
    """
    styles = styles_per_char(row)
    piece = slice_styled(row.plain, styles, 0, index)
    piece.append(text)
    piece.append_text(slice_styled(row.plain, styles, index, len(row.plain)))
    return piece


class _Overlay:
    """浮层组件的公共部分：宽度收敛 + 交回结果。"""

    def __init__(self, *, on_done: Callable[[Any], None] | None = None) -> None:
        self.on_done = on_done
        #: 与 Pi 的 `Focusable.focused` 同名：`Screen.set_focus` 会翻转它，
        #: 编辑器据此停止发 IME 光标标记（否则两个标记会抢同一个硬件光标）。
        self.focused = False
        self._finished = False

    def finish(self, result: Any) -> None:
        """把结果交回去。**幂等**——重复调用只生效一次。

        为什么幂等：一次按键可能被两条路径同时处理（组件自己 + 外层兜底），
        重复回调会让 `/login` 拿到两次结果、跑两遍流程。
        """
        if self._finished:
            return
        self._finished = True
        if self.on_done is not None:
            self.on_done(result)

    def handle_input(self, key: Key) -> bool:  # pragma: no cover - 子类实现
        raise NotImplementedError

    def invalidate(self) -> None:
        return None

    @staticmethod
    def _box_width(width: int) -> int:
        """浮层宽度：和输入框一样占据传入的完整终端宽度（宽度不变量优先）。"""
        return max(20, width)


class PickerComponent(_Overlay):
    """选项列表（供应商 / 模型 / 主题）。

    键位与旧界面完全一致：``↑↓`` 移动、``Enter`` 确认、``1``–``9`` 数字直达、
    其它字符**直接打字筛选**、``Backspace`` 删筛选、``Esc`` 取消
    （有筛选时先清筛选，再按一次才取消）。
    """

    def __init__(
        self,
        state: PickerState,
        palette: Any,
        *,
        on_done: Callable[[Choice | None], None] | None = None,
        on_delete: Callable[[Choice], bool] | None = None,
    ) -> None:
        super().__init__(on_done=on_done)
        self.state = state
        self.palette = palette
        self.on_delete = on_delete or getattr(state, "on_delete", None)

    def render(self, width: int) -> list[Text]:
        box = self._box_width(width)
        rows, _mapping = render_picker(self.state, self.palette, width=box)
        return _split(rows)

    def handle_input(self, key: Key) -> bool:
        # 二次确认态拦截
        if getattr(self.state, "confirming_delete", False):
            if (key.printable and key.char and key.char.lower() == "y") or key.name == "enter":
                choice = self.state.current
                if choice and self.on_delete:
                    keep_open = self.on_delete(choice)
                    if not keep_open:
                        self.finish(None)
                        return True
                    if choice in self.state.choices:
                        self.state.choices.remove(choice)
                    if choice in self.state.all_choices:
                        self.state.all_choices.remove(choice)
                    self.state.confirming_delete = False
                    if not self.state.choices:
                        self.finish(None)
                        return True
                    self.state.index = min(self.state.index, len(self.state.choices) - 1)
                    return True
                self.state.confirming_delete = False
                return True
            if (key.printable and key.char and key.char.lower() == "n") or key.name == "escape":
                self.state.confirming_delete = False
                return True
            return True

        # 触发删除确认（Ctrl+D 或 Delete 键）
        if (
            ((key.ctrl and key.name == "d") or key.name == "delete")
            and getattr(self.state, "allow_delete", False)
            and self.state.current is not None
            and not getattr(self.state.current, "disabled", False)
        ):
            self.state.confirming_delete = True
            return True

        # ⚠️ 刻意**不**把 `j`/`k` 当上下移动：这个选择器支持"直接打字筛选"，
        # 而模型名里真的会出现 j/k（如 `jamba-1.5`）。用 j/k 导航会让用户
        # 永远搜不到那些模型——这种"两个功能抢同一个键"的冲突只在真实数据上现形。
        if key.name == "up":
            self.state.move(-1)
            return True
        if key.name == "down":
            self.state.move(1)
            return True
        if key.name == "enter":
            choice = self.state.select()
            if choice is None:
                return True  # 禁用项：不选，也不结束（不静默选一个别的）
            self.finish(choice)
            return True
        if key.name == "escape":
            if self.state.query:
                # 有筛选时 Esc 先清筛选 —— 否则用户一按就把整个选择取消了
                self.state.set_query("")
                return True
            self.finish(None)
            return True
        if key.name == "backspace":
            if self.state.query:
                self.state.set_query(self.state.query[:-1])
            return True
        if key.name == "paste":
            self.state.set_query(self.state.query + _single_line(key.char or ""))
            return True
        if key.printable and key.char:
            if key.char.isdigit():
                choice = self.state.select_index(int(key.char))
                if choice is not None:
                    self.finish(choice)
                    return True
                # 序号越界：当成筛选字符（用户可能就是想搜 "3"）
            self.state.set_query(self.state.query + key.char)
            return True
        return False


class PromptComponent(_Overlay):
    """单行输入（API Key）。

    ``mask=True`` 时显示成圆点：**这不是装饰**——密钥一旦明文出现在终端里，
    就会被回滚缓冲、屏幕共享、截图一起带走。用户至少能确认"输了几个字符"。
    """

    def __init__(
        self,
        *,
        title: str,
        label: str,
        palette: Any,
        mask: bool = True,
        footer: str = "Enter 确认 · Esc 取消",
        error: str = "",
        on_done: Callable[[str | None], None] | None = None,
    ) -> None:
        super().__init__(on_done=on_done)
        self.title = title
        self.label = label
        self.palette = palette
        self.mask = mask
        self.footer = footer
        self.error = error
        self.value = ""
        self._col = 0

    def render(self, width: int) -> list[Text]:
        box = self._box_width(width)
        body = render_prompt_form(
            self.title,
            self.label,
            self.value,
            self.palette,
            width=box,
            mask=self.mask,
            footer=self.footer,
            error=self.error,
        )
        rows = _split(body)
        # 把 IME 光标标记插在输入光标那个字符（`▏`）之前，让硬件光标停在这里。
        # 不插的话中文输入法的候选窗会飘到别处——而"粘贴密钥"往往正是用输入法
        # 或剪贴板工具完成的。
        for index, row in enumerate(rows):
            position = row.plain.find("▏")
            if position >= 0:
                rows[index] = _insert_text(row, position, CURSOR_MARKER)
                break
        return rows

    def handle_input(self, key: Key) -> bool:
        if key.name == "enter":
            self.finish(self.value.strip())
            return True
        if key.name == "escape":
            self.finish(None)
            return True
        if key.name == "paste":
            # 单行字段：粘贴内容里的换行直接丢掉（否则值里会带上 `\n`，
            # 写进 `.env` 就变成两行，读回来是坏密钥）。
            self._insert(_single_line(key.char or ""))
            return True
        if key.name == "backspace":
            if self._col > 0:
                self.value = self.value[: self._col - 1] + self.value[self._col :]
                self._col -= 1
            return True
        if key.name == "delete":
            self.value = self.value[: self._col] + self.value[self._col + 1 :]
            return True
        if key.name == "left":
            self._col = max(0, self._col - 1)
            return True
        if key.name == "right":
            self._col = min(len(self.value), self._col + 1)
            return True
        if key.name == "home" or (key.ctrl and key.name == "a"):
            self._col = 0
            return True
        if key.name == "end" or (key.ctrl and key.name == "e"):
            self._col = len(self.value)
            return True
        if key.ctrl and key.name == "u":
            self.value = ""
            self._col = 0
            return True
        if key.ctrl and key.name == "w":
            self._delete_word_back()
            return True
        if key.printable and key.char:
            self._insert(key.char)
            return True
        return False

    def _insert(self, text: str) -> None:
        if not text:
            return
        self.error = ""  # 一开始输入就把上一次的错误提示清掉
        self.value = self.value[: self._col] + text + self.value[self._col :]
        self._col += len(text)

    def _delete_word_back(self) -> None:
        index = self._col
        while index > 0 and self.value[index - 1].isspace():
            index -= 1
        while index > 0 and not self.value[index - 1].isspace():
            index -= 1
        self.value = self.value[:index] + self.value[self._col :]
        self._col = index


class ConfirmComponent(_Overlay):
    """是非确认（"要把密钥写进文件吗？"）。

    **默认焦点在"否"**（与权限弹窗 UI-SPEC §5.8 同一条安全默认）：
    误按一次 Enter 不应该做出"把密钥写进磁盘"这种决定。

    ⚠️ 与旧界面的**一处刻意差异**：旧界面里 ``Enter`` 没有绑定（只有 ``1``/``2``/``Esc``），
    这里让 ``Enter`` 执行**当前焦点**那一项，而焦点默认在"否"——于是"直接回车"
    等于选了安全的那个，而不是"什么都不发生"。用户少按一次键，语义也不含糊。
    """

    def __init__(
        self,
        *,
        title: str,
        question: str,
        palette: Any,
        detail: str = "",
        yes: str = "记住",
        no: str = "仅本次",
        on_done: Callable[[bool | None], None] | None = None,
    ) -> None:
        super().__init__(on_done=on_done)
        self.title = title
        self.question = question
        self.palette = palette
        self.detail = detail
        self.yes = yes
        self.no = no
        #: 0 = 是，1 = 否。**默认 1（否）**
        self.focus_index = 1

    def render(self, width: int) -> list[Text]:
        box = self._box_width(width)
        body = render_confirm(
            self.title,
            self.question,
            self.palette,
            width=box,
            detail=self.detail,
            yes=self.yes,
            no=self.no,
        )
        rows = _split(body)
        self._highlight_focus(rows, box)
        return rows

    def _highlight_focus(self, rows: list[Text], box: int) -> None:
        """给当前焦点那一项加粗 + 主题色（**唯一的焦点指示**）。"""
        target = f"[{self.focus_index + 1}] "
        for row in rows:
            index = row.plain.find(target)
            if index >= 0:
                end = index + len(target) + len(self.yes if self.focus_index == 0 else self.no)
                row.stylize(f"bold {self.palette.accent}", index, min(end, len(row.plain)))
                return

    def handle_input(self, key: Key) -> bool:
        if key.name in ("left", "right", "up", "down", "tab"):
            self.focus_index = 1 - self.focus_index
            return True
        if key.name == "enter":
            self.finish(self.focus_index == 0)
            return True
        if key.name == "escape":
            self.finish(None)
            return True
        if key.printable and key.char:
            char = key.char.lower()
            if char in ("1", "y"):
                self.finish(True)
                return True
            if char in ("2", "n"):
                self.finish(False)
                return True
        return False


class PanelComponent(_Overlay):
    """只读文本面板（``/help``、``/status``）。

    内容超出可见高度时**可以在面板内滚动**：主屏模式下滚出屏幕的内容进了终端的
    回滚缓冲，但浮层是"盖在"内容上的，所以它得自己滚——否则 ``/help`` 在 24 行
    的终端上会被砍掉一截，而"看不到的命令"比"排版难看"严重得多。

    ``max_rows`` 由调用方给（它知道终端有多高），组件自己不读终端——
    这样它的行为完全可以用一个数字来测。
    """

    def __init__(
        self,
        text: Text,
        *,
        palette: Any,
        max_rows: int = 20,
        footer: str = "↑↓ 滚动 · Esc 关闭",
        on_done: Callable[[None], None] | None = None,
    ) -> None:
        super().__init__(on_done=on_done)
        self.text = text
        self.palette = palette
        self.max_rows = max(3, max_rows)
        self.footer = footer
        self.offset = 0

    def render(self, width: int) -> list[Text]:
        body = _split(self.text)
        # 留 1 行给底部的滚动提示（它被裁掉的话用户就不知道还能翻）
        visible = max(1, self.max_rows - 1)
        self.offset = max(0, min(self.offset, max(0, len(body) - visible)))
        window = [_clip_row(row, width) for row in body[self.offset : self.offset + visible]]
        if len(body) > visible:
            hint = f"── 第 {self.offset + 1}–{self.offset + len(window)} 行 / 共 {len(body)} 行 · {self.footer} ──"
            window = [*window, Text(clip(hint, width), style=self.palette.text_faint)]
        return window

    def handle_input(self, key: Key) -> bool:
        if key.name in ("escape", "enter", "q"):
            self.finish(None)
            return True
        if key.name == "up":
            self.offset -= 1
            return True
        if key.name == "down":
            self.offset += 1
            return True
        if key.name == "pageup":
            self.offset -= max(1, self.max_rows - 2)
            return True
        if key.name == "pagedown":
            self.offset += max(1, self.max_rows - 2)
            return True
        if key.name == "home":
            self.offset = 0
            return True
        if key.name == "end":
            self.offset = len(_split(self.text))
            return True
        return False


def _clip_row(row: Text, width: int) -> Text:
    """把一行裁到 ``width`` 以内（**保留样式**）。

    面板的内容来自别处的渲染结果（帮助 / 状态 / 事件流），宽度不受它控制，
    所以它必须自己兜住——组件的契约是"每一行都 <= width"，
    而超宽的症状是整个界面错位（见 `component.py` 的说明）。
    """
    if visible_width(row.plain) <= width:
        return row
    styles = styles_per_char(row)
    kept = 0
    cut = len(row.plain)
    for index, char in enumerate(row.plain):
        char_width = visible_width(char) or 1
        if kept + char_width > width:
            cut = index
            break
        kept += char_width
    return slice_styled(row.plain, styles, 0, cut)


def _single_line(text: str) -> str:
    """去掉换行（单行字段用）。

    **为什么必须做**：粘贴一个密钥时终端常常在末尾带一个换行。它进了 ``.env``
    就变成两行，读回来是坏密钥——而报错是"鉴权失败"，离原因很远。
    """
    return text.replace("\r\n", "").replace("\r", "").replace("\n", "")


class PermissionComponent(_Overlay):
    """**需要授权**弹窗（UI-SPEC §5.8）。

    与 :class:`ConfirmComponent` 的区别不只是"多两个选项"：这是一个**高危决策**，
    因此：

    * 参数区**绝不截断**——用户得看得见完整命令才敢判断（超过 5 行内部滚动）；
    * 默认焦点在 ``[3] 拒绝``：**误按 Enter 是拒绝**，不是放行；
    * ``Esc`` 等同于拒绝；
    * 工具非只读时顶部加警示条（``danger`` 色）。

    四个选项与 :class:`~logox.permission_types.PermissionChoice` 一一对应。
    ``allow_session`` / ``allow_project`` 为假时对应选项**不出现在屏幕上**——
    画一个按了没用的按钮比不画更糟（用户会以为"我允许了"）。
    """

    def __init__(
        self,
        ask: PermissionAsk,
        palette: Any,
        *,
        max_rows: int = 0,
        on_done: Callable[[PermissionChoice], None] | None = None,
    ) -> None:
        super().__init__(on_done=on_done)
        self.ask = ask
        self.palette = palette
        #: 弹窗最多占多少行（0 = 不限）。**必须有上限**：浮层比终端还高时，
        #: 多出来的行会被推到屏幕外——连四个选项一起推出去，用户就**没法回答**了。
        self.max_rows = max(0, max_rows)
        self.offset = 0
        #: 可选项列表：``(选项, 标签)``。默认焦点落在"拒绝"上（见 `_default_index`）
        self.options: list[tuple[PermissionChoice, str]] = self._build_options()
        self.focus_index = self._default_index()
        #: 补充要求输入框状态（D98）
        self.input_value: str = ""
        self.input_cursor: int = 0
        self.focus_field: str = "options"  # "options" | "input"

    # -- 选项 ------------------------------------------------------------ #

    def _build_options(self) -> list[tuple[PermissionChoice, str]]:
        options = [(PermissionChoice.ONCE, "仅本次允许")]
        target = f"{self.ask.tool}:{self.ask.rule}" if (self.ask.rule and self.ask.rule != "*") else self.ask.tool
        if self.ask.allow_session:
            options.append((PermissionChoice.SESSION, f"本会话总是允许 {target}"))
        options.append((PermissionChoice.DENY, "拒绝"))
        if self.ask.allow_project:
            options.append((PermissionChoice.PROJECT, f"持久允许 {target}（写入项目 state.toml）"))
        return options

    def _default_index(self) -> int:
        """默认焦点 = **拒绝**（UI-SPEC §5.8 的安全默认）。"""
        for index, (choice, _label) in enumerate(self.options):
            if choice is PermissionChoice.DENY:
                return index
        return 0

    # -- 渲染 ------------------------------------------------------------ #

    def _fixed_rows(
        self, inner: int, *, with_rule: bool, with_cwd: bool, with_input_blank: bool = True
    ) -> int:
        """除参数区之外，这个弹窗**必须**占用的行数（含边框与末行滚动提示）。"""
        rows = 2  # 上下边框
        if self.ask.risk == "high":
            rows += len(self._wrap(self.ask.risk_note or "高风险操作", inner - 2)) + 1
        rows += len(self._wrap(f"工具：{self.ask.tool}", inner - 2)) + 1
        if with_rule:
            rows += len(self._wrap(f"命中规则：{self.ask.rule}", inner - 2)) + 1
        if with_cwd:
            rows += len(self._wrap(f"工作目录：{self.ask.cwd}", inner - 2))
        rows += 1  # 选项区之前的空行
        for index, (_choice, label) in enumerate(self.options, start=1):
            rows += len(self._wrap(f"[{index}] {label}", inner - 2))
        rows += 1  # ❯ 输入框行
        if with_input_blank:
            rows += 1  # 补充要求输入框前的空行
        return rows

    def _detail_capacity(
        self,
        inner: int,
        *,
        with_rule: bool,
        with_cwd: bool,
        budget: int,
        with_input_blank: bool = True,
    ) -> int:
        """参数区能占几行。**选项优先**：宁可少显示参数，也不能把按钮挤出屏幕。"""
        if budget <= 0:
            return PERMISSION_DETAIL_ROWS
        room = budget - self._fixed_rows(
            inner, with_rule=with_rule, with_cwd=with_cwd, with_input_blank=with_input_blank
        )
        return max(1, min(PERMISSION_DETAIL_ROWS, room))

    def minimum_rows(self, inner: int) -> int:
        """这个弹窗**最小**需要多少行（只留 1 行参数、去掉规则与工作目录）。

        界面用它做判断：终端比这个还矮时**问都问不出结果**——那时候必须
        走"拒绝 + 说明原因"，而不是画一张连按钮都看不见的弹窗（那会让回合卡死）。
        """
        return self._fixed_rows(inner, with_rule=False, with_cwd=False, with_input_blank=False) + 1

    def _effective_rows(self, inner: int) -> int:
        """实际使用的高度上限。"""
        if self.max_rows <= 0:
            return 0
        return max(self.minimum_rows(inner), self.max_rows)

    def _body(self, inner: int, budget: int) -> list[str]:
        """按内宽**折行**拼出内容（绝不横向截断）。"""
        ask = self.ask
        with_input_blank = budget <= 0 or budget >= 16
        with_rule = bool(ask.rule)
        with_cwd = bool(ask.cwd)
        capacity = self._detail_capacity(
            inner, with_rule=with_rule, with_cwd=with_cwd, budget=budget, with_input_blank=with_input_blank
        )
        if budget > 0 and capacity <= 1 and with_cwd:
            with_cwd = False
            capacity = self._detail_capacity(
                inner, with_rule=with_rule, with_cwd=with_cwd, budget=budget, with_input_blank=with_input_blank
            )
        if budget > 0 and capacity <= 1 and with_rule:
            with_rule = False
            capacity = self._detail_capacity(
                inner, with_rule=with_rule, with_cwd=with_cwd, budget=budget, with_input_blank=with_input_blank
            )
        if budget > 0 and capacity <= 1 and with_input_blank:
            with_input_blank = False
            capacity = self._detail_capacity(
                inner, with_rule=with_rule, with_cwd=with_cwd, budget=budget, with_input_blank=with_input_blank
            )

        body: list[str] = []
        if ask.risk == "high":
            body.extend(self._wrap("▶▶ " + (ask.risk_note or "高风险操作"), inner - 2, indent="  "))
            body.append("")
        body.extend(self._wrap(f"工具：{ask.tool}", inner - 2, indent="  "))
        body.append("")
        body.extend(self._detail_window(inner, capacity))
        if with_rule:
            source = f"（{ask.rule_scope}）" if ask.rule_scope else ""
            body.append("")
            body.extend(self._wrap(f"命中规则：{ask.rule}{source}", inner - 2, indent="  "))
        if with_cwd:
            body.extend(self._wrap(f"工作目录：{ask.cwd}", inner - 2, indent="  "))
        body.append("")
        for index, (_choice, label) in enumerate(self.options, start=1):
            body.extend(self._wrap(f"[{index}] {label}", inner - 2, indent="  "))

        # 补充要求输入框（内有灰色提示字“输入要求...”）
        if with_input_blank:
            body.append("")
        cursor_char = "▏" if self.focus_field == "input" else ""
        if self.input_value:
            text_disp = self.input_value[: self.input_cursor] + cursor_char + self.input_value[self.input_cursor :]
            body.extend(self._wrap(f"❯ {text_disp}", inner - 2, indent="  "))
        else:
            body.extend(self._wrap(f"❯ 输入要求...{cursor_char}", inner - 2, indent="  "))
        return body

    @staticmethod
    def _wrap(text: str, width: int, *, indent: str = "") -> list[str]:
        """按 cell 宽度折行并加缩进（``width`` 是**扣掉缩进之后**的可用宽度）。"""
        from logox.tui import format as fmt

        wrapped = fmt.wrap_cells(text, max(8, width))
        return [indent + row for row in wrapped.split("\n")]

    def _detail_window(self, inner: int, capacity: int) -> list[str]:
        """参数区：**先折行、再按行数开窗滚动**。"""
        from logox.tui import format as fmt

        raw = (self.ask.detail or "（无参数）").split("\n")
        rows: list[str] = []
        for line in raw:
            rows.extend(
                "  " + row for row in fmt.wrap_cells(line, max(8, inner - 2)).split("\n")
            )
        total = len(rows)
        hint_rows = 1 if total > capacity and capacity > 1 else 0
        visible = max(1, capacity - hint_rows)
        self.offset = max(0, min(self.offset, max(0, total - visible)))
        window = rows[self.offset : self.offset + visible]
        if hint_rows:
            window = [
                *window,
                f"  …… 第 {self.offset + 1}–{self.offset + len(window)} 行 / 共 {total} 行"
                "（PgUp/PgDn 滚动）",
            ]
        return window

    def render(self, width: int) -> list[Text]:
        box = self._box_width(width)
        inner = max(10, box - 2)
        rows = _split(
            render_frame_only(
                self._body(inner, self._effective_rows(inner)),
                "需要授权",
                "数字键选择 · Enter 确认 · Esc 拒绝 · 亦可直接输入要求",
                self.palette,
                box,
            )
        )
        self._highlight_focus(rows)
        return rows

    def _highlight_focus(self, rows: list[Text]) -> None:
        """把焦点标出来（加粗 + 主题色）。"""
        if self.focus_field == "options":
            marker = f"  [{self.focus_index + 1}] "
            for row in rows:
                index = row.plain.find(marker)
                if index >= 0:
                    row.stylize(f"bold {self.palette.accent}", index, len(row.plain))
                    break
        else:
            for row in rows:
                index = row.plain.find("  ❯ ")
                if index >= 0:
                    row.stylize(f"bold {self.palette.accent}", index, index + 4)
                    break

        # 任何时候输入框若包含未修改的占位符，将占位符置灰 (text_faint)
        for row in rows:
            ph_idx = row.plain.find("输入要求...")
            if ph_idx >= 0:
                row.stylize(str(self.palette.text_faint), ph_idx, ph_idx + len("输入要求..."))

        # 语法高亮 Diff 变更行（+ 为绿，- 为红，@@ 为青色）
        for row in rows:
            plain_trimmed = row.plain.strip()
            if plain_trimmed.startswith("@@"):
                row.stylize("bold cyan")
            elif plain_trimmed.startswith("+") and not plain_trimmed.startswith("+++"):
                row.stylize("green")
            elif plain_trimmed.startswith("-") and not plain_trimmed.startswith("---"):
                row.stylize("red")

    # -- 输入 ------------------------------------------------------------ #

    def handle_input(self, key: Key) -> bool:
        if self.focus_field == "options":
            if key.name == "down":
                if self.focus_index < len(self.options) - 1:
                    self.focus_index += 1
                else:
                    self.focus_field = "input"
                return True
            if key.name == "up":
                if self.focus_index > 0:
                    self.focus_index -= 1
                else:
                    self.focus_field = "input"
                return True
            if key.name == "tab":
                self.focus_field = "input"
                return True
            if key.name in ("pageup", "pagedown"):
                step = PERMISSION_DETAIL_ROWS if key.name == "pagedown" else -PERMISSION_DETAIL_ROWS
                self.offset = max(0, self.offset + step)
                return True
            if key.name == "enter":
                self.finish(self.options[self.focus_index][0])
                return True
            if key.name == "escape":
                self.finish(PermissionChoice.DENY)
                return True
            if key.printable and key.char:
                if key.char.isdigit():
                    position = int(key.char) - 1
                    if 0 <= position < len(self.options):
                        self.finish(self.options[position][0])
                        return True
                # 用户键入非数字字符，自动切入输入框
                self.focus_field = "input"
                self._insert_char(key.char)
                return True
            return False

        # focus_field == "input"
        if key.name == "up":
            self.focus_field = "options"
            return True
        if key.name in ("down", "tab"):
            self.focus_field = "options"
            return True
        if key.name == "enter":
            self.finish(PermissionChoice.DENY)
            return True
        if key.name == "escape":
            if self.input_value:
                self.input_value = ""
                self.input_cursor = 0
                self.focus_field = "options"
                return True
            self.finish(PermissionChoice.DENY)
            return True
        if key.name == "backspace":
            if self.input_cursor > 0:
                self.input_value = self.input_value[: self.input_cursor - 1] + self.input_value[self.input_cursor :]
                self.input_cursor -= 1
            elif not self.input_value:
                self.focus_field = "options"
            return True
        if key.name == "delete":
            if self.input_cursor < len(self.input_value):
                self.input_value = self.input_value[: self.input_cursor] + self.input_value[self.input_cursor + 1 :]
            return True
        if key.name == "left":
            self.input_cursor = max(0, self.input_cursor - 1)
            return True
        if key.name == "right":
            self.input_cursor = min(len(self.input_value), self.input_cursor + 1)
            return True
        if key.name == "home":
            self.input_cursor = 0
            return True
        if key.name == "end":
            self.input_cursor = len(self.input_value)
            return True
        if key.name == "paste":
            self._insert_char(key.char or "")
            return True
        if key.printable and key.char:
            self._insert_char(key.char)
            return True
        return False

    def _insert_char(self, text: str) -> None:
        if not text:
            return
        single = text.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
        self.input_value = (
            self.input_value[: self.input_cursor] + single + self.input_value[self.input_cursor :]
        )
        self.input_cursor += len(single)


def render_frame_only(
    body: list[str], title: str, footer: str, palette: Any, width: int
) -> Text:
    """给**已经排版好**的行套上圆角框（供权限弹窗这类需要自己控制换行的组件用）。

    :func:`logox.tui.content.overlay.render_picker` 等函数自己负责内容与边框的配合；
    权限弹窗的内容是**逐行拼出来**的（有段间距与滚动提示），所以它只借用边框。
    """
    from logox.tui.content.overlay import frame_box

    return frame_box(title, body, footer, palette, width)
