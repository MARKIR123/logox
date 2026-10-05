"""卡片类内容的渲染：工具卡片、推理块、diff 视图（D13 / D14 / D31 / D40）。

**纯函数，不含任何界面框架**——输入"数据 + 宽度 + 调色板"，输出 Rich ``Text``。
于是每一条视觉规则都能用一句断言测出来（"给定宽度，第 3 行长什么样"），
不需要真终端、不需要事件循环、不需要界面框架。

关于 diff 数据
--------------
:class:`DiffHunk` 与 :func:`parse_unified_diff` 住在 :mod:`logox.difftext`
（D140 / F-50 搬迁完成）—— 因为**工具层的生成侧也要用它们**，
放在这里会让 `import logox.tools.*` 连带加载界面层。本模块现在只保留渲染职责，
并按老路径把它们**转出**（兼容既有 import）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from rich.cells import cell_len
from rich.text import Text

from logox.config.schema import ThemePalette
from logox.difftext import DiffHunk, DiffLineKind, parse_unified_diff
from logox.kernel.events import ChangeStat
from logox.tui.format import EMPTY, clip, format_duration, pad_right
from logox.tui.format import diff_badge as render_diff_badge

__all__ = [
    "COMPACT_WIDTH",
    "CardContext",
    "DiffHunk",
    "ToolCardState",
    "parse_unified_diff",
    "render_diff",
    "render_reasoning",
    "render_tool_card",
    "render_tool_summary",
]

ToolCardState = Literal["pending", "running", "ok", "error", "denied", "cancelled"]

#: 状态 → 语义 token 名（组件里不出现字面色值）
STATE_TOKEN: dict[str, str] = {
    "pending": "text_faint",
    "running": "accent",
    "ok": "success",
    "error": "danger",
    "denied": "warning",
    "cancelled": "warning",
}

#: 窄屏阈值：低于此宽度时 diff 只显示变更行（UI-SPEC §5.6）
COMPACT_WIDTH = 80

#: 工具卡展开后**最多显示多少行输出**（F-43 / D136）。
#:
#: 为什么必须有上限：`shell` / `read` 的输出动辄几百行，全部铺开会把时间线冲掉 ——
#: 而用户按下 `Ctrl+O` 想看的通常是"这台命令报了什么错"，不是从头读一遍日志。
#: 完整内容仍可从 `transcript.jsonl` / 工具 blob 里查。
LARGE_OUTPUT_PREVIEW = 30

#: 大 diff 阈值与折叠后的预览行数（UI-SPEC §5.6）
LARGE_DIFF_LINES = 200
LARGE_DIFF_PREVIEW = 20

#: 推理块展开后的最大行数
MAX_REASONING_LINES = 20


@dataclass(frozen=True)
class CardContext:
    """渲染卡片所需的全部外部输入。"""

    palette: ThemePalette
    glyphs: dict[str, str] = field(default_factory=dict)
    width: int = 100
    diff_context_lines: int = 3

    def token(self, name: str) -> str:
        return getattr(self.palette, name)


# --------------------------------------------------------------------------- #
# diff 数据（D140：类型与解析已下沉到 `logox.difftext`）
# --------------------------------------------------------------------------- #
#
# ⚠️ 这里**只做转出**，不再自己定义：`DiffHunk` / `parse_unified_diff` 的消费者有两个层
# （工具层的生成侧 + 界面层的渲染侧），所以它们的家是**两边之下的中立模块**。
# 留在本模块会导致 `import logox.tools.*` 顺手把界面层拖进来（F-50）。
# 老的 `from logox.tui.content.cards import DiffHunk` 写法依然可用 —— 这是**故意**的兼容。

# --------------------------------------------------------------------------- #
# 纯渲染函数
# --------------------------------------------------------------------------- #


def render_tool_summary(
    *,
    state: ToolCardState,
    name: str,
    args_summary: str,
    duration_ms: int | None,
    error_kind: str | None,
    glyphs: dict[str, str],
) -> str:
    """折叠态的**单行摘要**（D13 核心：一行说清"做了什么、成没成、多久"）。"""
    glyph = {
        "pending": glyphs.get("pending", "·"),
        "running": glyphs.get("running", "✻"),
        "ok": glyphs.get("success", "✓"),
        "error": glyphs.get("error", "✗"),
        "denied": glyphs.get("denied", "⊘"),
        "cancelled": glyphs.get("cancelled", "◼"),
    }[state]

    parts = [f"{glyph} {name}"]
    if args_summary:
        parts.append(args_summary)
    if state == "running":
        parts.append("⟳")
    elif state == "error" and error_kind:
        parts.append(error_kind)
    if duration_ms is not None:
        parts.append(format_duration(duration_ms))
    return "  ".join(parts)


def render_tool_card(
    *,
    state: ToolCardState,
    name: str,
    args_summary: str,
    context: CardContext,
    duration_ms: int | None = None,
    error_kind: str | None = None,
    change_stat: ChangeStat | None = None,
    args_text: str = "",
    payload: str = "",
    expanded: bool = False,
    indent: int = 0,
) -> Text:
    """渲染一张工具卡片：**折叠态只有一行**，展开态才在下面挂内容（D132）。

    两个状态各写什么（用户裁定）：

    ============ ==================================================================
    折叠态         **一行**：状态字形/颜色（= 执行结果）+ 工具名（= 操作） +
                   ``args_summary``（= 参数）[+ 耗时]。**没有**变更徽标、**没有**键位提示
    展开态         同一行 + 变更徽标 + ``Ctrl+O 折叠``，下面缩进挂参数正文/工具输出
    ============ ==================================================================

    ⚠️ **为什么折叠态要把徽标与提示都拿掉**：它们原先各占一行/一段（``└ diff +1 -1``
    外加 ``Ctrl+O 展开``）。用户的原话是"虽然没有完全展开，但 diff 和工具调用还是有一块在"
    —— 也就是说那种"折叠了但还占一块"的中间态正是要消灭的东西。

    ⚠️ **同时补上了"怎么收回"的提示**（D132）：原先只在折叠态写 ``Ctrl+O 展开``，
    展开后一个字都不写 —— 用户展开完就找不到收回的路，合理地以为"关不掉、没这个快捷键"。

    状态用**符号 + 前景色**表达，**不用底色**（D81，用户裁定）。
    运行中 / 成功 / 失败各有一个字形（``✻``/``✓``/``✗``）与颜色，
    扫一眼就知道哪些还在跑；展开的内容用**缩进**挂在摘要下。

    ⚠️ **为什么放弃底色**（D80 曾经做过，已撤销）：那些底色与正文背景的对比度
    被刻意压在 **1.10–1.30**（"只是分区、不抢眼"）。这个量级**只在与校准终端
    一致时才成立**——用户的终端有自己的配色与色偏，于是那些底会变成一层脏色块。
    改用结构分区（字形 + 缩进 + 空行）后，**在任何终端上表现一致**。
    """
    palette = context.palette
    width = max(20, context.width - indent)
    content_width = max(4, width - 2 - indent)

    summary = render_tool_summary(
        state=state,
        name=name,
        args_summary=args_summary,
        duration_ms=duration_ms,
        error_kind=error_kind,
        glyphs=context.glyphs,
    )
    pad = " " * indent
    summary_width = max(4, width - indent)

    if not expanded:
        # ★ D132：折叠态 **就这一行**（多一个字都不加，见 docstring）
        return Text(
            pad + clip(summary, summary_width),
            style=getattr(palette, STATE_TOKEN[state]),
        )

    # 展开态：摘要 +（可选）变更徽标 + **怎么收回**的提示。
    # ⚠️ 先把尾巴的宽度算出来再裁摘要 —— 否则尾巴会把行撑超宽，
    #    而渲染器的兜底是**硬切**，被切掉的恰好是那句提示（等于没写）。
    tail: list[tuple[str, str]] = []
    badge = render_diff_badge(change_stat)
    if badge != EMPTY:
        tail.append(("  " + badge, palette.text_muted))
    tail.append(("   Ctrl+O 折叠", palette.text_faint))
    budget = max(8, summary_width - sum(cell_len(text) for text, _ in tail))

    out = Text(pad + clip(summary, budget), style=getattr(palette, STATE_TOKEN[state]))
    for text, style in tail:
        out.append(text, style=style)

    # 展开内容用**缩进**挂在摘要下（缩进是可依赖的结构信号，底色不是）
    # ★ D139：两个内容块（① 完整参数、② 结果主体）走**同一个**渲染函数。
    # ★ D182：展开后 100% 完整展示全部输出，不再截断，亦不再打印“另有xx行未显示”占位提示。
    def append_content(text: str, style: str) -> None:
        lines = text.splitlines() or [""]
        for line in lines:
            out.append("\n" + pad + "  ")
            out.append(clip(line, content_width), style=style)

    # 顺序即 UI-SPEC §5.6 的顺序：① 完整参数 → ② 结果主体
    if args_text:
        append_content(args_text, palette.text_muted)
    if payload:
        # ★ D161：工具**结果主体**用专属 token（此前借 text_primary）。
        #   它和"完整参数"（text_muted）是两个不同的东西：参数是你想看"它到底干了什么"，
        #   结果是你想看"它得到了什么"—— 分开之后两者可以各自调深浅。
        append_content(payload, palette.tool_output_fg)
    return out


def quote_block(lines: list[Text], *, marker: str = "▌", marker_style: str = "", indent: int = 0) -> Text:
    """把多行包成**引用块**：每行左侧一个竖线标记 + 缩进（D81）。

    **为什么用这个替代"整宽底色块"（D80 已撤销）**
    ---------------------------------------------
    底色那套做法要求"与校准终端一致"：我们把它压到 1.10–1.30 的对比度
    （刻意"只是分区、不抢眼"），而**这个量级只在终端配色与校准时相同才成立**。
    用户的终端有自己的配色与色偏，于是那些底变成一层脏色块——原话
    "反而在我这个自定义的终端上不好看"。

    结构信号（竖线、缩进、字形、空行）**不依赖任何颜色**，在任何终端上表现一致，
    而且**天然对色盲友好**（UI-SPEC §10 的"不得仅靠颜色表意"）。

    ⚠️ 每行都带标记，**不是只给第一行**：只给第一行的话，折行后的续行会"漏出"
    引用块之外，看起来像正文（这与列表项需要悬挂缩进是同一个道理）。
    """
    prefix = (" " * indent) + marker + " "
    out = Text()
    for index, line in enumerate(lines):
        if index:
            out.append("\n")
        out.append(prefix, style=marker_style)
        out.append_text(line)
    return out


def render_diff(
    *,
    path: str,
    stat: ChangeStat | None,
    hunks: list[DiffHunk],
    context: CardContext,
    expanded: bool = True,
    indent: int = 0,
) -> Text:
    """渲染 unified diff（D14 / D40）。"""
    palette = context.palette
    compact = context.width < COMPACT_WIDTH
    width = max(20, context.width - indent - 1)
    pad = " " * indent
    out = Text()

    out.append(pad + clip(path, width), style=f"bold {palette.text_primary}")
    badge = render_diff_badge(stat)
    if badge != EMPTY:
        out.append(f"   {badge}", style=palette.text_muted)
    if not expanded:
        # D125-b：折叠态必须给出**怎么展开**的提示。
        # 原先这里直接 return、一个字都不留 —— 用户看到一行路径 + 徽标，
        # 无从知道按哪个键能看到 diff（对比工具卡就写了 `Ctrl+O 展开`）。
        out.append("   Ctrl+O 展开", style=palette.text_faint)
        return out

    # ★ D182：展开后 100% 完整展示全部 diff，不再截断行数
    for hunk in hunks:
        out.append("\n" + pad + clip(hunk.header, width), style=palette.text_faint)
        for kind, text in hunk.lines:
            if compact and kind == "context":
                continue  # 窄屏：上下文一律不显示
            # 注意：Rich 的 append 只接 str，要插入带样式的片段必须用 append_text
            out.append("\n" + pad)
            out.append_text(_diff_line(kind, text, width, palette))

    return out


def _diff_line(kind: DiffLineKind, text: str, width: int, palette: ThemePalette) -> Text:
    """渲染一行 diff（不含换行；调用方负责拼接）。"""
    prefix = {"add": "+ ", "del": "- ", "context": "  ", "meta": "  "}[kind]
    body = pad_right(clip(prefix + text, width), width)
    if kind == "add":
        return Text(body, style=f"{palette.diff_add_fg} on {palette.diff_add_bg}")
    if kind == "del":
        return Text(body, style=f"{palette.diff_del_fg} on {palette.diff_del_bg}")
    if kind == "meta":
        return Text(body, style=palette.text_faint)
    return Text(body, style=palette.text_muted)


def render_reasoning(
    *,
    text: str,
    context: CardContext,
    duration_ms: int | None = None,
    generating: bool = False,
    expanded: bool = False,
    mode: str = "collapsed",
    indent: int = 0,
) -> Text:
    """渲染推理块（D31：默认收起；``hidden`` 时调用方不应渲染它）。"""
    palette = context.palette
    glyph = context.glyphs.get("thinking", "▸")
    width = max(20, context.width - indent - 2)
    pad = " " * indent
    out = Text()

    if generating:
        out.append(f"{pad}{glyph} 思考中… ({format_duration(duration_ms)})", style=palette.text_muted)
    else:
        out.append(
            f"{pad}{glyph} 思考 ({format_duration(duration_ms)} · {cell_len(text)} 字)",
            style=palette.text_muted,
        )
    if not expanded:
        # D125：思考链归 `Ctrl+T`（**不再是 `Ctrl+O`** —— 那个键改成只切工具与 diff）。
        out.append("   Ctrl+T 展开", style=palette.text_faint)

    if expanded and text:
        # ★ D182：展开后 100% 完整展示全部思考文本，不再截断，亦不再打印“另有xx行未显示”占位提示。
        lines = text.splitlines() or [text]
        for line in lines:
            out.append(f"\n{pad}│ ", style=palette.border_subtle)
            # ★ D161：展开的思考正文用**专属** token（此前借 text_muted）。
            #   接通的意义不是"改外观"（两者当前取值相同）而是**交出控制权** ——
            #   从此你可以只调思考正文而不动状态行/思考标题。
            out.append(clip(line, width), style=f"italic {palette.thinking_text}")
    return out

