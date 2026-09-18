"""ANSI 感知的宽度计算（D80 / MODULE_tui_render §4.6）。

为什么需要这一层
----------------
`rich.cells.cell_len` 是**按原始字节**数的——它不知道转义序列的存在：

.. code-block:: python

   cell_len("\\x1b_Ga=p\\x1b\\\\")   # → 7    但它实际占用 **0** 格

于是只要输出里带了任何转义序列（IME 光标标记就是 APC 序列），
**所有宽度计算都会偏大**，而行宽不变量是整个渲染器的硬契约（见 `component.py`）。
后果是"行看着没超宽、实际超了"→ 终端折行 → **整个界面错位**。

因此本模块提供 :func:`visible_width`：**先把转义序列剥掉，再量宽度**。
`logox.tui.render` 里的所有宽度计算都应当用它，而不是直接用 `cell_len`。
"""

from __future__ import annotations

import re
from typing import Any

from rich.cells import cell_len
from rich.color import ColorSystem
from rich.style import Style
from rich.text import Text

__all__ = [
    "LINE_RESET",
    "slice_styled",
    "split_styled_lines",
    "strip_ansi",
    "styles_per_char",
    "text_to_ansi",
    "visible_width",
]

#: 每一行结尾都要附上的复位串（对齐 Pi 的 ``TUI.SEGMENT_RESET``）。
#:
#: 前半 ``\x1b[0m`` 复位 SGR（颜色/粗体），后半 ``\x1b]8;;\x07`` 关闭超链接（OSC 8）。
#: **为什么必须逐行加**：一旦某行漏了复位，它的颜色会"漏"到下一行，
#: 而下一行可能根本不该有颜色——症状是"某几行颜色莫名其妙"。
LINE_RESET = "\x1b[0m\x1b]8;;\x07"

#: CSI 序列：``ESC [ <参数> <终止字节>``（颜色、光标移动、同步输出都是这个形式）
_CSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")

#: OSC 序列：``ESC ] ... BEL`` 或 ``ESC ] ... ESC \``（设标题、剪贴板、超链接）
_OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")

#: APC / DCS / PM / SOS 序列：``ESC _ ... ESC \`` 等（**IME 光标标记走这条**）
_STRING_SEQUENCES = re.compile(r"\x1b[_PX^][^\x1b]*(?:\x1b\\)")

#: 其余双字节 ESC 序列（如 ``ESC ( B`` 选字符集）
_SHORT_ESCAPES = re.compile(r"\x1b[()#][0-9A-Za-z]")


def strip_ansi(text: str) -> str:
    """剥掉全部转义序列，只留下**真正占格子**的字符。

    覆盖四类：CSI（颜色/光标）、OSC（标题/剪贴板/超链接）、
    APC/DCS/PM/SOS（应用自定义，**IME 光标标记属于这一类**）、
    以及 ``ESC ( B`` 这种字符集选择序列。

    只做 CSI 是不够的——实测正是 APC 那一类（``\\x1b_G...``）让宽度算多了 7 格。
    """
    if "\x1b" not in text:
        return text  # 快路径：绝大多数文本没有转义序列
    text = _STRING_SEQUENCES.sub("", text)
    text = _OSC.sub("", text)
    text = _CSI.sub("", text)
    return _SHORT_ESCAPES.sub("", text)


def visible_width(text: str) -> int:
    """**实际占用的格数**（忽略转义序列；中文算 2 格）。

    这是渲染层唯一的宽度口径。等价于 Pi 的 ``visibleWidth()``。
    """
    return cell_len(strip_ansi(text))


# --------------------------------------------------------------------------- #
# 反向：把带样式的行变成**可以直接写进终端的字节**
# --------------------------------------------------------------------------- #


def _as_style(value: Any) -> Style:
    """把 ``Text`` 里的样式字段统一成 :class:`rich.style.Style`。

    ``Text.style`` / ``Span.style`` 允许是 ``str``（``"bold #ff0000"``）或 ``Style``，
    两种都要能处理，否则"主题里写字符串"和"代码里写 Style"会走两条分支。
    """
    if value is None:
        return Style.null()
    if isinstance(value, Style):
        return value
    return Style.parse(str(value))


def styles_per_char(text: Text) -> list[Style]:
    """逐字符的**最终样式**：基样式打底，``spans`` 按顺序叠加。

    为什么是"叠加"而不是"覆盖"（这是 Rich 的语义，也是本项目踩过的坑）：
    ``Text("x", style="bold")`` 再 ``stylize("#ff0000", 0, 1)`` 的意图是
    **又粗又红**，不是"红但不粗"。写成覆盖的话基样式会被静默丢掉——
    症状是"某一行的某几个字莫名不加粗了"，很难看出是哪一步丢的。

    这也是 :func:`split_styled_lines`、:func:`slice_styled`、:func:`text_to_ansi`
    共用的**唯一**一处样式展开实现：口径只有一份，就不会出现
    "量的口径和写的口径不一致"。
    """
    base = _as_style(text.style)
    styles: list[Style] = [base] * len(text.plain)
    for span in text.spans:
        overlay = _as_style(span.style)
        for index in range(max(0, span.start), min(len(styles), span.end)):
            styles[index] = styles[index] + overlay
    return styles


def slice_styled(plain: str, styles: list[Style], start: int, end: int) -> Text:
    """按 ``[start, end)`` 取一段，**样式跟着字符走**。

    ``styles`` 必须是 :func:`styles_per_char` 的结果（已经把基样式算进去了，
    所以这里不再接受 base 参数——多一个参数就多一处能传错的地方）。
    """
    row = Text()
    if end <= start:
        return row
    row.append(plain[start:end])
    run_start = start
    for index in range(start, end):
        if index + 1 < end and styles[index + 1] == styles[run_start]:
            continue
        row.stylize(styles[run_start], run_start - start, index + 1 - start)
        run_start = index + 1
    return row


def split_styled_lines(text: Text) -> list[Text]:
    """把一段 ``Text``（含换行）切成帧要用的行列表，**每一行都保留自己的样式**。

    ⚠️ **这是本模块最容易被写错的一步**，而且写错了不会有任何报错：
    直接从 ``text.plain.split("\\n")`` 重建行的话，**所有 spans 都会被丢掉**
    ——Markdown 的加粗、行内代码色、链接色全部消失，屏幕上只剩一个颜色的字。
    实测就是这样：内容层渲染出了 4 段样式，交付给终端的却是 0 段。

    末尾的换行是**行终止符**，不是"多一个空行"（``"a\\n"`` 是一行）。
    """
    plain = text.plain
    if not plain:
        return []
    if plain.endswith("\n"):
        plain = plain[:-1]
        if not plain:
            return []
    styles = styles_per_char(text)
    rows: list[Text] = []
    start = 0
    for index, char in enumerate(plain):
        if char != "\n":
            continue
        rows.append(slice_styled(plain, styles, start, index))
        start = index + 1
    rows.append(slice_styled(plain, styles, start, len(plain)))
    return rows


def text_to_ansi(text: Text, *, color_system: ColorSystem = ColorSystem.TRUECOLOR) -> str:
    """把一行 :class:`rich.text.Text` 渲染成**带 ANSI 转义**的字符串。

    为什么需要它（这是本项目一个"看着能跑、实际丢东西"的坑）
    --------------------------------------------------------
    渲染器最初是写 ``line.plain``（**纯文本**）的。后果是：主题里 53 个颜色令牌
    全部不起作用，Markdown 的粗体/代码色、工具卡片的状态色**一个都看不见**——
    界面上全是一个颜色的字，而代码里"看起来"一切正常（因为宽度断言用的是 plain，
    照样通过）。**没有任何报错**。

    实现方式：把 ``Text.spans`` 展开成"逐字符样式表"（:func:`styles_per_char`），
    再把**样式相同的连续字符**合成一段输出。为什么不用 ``Console.print`` 到一个
    缓冲区：那会额外引入换行/裁切策略，而这里的行**已经**由 :func:`visible_width`
    保证过宽度，再经一层终端宽度推断只会引入新的不确定性。

    ⚠️ 与 ``visible_width`` 是一对：**量的口径和写的口径必须是同一个**。
    """
    plain = text.plain
    if not plain:
        return ""
    styles = styles_per_char(text)
    out: list[str] = []
    run_start = 0
    for index in range(1, len(plain) + 1):
        if index < len(plain) and styles[index] == styles[run_start]:
            continue
        chunk = plain[run_start:index]
        out.append(styles[run_start].render(chunk, color_system=color_system))
        run_start = index
    return "".join(out)
