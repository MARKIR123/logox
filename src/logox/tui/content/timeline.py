"""会话时间线（UI-SPEC §5.2 / §5.3 / §5.6 / §5.12 / §6）。

**纯状态机 + 纯函数，不含任何界面框架。**
:class:`TimelineBuffer` 是"事件 → 显示块"的状态机，
:func:`render_blocks` 与 :func:`render_cached` 是"块 → 文本"的纯函数。
因此时间线的绝大多数行为都能用普通 unittest 覆盖。

卡片由 `cards.py` 的纯函数生成；UI 与完整文本兼容入口共用视觉规则。
按块内容键缓存排版（D199），包括未变的最后一块。新增块仅计算新块；较早的
工具状态或推理正文原地改变，也会让对应结果失效。键保留字符串引用，不进行
全文序列化；每帧仍遍历块与行，不能声称成本与会话长度无关。

UI 使用不可变的文本片段和行，避免先拼接完整历史再拆行。正在增长的 Markdown
复用未变结构及代码行；完整 Text 兼容访问按需拼接。缓存仅保留当前块，删除历史
或清空后释放旧结果。TimelineBuffer.invalidate 负责请求重画，不代替内容键校验。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Literal

from rich.text import Text

from logox.difftext import DiffHunk
from logox.kernel import events as ev
from logox.kernel.events import ChangeStat
from logox.tui import format as fmt
from logox.tui.content.cards import (
    CardContext,
    ToolCardState,
    quote_block,
    render_diff,
    render_reasoning,
    render_tool_card,
)
from logox.tui.content.markdown import MarkdownRenderCache, render_markdown
from logox.tui.content.smoother import StreamSmoother

__all__ = [
    "ActiveStatus",
    "Block",
    "RenderResult",
    "SPINNER_FRAMES",
    "TimelineBuffer",
    "TimelineRenderCache",
    "format_timer",
    "render_active_spinner",
    "render_blocks",
    "render_cached",
]

BlockKind = Literal["user", "assistant", "divider", "notice", "tool", "reasoning", "diff", "raw", "anamnesis"]

#: 超过这么多块时可折叠历史（UI-SPEC §11 第 7 项）
FOLD_THRESHOLD = 30

#: 用户消息色块的左右缩进（D80/D89：归零左对齐）。
_USER_INDENT = 0

#: `TimelineView` 的 CSS 左右内边距合计占用的格数（``padding: 0 1`` → 2 格）。
#: 正文可用宽度必须扣掉它，否则正文会盖到右边的侧栏上（实测踩到）。
_HORIZONTAL_PADDING = 2

#: 纵向滚动**能力**是否启用（D70）。滚动条本身在 D73 被去掉了，但能力保留。
_SCROLLBAR_COLUMNS = 0


@dataclass
class Block:
    """时间线上的一个显示块。"""

    kind: BlockKind
    text: str = ""
    token: str = "text_primary"

    # 工具卡片字段
    name: str = ""
    args_summary: str = ""
    #: ★ D139：展开态第一项「完整参数」（UI-SPEC §5.6 ①，JSON 美化 2 空格）。
    #: 与 `args_summary` 的分工：**摘要用于折叠行**（短、扫一眼），
    #: **完整参数用于展开**（长、要看细节）。此前这个字段根本不存在，
    #: 于是 `edit` 的 `old_string`/`new_string` 在界面上永远看不到。
    args_text: str = ""
    state: ToolCardState = "ok"
    duration_ms: int | None = None
    error_kind: str | None = None
    change_stat: ChangeStat | None = None
    payload: str = ""

    # diff 字段
    path: str = ""
    hunks: list[DiffHunk] = field(default_factory=list)

    # 手工指定是否展开（None = 用全局开关）
    expanded: bool | None = None
    card: Any | None = None


_MODEL_BLOCK_KINDS = frozenset({"assistant", "reasoning", "tool", "diff"})

#: Pi 风格盲文点动子序列（10 帧，每帧 1 格宽，顺时针旋转）
SPINNER_FRAMES: tuple[str, ...] = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")


@dataclass
class ActiveStatus:
    """运行中的短暂状态（不存入持久 blocks，避免污染历史与破坏前缀缓存）。"""

    kind: str  # "thinking", "reasoning", "generating", "tool"
    label: str = ""
    started_at: float = field(default_factory=time.time)
    tool_name: str = ""


def format_timer(seconds: float) -> str:
    """实时秒数格式化：小于 60 秒保留一位小数，超过 60 秒按分秒。"""
    seconds = max(0.0, float(seconds))
    if seconds < 60.0:
        return f"{seconds:.1f}s"
    minutes, rest = divmod(int(seconds), 60)
    return f"{minutes}m{rest:02d}s"


def render_active_spinner(
    status: ActiveStatus,
    palette: Any,
    *,
    has_model_header: bool = True,
    now: float | None = None,
    show_track: bool = True,
) -> Text:
    """渲染翡翠绿双轨底部的动子与状态行（纯函数，无副作用）。"""
    if now is None:
        now = time.time()
    success_color = str(getattr(palette, "success", "") or "green")
    text_muted = str(getattr(palette, "text_muted", "") or "dim")

    frame_idx = int(now * 10) % len(SPINNER_FRAMES)
    frame_char = SPINNER_FRAMES[frame_idx]
    elapsed = max(0.0, now - status.started_at)
    timer_str = f"({format_timer(elapsed)})"

    label = status.label
    if not label:
        if status.kind == "reasoning":
            label = "思考中"
        elif status.kind == "generating":
            label = "正在生成"
        elif status.kind == "tool":
            label = f"执行工具 {status.tool_name}" if status.tool_name else "执行工具中"
        else:
            label = "正在思考"

    out = Text()
    if not has_model_header:
        out.append_text(Text("✦ Logox\n", style=f"bold {success_color}"))

    # ★ D176：动子行的轨道也受开关控制 —— 但本函数是**纯函数**，
    #   所以由调用方把开关传进来（纯函数不读外部状态）。
    prefix = Text("▎ ", style=success_color) if show_track else Text("")
    out.append_text(prefix)
    out.append(frame_char + " ", style=f"bold {success_color}")
    out.append(label + " ", style=text_muted)
    out.append(timer_str, style=text_muted)
    out.append("\n")
    return out


def _prefix_lines(content: Text, prefix: Text) -> Text:
    """给 Text 的每一行前缀附加 prefix（保留各行自身样式）。"""
    out = Text()
    lines = content.split("\n")
    for index, line in enumerate(lines):
        if index > 0:
            out.append("\n")
        out.append_text(prefix)
        out.append_text(line)
    return out


def render_blocks(
    blocks: list[Block],
    context: CardContext,
    *,
    expand_tools: bool = False,
    expand_reasoning: bool = False,
    #: ★ D176：是否绘制双轨标记（`▌` / `▎`）。纯函数，所以由调用方传入（不读外部状态）
    show_track: bool = True,
    prev_block: Block | None = None,
    active_status: ActiveStatus | None = None,
    now: float | None = None,
    block_ranges: list[tuple[int, int, Block]] | None = None,
    markdown_cache: MarkdownRenderCache | None = None,
    assistant_rows_out: list[Text] | None = None,
) -> Text:
    """把块序列渲染成 Rich ``Text``（纯函数，可完全单测）。

    ★ 两个展开开关是**正交**的（D125）：``expand_tools`` 管工具卡与 diff（``Ctrl+O``），
    ``expand_reasoning`` 管普通思考与入梦卡片（``Ctrl+T``）。**拆开的原因**是这两类内容的
    "想看程度"完全不同 —— 用户经常只想看"到底执行了什么"，而思考链是冗长的内心独白；
    一个开关管两件事时，用户会被迫同时收下不想要的那一半。
    """
    if assistant_rows_out is not None and len(blocks) == 1 and blocks[0].kind == "assistant":
        rows = _assistant_rows(blocks[0], prev_block, context, show_track, markdown_cache)
        assistant_rows_out.extend(rows)
        if block_ranges is not None:
            block_ranges.append((0, len(rows), blocks[0]))
        return Text()
    palette = context.palette
    width = max(20, context.width)
    out = Text()
    success_color = str(getattr(palette, "success", "") or "green")
    prefix = Text("▎ ", style=success_color) if show_track else Text("")
    sub_context = CardContext(
        palette=palette,
        glyphs=context.glyphs,
        width=max(18, width - 2),
        diff_context_lines=context.diff_context_lines,
    )

    for index, block in enumerate(blocks):
        prev = blocks[index - 1] if index > 0 else prev_block
        b_start = out.plain.count("\n")
        if block.kind == "anamnesis" and block.card is not None:
            card = block.card.render(expanded=block.expanded if block.expanded is not None else expand_reasoning,
                                     width=sub_context.width, color=success_color)
            out.append_text(_prefix_lines(card, prefix))
            out.append("\n")
        elif block.kind in _MODEL_BLOCK_KINDS:
            # 若前一个块不是模型块，说明本块是一个新模型回合的起点，输出统一角色头部 ✦ Logox
            if prev is None or prev.kind not in _MODEL_BLOCK_KINDS:
                out.append_text(Text("✦ Logox\n", style=f"bold {success_color}"))

            if block.kind == "assistant":
                # 若前序也是模型块（如思考或工具卡片），在回答正文前保留一行空行呼吸分段
                if prev is not None and prev.kind in _MODEL_BLOCK_KINDS:
                    out.append("\n")
                for line in render_markdown(block.text, sub_context.width, sub_context, cache=markdown_cache):
                    out.append_text(prefix)
                    out.append_text(line)
                    out.append("\n")
                out.append("\n")
            elif block.kind == "reasoning":
                card = render_reasoning(
                    text=block.text,
                    context=sub_context,
                    duration_ms=block.duration_ms,
                    generating=block.state == "running",
                    expanded=block.expanded if block.expanded is not None else expand_reasoning,
                    indent=0,
                )
                out.append_text(_prefix_lines(card, prefix))
                out.append("\n")
            elif block.kind == "tool":
                # 若前序是思考块，在工具卡片前留一行空行呼吸分段
                if prev is not None and prev.kind == "reasoning":
                    out.append("\n")
                card = render_tool_card(
                    state=block.state,
                    name=block.name,
                    args_summary=block.args_summary,
                    context=sub_context,
                    duration_ms=block.duration_ms,
                    error_kind=block.error_kind,
                    change_stat=block.change_stat,
                    # ★ D139：展开态第一项「完整参数」（UI-SPEC §5.6 ①）——
                    #   此前这里**没有传** `args_text`，所以即使块上有内容也渲染不出来。
                    args_text=block.args_text,
                    payload=block.payload,
                    expanded=block.expanded if block.expanded is not None else expand_tools,
                    indent=0,
                )
                out.append_text(_prefix_lines(card, prefix))
                out.append("\n")
            elif block.kind == "diff":
                # D125 / Q1=A：diff 与工具卡**同一个键**（`Ctrl+O`）——
                # "改了哪些文件"和"执行了什么命令"是同一类动作产物，
                # 用户的意图总是"我要核对这一步干了什么"。
                #
                # ★ D132（用户裁定"完全折叠"）：**折叠态整块不出现**。
                #   原先折叠时还会输出一行"路径 + 徽标 + Ctrl+O 展开" —— 用户的原话是
                #   "虽然没完全展开，但 diff 和工具调用还是有一块在"。既然变更统计已经由
                #   工具卡那一行承担，这一行就只剩下噪音了。
                #   ⚠️ 必须用 `continue` 而不是"渲染成空字符串"：后者仍会被
                #   `_prefix_lines` 加一个轨道前缀，结果多出一行只有 `▎` 的空行。
                if not (block.expanded if block.expanded is not None else expand_tools):
                    continue
                card = render_diff(
                    path=block.path,
                    stat=block.change_stat,
                    hunks=block.hunks,
                    context=sub_context,
                    expanded=True,
                    indent=0,
                )
                out.append_text(_prefix_lines(card, prefix))
                out.append("\n")
        elif block.kind == "user":
            user_fg = str(getattr(palette, "user_message_fg", "") or getattr(palette, "text_primary", ""))
            accent = str(getattr(palette, "accent", "") or "cyan")
            user_lines = [
                Text(line, style=f"bold {user_fg}")
                for line in fmt.wrap_cells(block.text, width - 2, reflow=False).split("\n")
            ]
            if show_track:
                out.append_text(
                    quote_block(user_lines, marker="▌", marker_style=f"bold {accent}", indent=0)
                )
            else:
                # ★ D176：关掉轨道时**整块前缀都不画**（不留空格），复制出来就是纯文本
                out.append_text(Text("\n").join(user_lines))
            out.append("\n")
        elif block.kind in ("divider", "notice"):
            out.append(fmt.wrap_cells(block.text, width), style=getattr(palette, block.token))
            out.append("\n")
        else:  # raw
            out.append(fmt.clip(block.text, width), style=getattr(palette, block.token))
            out.append("\n")

        b_end = out.plain.count("\n")
        if block_ranges is not None and b_end > b_start:
            block_ranges.append((b_start, b_end, block))

    if active_status is not None:
        last_block = blocks[-1] if blocks else prev_block
        has_model_header = bool(last_block is not None and last_block.kind in _MODEL_BLOCK_KINDS)

        # 若上一个块是 assistant，其末尾留了呼吸空行（\n\n）；
        # 去掉多余换行让翡翠绿双轨保持连续紧凑
        if out.plain.endswith("\n\n"):
            out = out[:-1]
        elif out.plain and not out.plain.endswith("\n"):
            out.append("\n")

        spinner_text = render_active_spinner(
            active_status,
            palette,
            has_model_header=has_model_header,
            now=now,
            show_track=show_track,
        )
        out.append_text(spinner_text)

    return out


def _assistant_rows(
    block: Block, prev: Block | None, context: CardContext, show_track: bool,
    cache: MarkdownRenderCache | None,
) -> list[Text]:
    """直接交付正文行，避开全文拼接与全文 span 分割；视觉与 render_blocks 一致。"""
    color = str(getattr(context.palette, "success", "") or "green")
    width = max(18, max(20, context.width) - 2)
    body = render_markdown(block.text, width, context, cache=cache)
    previous = cache.decorated_lines if cache is not None and cache.decoration == (show_track, color) else []
    decorated: list[tuple[Text, Text]] = []
    rows: list[Text] = []
    if prev is None or prev.kind not in _MODEL_BLOCK_KINDS:
        rows.append(Text("✦ Logox", style=f"bold {color}"))
    else:
        rows.append(Text())
    for index, line in enumerate(body):
        if index < len(previous) and previous[index][0] == line:
            row = previous[index][1]
        else:
            row = Text()
            # 原实现的前缀只给轨道着色；不能把颜色继承到整个正文。
            if show_track:
                row.append("▎ ", style=color)
            row.append_text(line)
        decorated.append((line, row))
        rows.append(row)
    rows.append(Text())
    if cache is not None:
        cache.decorated_lines = decorated
        cache.decoration = (show_track, color)
    return rows


@dataclass
class RenderResult:
    """渲染结果与本帧工作量（D56 / D199）。

    UI 通过 segments 取得每个块的不可变 Text 或行，复用未变结果而不先合并
    完整历史。兼容调用保留 head_text / prefix_text / tail_text；text 属性按需
    拼接，分段入口优先拼接 segments。prefix_blocks 表示复用块数，
    tail_rendered_blocks 表示本帧重排块数，不要求两者在空间上形成连续前后缀。
    """

    prefix_blocks: int = 0
    tail_rendered_blocks: int = 0
    tail_chars: int = 0
    prefix_hit: bool = False
    #: 本帧是否有块需要重新排版。
    cache_rebuilt: bool = False
    #: 位于前缀之前的内容（折叠提示）；非空时前缀**不是**整段文本的开头
    head_text: Text = field(default_factory=Text)
    #: 命中的前缀文本（**就是缓存里那个对象**，身份稳定 ⇒ 上层可以据此复用行）
    prefix_text: Text | None = None
    #: 兼容入口的动态状态尾部。
    tail_text: Text = field(default_factory=Text)
    #: 渲染行与 Block 的区间映射 [(start_line, end_line, block)]
    block_ranges: list[tuple[int, int, Block]] = field(default_factory=list)
    segments: list[tuple[Block | None, Text | list[Text]]] | None = None

    @property
    def text(self) -> Text:
        """整段文本（头部 + 前缀 + 尾部）。**按需拼接**，见类 docstring。"""
        out = Text()
        if self.segments is not None:
            for _block, piece in self.segments:
                if isinstance(piece, list):
                    for row in piece:
                        out.append_text(row)
                        out.append("\n")
                else:
                    out.append_text(piece)
            return out
        out.append_text(self.head_text)
        if self.prefix_text is not None:
            out.append_text(self.prefix_text)
        out.append_text(self.tail_text)
        return out

    @property
    def reusable_prefix(self) -> Text | None:
        """可以直接复用"已切好的行"的前缀；头部非空时返回 ``None``。

        为什么头部会让它失效：增量切分的前提是"行列表 = 前缀的行 + 尾部的行"，
        而头部插在最前面时，``prefix_text`` 就不再对应整段文本的第 0 个字符了。
        """
        if self.head_text.plain:
            return None
        return self.prefix_text


@dataclass
class _BlockRenderEntry:
    block: Block
    key: tuple[Any, ...]
    text: Text | None
    ranges: list[tuple[int, int, Block]]
    newline_count: int
    active_text: Text | None
    rows: list[Text] | None = None
    active_rows: list[Text] | None = None


@dataclass
class TimelineRenderCache:
    """按消息块缓存不可变文本；完整文本仅供兼容读取时按需拼接。"""

    blocks: list[Block] = field(default_factory=list)
    entries: dict[int, _BlockRenderEntry] = field(default_factory=dict)
    markdown: dict[int, MarkdownRenderCache] = field(default_factory=dict)
    _text: Text | None = None
    params: tuple[Any, ...] | None = None
    expand_tools: bool = False
    expand_reasoning: bool = False
    show_track: bool = True
    width: int = 0
    block_ranges: list[tuple[int, int, Block]] = field(default_factory=list)
    newline_count: int = 0
    segments: list[tuple[Block | None, Text | list[Text]]] = field(default_factory=list)
    line_offsets: list[int] = field(default_factory=lambda: [0])
    range_offsets: list[int] = field(default_factory=lambda: [0])

    @property
    def covers(self) -> int:
        return len(self.blocks)

    @property
    def text(self) -> Text | None:
        if self._text is None and self.blocks:
            out = Text()
            for block in self.blocks:
                entry = self.entries[id(block)]
                if entry.text is None:
                    entry.text = Text()
                    for row in entry.rows or []:
                        entry.text.append_text(row)
                        entry.text.append("\n")
                out.append_text(entry.text)
            self._text = out
        return self._text

    @text.setter
    def text(self, value: Text | None) -> None:
        self._text = value

    def matches(
        self, *, expand_tools: bool, expand_reasoning: bool, show_track: bool = True, width: int,
    ) -> bool:
        return (
            self.params is not None
            and self.show_track == show_track
            and self.expand_tools == expand_tools
            and self.expand_reasoning == expand_reasoning
            and self.width == width
        )


def _block_render_key(
    block: Block, prev: Block | None, *, expand_tools: bool, expand_reasoning: bool,
) -> tuple[Any, ...]:
    # 字符串保留引用，未变时比较不会扫描整段正文。统计与 DiffHunk 是冻结值。
    return (
        block.kind, block.text, block.token, block.name, block.args_summary,
        block.args_text, block.state, block.duration_ms, block.error_kind,
        block.change_stat, block.payload, block.path, tuple(block.hunks),
        block.expanded, prev.kind if prev is not None else None,
        getattr(block.card, "revision", 0), block.card,
        expand_tools if block.kind in {"tool", "diff"} and block.expanded is None else None,
        expand_reasoning if block.kind in {"reasoning", "anamnesis"} and block.expanded is None else None,
    )


def render_cached(
    blocks: list[Block],
    cache: TimelineRenderCache,
    *,
    context: CardContext,
    expand_tools: bool = False,
    expand_reasoning: bool = False,
    show_track: bool = True,
    hidden_count: int = 0,
    folded: bool = False,
    active_status: ActiveStatus | None = None,
    now: float | None = None,
    segmented: bool = False,
) -> RenderResult:
    """按内容键复用每个块；新增块不使已有历史重新排版（D199）。

    ``segmented=True`` 给 UI 不可变文本片段，避免先合并整个历史。
    默认保留完整 Text 的兼容入口；缓存只拥有当前块，不累积已删除历史。
    """
    params = (
        context.width, show_track,
        tuple(vars(context.palette).items()), tuple(sorted(context.glyphs.items())),
        context.diff_context_lines, segmented,
    )
    same_params = cache.params == params
    entries = cache.entries if same_params else {}
    segments = cache.segments
    ranges = cache.block_ranges
    line_offsets = cache.line_offsets
    range_offsets = cache.range_offsets
    rebuilding = False
    structure_changed = len(cache.blocks) != len(blocks)
    misses = 0
    rendered_chars = 0
    prev: Block | None = None
    for index, block in enumerate(blocks):
        key = _block_render_key(block, prev, expand_tools=expand_tools, expand_reasoning=expand_reasoning)
        entry = entries.get(id(block))
        changed = entry is None or entry.block is not block or entry.key != key
        same_position = index < len(cache.blocks) and cache.blocks[index] is block
        structure_changed |= not same_position
        if not rebuilding and (changed or not same_position or not same_params):
            # 保留已核验的前缀；变化后的区间才重新计算。旧列表不原地修改。
            segments = segments[:index]
            ranges = ranges[:range_offsets[index]]
            line_offsets = line_offsets[:index + 1]
            range_offsets = range_offsets[:index + 1]
            rebuilding = True
        if changed:
            if block.kind != "assistant":
                cache.markdown.pop(id(block), None)
            local_ranges: list[tuple[int, int, Block]] = []
            assistant_rows = [] if segmented and block.kind == "assistant" else None
            text = render_blocks(
                [block], context, expand_tools=expand_tools,
                expand_reasoning=expand_reasoning, show_track=show_track,
                prev_block=prev, block_ranges=local_ranges,
                markdown_cache=cache.markdown.setdefault(id(block), MarkdownRenderCache()) if block.kind == "assistant" else None,
                assistant_rows_out=assistant_rows,
            )
            entry = _BlockRenderEntry(
                block, key, text if assistant_rows is None else None,
                local_ranges, text.plain.count("\n") if assistant_rows is None else len(assistant_rows),
                text[:-1] if text.plain.endswith("\n") else text,
                assistant_rows, assistant_rows[:-1] if assistant_rows is not None else None,
            )
            entries[id(block)] = entry
            misses += 1
            rendered_chars += len(text.plain) if assistant_rows is None else sum(len(row.plain) + 1 for row in assistant_rows)
        if rebuilding:
            offset = line_offsets[-1]
            segments.append((block, entry.rows if entry.rows is not None else entry.text))
            ranges.extend((s + offset, e + offset, b) for s, e, b in entry.ranges)
            line_offsets.append(offset + entry.newline_count)
            range_offsets.append(len(ranges))
        prev = block

    if len(segments) > len(blocks):
        # 删除/折叠尾部也要释放旧片段，即使留下的块全部命中。
        segments = segments[:len(blocks)]
        ranges = ranges[:range_offsets[len(blocks)]]
        line_offsets = line_offsets[:len(blocks) + 1]
        range_offsets = range_offsets[:len(blocks) + 1]
        rebuilding = True
    if rebuilding or structure_changed or not same_params:
        cache.text = None
    if structure_changed or not same_params:
        retained = {id(b) for b in blocks}
        entries = {key: entry for key, entry in entries.items() if key in retained}
        cache.markdown = {key: value for key, value in cache.markdown.items() if key in retained}
        cache.blocks = list(blocks)
    cache.entries = entries
    cache.segments = segments
    cache.line_offsets = line_offsets
    cache.range_offsets = range_offsets
    cache.params = params
    cache.width = context.width
    cache.expand_tools = expand_tools
    cache.expand_reasoning = expand_reasoning
    cache.show_track = show_track
    cache.newline_count = line_offsets[-1]
    cache.block_ranges = ranges

    head = Text()
    if folded and hidden_count > 0:
        head.append(
            f"—— 已折叠 {hidden_count} 块历史（Ctrl+H 展开）——\n\n",
            style=context.palette.text_faint,
        )
    tail = Text()
    trimmed = False
    if active_status is not None:
        segments = list(segments)
        suffix = ""
        for _block, piece in reversed(segments):
            ending = (
                "".join(row.plain[-2:] + "\n" for row in piece[-2:])[-2:]
                if isinstance(piece, list) else piece.plain[-2:]
            )
            suffix = ending[-(2 - len(suffix)):] + suffix
            if len(suffix) == 2:
                break
        for index in range(len(segments) - 1, -1, -1):
            block, text = segments[index]
            if isinstance(text, list):
                if text:
                    if suffix == "\n\n" and not text[-1].plain:
                        segments[index] = (block, entries[id(block)].active_rows)
                        trimmed = True
                    break
            elif text.plain:
                if suffix == "\n\n":
                    segments[index] = (block, entries[id(block)].active_text)
                    trimmed = True
                break
        tail = render_blocks(
            [], context, prev_block=prev, active_status=active_status,
            show_track=show_track, now=now,
        )
    head_lines = head.plain.count("\n")
    all_ranges = [(s + head_lines, e + head_lines, b) for s, e, b in ranges] if head_lines else ranges
    prefix = None if segmented else cache.text
    if trimmed and prefix is not None:
        prefix = prefix[:-1]
    if segmented:
        if head.plain:
            segments = [(None, head), *segments]
        if tail.plain:
            segments.append((None, tail))
    return RenderResult(
        prefix_blocks=len(blocks) - misses,
        tail_rendered_blocks=misses,
        tail_chars=rendered_chars + len(tail.plain),
        prefix_hit=bool(blocks) and misses < len(blocks),
        cache_rebuilt=bool(misses),
        head_text=head, prefix_text=prefix, tail_text=tail,
        block_ranges=all_ranges, segments=segments if segmented else None,
    )


_RICH_DISPLAY_KINDS = frozenset({"diff"})


def _is_rich_display(display: object | None) -> bool:
    """这个展示提示是否自带渲染器；是则避免重复铺原始文本。"""
    return display is not None and getattr(display, "kind", None) in _RICH_DISPLAY_KINDS


def _coerce_hunk(raw: object) -> DiffHunk:
    """把 ``DiffHunk`` 或等价 dict 归一化成 :class:`DiffHunk`。

    `DiffHunk.lines` 是 ``(kind, text)`` 的**元组序列**，而从 JSON 反序列化回来的是
    列表的列表 —— 这里统一转成元组，保证渲染侧拿到的东西形状一致
    （否则 `for kind, text in ...` 在 dict 路径下仍能工作，但类型不一致会让下游的判断
    变成"看运气"）。
    """
    if isinstance(raw, DiffHunk):
        return raw
    if isinstance(raw, dict):
        header = str(raw.get("header", ""))
        lines: list[tuple[str, str]] = []
        for item in raw.get("lines", []) or []:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                lines.append((str(item[0]), str(item[1])))
        return DiffHunk(header=header, lines=tuple(lines))  # type: ignore[arg-type]
    return DiffHunk(header=str(raw))


class TimelineBuffer:
    """事件 → 显示块的纯状态机。"""

    def __init__(self, *, fold_threshold: int = FOLD_THRESHOLD) -> None:
        self.blocks: list[Block] = []
        self.fold_threshold = fold_threshold
        self.folded = False
        #: 工具+diff / 普通思考+入梦分别跟随 Ctrl+O / Ctrl+T。
        #: 都是**会话内 UI 状态**，不进持久化 —— 所以 `/resume` 载入历史后回到默认折叠。
        self.expand_tools = False
        #: ★ D176：是否绘制**双轨标记**（用户消息 `▌` / 模型输出 `▎`）。
        #: 它纯粹是视觉分区：终端里占了格子的字形**一定会被复制**，无法标记为「装饰」，
        #: 所以给它一个开关 —— 要复制干净文本时按 `Ctrl+B` 关掉（默认开）。
        #: 与 `expand_tools` 同类：**只影响渲染**，不进持久化。
        self.show_track = True
        self.expand_reasoning = False
        self.smoother = StreamSmoother()
        #: 是否有结构性变化（块的增删、既有块被改）需要重绘
        self._dirty = False
        #: 结构性变化是否需要**重算布局**（块数变了才需要；正文变长不需要）
        self._dirty_layout = False
        #: 当前活跃状态（短暂状态，不属于持久 blocks）
        self.active_status: ActiveStatus | None = None

    def clear_active_status(self) -> None:
        """清空活跃动子状态（回合结束/取消/异常时调用）。"""
        self.active_status = None

    # -- 变更登记（D56：被动失效） --------------------------------------- #

    def invalidate(self, *, layout: bool = True) -> None:
        """声明"数据变了"。

        谁改了数据谁负责调用它以请求重画；渲染缓存另外校验实际显示字段。
        ``layout=True`` 表示块的**数量**变了（需要重算布局），
        ``layout=False`` 表示只改了块的内容（只需重绘）。
        """
        self._dirty = True
        if layout:
            self._dirty_layout = True

    def has_pending(self) -> bool:
        """本帧是否有东西要渲染（含"只改了显示状态"的情况与平滑器未释出字符）。"""
        return (
            self._dirty
            or self.smoother.has_pending()
        )

    def take_dirty_layout(self) -> bool:
        """取走"需要重算布局"标记（取走即清零，与 `_dirty` 分开跑）。"""
        value = self._dirty_layout
        self._dirty_layout = False
        return value

    # -- 基础写入 ------------------------------------------------------- #

    def add_anamnesis_reference(self, run_id: str) -> Block:
        from logox.tui.content.anamnesis import AnamesisCard

        existing = next((b for b in self.blocks if b.kind == "anamnesis" and b.card.run_id == run_id), None)
        if existing is not None:
            return existing
        self.flush_delta()
        block = Block(kind="anamnesis", card=AnamesisCard(run_id, phase="restoring"))
        self.blocks.append(block)
        self.invalidate()
        return block

    def add_user(self, text: str) -> Block:
        self.flush_delta()
        self.clear_active_status()
        block = Block(kind="user", text=text)
        self.blocks.append(block)
        self.invalidate()
        return block

    def add_assistant(self, text: str) -> Block:
        block = Block(kind="assistant", text=text)
        self.blocks.append(block)
        self.invalidate()
        return block

    def add_delta(self, text: str) -> None:
        """流式正文增量：喂入平滑器，**不产生块、也不失效缓存**（节流的关键）。"""
        self.smoother.feed_text(text)
        self._dirty = True

    def add_reasoning_delta(self, text: str) -> None:
        """流式推理增量：喂入平滑器（真实模型一次思考有几百个片段）。"""
        self.smoother.feed_reasoning(text)
        self._dirty = True

    def step_delta(self, *, include_reasoning: bool = True) -> Block | None:
        """从流式平滑器按弹性打字机速率释出本帧正文增量并合并成块。"""
        if include_reasoning:
            self.step_reasoning()
        text = self.smoother.step_text()
        if not text:
            return None
        last = self.blocks[-1] if self.blocks else None
        if last is not None and last.kind == "assistant":
            last.text += text
            self.invalidate(layout=False)
            return last
        return self.add_assistant(text)

    def step_reasoning(self) -> Block | None:
        """从流式平滑器按弹性打字机速率释出本帧推理增量并追加到推理块。"""
        added = self.smoother.step_reasoning()
        if not added:
            return None
        block = self._last_of("reasoning")
        if block is None:
            block = self.start_reasoning()
        self.update_reasoning(block.text + added)
        return block

    def flush_delta(self) -> Block | None:
        """把缓冲的增量全部落成块（终态瞬时排空）。返回新产生的**正文**块。

        ⚠️ **必须与紧邻的上一个正文块合并，不能另起一块**（D76）。
        """
        self.flush_reasoning()
        text = self.smoother.flush_all_text()
        if not text:
            return None
        last = self.blocks[-1] if self.blocks else None
        if last is not None and last.kind == "assistant":
            # 同一段正文的后续帧 → 续写。**不能新建块**，否则渲染层会把它当独立段落。
            last.text += text
            self.invalidate(layout=False)
            return last
        return self.add_assistant(text)

    def flush_reasoning(self) -> Block | None:
        """把缓冲的推理增量全部**累加**进当前推理块（终态瞬时排空）。"""
        added = self.smoother.flush_all_reasoning()
        if not added:
            return None
        block = self._last_of("reasoning")
        if block is None:
            block = self.start_reasoning()
        self.update_reasoning(block.text + added)
        return block

    def add_divider(self, text: str) -> Block:
        self.flush_delta()
        block = Block(kind="divider", text=text, token="text_faint")
        self.blocks.append(block)
        self.invalidate()
        return block

    def add_notice(self, text: str, *, token: str = "text_muted") -> Block:
        block = Block(kind="notice", text=text, token=token)
        self.blocks.append(block)
        self.invalidate()
        return block

    def start_reasoning(self, *, duration_ms: int | None = None) -> Block:
        block = Block(kind="reasoning", text="", state="running", duration_ms=duration_ms)
        self.blocks.append(block)
        self.invalidate()
        return block

    def update_reasoning(self, text: str, *, duration_ms: int | None = None) -> None:
        """更新推理块。**不追加**而是整体覆盖（调用方给的是合并后的全文）。"""
        block = self._last_of("reasoning")
        if block is None:
            block = self.start_reasoning()
        block.text = text
        block.state = "ok"
        block.duration_ms = duration_ms if duration_ms is not None else block.duration_ms
        # 改的是**已有块** → 必须失效缓存（E-10/E-24 的触发条件之一）
        self.invalidate(layout=False)

    def add_reasoning(self, text: str, *, duration_ms: int | None = None) -> Block:
        """追加一个已完成的推理块（主要用于测试与历史载入）。"""
        block = Block(kind="reasoning", text=text, state="ok", duration_ms=duration_ms)
        self.blocks.append(block)
        self.invalidate()
        return block

    def start_tool(
        self,
        *,
        call_id: str,
        name: str,
        args_summary: str = "",
        args_text: str = "",
    ) -> Block:
        block = Block(
            kind="tool",
            name=name,
            args_summary=args_summary,
            args_text=args_text,
            state="running",
            text=call_id,
        )
        self.blocks.append(block)
        self.invalidate()
        return block

    def finish_tool(
        self,
        *,
        call_id: str,
        ok: bool,
        duration_ms: int,
        error_kind: str | None = None,
        change_stat: ChangeStat | None = None,
        payload: str = "",
    ) -> None:
        block = self._tool_block(call_id)
        if block is None:
            return
        block.state = "ok" if ok else "error"
        block.duration_ms = duration_ms
        block.error_kind = error_kind
        # ★ F-43 的落点（D136）：把**工具输出正文**存进块，供 Ctrl+O 展开查看。
        if payload:
            block.payload = payload
        block.change_stat = change_stat
        if not ok:
            # D40：失败时**自动展开**（否则用户不知道为何没改成）
            block.expanded = True
        # 改的是**已有块**，且失败时会展开 → 必须失效（T-22/T-23）
        self.invalidate(layout=False)

    def _attach_display_diff(self, event: ev.ToolCallFinished) -> None:
        """把展示提示里的 diff 挂到卡片后面（D139）。

        为什么需要这一步：`fs_edit` 从 M5 起就在 `ToolResult.display` 里附上真正的 hunks，
        界面侧的 `render_diff` 也早就写好了 —— 但**中间那一节从来没接上**：
        `attach_diff()` 全项目只有测试在调用。结果就是 `edit` 卡片展开后只有一行
        "已成功编辑文件 …（+0 -2 行）"，**看不到改了哪几行**（用户报障）：
        "目前 edit 还是无法正常显示"。

        只处理 ``kind == "diff"``：其余类型（text / lines / table / error）目前没有专用渲染器，
        仍然走 `payload` 的纯文本路径 —— **不假装支持**（宁可少一个视图，也不要一个空壳视图）。
        """
        display = getattr(event, "display", None)
        if display is None or getattr(display, "kind", None) != "diff":
            return
        payload = getattr(display, "payload", None)
        if not isinstance(payload, dict):
            return
        hunks = payload.get("hunks")
        if not isinstance(hunks, list) or not hunks:
            return
        self.attach_diff(
            call_id=event.call_id,
            path=str(payload.get("path", "")),
            hunks=[_coerce_hunk(item) for item in hunks],
            # 统计直接用事件上的 `change_stat`（调度器已填）—— 不解析 payload 里的那份副本，
            # 因为**同一件事只有一个来源**才不会出现"徽标和 diff 对不上"。
            stat=event.change_stat,
        )


    def attach_diff(
        self,
        *,
        call_id: str,
        path: str,
        hunks: list[DiffHunk] | list[dict[str, object]],
        stat: ChangeStat | None = None,
    ) -> None:
        """把 diff 挂到某个工具卡片之后（折叠态整块不出现，D132）。

        ``hunks`` 接受 ``DiffHunk`` **或等价的 dict**：事件里的展示提示是纯数据
        （``DisplayHint.payload`` 由工具层序列化而来），而回放路径（`store/replay.py`）
        更不可能 import 界面类型 —— 由这里统一**归一化**，是唯一合理的收口点。
        """
        normalized = [_coerce_hunk(h) for h in hunks]
        block = self._tool_block(call_id)
        if stat is not None and block is not None and block.change_stat is None:
            block.change_stat = stat
        self.blocks.append(
            Block(
                kind="diff",
                path=path,
                hunks=normalized,
                change_stat=stat,
                # ★ D125-b：`expanded=None` = **跟随全局开关**（Ctrl+O）。
                #   原先写死 `False` 会让 diff **永远折叠、且没有任何提示**
                #   （`render_diff` 折叠时直接 return，连 hint 都不留）——
                #   于是 Q1 那条"diff 归 Ctrl+O"的裁定在实现上完全落空。
                #   默认值仍是折叠（`expand_tools` 默认 False），**这不是行为变更**，
                #   只是把一根没接上的线接通。
                expanded=None,
            )
        )
        # 卡片自身的 `change_stat` 也可能被改 → 整体失效
        self.invalidate()

    # -- 折叠 ----------------------------------------------------------- #

    @property
    def foldable(self) -> bool:
        return len(self.blocks) > self.fold_threshold

    def toggle_fold(self) -> None:
        self.folded = self.foldable and not self.folded
        self.invalidate(layout=False)  # 可见块集合变了，但块数没变

    @property
    def visible_blocks(self) -> list[Block]:
        if not self.folded or not self.foldable:
            return list(self.blocks)
        return self.blocks[-self.fold_threshold :]

    @property
    def hidden_count(self) -> int:
        return len(self.blocks) - len(self.visible_blocks)

    def toggle_expand_tools(self) -> None:
        """``Ctrl+O``：切换**工具卡与 diff** 的展开（D125）。

        ★ D132（修 F-40）：**存在"自动展开"的例外时，这一下负责把它收起来**。

        背景：失败的工具卡会自动展开一次（D40），而那种卡片的 `block.expanded=True`
        会**压过全局开关** → 按 `Ctrl+O` **收不起来**（F-40）。

        ⚠️ 这里**不能**写成"清掉例外 + 普通切换"：那样第一下只是把全局开关翻成
        `True`，用户看到的是"失败卡没动、其它卡全展开了" —— 体感就是**没修好**。
        所以规则定为：

        * **有例外** → 这一下只做“清掉例外 + 统一成折叠”（用户按它就是为了收起来）；
        * **没例外** → 普通的展开/收起切换。
        """
        if self._clear_block_expand_overrides(("tool", "diff")):
            self.expand_tools = False
        else:
            self.expand_tools = not self.expand_tools
        self.invalidate(layout=False)  # 每张卡片的渲染结果都变了（T-25）

    def toggle_track(self) -> None:
        """``Ctrl+B``：切换**双轨标记**的显示（D176）。

        为什么需要它：终端里「占了屏幕格子的字形一定会被复制」—— 无法把 `▌` / `▎`
        标记成「装饰、不参与选择」。所以解法是给显示一个开关：要复制时关掉，
        轨道**整个消失**（不是换成空格 —— 那样复制出来仍会多两个空格）。

        视觉上：关掉后两种消息的区别只剩「正文颜色 + 有没有 `✦ Logox` 头部」，
        分区变弱但**复制干净**；再按一下恢复。
        """
        self.show_track = not self.show_track
        # 前缀只改「内容」不改「行数」（换行宽度仍按原样预留）⇒ 不必重算布局
        self.invalidate(layout=False)

    def toggle_expand_reasoning(self) -> None:
        """``Ctrl+T``：切换**普通思考与入梦卡片**的展开（D125）。

        普通思考与入梦共用此开关，与 :meth:`toggle_expand_tools` 独立。
        用户经常只想核对"执行了什么"，而思考链是冗长的内心独白。
        （例外处理与 `Ctrl+O` 一致）
        """
        if self._clear_block_expand_overrides(("reasoning", "anamnesis")):
            self.expand_reasoning = False
        else:
            self.expand_reasoning = not self.expand_reasoning
        self.invalidate(layout=False)

    def _clear_block_expand_overrides(self, kinds: tuple[str, ...]) -> bool:
        """把指定的块打回"跟随全局开关"（`expanded=None`）；返回**是否真的清了东西**。

        块级覆盖来自失败自动展开或单卡片点击；用户一按全局键，
        意图就是让全局开关说了算（F-40）。注意**只清已有块**：
        之后新失败的卡片仍会自动展开一次（那是 D40 要求的）。

        返回值用于区分"这一下是清例外"还是"这一下是普通切换" ——
        两者的用户预期不同（见 :meth:`toggle_expand_tools`）。
        """
        cleared = False
        for block in self.blocks:
            if block.kind in kinds and block.expanded is not None:
                block.expanded = None
                cleared = True
        return cleared

    def set_expand_tools(self, value: bool) -> None:
        if self.expand_tools != value:
            self.expand_tools = value
            self._clear_block_expand_overrides(("tool", "diff"))
            self.invalidate(layout=False)

    def set_expand_reasoning(self, value: bool) -> None:
        if self.expand_reasoning != value:
            self.expand_reasoning = value
            self._clear_block_expand_overrides(("reasoning", "anamnesis"))
            self.invalidate(layout=False)

    def ingest(self, event: ev.AnyEvent, *, tool_args_summary: str = "") -> bool:
        """摄入一个事件。返回 ``True`` 表示**需要一次重绘**。

        注意返回值的口径（M4 校正）：**流式增量返回 ``False``**——它们只进缓冲，
        由帧级定时器按 ``ui.stream_fps`` 合并重绘。结构性事件才返回 ``True``。
        """
        if isinstance(event, ev.UserPromptSubmit):
            self.add_user(event.text)
            self.clear_active_status()
            return True
        if isinstance(event, ev.ModelDelta):
            if event.kind == "text":
                self.add_delta(event.delta)
                if self.active_status is None or self.active_status.kind != "generating":
                    self.active_status = ActiveStatus(kind="generating", label="正在生成", started_at=time.time())
            elif event.kind == "reasoning":
                # 与正文同一条缓冲路径（M4 修正）：真实模型一次思考有几百个片段，
                # 逐个覆盖推理块文本是 O(n²)，且会每片段触发一次重绘。
                self.add_reasoning_delta(event.delta)
                if self.active_status is None or self.active_status.kind != "reasoning":
                    self.active_status = ActiveStatus(kind="reasoning", label="思考中", started_at=time.time())
            return False  # 流式增量交给定时器
        if isinstance(event, ev.ModelRequestStarted):
            self.flush_delta()
            self.active_status = ActiveStatus(kind="thinking", label="正在思考", started_at=time.time())
            return True
        if isinstance(event, ev.ModelRequestFinished):
            return self.flush_delta() is not None
        if isinstance(event, ev.ToolCallRequested):
            self.start_tool(
                call_id=event.call_id,
                name=event.name,
                # 摘要优先用调用方给的（M5 起来自 ToolSpec.summary_template），否则从入参推导
                args_summary=tool_args_summary or fmt.summarize_args(event.args),
                # ★ D139：展开态要看的「完整参数」（UI-SPEC §5.6 ①）。
                #   折叠行装不下 `edit` 的 old_string/new_string —— 它们只在这里出现。
                args_text=fmt.format_args(event.args),
            )
            self.active_status = ActiveStatus(
                kind="tool",
                label=f"执行工具 {event.name}",
                started_at=time.time(),
                tool_name=event.name,
            )
            return True
        if isinstance(event, ev.ToolCallFinished):
            self.finish_tool(
                call_id=event.call_id,
                ok=event.ok,
                duration_ms=event.duration_ms,
                error_kind=event.error_kind,
                change_stat=event.change_stat,
                # ★ D136：`ToolCallFinished.content` 一直是有的（调度器填充、持久化也在用），
                #   只是**从来没人把它传到这里** —— 于是 Ctrl+O 展开后一片空白（F-43）。
                # ★ D139：若工具给了**展示提示**（`DisplayHint`），这里就不再重复铺一遍原文：
                #   规格 §5.6 说"结果主体"由 `DisplayHint` 的渲染器负责（`diff` 就是 diff 视图）。
                payload="" if _is_rich_display(event.display) else (event.content or ""),
            )
            # ★ D139：`edit` / `write` 这类工具把真正的 diff 放在展示提示里 ——
            #   以前这一节**没有任何人接**（`attach_diff()` 全项目只有测试在调用），
            #   于是"改了哪几行"在界面上完全看不到。这里把它挂成卡片后面的 diff 块。
            self._attach_display_diff(event)
            self.active_status = ActiveStatus(kind="thinking", label="处理结果中", started_at=time.time())
            return True
        if isinstance(event, ev.TurnFinished):
            self.clear_active_status()
            # ★ 用户裁定 Q-D：模型**没按契约**给出摘要时，历史里要留下一点说明。
            #
            # 为什么不是每轮都加：模型自己把摘要写在最后一行时，它就**显示在正文末尾**，
            # 再加一行纯属噪声；只有兜底那两种情况（补写 / 自动生成）才需要告诉用户
            # 「这句摘要是我们替你生成的」——否则用户会以为那是模型的原话。
            source = getattr(event, "summary_source", "") or ""
            if source in ("model_fallback", "deterministic") and event.turn_summary:
                reason = "由模型补写" if source == "model_fallback" else "由本地自动生成"
                # ★ 带上链路诊断（D135-4）：用户报"为什么是自动生成的"时，
                #   屏幕上直接写着卡在哪一层，不必再去翻会话文件。
                why = getattr(event, "summary_reason", "") or ""
                tail = f"｜诊断 {why}" if why and source == "deterministic" else ""
                self.add_notice(
                    f"（本轮摘要{reason}：{event.turn_summary}{tail}）",
                    token="text_faint",
                )
            return True
        if isinstance(event, ev.SessionStart):
            if not getattr(event, "resumed", False) and not self.blocks:
                # ★ D137：带上**源码最后改动时间** —— 让"我改完之后的代码跑起来了吗"
                #   变成一眼可见的事实（本项目已三次被"没重启"造成的旧行为误导）。
                from logox.tui.buildinfo import code_stamp

                self.add_divider(
                    f"—— 会话开始 · {event.model} · shell={event.shell_backend} · "
                    f"记忆文件 {len(event.memory_sources)} 份 · 代码 {code_stamp()} ——"
                )
            return True
        if isinstance(event, ev.CompactionStarted):
            self.add_divider("—— 正在压缩上下文 ——")
            return True
        if isinstance(event, ev.CompactionFinished):
            self.add_divider(
                f"—— 上下文已压缩 → {event.tokens_after} tok，"
                f"{event.message_count_after} 条消息{'（已降级为进一步裁剪）' if event.degraded else ''} ——"
            )
            return True
        if isinstance(event, ev.RewindPerformed):
            self.add_divider(f"—— 已回滚到第 {event.to_turn} 个检查点 ——")
            return True
        if isinstance(event, ev.RetryScheduled):
            self.add_notice(
                f"第 {event.attempt} 次重试（{event.delay_s:.1f}s 后）：{event.reason}", token="warning"
            )
            return True
        if isinstance(event, ev.ErrorOccurred):
            self.clear_active_status()
            self.add_notice(f"{event.category}: {event.message}", token="danger")
            return True
        if isinstance(event, ev.SubscriberQuarantined):
            self.add_notice(
                f"订阅者 {event.subscriber} 连续失败 {event.failures} 次，已停用（详见 /debug）",
                token="danger",
            )
            return True
        if isinstance(event, ev.McpServerStateChanged) and event.state == "degraded":
            self.add_notice(f"MCP server {event.server} 不可用：{event.error or '未知原因'}", token="warning")
            return True
        return False

    # -- 内部 ----------------------------------------------------------- #

    def _last_of(self, kind: BlockKind) -> Block | None:
        for block in reversed(self.blocks):
            if block.kind == kind:
                return block
            if block.kind in ("user", "divider"):
                break
        return None

    def _tool_block(self, call_id: str) -> Block | None:
        for block in reversed(self.blocks):
            if block.kind == "tool" and block.text == call_id:
                return block
        return None

