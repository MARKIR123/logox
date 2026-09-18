"""卡片类内容的渲染：工具卡片、推理块、diff 视图（D13 / D14 / D31 / D40）。

**纯函数，不含任何界面框架**——输入"数据 + 宽度 + 调色板"，输出 Rich ``Text``。
于是每一条视觉规则都能用一句断言测出来（"给定宽度，第 3 行长什么样"），
不需要真终端、不需要事件循环、不需要界面框架。

关于 diff 数据
--------------
:class:`DiffHunk` 与 :func:`parse_unified_diff` 在 M1.5 先放在这里。
**生成侧**（``tools/edit/diff.py``，M5）将产出同一结构，届时本模块只保留渲染职责。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from rich.cells import cell_len
from rich.text import Text

from logox.config.schema import ThemePalette
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
# diff 数据与解析
# --------------------------------------------------------------------------- #

DiffLineKind = Literal["context", "add", "del", "meta"]


@dataclass(frozen=True)
class DiffHunk:
    """一个 hunk（``@@`` 块）。``lines`` 为 ``(kind, text)`` 序列。"""

    header: str
    lines: tuple[tuple[DiffLineKind, str], ...] = ()

    @property
    def added(self) -> int:
        return sum(1 for kind, _ in self.lines if kind == "add")

    @property
    def removed(self) -> int:
        return sum(1 for kind, _ in self.lines if kind == "del")


def parse_unified_diff(text: str) -> list[DiffHunk]:
    """把 unified diff 文本解析成 hunks（文件头 ``---``/``+++`` 由调用方处理）。"""
    hunks: list[DiffHunk] = []
    header: str | None = None
    lines: list[tuple[DiffLineKind, str]] = []

    def flush() -> None:
        nonlocal header, lines
        if header is not None:
            hunks.append(DiffHunk(header=header, lines=tuple(lines)))
        header, lines = None, []

    for raw in text.splitlines():
        if raw.startswith("@@"):
            flush()
            header = raw
            continue
        if header is None:
            continue
        if raw.startswith("+++") or raw.startswith("---"):
            continue
        if raw.startswith("+"):
            lines.append(("add", raw[1:]))
        elif raw.startswith("-"):
            lines.append(("del", raw[1:]))
        elif raw.startswith("\\"):
            lines.append(("meta", raw))
        else:
            lines.append(("context", raw[1:] if raw.startswith(" ") else raw))
    flush()
    return hunks


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
        "running": glyphs.get("running", "⏺"),
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
    """渲染一张工具卡片（**整宽状态底色块**：折叠态一行 + 可选展开内容）。

    状态用**符号 + 前景色**表达，**不用底色**（D81，用户裁定）。
    运行中 / 成功 / 失败各有一个字形（``⏺``/``✓``/``✗``）与颜色，
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
    out = Text(pad + clip(summary, width - indent), style=getattr(palette, STATE_TOKEN[state]))

    badge = render_diff_badge(change_stat)
    if badge != EMPTY:
        # D40：编辑类卡片默认折叠，只在摘要下追加一行**变更徽标**
        out.append("\n" + pad + "  └ ")
        out.append(badge, style=palette.text_muted)
        if not expanded:
            out.append("      Ctrl+O 展开", style=palette.text_faint)

    if expanded:
        # 展开内容用**缩进**挂在摘要下（缩进是可依赖的结构信号，底色不是）
        if args_text:
            out.append("\n" + pad + "  ")
            out.append(clip(args_text, content_width), style=palette.text_muted)
        if payload:
            out.append("\n" + pad + "  ")
            out.append(clip(payload, content_width), style=palette.text_primary)
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
        return out

    total = sum(len(hunk.lines) for hunk in hunks)
    truncated = total > LARGE_DIFF_LINES
    shown = 0

    for hunk in hunks:
        out.append("\n" + pad + clip(hunk.header, width), style=palette.text_faint)
        for kind, text in hunk.lines:
            if compact and kind == "context":
                continue  # 窄屏：上下文一律不显示
            if truncated and shown >= LARGE_DIFF_PREVIEW:
                continue
            # 注意：Rich 的 append 只接 str，要插入带样式的片段必须用 append_text
            out.append("\n" + pad)
            out.append_text(_diff_line(kind, text, width, palette))
            shown += 1

    if truncated:
        out.append(
            f"\n{pad}… 共 {total} 行，只预览前 {LARGE_DIFF_PREVIEW} 行（Ctrl+O 展开全部）",
            style=palette.text_faint,
        )
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
            out.append("   Ctrl+O 展开", style=palette.text_faint)

    if expanded and text:
        lines = text.splitlines() or [text]
        if len(lines) > MAX_REASONING_LINES:
            lines = [*lines[:MAX_REASONING_LINES], f"… 另有 {len(lines) - MAX_REASONING_LINES} 行未显示"]
        for line in lines:
            out.append(f"\n{pad}│ ", style=palette.border_subtle)
            out.append(clip(line, width), style=f"italic {palette.text_muted}")
    return out

