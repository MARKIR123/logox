"""浮层的内容与状态机（**纯逻辑，不含任何 Textual 依赖**）。

为什么它从 `widgets/picker.py` 里搬出来
====================================

这些代码原本住在 ``logox/tui/widgets/picker.py``，功能上一直是"纯函数 + 状态机 +
一层 Textual 包装"。但那个文件**顶部 import 了 Textual**，于是任何想用它们的
地方（包括 D80 之后的纯净终端流渲染器）都会被迫把 Textual 一起拖进来——
而"不装 Textual 也能跑 Logox"正是这次重写要拿的东西之一。

因此按本项目的分层约定做一次拆分：

============================ ==================================================
``logox.tui.content.overlay``        **纯逻辑**：选项状态机 + 三个渲染函数（本文件）
``logox.tui.widgets.picker`` Textual 包装（``ModalScreen``），转发到本文件
============================ ==================================================

**行为一行没改**：搬迁的代码原样保留，`widgets/picker.py` 通过
``from logox.tui.content.overlay import ...`` 重新导出，所以既有的导入路径与测试
（``tests/tui/test_picker_and_login.py``）全部照旧可用。

三条设计约束（都是踩过坑得来的）
==============================

* **渲染与界面框架解耦**：:func:`render_picker` 是纯函数，绝大多数用例不需要
  起一个界面就能覆盖。
* **宽度一律按 cell 算**（``rich.cells.cell_len``）：中文选项用 ``len()`` 补白必然歪
  （M1.5 踩过：``str.ljust`` 让帮助浮层的列对不齐）。
* **内容必须放得进 80×24**：选项多时要在内部滚动或截断，而不是把边框撑破。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rich.cells import cell_len
from rich.text import Text

from logox.config.schema import ThemePalette
from logox.tui.format import clip, pad_right

__all__ = [
    "BOX_WIDTH",
    "MAX_VISIBLE",
    "Choice",
    "PickerState",
    "frame_box",
    "render_confirm",
    "render_picker",
    "render_prompt_form",
]

#: 浮层内容的最大宽度（cell）。与 ``help.py`` 的 BOX_WIDTH 同一量级——
#: 太宽的列表在终端里很难扫读。
BOX_WIDTH = 72
#: 选项最多显示多少行（超出的部分折叠成一行"…还有 N 项"）
MAX_VISIBLE = 12


@dataclass(frozen=True)
class Choice:
    """一个可选项。"""

    value: str
    #: 主文本（看这一列做决定）
    label: str
    #: 右侧说明（可空）。**渲染在右边**，这样左边一列永远是整齐的
    hint: str = ""
    #: 是否禁用（禁用项可见但不可选中——让用户知道"有这个东西，但现在不能用"）
    disabled: bool = False


@dataclass
class PickerState:
    """选择器的可测状态机（与渲染分离）。

    支持**过滤**（像 Pi agent 那样直接打字缩小范围）：``all_choices`` 是全集，
    ``query`` 是当前输入，``choices`` 是过滤后的可见项。
    """

    choices: list[Choice] = field(default_factory=list)
    index: int = 0
    title: str = ""
    footer: str = ""
    #: 用户已输入的过滤串（空 = 不过滤）
    query: str = ""
    #: 可见窗口最多几行（``None`` = 用模块默认 ``MAX_VISIBLE``）；D175 为补全而加
    window_size: int | None = None
    #: 全集（过滤前的原始列表）。为空时 `choices` 就是全集。
    all_choices: list[Choice] = field(default_factory=list)
    #: 是否允许使用快捷键删除选项（如 /resume 弹窗）
    allow_delete: bool = False
    #: 是否正处于二次确认删除态
    confirming_delete: bool = False
    #: 自定义删除确认提示模板（空则使用默认回收站提示）
    delete_prompt: str = ""
    #: 顶部只读展示行（如环境路径、沙箱信息、运行模式，不参与光标导航与筛选）
    header_lines: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.all_choices:
            self.all_choices = list(self.choices)

    def set_query(self, query: str) -> None:
        """按子串过滤（大小写不敏感，匹配 label 或 value）。

        **过滤后把光标重置到第一项**：否则光标可能停在一个已被过滤掉的位置，
        用户按 Enter 会得到一个自己没看见的选项。
        """
        self.query = query
        if not query:
            self.choices = list(self.all_choices)
        else:
            needle = query.casefold()
            self.choices = [
                choice
                for choice in self.all_choices
                if needle in choice.label.casefold() or needle in choice.value.casefold()
            ]
        self.index = next((i for i, c in enumerate(self.choices) if not c.disabled), 0)

    def move(self, delta: int) -> None:
        """上下移动，**跳过禁用项**。全是禁用项时原地不动。"""
        if not self.choices:
            return
        step = 1 if delta > 0 else -1
        position = self.index
        for _ in range(len(self.choices)):
            position = (position + step) % len(self.choices)
            if not self.choices[position].disabled:
                self.index = position
                return

    @property
    def current(self) -> Choice | None:
        if not self.choices:
            return None
        return self.choices[self.index]

    def select(self) -> Choice | None:
        """确认当前项；禁用项或空列表返回 ``None``（**不静默选一个别的**）。"""
        choice = self.current
        return None if choice is None or choice.disabled else choice

    def select_index(self, number: int) -> Choice | None:
        """按**屏幕上的序号**（1 起）选择——数字键直达，和终端里的其它选择器一致。

        序号是**可见列表里的位置**（含被跳过的禁用项），因此与用户看到的一致。
        越界或命中禁用项时返回 ``None``（不静默选别的）。
        """
        position = number - 1
        if position < 0 or position >= len(self.choices):
            return None
        choice = self.choices[position]
        return None if choice.disabled else choice

    @property
    def visible_window(self) -> tuple[int, int]:
        """``(起点, 终点)``——选项过多时保持当前项在窗口内。

        ``window_size`` 可配（D175）：picker 默认 12 行，而 `/` 补全按 Pi 的口径用 5 行。
        窗口大小只影响"显示几行"，与"能选到哪"无关。
        """
        size = self.window_size or MAX_VISIBLE
        total = len(self.choices)
        if total <= size:
            return 0, total
        half = size // 2
        start = max(0, min(self.index - half, total - size))
        return start, start + size


# --------------------------------------------------------------------------- #
# 纯渲染（无界面框架依赖，可直接单测）
# --------------------------------------------------------------------------- #


def frame_box(
    title: str,
    body: list[str],
    footer: str,
    palette: ThemePalette,
    width: int,
    *,
    #: ``{body 行号: 样式}``：给该行**整行**加样式（含左右竖线），如 ``"bold accent"``。
    #: 逐行应用且不含换行符 ⇒ 不会溢出到下一行。D171：picker / 补全 / 确认框共用。
    highlights: dict[int, str] | None = None,
) -> Text:
    """给内容套一个圆角框（宽度按 cell 精算，绝不超过 ``width``）。

    **浮层为什么用边框而不是底色**：D81 撤销了全部背景色（"差一点才看得见"的设计
    在别人的终端上会失效）。边框是**结构信号**——它在任何终端配色下都成立。

    ⚠️ 标题与页脚都要 ``clip`` 到 ``inner``：它们原本直接拼进边框行，
    一旦比内宽还长，**整行就会超宽** → 终端折行 → 浮层与下面的内容全部错位。
    ``inner >= 10`` 而页脚最长有 22 格（"Enter 确认 · Esc 取消"），
    窄终端上必然发生（实测在 width=24 时超了 2 格）。
    """
    inner = max(10, width - 2)  # 左右边框各占 1 cell
    out = Text()
    head = clip(f"─ {title} " if title else "─", inner)
    # ★ D171：边框用输入框的专属 token（input_border），不再用 border_strong。
    #   用户要求「提示框的颜色用和输入框一样的颜色」—— 同一屏里两套边框色看着不协调。
    border_style = str(getattr(palette, "input_border", "") or palette.border_strong)
    out.append("╭" + head + "─" * max(0, inner - cell_len(head)) + "╮\n", style=border_style)
    for index, line in enumerate(body):
        # ★ D171：选中行强调 —— 样式由调用方给（picker / 补全 / 确认框统一传 `bold accent`）。
        #   **逐行应用、不含换行符**：于是「高亮溢出到下一个选项」在机制上不可能发生。
        #   （旧实现是在整块 Text 上按累加偏移 stylize，偏移算错一行 ⇒ 高亮落到下一项，
        #   用户实测报障；那段代码已删除。）
        row_style = (highlights or {}).get(index, "")
        left_style = row_style or border_style
        content_style = row_style or palette.text_primary
        out.append("│", style=left_style)
        out.append(pad_right(clip(line, inner), inner), style=content_style)
        out.append("│\n", style=left_style)
    tail = clip(f"─ {footer} " if footer else "─", inner)
    # ★ D170：下框线与上框线用**同一个**颜色。
    #   以前尾巴用 `border_subtle`（更暗），于是每个提示框的下边框都比其它三边深一档 ——
    #   用户实测报障（`/rewind`、`/resume`、`/` 补全全部中招）。边框是一体的，色调就该一致。
    # ★ D171 修正：页脚也必须用**同一个** border_style ——
    #   否则又回到「下框线比其它三边深一档」的老问题（用户最初报的就是这个，
    #   本探针在改完头/身后当场抓到它又回来了）。
    out.append("╰" + tail + "─" * max(0, inner - cell_len(tail)) + "╯", style=border_style)
    return out


def render_picker(
    state: PickerState, palette: ThemePalette, *, width: int = BOX_WIDTH, numeric: bool = True
) -> tuple[Text, list[tuple[str, str]]]:
    """渲染选项列表。

    :param numeric: 是否在每项前显示数字序号（`1`–`9` 可直接按）。终端选择器的
        惯例，也让"我要第三个"这类表达有落点。
    :returns: ``(要显示的 Text, [(行号, 选项 value), ...])``——第二项是"哪一行对应哪个
        选项"的映射，供**鼠标点击**用（键盘用不上，但点击卡片是 UI-SPEC 承诺的增强）。
    """
    inner = max(10, width - 2)
    start, end = state.visible_window
    lines: list[str] = []
    rows: list[tuple[str, str]] = []

    if state.header_lines:
        for h_line in state.header_lines:
            lines.append(pad_right(clip(h_line, inner), inner))
        lines.append("  " + "─" * max(0, inner - 4))

    if state.query:
        # 过滤中：把输入显式画出来（否则用户不知道自己按的键去哪了）
        lines.append(f"  筛选：{state.query}▏")
    if not state.choices:
        lines.append("  （没有匹配的项）" if state.query else "  （没有可选项）")

    selected_line_idx: int | None = None
    for position in range(start, end):
        choice = state.choices[position]
        # ★ D175：指针字形统一为 ❯（用户反馈 → 太丑；项目里 prompt 浮层也已在用 ❯）
        marker = "❯" if position == state.index else " "
        # 序号按**可见位置**给；若选项标签已经自带 [序号]，则不重复添加数字前缀
        if numeric and not choice.label.strip().startswith("[") and position < 9:
            label = f"{position + 1:>2}  {choice.label}"
        else:
            label = choice.label
        if choice.hint:
            # 说明右对齐——左列整齐才好扫读（用 cell 计算，中文才不会歪）
            hint_len = cell_len(choice.hint)
            avail_label = inner - 3 - hint_len - 2
            if avail_label > 6 and cell_len(label) > avail_label:
                label = clip(label, avail_label, ellipsis="…")
            text = f" {marker} {label}"
            gap = inner - cell_len(text) - hint_len - 2
            text = text + " " * max(1, gap) + choice.hint
        else:
            avail_label = inner - 3
            if avail_label > 3 and cell_len(label) > avail_label:
                label = clip(label, avail_label, ellipsis="…")
            text = f" {marker} {label}"
        lines.append(text)
        if position == state.index and not choice.disabled:
            selected_line_idx = len(lines) - 1
        rows.append((len(lines) - 1, choice.value))

    if end < len(state.choices):
        lines.append(f"   … 还有 {len(state.choices) - end} 项（输入可筛选）")

    if state.confirming_delete:
        choice = state.current
        target_name = choice.label if choice else "当前项"
        lines.append("")
        prompt_tmpl = getattr(state, "delete_prompt", "") or "确定将「{target_name}」移入回收站 (.trash)？"
        lines.append(f"  ⚠️  {prompt_tmpl.format(target_name=target_name)}")
        lines.append("      y / Enter 确认 · n / Esc 取消")
        footer_text = "y/Enter 确认 · n/Esc 取消"
    else:
        footer_text = state.footer

    highlights: dict[int, str] = {}
    if selected_line_idx is not None and 0 <= state.index < len(state.choices):
        highlights[selected_line_idx] = f"bold {palette.accent}"

    text = frame_box(
        state.title,
        lines,
        footer_text,
        palette,
        width,
        # ★ D171：选中行强调（用户裁定「提示框统一这样设计」：`bold accent`，无底色）。
        highlights=highlights,
    )
    return text, rows


def render_prompt_form(
    title: str,
    label: str,
    value: str,
    palette: ThemePalette,
    *,
    width: int = BOX_WIDTH,
    mask: bool = True,
    footer: str = "",
    error: str = "",
) -> Text:
    """渲染单行输入表单（如 API Key）。

    ``mask=True`` 时显示为圆点——**这不是装饰**：密钥一旦明文出现在终端里，
    就会被回滚缓冲、屏幕共享、截图一起带走。用户至少能确认"输了几个字符"。
    """
    inner = max(10, width - 2)
    shown = "•" * len(value) if mask else value
    cursor = "▏"
    lines = [
        f"  {label}",
        "",
        f"  {clip(shown, inner - 6)}{cursor}",
        f"  （{len(value)} 个字符）" if mask else "",
    ]
    if error:
        lines.append("")
        lines.append(f"  ⚠ {error}")
    text = frame_box(title, [line for line in lines], footer, palette, width)
    if error:
        # 错误行标红：位置 = 它所在那一行
        plain = text.plain
        start = plain.rfind(error)
        if start >= 0:
            text.stylize(palette.danger, start, start + len(error))
    return text


def render_confirm(
    title: str,
    question: str,
    palette: ThemePalette,
    *,
    width: int = BOX_WIDTH,
    detail: str = "",
    yes: str = "记住",
    no: str = "仅本次",
) -> Text:
    """渲染是非确认（默认焦点在**否**——安全的那个选项，对齐权限弹窗 §5.8 的做法）。"""
    lines = [f"  {question}"]
    if detail:
        lines += ["", f"  {detail}"]
    lines += ["", f"  [1] {yes}        [2] {no}"]
    # ★ D171：焦点项高亮（与 picker/补全同一套：selected_bg 底色 + 保留 bold accent 的强调）
    focus_row = len(lines) - 1
    return frame_box(
        title,
        lines,
        "Enter 确认 · Esc 取消",
        palette,
        width,
        highlights={focus_row: f"bold {palette.accent}"},
    )
