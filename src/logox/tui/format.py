"""纯函数格式化工具（无状态、无 IO，因此可被穷尽测试）。

为什么单独成模块
----------------
状态栏、工具卡片、diff 徽标、时间线的显示文本全部由这里的函数产出。
把它们从 widget 里抽出来，是为了**在不启动 Textual 的前提下测掉格式化的全部分支**
（widget 测试成本高，纯函数测试成本几乎为零）。

**宽度规则**：任何涉及显示宽度的计算一律走 ``rich.cells.cell_len``——
中文占 2 cell，用 ``len()`` 会导致边框断裂（UI-SPEC §4）。
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from rich.cells import cell_len

from logox.kernel.events import ChangeStat

__all__ = [
    "wrap_cells",
    "EMPTY",
    "clip",
    "diff_badge",
    "format_bytes",
    "format_clock",
    "format_cost",
    "format_duration",
    "format_ratio",
    "format_tokens",
    "pad_right",
    "short_path",
    "summarize_args",
]

EMPTY = "—"
"""无数据时的占位符（UI-SPEC §5.1：无数据显示 ``—``，而不是显示 0）。"""


# --------------------------------------------------------------------------- #
# 宽度安全（UI-SPEC §4）
# --------------------------------------------------------------------------- #


def wrap_cells(text: str, width: int, *, indent: str = "", reflow: bool = True) -> str:
    """按 **cell 宽度**折行（**换行、不丢内容**），并默认**重排 (reflow)** 软换行。

    为什么需要它而不是 `clip`：助手正文可能很长，用 `clip` 会**截断丢内容**；
    而纯函数渲染层（`render_blocks`）必须自己保证"行宽不超过 width"——
    因为它是被单测直接断言的，不能指望 Textual 的 widget 层兜底。

    「重排」是什么意思，为什么默认要开
    ----------------------------------
    大模型吐出来的正文是**窄栏 Markdown**风格：它并不知道我们的终端有多宽，
    所以会按自己的感觉在句子中间插换行符。如果我们把这些换行符当成**硬换行**
    原样保留，就会看到：

    .. code-block:: text

        It contains three sections, and the second one is the important
        one.                                    ← 一个孤零零的单词占一整行
        这一段中文没有硬换行，会由我们按终端宽度折好
        上面两行的长度明显对不上，看着像排版坏了

    用户报的就是这个现象（**"莫名其妙的换行"**）。

    正确做法是把"**视觉换行由终端宽度决定**"这件事贯彻到底：
    模型给的换行是**软换行 (soft wrap)**，我们按段落重新折一遍。

    **不重排**（``reflow=False``）用于用户自己敲进来的文本——那里的换行
    是**用户有意打的**，重排会篡改他的原话。

    重排规则（刻意保守：宁可少合并，也不要把结构化内容搅坏）
    --------------------------------------------------------

    ================================================  ==========================
    内容                                               怎么处理
    ================================================  ==========================
    空行                                               段落边界，**绝不跨过它合并**
    行首带缩进（两个空格以上 / Tab）                   视为代码或结构化内容，**不合并**
    行首是列表标记（``-`` ``*`` ``+`` ``1.`` ``1)``）  **不合并**（每一项独立折行）
    行首是引用标记（``>``）                            不合并
    其余（普通散文）                                   合并成一段后按宽度重新折行
    ================================================  ==========================

    **代码块**因此天然是安全的：模型输出代码时一定会缩进（或落在 ``` 围栏里），
    而围栏行与缩进行都不参与合并。

    拼接时中文**不加空格**
    ----------------------
    英文换行处原本是空格，合并成一行要补回来；中文换行处原本没有空格，
    补了就会看到"中文 里面 到处 是 空隙"。因此按**相邻字符是否 CJK**
    决定补不补空格（见 :func:`_join`）。
    """
    if width <= 0:
        return ""
    if not reflow:
        raw_lines: list[str] = []
        for line in text.split("\n"):
            raw_lines.extend(_wrap_single_line(line, width))
        return "\n".join(raw_lines).rstrip("\n")
    # 末尾的换行不留空行：调用方自己会补块间距，这里多留一行就是白白多一格空行
    return "\n".join(_wrap_with_reflow(text, width)).rstrip("\n")


def _wrap_with_reflow(text: str, width: int) -> list[str]:
    """按**空行**切段落，逐段重排折行（:func:`wrap_cells` 的实现主体）。"""
    paragraphs: list[list[str]] = [[]]
    for line in text.split("\n"):
        if line.strip():
            paragraphs[-1].append(line)
        else:
            paragraphs.append([])

    lines: list[str] = []
    for paragraph in paragraphs:
        if not paragraph:
            continue
        if lines:
            lines.append("")  # 段落之间留一个空行（连续多个空行压成一个）
        lines.extend(_wrap_paragraph(paragraph, width))
    return lines


def _wrap_paragraph(raw_lines: list[str], width: int) -> list[str]:
    """折行一个段落：结构化的行原样折，**连续散文行合并成一段后重折**。

    段落内**除了 Tab 缩进**之外没有任何东西会打断合并——这一点是关键：
    模型每写完一句话就换行（窄栏 Markdown 习惯），如果换行也打断合并，
    屏幕上就会出现"开头一句独占一行、没有填满"的怪现象（实测在真 app 里看到过）。
    """
    out: list[str] = []
    group: list[tuple[str, str]] = []  # (行首缩进, 去掉缩进与尾部空白的正文)

    def pending() -> str:
        """当前待合并的正文（组内为单行时原样返回，不做任何拼接）。"""
        if not group:
            return ""
        joined = group[0][1]
        for lead, body in group[1:]:
            joined = _join(joined, body, opening=lead)
        return joined

    def flush() -> None:
        if not group:
            return
        # 组内缩进以**首行**为准：后续行的缩进是模型自己的对齐，合并后不再需要
        out.extend(_wrap_single_line(group[0][0] + pending(), width))
        group.clear()

    for raw in raw_lines:
        if _is_structured(raw):
            flush()
            body = raw.rstrip()
            out.extend(_wrap_single_line(body, width, indent=_hanging_indent(body)))
            continue
        lead = _leading_ws(raw)
        if group and lead.startswith("\t"):
            flush()  # Tab 缩进 = 另一块内容，不跨过它合并
        group.append((lead, raw.strip()))
    flush()
    return out


def _is_structured(line: str) -> bool:
    """这一行是否**不该参与重排**（缩进、列表标记、引用标记、表格、围栏）。"""
    head = _leading_ws(line)
    if head.startswith("\t"):  # 缩进用的是 Tab
        return True
    if cell_len(head) >= 2:  # 两个以上 cell 缩进 = 代码或结构化内容
        return True
    body = line.lstrip(_INDENT_CHARS)
    return body.startswith(_STRUCTURED_PREFIXES) or _ORDERED_ITEM.match(body) is not None


#: 行首出现这些标记时视为结构化内容（无序列表、引用、代码围栏、标题、表格）
_STRUCTURED_PREFIXES = ("- ", "* ", "+ ", "> ", "```", "#", "|")

#: 有序列表标记（``1.`` / ``2)``）——**必须单独用正则**：
#: 列表项一旦被当成散文合并，屏幕上就会出现 ``1. Open the file2. Change ...``
_ORDERED_ITEM = re.compile(r"\d{1,3}[.)]\s")


def _hanging_indent(line: str) -> str:
    """列表项换行后的对齐空白（``- abc`` → 后续行缩进 2 cell）。

    没有它的话，一条长列表项的第二行会顶到最左边，与项目符号平齐，
    看起来像**另起了一个新段落**。
    """
    head = _leading_ws(line)
    marker, marker_width = _split_marker(line.lstrip(_INDENT_CHARS))
    return head + " " * marker_width


def _join(left: str, right: str, *, opening: str = "") -> str:
    """把重排后相邻的两行接起来，**按需补空格**。

    英文（非 CJK）之间原本就有空格，必须补回来，否则 ``importantone``；
    中文之间原本没有空格，补了会看到满屏空隙。
    """
    if not left or not right:
        return left + right
    if not opening and _is_cjk(left[-1]) and _is_cjk(right[0]):
        return left + right
    return f"{left} {right}"


def _is_cjk(char: str) -> bool:
    """是否是**全角**字符（中文、日文、韩文，以及中文标点）。

    用来决定重排拼接时补不补空格；也用来避免把中文标点排到行首。
    """
    code = ord(char)
    return (
        0x3000 <= code <= 0x303F  # CJK 标点
        or 0x3400 <= code <= 0x4DBF  # 扩展 A
        or 0x4E00 <= code <= 0x9FFF  # 基本区
        or 0xF900 <= code <= 0xFAFF  # 兼容表意
        or 0xFF00 <= code <= 0xFF60  # 全角形式
        or 0xFFE0 <= code <= 0xFFE6
        or 0x3040 <= code <= 0x30FF  # 日文假名
        or 0xAC00 <= code <= 0xD7AF  # 韩文
    )


def _wrap_single_line(line: str, width: int, *, indent: str = "") -> list[str]:
    """把**一行**折成多行（输入保证不含 ``\\n``）。

    ``indent`` 是**悬挂缩进**：列表项换行后要对齐到正文，而不是回到项目符号下面。

    首行与续行的宽度预算**必须分开算**：

    * 首行预算 = ``width - 原文缩进 - 列表标记``；
    * 续行预算 = ``width - 悬挂缩进``（续行还要再花掉 ``fill``）。

    ⚠️ **绝不"折完再拼回去重折"**。第一版就是这么写的：折出首行后把剩下的拼成
    一个字符串、再折一次——而拼接时为了去掉分隔空格用了
    ``" ".join(rest.split(" ")[1:])``，那一行**把整段正文丢掉了**。
    实测：157 个字的输入只剩 25 个字（用随机文本跑 2000 组，1624 组丢内容），
    用户看到的正是"话说到一半突然没了"。

    现在的做法是用 :func:`_wrap_pieces` 的 ``take`` 参数**切一次首行**，
    剩下的交给它自己用续行预算继续折——不经过任何拼接。
    """
    line = line.rstrip()
    if cell_len(line) <= width:
        return [line]

    prefix = _leading_ws(line)
    body = line.lstrip(_INDENT_CHARS)
    first_budget = width - cell_len(prefix)
    if first_budget < 12:  # 原文缩进吃掉太多宽度 → 只能牺牲它，不能超宽
        prefix, first_budget = "", width

    # 列表标记**先摘下来**，折完再贴回首行。
    # 不摘的话，窄宽度下标记会被单独折成一行 ``-``，列表项就变成了一个孤零零的破折号
    # （实测：width=40 时首行只剩 ``-``）。
    marker, marker_width = _split_marker(body)
    body = body[marker_width:]

    fill = indent if cell_len(indent) >= cell_len(prefix) else " " * cell_len(prefix)
    if cell_len(fill) > width // 2:
        fill = ""
    # 续行要比首行多让出"标记 + 悬挂缩进"的差值，否则续行会超宽
    tail_budget = width - cell_len(fill) - max(0, cell_len(marker) - cell_len(prefix))
    if tail_budget < 1:
        # 太窄：连悬挂缩进都放不下 → 保住"不超宽"这条硬不变量，放弃标记对齐
        return _wrap_pieces(line.lstrip(_INDENT_CHARS), width)

    head_budget = max(1, first_budget - marker_width)
    head = _wrap_pieces(body, head_budget, take=1)
    out = [prefix + marker + (head[0] if head else "")]
    # 首行吃掉了多少字符 → 余下的原样交给续行折（**不做任何拼接**）
    consumed = len(head[0]) if head else 0
    rest = body[consumed:].lstrip()
    if rest:
        out.extend(fill + piece for piece in _wrap_pieces(rest, tail_budget))
    return out


def _split_marker(body: str) -> tuple[str, int]:
    """摘出列表/引用标记（``- `` / ``1. `` / ``> ``），返回 ``(标记, cell 宽度)``。

    标记必须在**每一次折行里都粘在正文前面**：它一旦被当成普通文本参与折行，
    窄宽度下就会独占一行，用户看到的列表项是一个孤零零的破折号。
    """
    ordered = _ORDERED_ITEM.match(body)
    if ordered is not None:
        return body[: ordered.end()], cell_len(body[: ordered.end()])
    if body.startswith(_MARKERS):
        return body[:2], cell_len(body[:2])
    return "", 0


#: 无序列表 / 引用标记（两字符，后面跟正文）
_MARKERS = ("- ", "* ", "+ ", "> ")


#: 参与"缩进"判定的字符：ASCII 空白 + 全角空格（中文文档里很常见）+ 不间断空格
_INDENT_CHARS = " \t\u3000\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u00a0"


def _leading_ws(line: str) -> str:
    """行首的缩进（**含全角空格**）。

    不能只用 ``lstrip()``：Python 的 ``str.lstrip()`` 默认把全角空格 ``U+3000``
    也算空白，而它 **占 2 cell**——一旦按"字符数"当缩进算，宽度预算就少算一倍，
    结果是"缩进 2 + 正文"超宽（实测在窄终端下被抓到）。
    """
    return line[: len(line) - len(line.lstrip(_INDENT_CHARS))]


def _wrap_pieces(text: str, budget: int, *, take: int | None = None) -> list[str]:
    """把一段连续文本折成若干行（每行不超过 ``budget`` cell）。

    ``take``：只折出**前 N 行**（``None`` = 全部）。

    为什么用"扫描断点"的写法
    ------------------------
    这里前后写过三版，前两版都在**记账**上出错（"这一行里到底已经有什么"），
    症状是**丢字**与**重复字**交替出现，而且只在特定文本+宽度组合下复现：

    * 丢字：回溯断行时截短了当前行，却忘了把触发换行的 token 交回续行；
    * 重复字：又把整个 token 拼了一次（实测 ``…1，。及及*…``、``…1，。。及*…``）。

    现在的写法**没有"当前行"这个可变状态**：每一轮都从 ``start`` 出发，
    先算出这一行**最多**能吃到哪个下标（``hard``，保证不超预算），
    再在这个范围内找一个**更好的断点**（``preferred``，标点或空格之后）。
    然后**一次性**切出 ``text[start:end]`` 并推进 ``start``。

    这样"每个字符属于哪一行"由**区间划分**决定（区间首尾相接、无缝无叠），
    而不是靠拼接与清空维护——**丢字与重复在结构上就不可能发生**。

    断行优先级：``preferred``（标点/空格之后）> ``hard``（预算边界）。
    中文没有空格，硬断会把"（颜色、底色、高亮）"劈成"（颜色、底 / 色、高亮）"，
    这正是用户报的"**错误的断句**"，所以标点断点优先。
    """
    tokens = _tokenize_for_wrap(text)
    # 把 token 摊成 (起点, 终点, 是否空白)——用字符下标区间记账，杜绝拼接错误
    spans: list[tuple[int, int, bool]] = []
    cursor = 0
    for token in tokens:
        spans.append((cursor, cursor + len(token), token.isspace()))
        cursor += len(token)
    total = len(text)

    lines: list[str] = []
    start = 0
    while start < total and (take is None or len(lines) < take):
        start = _skip_blank(text, spans, start)
        if start >= total:
            break
        hard = _scan_hard(text, spans, start, budget)
        end = _scan_preferred(text, spans, start, hard)
        piece = text[start:end].strip()
        if piece:
            lines.append(piece)
        start = end
    return lines or [""]


def _skip_blank(text: str, spans: list[tuple[int, int, bool]], start: int) -> int:
    """跳过行首空白（折行处的空白不带进下一行）。"""
    while start < len(text) and text[start].isspace():
        start += 1
    return start


def _scan_hard(text: str, spans: list[tuple[int, int, bool]], start: int, budget: int) -> int:
    """这一行**最多**能吃到哪个下标（不含），保证 ``cell_len(text[start:end]) <= budget``。

    返回的下标**严格大于** ``start``，因此调用方每轮一定前进、不会死循环。
    """
    used = 0
    end = start
    for span_start, span_end, blank in spans:
        if span_end <= start:
            continue
        if span_start < start:  # 起点落在 token 中间 → 从 start 开始按字符算
            span_start = start
        if blank:
            if used + 1 > budget:
                break
            used += 1
            end = span_end
            continue
        width = cell_len(text[span_start:span_end])
        if used + width <= budget:
            used += width
            end = span_end
            continue
        # 整个 token 放不下 → 能塞几个字符就塞几个
        for char in text[span_start:span_end]:
            char_width = cell_len(char)
            if used + char_width > budget:
                break
            used += char_width
            end += 1
        break  # 这个 token 已经处理完（或行已满）
    return max(end, start + 1) if end <= start else end


def _scan_preferred(text: str, spans: list[tuple[int, int, bool]], start: int, hard: int) -> int:
    """在 ``(start, hard]`` 里找**更好的断点**；找不到就返回 ``hard``。

    更好的断点 = 标点之后，或空格处。只往回看一小段：为了一个远处的标点
    把整行掏空反而更难看。
    """
    if hard - start < 4:
        return hard
    floor = max(start + 1, hard - max(8, (hard - start) // 4))
    for index in range(hard - 1, floor - 1, -1):
        char = text[index]
        if char in _BREAK_AFTER:
            return index + 1  # 标点留在本行尾部
        if char.isspace():
            return index  # 空白丢掉
    return hard


#: 断行**优先落在这些字符之后**（中文标点与句读）——把句子断在标点处才读得顺
_BREAK_AFTER = "，。！？；：、）】》」』,.!?;:)]}\"'"


def _prefer_break(line: str) -> tuple[str, str]:
    """在 ``line`` 里找**最好的断点**，返回 ``(本行, 余下)``。

    找不到合适断点（或回溯会掏空整行）时返回 ``(line, "")``，由调用方按硬断处理。

    为什么要这么做：中文没有空格，逐字硬断会把"（颜色、底色、高亮）"劈成
    "（颜色、底" + "色、高亮）"——单看完全读不通。断在标点后则至少是完整短语。
    这也是用户报的"**错误的断句**"的直接对策。

    ``line`` 的长度已经接近预算，所以只需往回看一小段：为了一个远处的标点
    把整行掏空反而更糟。
    """
    floor = max(1, len(line) - max(8, len(line) // 4))
    for index in range(len(line) - 1, floor - 1, -1):
        if line[index] in _BREAK_AFTER or line[index].isspace():
            # ⚠️ 断点字符**必须被本行吃掉**（留在 head 里）或**直接丢掉**。
            # 早期版本用 ``line[index + 1 :]`` 当 carry，那个字符既不在 head
            # 也不在 carry —— 而调用方要用 carry 去接续行，于是断点处的字
            # **既没丢也没留，而是被后续拼接重复了一遍**（实测 ``中 英文``、
            # 以及 ``word word`` 里多出一个 ``word``）。
            # 空白本来就是折行处，直接丢弃；标点则跟在本行尾部。
            head = line[: index + 1].rstrip()
            carry = line[index + 1 :].lstrip()
            if head and carry:
                return head, carry
    return line, ""


def _split_oversized(token: str, budget: int) -> list[str]:
    """逐字符硬切一个装不下的超长 token。"""
    pieces: list[str] = []
    current, used = "", 0
    for char in token:
        char_width = cell_len(char)
        if current and used + char_width > budget:
            pieces.append(current)
            current, used = "", 0
        current += char
        used += char_width
    if current:
        pieces.append(current)
    return pieces


def _tokenize_for_wrap(line: str) -> list[str]:
    """把一行切成可断点单元：**连续的非空白**（词）与**单个空白**。"""
    tokens: list[str] = []
    buffer = ""
    for char in line:
        if char.isspace():
            if buffer:
                tokens.append(buffer)
                buffer = ""
            tokens.append(char)
        else:
            buffer += char
    if buffer:
        tokens.append(buffer)
    return tokens


def clip(text: str, width: int, *, ellipsis: str = "…") -> str:
    """按 **cell 宽度**截断（不是字符数），保证中文不会被截半个字。

    **多行文本按行处理**——这一点是踩过坑才补上的：
    ``cell_len("\\n")`` 算 0 宽，所以"整体宽度"看起来不超限时函数会原样返回；
    而真超限时它会**从中间截断**，把换行、缩进与后半段一起丢掉。
    用户看到的是"一段莫名其妙的换行 + 内容凭空消失"。

    现在的语义是**逐行 clip、换行原样保留**：既不会丢内容，也不会把多行压成一行。
    需要完整长文本换行（而不是截断）的场合，仍然应当交给 Textual 自己 word-wrap
    （见 ``render_blocks`` 里 assistant 分支）。
    """
    if width <= 0:
        return ""
    if "\n" in text:
        return "\n".join(clip(line, width, ellipsis=ellipsis) for line in text.split("\n"))
    if cell_len(text) <= width:
        return text
    ellipsis_width = cell_len(ellipsis)
    budget = max(0, width - ellipsis_width)
    out: list[str] = []
    used = 0
    for char in text:
        char_width = cell_len(char)
        if used + char_width > budget:
            break
        out.append(char)
        used += char_width
    return "".join(out) + ellipsis


def pad_right(text: str, width: int) -> str:
    """按 cell 宽度右填充空格。"""
    padding = width - cell_len(text)
    return text + " " * padding if padding > 0 else clip(text, width)


def short_path(path: str, width: int) -> str:
    """把路径缩到指定宽度，**保留尾部的文件名**（尾部信息量最大）。"""
    if cell_len(path) <= width:
        return path
    parts = path.replace("\\", "/").split("/")
    tail = parts[-1]
    if cell_len(tail) >= width:
        return clip(tail, width)
    prefix = "…/"
    budget = width - cell_len(prefix) - cell_len(tail)
    head = "/".join(parts[:-1])
    return clip(head, budget) + prefix[len(prefix) - 1 :] + tail if budget > 0 else clip(tail, width)


# --------------------------------------------------------------------------- #
# 数值格式化（UI-SPEC §5.1 / §5.5）
# --------------------------------------------------------------------------- #


def format_duration(ms: int | float | None) -> str:
    """耗时：``0.4s`` / ``12.3s`` / ``1m03s`` / ``1h02m``。

    工具卡片用一位小数（``0.4s``），超过 10 秒则不再显示小数——精度对判断"是否卡住"没有帮助。
    """
    if ms is None:
        return EMPTY
    seconds = max(0.0, float(ms) / 1000.0)
    if seconds < 10:
        return f"{seconds:.1f}s"
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes, rest = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m{rest:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def format_clock(ms: int | float | None) -> str:
    """累计时长（``Σ 12:34`` 用的格式）：``12:34`` / ``1:02:03``。"""
    if ms is None:
        return EMPTY
    total = max(0, int(ms) // 1000)
    hours, rest = divmod(total, 3600)
    minutes, seconds = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def format_tokens(count: int | None) -> str:
    """``1.2k`` / ``12.4k`` / ``1.2M``。"""
    if count is None:
        return EMPTY
    if count < 1000:
        return str(count)
    if count < 1_000_000:
        return f"{count / 1000:.1f}k"
    return f"{count / 1_000_000:.1f}M"


def format_cost(usd: float | None) -> str:
    """``$0.0031``；金额极小时保留 4 位（否则会显示成 $0.00，看起来像免费）。"""
    if usd is None:
        return EMPTY
    if usd == 0:
        return "$0"
    if usd < 0.01:
        return f"${usd:.4f}"
    if usd < 1:
        return f"${usd:.3f}"
    return f"${usd:.2f}"


def format_ratio(ratio: float | None, *, digits: int = 0) -> str:
    """``42%``；``None`` → ``—``（未上报与 0% 必须区分，D39）。"""
    if ratio is None:
        return EMPTY
    return f"{ratio * 100:.{digits}f}%"


def format_bytes(count: int | None) -> str:
    """``1.2 KB`` / ``3.4 MB``。"""
    if count is None:
        return EMPTY
    if count < 1024:
        return f"{count} B"
    if count < 1024 * 1024:
        return f"{count / 1024:.1f} KB"
    return f"{count / (1024 * 1024):.1f} MB"


# --------------------------------------------------------------------------- #
# diff 徽标（D40 的五类情形）
# --------------------------------------------------------------------------- #


def diff_badge(stat: ChangeStat | None) -> str:
    """把 ``ChangeStat`` 渲染成折叠态的一行徽标。

    | kind | 徽标 |
    |---|---|
    | ``modify`` | ``diff +8 -3`` |
    | ``new`` | ``diff +42 -0  (new)`` |
    | ``rewrite`` | ``diff +120 -118  (rewrite)`` |
    | ``binary`` | ``binary changed  1.2 KB → 1.4 KB`` |
    | ``large`` | ``diff +312 -48  (large)`` |

    无变更统计时返回 ``EMPTY``——**不显示 ``diff +0 -0``**（那会让人以为改了东西）。
    """
    if stat is None:
        return EMPTY
    if stat.kind == "binary":
        before = format_bytes(stat.bytes_before)
        after = format_bytes(stat.bytes_after)
        return f"binary changed  {before} → {after}"

    badge = f"diff +{stat.added} -{stat.removed}"
    suffix = {"new": "  (new)", "rewrite": "  (rewrite)", "large": "  (large)"}.get(stat.kind, "")
    return badge + suffix


#: 参数摘要里优先展示的键（按此顺序取第一个命中的）——它们最能说明"这一步在动什么"
_SUMMARY_KEYS = ("path", "file", "command", "pattern", "query", "url", "name", "glob")


def summarize_args(args: Mapping[str, object] | None, *, width: int = 44) -> str:
    """从工具入参里挑出**最能说明意图**的一项，作为卡片的参数摘要。

    M5 起，真实摘要由工具自己声明的 ``ToolSpec.summary_template`` 提供
    （每个工具最清楚该显示什么）；本函数是那之前的**兜底默认**，
    保证卡片在任何情况下都不是"光秃秃一个工具名"。
    """
    if not args:
        return ""
    for key in _SUMMARY_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return clip(value.strip(), width)
    for key, value in args.items():
        if isinstance(value, (str, int, float, bool)):
            return clip(f"{key}={value}", width)
    return ""
