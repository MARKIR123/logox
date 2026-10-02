"""Markdown 渲染（D79：对齐 Pi agent 的 `Markdown` 组件）。

为什么需要它
------------
模型回答**默认就是 Markdown**。我们之前把正文当纯文本画，于是屏幕上出现的是
字面量 ``1. **终端主题/配色问题**`` —— 星号和序号原样打印。

这不是"少了一层美化"，而是**信息层级完全丢失**：标题、要点、代码块、引用
全都退化成一坨同色同粗的正文。用户说"Textual 好像也没有多美观"，
这是最大的一个原因（第二个原因见 `theme` 的 D79 说明：没有背景分层）。

对齐 Pi 的哪些部分
------------------
Pi 的 ``Markdown`` 组件支持：标题 / 粗体 / 斜体 / 行内代码 / 代码块（含语法高亮
回调）/ 列表 / 链接 / 引用 / 分隔线，并且整块用 ``md*`` 语义色着色。
本模块覆盖同一组元素，色值取自 ``ThemePalette`` 的 ``md_*`` / ``syntax_*``。

设计约束（为什么是纯函数）
--------------------------
输入 ``(markdown 文本, 宽度, CardContext)`` → 输出 ``list[Text]``（每项一行）。
**不碰 Textual**，因此绝大部分行为都能用普通 unittest 覆盖，不需要启动应用
——这和 `cards.py` / `format.py` 是同一套路子（M1.5 定下的可测性设计）。

行宽不变量
----------
每一行的 ``cell_len`` 必须 ``<= width``。这是**本模块的责任**，不能指望上层的
widget 兜底：Textual 遇到超宽行会**裁掉**（不是折行），而我们已经被这类问题
咬过三次（见 PROJECT-REVIEW §5.21）。折行统一走 `format.wrap_cells`。

表格（D115）
------------
GFM 表格（``| 表头 |`` 紧跟 ``|---|`` 分隔行）渲染成**闭合的框线表**：

* 列宽按内容自适应，按分隔行的 ``:`` 标记左/中/右对齐；
* 整表放不下时**按列折行**（内容一个字都不丢），而不是截断；
* 终端窄到连一列都放不下时，降级为**记录式列表**（表头当字段名）。

改之前这里**根本没有表格分支**：``| 方案 | 优点 |`` 被当成普通段落，
用户看到的是字面量竖线，而且窄终端下还会在**格子中间**折行
（``| 事件总线 | 解耦彻底 || 调试链路长`` —— 一行数据被劈成两行）。
这正是用户报的"表格无法渲染"。

不做什么（明确登记）
--------------------
* 脚注、任务列表（``- [x]``）、嵌套列表缩进：暂不支持，按普通段落渲染。
* 表格的**跨行合并**（``<br>``、``colspan``）：不解析，按字面量渲染。
* 完整语法高亮器：只做"注释/字符串/数字/关键字"四类，够用且不引入依赖。
  M6/M7 若要真高亮，把 `_highlight_code` 换成一个 Pygments 适配即可。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from rich.cells import cell_len
from rich.text import Text

from logox.config.schema import ThemePalette
from logox.tui import format as fmt
from logox.tui.content.cards import CardContext

__all__ = ["MarkdownBlock", "MarkdownRenderCache", "parse_markdown", "render_markdown"]

# --------------------------------------------------------------------------- #
# 块级解析
# --------------------------------------------------------------------------- #

BlockKind = str  # "code" | "heading" | "bullet" | "ordered" | "quote" | "hr" | "para" | "table"

#: 代码围栏：``` 或 ~~~，后面可跟语言名
_FENCE = re.compile(r"^\s*(`{3,}|~{3,})\s*([^\s`]*)")
#: ``#`` 到 ``######`` 后必须跟空格（``#tag`` 不是标题）
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
#: 无序列表：``-`` ``*`` ``+`` 后跟空格
_BULLET = re.compile(r"^(\s*)[-*+]\s+(.*)$")
#: 有序列表：``1.`` / ``1)`` 后跟空格
_ORDERED = re.compile(r"^(\s*)(\d{1,3})[.)]\s+(.*)$")
#: 引用：``>`` 后可有空格
_QUOTE = re.compile(r"^\s*>\s?(.*)$")
#: 分隔线：三个以上 ``-`` ``*`` ``_``（可含空格）
_HR = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")
#: 表格分隔行的**单个单元格**：``---`` / ``:--`` / ``--:`` / ``:-:``
_TABLE_DELIM_CELL = re.compile(r"^:?-+:?$")


@dataclass
class MarkdownBlock:
    """一个块级元素。``text`` 是原始内容（未做行内解析）。"""

    kind: BlockKind
    text: str = ""
    level: int = 0  # 标题层级（1–6）
    marker: str = ""  # 列表符号（``-`` / ``1.``）
    lang: str = ""  # 代码块语言
    lines: list[str] = field(default_factory=list)  # 列表项 / 代码块的原始行
    header: list[str] = field(default_factory=list)  # 表格表头（已切分好单元格）
    align: list[str] = field(default_factory=list)  # 表格每列对齐（left/center/right）
    rows: list[list[str]] = field(default_factory=list)  # 表格正文行


def _split_table_row(line: str) -> list[str] | None:
    """把一行 ``| a | b |`` 切成单元格；**不含竖线则返回 ``None``**。

    为什么必须检查竖线：``a | b`` 这种普通句子里也会有 ``|``。
    真正的判据是"下一行是分隔行"（见 :func:`_parse_table_align`），
    这里只负责"这行看起来像不像一行表格"。

    支持 ``\\|`` 转义（单元格里真的需要竖线时用它）。
    """
    text = line.strip()
    if "|" not in text:
        return None
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|") and not text.endswith("\\|"):
        text = text[:-1]

    cells: list[str] = []
    current: list[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\\" and text[index + 1 : index + 2] == "|":
            current.append("|")
            index += 2
            continue
        if char == "|":
            cells.append("".join(current).strip())
            current = []
            index += 1
            continue
        current.append(char)
        index += 1
    cells.append("".join(current).strip())
    return cells


def _parse_table_align(line: str, ncols: int) -> list[str] | None:
    """解析分隔行 ``|---|:--:|`` → 每列对齐方式；**不是分隔行返回 ``None``**。

    列数必须与表头一致（GFM 的规矩）：不一致说明这只是一行恰好带竖线的正文。
    """
    cells = _split_table_row(line)
    if cells is None or len(cells) != ncols:
        return None
    if not all(_TABLE_DELIM_CELL.match(cell) for cell in cells):
        return None
    align: list[str] = []
    for cell in cells:
        if cell.startswith(":") and cell.endswith(":"):
            align.append("center")
        elif cell.endswith(":"):
            align.append("right")
        else:
            align.append("left")
    return align


def _pad_row(cells: list[str], ncols: int) -> list[str]:
    """把一行的单元格数补齐到 ``ncols``（多则截断，少则补空）。"""
    if len(cells) >= ncols:
        return list(cells[:ncols])
    return [*cells, *([""] * (ncols - len(cells)))]


def parse_markdown(source: str) -> list[MarkdownBlock]:
    """把 Markdown 文本切成块序列（**只做块级**，行内解析交给渲染阶段）。

    刻意做成**两遍**（块级 → 行内）：这样"段落合并"（把模型的硬换行合成一段）
    发生在块级，而行内样式不会干扰换行宽度计算——我们在这里踩过坑，
    见 `format.wrap_cells` 的文档。
    """
    blocks: list[MarkdownBlock] = []
    lines = source.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    index = 0
    para: list[str] = []

    def flush_para() -> None:
        if para:
            blocks.append(MarkdownBlock(kind="para", text="\n".join(para)))
            para.clear()

    while index < len(lines):
        raw = lines[index]

        # ① 代码围栏：整段原样收下，**不参与任何重排**
        fence = _FENCE.match(raw)
        if fence is not None:
            flush_para()
            marker, lang = fence.group(1), fence.group(2)
            body: list[str] = []
            index += 1
            while index < len(lines) and not lines[index].strip().startswith(marker[0] * 3):
                body.append(lines[index])
                index += 1
            index += 1  # 跳过收尾围栏（缺失时也只是越界，循环自然结束）
            blocks.append(MarkdownBlock(kind="code", lang=lang, lines=body))
            continue

        # ② 空行 = 段落边界
        if not raw.strip():
            flush_para()
            index += 1
            continue

        # ③ 分隔线（必须在无序列表**之前**判断：`---` 也匹配 `- ` 的前缀思路）
        if _HR.match(raw):
            flush_para()
            blocks.append(MarkdownBlock(kind="hr", text=raw.strip()))
            index += 1
            continue

        # ③.5 表格：``| 表头 |`` **紧跟**分隔行才算数（``a | b`` 这种正文不会误判）
        if index + 1 < len(lines):
            header = _split_table_row(raw)
            if header is not None:
                align = _parse_table_align(lines[index + 1], len(header))
                if align is not None:
                    flush_para()
                    rows: list[list[str]] = []
                    index += 2
                    while index < len(lines):
                        cells = _split_table_row(lines[index])
                        if cells is None or not lines[index].strip():
                            break
                        rows.append(cells)
                        index += 1
                    blocks.append(MarkdownBlock(kind="table", header=header, align=align, rows=rows))
                    continue

        # ④ 标题
        heading = _HEADING.match(raw)
        if heading is not None:
            flush_para()
            blocks.append(
                MarkdownBlock(
                    kind="heading", text=heading.group(2).strip(), level=len(heading.group(1))
                )
            )
            index += 1
            continue

        # ⑤ 引用（连续多行合成一块）
        quote = _QUOTE.match(raw)
        if quote is not None:
            flush_para()
            body = [quote.group(1)]
            index += 1
            while index < len(lines):
                nxt = _QUOTE.match(lines[index])
                if nxt is None:
                    break
                body.append(nxt.group(1))
                index += 1
            blocks.append(MarkdownBlock(kind="quote", lines=body))
            continue

        # ⑥ 有序列表（**必须在无序之前**：`1.` 不会被 `_BULLET` 匹配，但顺序更清楚）
        ordered = _ORDERED.match(raw)
        if ordered is not None:
            flush_para()
            blocks.append(
                MarkdownBlock(
                    kind="ordered",
                    text=ordered.group(3),
                    marker=f"{ordered.group(2)}.",
                    lines=[ordered.group(3)],
                )
            )
            index += 1
            continue

        # ⑦ 无序列表
        bullet = _BULLET.match(raw)
        if bullet is not None:
            flush_para()
            blocks.append(
                MarkdownBlock(kind="bullet", text=bullet.group(2), marker="-", lines=[bullet.group(2)])
            )
            index += 1
            continue

        # ⑧ 普通段落：连续行先攒着，最后一起重排
        para.append(raw.strip())
        index += 1

    flush_para()
    return blocks


# --------------------------------------------------------------------------- #
# 行内解析
# --------------------------------------------------------------------------- #

#: ``**粗体**`` / ``__粗体__``
_BOLD = re.compile(r"\*\*(.+?)\*\*|__(.+?)__")
#: ``*斜体*`` / ``_斜体_``（不能跨行）
_ITALIC = re.compile(r"(?<![\*\w])\*([^*\n]+)\*(?!\*)|(?<![\w_])_([^_\n]+)_(?!\w)")
#: 行内代码：反引号，内部**不做任何解析**
_CODE = re.compile(r"`([^`\n]+)`")
#: 链接：``[文字](地址)``
_LINK = re.compile(r"\[([^\]\n]+)\]\(([^)\s]+)\)")


def render_inline(text: str, palette: ThemePalette, *, base: str | None = None) -> Text:
    """把一行行内 Markdown 渲染成 Rich ``Text``（**不加换行**）。

    实现方式：先找出所有"特殊片段"的区间，再按区间顺序拼装。
    为什么不做递归下降的完整解析器：行内语法只有 4 种，区间法**一眼能验**，
    而递归下降在"嵌套 + 转义"边界上很容易出隐性错误。

    重叠时的优先级：**行内代码 > 链接 > 粗体 > 斜体**。
    代码最先，因为它的内容是字面量（`` `a*b*c` `` 里的星号不该当斜体）。
    """
    base_style = base or palette.text_primary
    out = Text(style=base_style)

    spans: list[tuple[int, int, str, tuple[str, ...]]] = []  # (start, end, kind, groups)
    for kind, pattern in (("code", _CODE), ("link", _LINK), ("bold", _BOLD), ("italic", _ITALIC)):
        for match in pattern.finditer(text):
            spans.append((match.start(), match.end(), kind, match.groups()))

    # 按起点排序；重叠的**丢掉后出现的那个**（优先级已在上面按顺序扫过）
    spans.sort(key=lambda item: (item[0], item[1]))
    kept: list[tuple[int, int, str, tuple[str, ...]]] = []
    cursor = 0
    for span in spans:
        if span[0] >= cursor:
            kept.append(span)
            cursor = span[1]

    position = 0
    for start, end, kind, groups in kept:
        if start > position:
            out.append(text[position:start])
        out.append_text(_render_span(kind, groups, palette, base_style))
        position = end
    if position < len(text):
        out.append(text[position:])
    return out


def _render_span(kind: str, groups: tuple[str | None, ...], palette: ThemePalette, base: str) -> Text:
    """渲染一个行内特殊片段。"""
    if kind == "code":
        return Text(groups[0] or "", style=palette.md_code)
    if kind == "link":
        piece = Text(style=palette.md_link)
        piece.append(groups[0] or "")
        if groups[1]:
            piece.append(f" ({groups[1]})", style=palette.md_link_url)
        return piece
    if kind == "bold":
        return Text(groups[0] or groups[1] or "", style=f"bold {base}")
    return Text(groups[0] or groups[1] or "", style=f"italic {base}")


# --------------------------------------------------------------------------- #
# 代码高亮
# --------------------------------------------------------------------------- #

#: 只做四类：注释、字符串、数字、关键字。足够"看起来是代码"，且**零依赖**。
#:
#: 刻意做成"一个列表"而不是"一段长字符串再 split"：后者两个 lint 工具
#: （ruff / oxlint）会各执一词地重排它，而这里**分组本身就是文档**。
_KEYWORD_GROUPS: tuple[tuple[str, ...], ...] = (
    # Python
    ("def", "class", "return", "if", "elif", "else", "for", "while", "import", "from", "as"),
    ("with", "try", "except", "finally", "raise", "pass", "break", "continue", "lambda"),
    ("yield", "global", "nonlocal", "assert", "del", "in", "is", "not", "and", "or"),
    ("None", "True", "False", "async", "await", "match", "case"),
    # JS / TS
    ("let", "const", "var", "function", "export", "default", "new", "this"),
    ("typeof", "instanceof", "interface", "type", "enum"),
    # C / Rust 系
    ("public", "private", "static", "void", "int", "string", "bool", "float", "double"),
    ("struct", "impl", "fn", "mut", "pub", "use", "mod"),
)
_KEYWORDS = frozenset(word for group in _KEYWORD_GROUPS for word in group)
_CODE_TOKEN = re.compile(
    r"(?P<comment>#[^\n]*|//[^\n]*)"
    r"|(?P<string>\"[^\"\n]*\"|'[^'\n]*')"
    r"|(?P<number>\b\d+(?:\.\d+)?\b)"
    r"|(?P<word>[A-Za-z_][A-Za-z0-9_]*)"
)


def _highlight_code(line: str, palette: ThemePalette) -> Text:
    """给一行代码上色（朴素但可控）。

    刻意不做"真正的"词法分析：那需要按语言分词、还要处理多行字符串，
    收益（颜色更准）远小于成本（依赖 + 出错面）。Pi 也是把高亮做成**可选回调**
    （``highlightCode``），默认不启用。
    """
    out = Text()
    position = 0
    for match in _CODE_TOKEN.finditer(line):
        if match.start() > position:
            out.append(line[position : match.start()], style=palette.md_code_block)
        if match.group("comment"):
            out.append(match.group(), style=palette.syntax_comment)
        elif match.group("string"):
            out.append(match.group(), style=palette.syntax_string)
        elif match.group("number"):
            out.append(match.group(), style=palette.syntax_number)
        else:
            word = match.group()
            if word in _KEYWORDS:
                out.append(word, style=palette.syntax_keyword)
            elif line[match.end() : match.end() + 1] == "(":
                out.append(word, style=palette.syntax_function)
            else:
                out.append(word, style=palette.md_code_block)
        position = match.end()
    if position < len(line):
        out.append(line[position:], style=palette.md_code_block)
    return out


# --------------------------------------------------------------------------- #
# 块渲染
# --------------------------------------------------------------------------- #


@dataclass
class MarkdownRenderCache:
    """只保留当前 Markdown 结构的排版与代码行，不缓存历次增长的全文。"""

    params: tuple[Any, ...] | None = None
    blocks: dict[tuple[Any, ...], list[Text]] = field(default_factory=dict)
    code_lines: dict[tuple[int, str], list[Text]] = field(default_factory=dict)
    decorated_lines: list[tuple[Text, Text]] = field(default_factory=list)
    decoration: tuple[bool, str] | None = None


def render_markdown(
    source: str, width: int, context: CardContext, *, cache: MarkdownRenderCache | None = None,
) -> list[Text]:
    """把 Markdown 渲染成**行列表**（每行是一个 Rich ``Text``）。

    ``width`` 是可用宽度；保证每一行 ``cell_len <= width``。
    """
    palette = context.palette
    if width <= 0:
        return []
    lines: list[Text] = []
    blocks = parse_markdown(source)
    params = (width, tuple(vars(palette).items()))
    old = cache.blocks if cache is not None and cache.params == params else {}
    retained: dict[tuple[Any, ...], list[Text]] = {}
    if cache is not None and cache.params != params:
        cache.code_lines.clear()

    for index, block in enumerate(blocks):
        key = (
            block.kind, block.text, block.level, block.marker, block.lang,
            tuple(block.lines), tuple(block.header), tuple(block.align),
            tuple(tuple(row) for row in block.rows),
        )
        if key in old:
            rendered = old[key]
        elif block.kind == "code":
            rendered = _render_code_block(
                block, width, palette, line_cache=cache.code_lines if cache is not None else None,
            )
        elif block.kind == "heading":
            rendered = _render_heading(block, width, palette)
        elif block.kind == "hr":
            rendered = [_render_hr(width, palette)]
        elif block.kind == "quote":
            rendered = _render_quote(block, width, palette)
        elif block.kind in ("bullet", "ordered"):
            rendered = _render_list_item(block, width, palette)
        elif block.kind == "table":
            rendered = _render_table(block, width, palette)
        else:
            rendered = _render_paragraph(block.text, width, palette)
        retained[key] = rendered
        lines.extend(rendered)

        # 块之间空一行（最后一块之后不空）——阅读节奏靠它，不是靠行尾空格
        if index != len(blocks) - 1 and block.kind != "hr":
            lines.append(Text())
    if cache is not None:
        cache.params = params
        cache.blocks = retained
        live_code = {(width, raw) for block in blocks if block.kind == "code" for raw in block.lines}
        cache.code_lines = {key: value for key, value in cache.code_lines.items() if key in live_code}
    return lines


def _render_paragraph(text: str, width: int, palette: ThemePalette) -> list[Text]:
    """段落：**先按宽度重排，再做行内解析**。

    顺序不能反：先解析会让样式碎片参与换行计算，那种"折行时把样式算错"的问题
    已经在 `format.wrap_cells` 上出现过（见 D74）。
    """
    wrapped = fmt.wrap_cells(text, width)
    return [render_inline(line, palette) for line in wrapped.split("\n")]


def _render_heading(block: MarkdownBlock, width: int, palette: ThemePalette) -> list[Text]:
    """标题：**粗体 + 着色**，且**不显示 ``#`` 标记**。

    ⚠️ 这里刻意与 Pi 不同，理由是本项目的实际情况：
    Pi 用 ``mdHeading`` 单色 + 保留 Markdown 的 ``#`` 前缀；但用户对我们显示的
    字面量 ``**`` / ``1.`` 已经明确表达过不满（"怎么是这种符号"）。
    终端没有字号，标题的层级信息用"**粗体 + 暖色**"表达已经足够，
    再把 ``##`` 画出来只是把语法噪声搬到屏幕上。
    """
    wrapped = fmt.wrap_cells(block.text, width).split("\n")
    out: list[Text] = []
    for line in wrapped:
        piece = render_inline(line, palette, base=palette.md_heading)
        piece.stylize("bold")
        out.append(piece)
    return out


def _render_hr(width: int, palette: ThemePalette) -> Text:
    return Text("─" * max(4, min(width, 60)), style=palette.md_hr)


def _render_quote(block: MarkdownBlock, width: int, palette: ThemePalette) -> list[Text]:
    """引用：左侧 ``│`` 竖线 + 缩进（Pi 用 ``mdQuoteBorder``）。"""
    prefix = "│ "
    body_width = max(4, width - cell_len(prefix))
    out: list[Text] = []
    for raw in block.lines:
        for line in fmt.wrap_cells(raw, body_width).split("\n"):
            piece = Text(prefix, style=palette.md_quote_border)
            piece.append_text(render_inline(line, palette, base=palette.md_quote))
            out.append(piece)
    return out


def _render_list_item(block: MarkdownBlock, width: int, palette: ThemePalette) -> list[Text]:
    """列表项：符号着 ``mdListBullet``，**续行悬挂对齐**到正文起点。"""
    symbol = "• " if block.kind == "bullet" else f"{block.marker} "
    if cell_len(symbol) > width // 2:  # 极窄终端：符号让位给正文
        symbol = "• " if block.kind == "bullet" else f"{block.marker}"
    hang = " " * cell_len(symbol)
    body_width = max(4, width - cell_len(symbol))
    out: list[Text] = []
    for raw in block.lines:
        for i, line in enumerate(fmt.wrap_cells(raw, body_width).split("\n")):
            piece = Text(symbol if i == 0 else hang, style=palette.md_list_bullet)
            piece.append_text(render_inline(line, palette))
            out.append(piece)
    return out


# --------------------------------------------------------------------------- #
# 表格（D115）
# --------------------------------------------------------------------------- #

#: 每列的**最小**可用宽度（cell）。低于它就没法竖直折行了，整表降级为记录式列表。
MIN_TABLE_COL = 4

#: 单元格左右各留的空白 + 竖线，每列固定吃掉 3 cell（``│ 内容 │``）
_TABLE_COL_OVERHEAD = 3


def _render_table(block: MarkdownBlock, width: int, palette: ThemePalette) -> list[Text]:
    """表格：闭合框线 + 列宽自适应 + 超宽折行 + 极窄降级。

    三段式：``┌─┬─┐`` 顶边 → 表头 → ``├─┼─┤`` → 正文行 → ``└─┴─┘`` 底边。

    几个必须做对的点：

    * **宽度按 cell 算**：``| 方案 |`` 这类中文单元格用 ``len()`` 会少算一倍，
      边框会断在屏幕上（UI-SPEC §4）。
    * **量宽度要量"行内解析之后"的宽度**：``**粗体**`` 渲染出来只有 4 cell，
      按 8 个字算会白白多留一堆空格。
    * **放不下就折行，不截断**：单元格里的字一个都不能丢（否则用户看到的是
      "表格里的话说到一半没了"，与 §5.21 是同一类故障）。
    """
    ncols = len(block.header)
    if width <= 0 or ncols == 0:
        return []

    rows = [_pad_row(block.header, ncols)] + [_pad_row(row, ncols) for row in block.rows]
    # 行内解析一次，**只用来量列宽**（`**粗体**` 的视觉宽度是 4 cell，不是 8）。
    # 真正画的时候把**原始单元格**交给 `_table_row`——它自己解析，样式才不会丢。
    plains = [[render_inline(cell, palette).plain for cell in row] for row in rows]
    natural = [max(1, max(cell_len(row[col]) for row in plains)) for col in range(ncols)]

    budget = width - (_TABLE_COL_OVERHEAD * ncols + 1)
    if budget < ncols * MIN_TABLE_COL:
        # 连一列都放不下：画框线只会把内容挤没，退成记录式列表更有用
        return _render_table_records(block, width, palette)

    widths = _fit_columns(natural, budget)
    align = [*block.align, *(["left"] * ncols)][:ncols]

    border = palette.md_table_border
    out: list[Text] = [_table_edge(widths, "┌", "┬", "┐", border)]
    out.extend(_table_row(rows[0], widths, align, palette, header=True))
    out.append(_table_edge(widths, "├", "┼", "┤", border))
    for row in rows[1:]:
        out.extend(_table_row(row, widths, align, palette, header=False))
    out.append(_table_edge(widths, "└", "┴", "┘", border))
    return out


def _fit_columns(natural: list[int], budget: int) -> list[int]:
    """把 ``budget`` 个 cell 分配到各列：够用就按内容宽度，不够就按比例缩。

    步骤（三步都是**闭式**的，结果与列序无关、可复现）：

    1. 每列先拿 ``MIN_TABLE_COL`` 保底（内容本身更窄时按内容算）；
    2. 剩下的余量按"各列**超出**最小宽度的部分"成比例分（``quota * share // pool``）；
    3. 整除余下的零头，按小数部分从大到小**一列一格**地发掉。

    为什么不能写成"``budget * nat // total`` 再夹一个下限"：夹下限会把总和**顶到
    ``budget`` 以上**。实测 width=24、三列（4/6/18）时算出 4+4+9=17 > 14，
    整张表宽 27 而终端只有 24——右边框直接被终端裁掉，表格看起来是"开口"的。
    这条不变量（``sum(widths) <= budget``）必须由算法本身保证，不能靠事后夹取。
    """
    total = sum(natural)
    if total <= budget:
        return list(natural)

    widths = [min(nat, MIN_TABLE_COL) for nat in natural]
    shares = [max(0, nat - MIN_TABLE_COL) for nat in natural]
    pool = sum(shares)
    remaining = budget - sum(widths)
    if remaining <= 0 or pool <= 0:
        return widths

    # 走到这里必然有 ``remaining < pool``：两者相加正好是 ``total > budget``，
    # 所以"按比例分"绝不会把某一列分到超过它自己的内容宽度。
    taken = [remaining * share // pool for share in shares]
    widths = [width + extra for width, extra in zip(widths, taken, strict=True)]
    leftover = remaining - sum(taken)
    order = sorted(range(len(natural)), key=lambda col: (-(remaining * shares[col] % pool), col))
    for col in order[:leftover]:
        widths[col] += 1
    return widths


def _table_edge(widths: list[int], left: str, mid: str, right: str, style: str) -> Text:
    """画一条表格横边（``┌─┬─┐`` / ``├─┼─┤`` / ``└─┴─┘``）。"""
    piece = Text(left, style=style)
    for index, col in enumerate(widths):
        if index:
            piece.append(mid, style=style)
        piece.append("─" * (col + 2), style=style)
    piece.append(right, style=style)
    return piece


def _table_row(
    cells: list[str], widths: list[int], align: list[str], palette: ThemePalette, *, header: bool
) -> list[Text]:
    """渲染一行表格：每格先做行内解析、再按列宽折行，最后逐"视觉行"横向拼装。

    顺序**必须**是"先解析、后折行"：

    * 折行用的是**行内解析之后**的视觉文本（``**粗体**`` → ``粗体``），
      因此标记不会被劈成半截漏到屏幕上，宽度也都按视觉宽度算；
    * 反过来若先折行再解析（``_render_paragraph`` 的做法），
      ``**很长的一段粗体**`` 折在中间就会留下字面量 ``**``。

    单元格折行**保留换行**（``reflow=False``）：格子里的换行是排版意图，
    重排会把 ``| 甲 | 1 |`` 这类短内容搅乱。
    """
    base = palette.md_heading if header else palette.text_primary
    wrapped = [
        _styled_lines(render_inline(cell, palette, base=base), widths[col])
        for col, cell in enumerate(cells)
    ]
    height = max(len(lines) for lines in wrapped)

    out: list[Text] = []
    for line_index in range(height):
        line = Text("│", style=palette.md_table_border)
        for col in range(len(cells)):
            lines = wrapped[col]
            cell = lines[line_index] if line_index < len(lines) else Text()
            if header:
                cell.stylize("bold")  # 终端没有字号：表头靠**粗体 + 暖色**立住
            line.append(" ")
            line.append_text(_align_cell(cell, widths[col], align[col]))
            line.append(" ", style=palette.md_table_border)
            line.append("│", style=palette.md_table_border)
        out.append(line)
    return out


def _styled_lines(styled: Text, width: int) -> list[Text]:
    """按 cell 宽度折行，**样式随行保留**。

    做法：折行交给 ``fmt.wrap_cells``（它守"不丢字、不重复、不超宽"三条硬规矩），
    再用**字符下标**把折出来的片段从原 ``Text`` 上切下来——切的是同一个对象，
    所以粗体 / 行内代码 / 链接的颜色一格都不会丢。

    两个细节：

    * 折行是**只切不拼**的（区间首尾相接、行首行尾空白丢掉），所以每个片段都是
      原文的连续子串、顺序也与原文一致——从上一个片段结尾开始"顺序查找"在结构上不会错位；
    * 续行的**悬挂缩进空白是折行器补上去的**（不属于原文），所以先剥掉它再定位，
      再把这段空白原样补回行首（它是空格，本来就没有样式）。
    """
    plain = styled.plain
    out: list[Text] = []
    cursor = 0
    for piece in fmt.wrap_cells(plain, width, reflow=False).split("\n"):
        probe = piece.lstrip()
        if not probe:
            out.append(Text(piece))
            continue
        start = plain.find(probe, cursor)
        if start < 0:  # pragma: no cover - 折行只切不拼，正常不会走到这里
            out.append(Text(piece))
            continue
        end = start + len(probe)
        line = Text(piece[: len(piece) - len(probe)])
        line.append_text(styled[start:end])
        cursor = end
        out.append(line)
    return out or [Text()]


def _align_cell(content: Text, width: int, align: str) -> Text:
    """把一格内容按列宽对齐（左 / 中 / 右），**补齐按 cell 算**。"""
    pad = max(0, width - cell_len(content.plain))
    if align == "right":
        left, right = pad, 0
    elif align == "center":
        left, right = pad // 2, pad - pad // 2
    else:
        left, right = 0, pad
    out = Text(" " * left)
    out.append_text(content)
    out.append(" " * right)
    return out


def _render_table_records(block: MarkdownBlock, width: int, palette: ThemePalette) -> list[Text]:
    """极窄终端 / 列太多时的降级形态：**记录式列表**，表头当字段名。

    为什么值得单独写一条降级路径：框线表在 20 列的终端里无论怎么折都是
    "一格两个字"的碎片，读起来比不画表还糟。而"表头当字段名"这种纵向排布
    在任何宽度下都读得通——每一行都是完整的一句话。

    与框线表**共用同一条"先解析、后折行"的顺序**（见 :func:`_table_row`），
    所以单元格里的行内样式在降级路径里同样不会丢。
    """
    header = block.header
    rows = block.rows
    labels = [f"{name}：" if name else "" for name in header]
    if not rows:
        # 只有表头的表格（模型偶尔这么写）：按普通列表渲染，不凭空造出 "方案："
        rows = [header]
        labels = [""] * len(header)

    out: list[Text] = []
    for row in rows:
        cells = _pad_row(row, len(header))
        for index, cell in enumerate(cells):
            # ⚠️ 用 `append` 而不是 `Text("• ", style=...)`：后者会把 md_list_bullet
            # 设成**整个 Text 的基础样式**，再往里追加单元格内容时那一整行都会带上它。
            head = Text()
            head.append("• " if index == 0 else "  ", style=palette.md_list_bullet)
            label = labels[index] if index < len(labels) else ""
            if label:
                head.append(label, style=f"bold {palette.md_heading}")
            head.append_text(render_inline(cell, palette))
            out.extend(_styled_lines(head, max(1, width)))
    return out


def _render_code_block(
    block: MarkdownBlock, width: int, palette: ThemePalette,
    *, line_cache: dict[tuple[int, str], list[Text]] | None = None,
) -> list[Text]:
    """代码块：四周完整圆角边框闭合 + 语言标签，**内容不重排**（只对超长行硬切）。

    为什么代码不做 word-wrap：缩进和换行对代码是**语义**，重排会改变含义
    （这与正文的处理刚好相反，见 D68/D74）。
    """
    if width <= _CODE_BORDER_CELLS:
        # 边框本身就要吃掉 4 cell（左 "│ " + 右 " │"）：再窄就连"│ 内容 │"都排不下，
        # 退化成**裸文本**（不画边框）。这里必须用 `_split_hard` 而不是 `line[:width]`：
        # 后者按**字符数**切，中文会切出 2×width 个 cell，右侧照样捅出去一格。
        out: list[Text] = []
        for raw in block.lines:
            out.extend(Text(chunk, style=palette.md_code_block) for chunk in _split_hard(raw, width))
        return out

    out: list[Text] = []
    # 顶边：╭─ {lang} ──────╮ 或 ╭──────╮
    header = Text("╭", style=palette.md_code_block_border)
    if block.lang:
        label = f"─ {block.lang} "
        if width - 2 >= cell_len(label) + 1:
            header.append(label, style=palette.md_code_block_border)
            header.append("─" * ((width - 2) - cell_len(label)), style=palette.md_code_block_border)
        else:
            header.append("─" * (width - 2), style=palette.md_code_block_border)
    else:
        header.append("─" * (width - 2), style=palette.md_code_block_border)
    header.append("╮", style=palette.md_code_block_border)
    out.append(header)

    inner = max(1, width - _CODE_BORDER_CELLS)
    for raw in block.lines:
        key = (width, raw)
        if line_cache is not None and key in line_cache:
            out.extend(line_cache[key])
            continue
        rendered_line: list[Text] = []
        # 超长行硬切（不丢内容），逐段着色并右侧补齐闭合
        for chunk in _split_hard(raw, inner):
            piece = Text("│ ", style=palette.md_code_block_border)
            if chunk:
                piece.append_text(_highlight_code(chunk, palette))
            used = 2 + cell_len(chunk)
            pad = width - used - 2
            if pad > 0:
                piece.append(" " * pad)
            piece.append(" │", style=palette.md_code_block_border)
            rendered_line.append(piece)
        out.extend(rendered_line)
        if line_cache is not None:
            line_cache[key] = rendered_line

    # 底边：╰──────╯
    footer = Text("╰" + "─" * (width - 2) + "╯", style=palette.md_code_block_border)
    out.append(footer)
    return out


#: 代码块每行被边框与内边距吃掉的格数（左 "│ " 2 格 + 右 " │" 2 格）
_CODE_BORDER_CELLS = 4


def _split_hard(line: str, width: int) -> list[str]:
    """按 cell 宽硬切一行（**不丢内容**）。空行也要返回一项，否则代码块的空行会消失。"""
    if width <= 0:
        return [""]
    if cell_len(line) <= width:
        return [line]
    chunks: list[str] = []
    current = ""
    used = 0
    for char in line:
        char_width = cell_len(char)
        if used + char_width > width:
            chunks.append(current)
            current, used = "", 0
        current += char
        used += char_width
    chunks.append(current)
    return chunks
