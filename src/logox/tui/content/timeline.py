"""会话时间线（UI-SPEC §5.2 / §5.3 / §5.6 / §5.12 / §6）。

**纯状态机 + 纯函数，不含任何界面框架。**
:class:`TimelineBuffer` 是"事件 → 显示块"的状态机，
:func:`render_blocks` 与 :func:`render_cached` 是"块 → 文本"的纯函数。
因此时间线的绝大多数行为都能用普通 unittest 覆盖。

两个关键设计
------------
1. **卡片以文本块渲染**：卡片视觉由 `cards.py` 的纯渲染函数产出，时间线只是把它们
   拼起来。于是"同一套视觉"既用于主时间线，也能用在别处（例如浮层里的预览）。
2. **前缀缓存（D56）**：每次刷新只重渲染**活跃尾部**，因此每帧的工作量与会话长度无关。
   500 块的会话实测：单次全量渲染 9 ms，而缓存命中后只渲染 1 块。

前缀缓存的正确性靠**一条不变量**
--------------------------------

> **缓存里的块，必须已经写完、不会再变。**

因此前缀只推进到 ``blocks[:-1]``（**最后一块永远重渲染**），于是"正在流式的正文"
与"运行中的工具卡片"天然落在尾部。这比"猜哪些块会变"稳得多——猜错的症状是
**用户看到过时的信息却没有任何提示**，属于最恶劣的一类缺陷。

缓存失效是**被动**的：谁改了数据谁负责声明。为此 :class:`TimelineBuffer` 提供
``invalidate()``，并在每个会改动既有块的方法里调用它。

**为什么不做"每帧比对内容"**：比对需要把每个块序列化一遍再比，那本身就是
O(全部内容)，省下来的比花掉的还多。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Literal

from rich.text import Text

from logox.kernel import events as ev
from logox.kernel.events import ChangeStat
from logox.tui import format as fmt
from logox.difftext import DiffHunk
from logox.tui.content.cards import (
    CardContext,
    ToolCardState,
    quote_block,
    render_diff,
    render_reasoning,
    render_tool_card,
)
from logox.tui.content.markdown import render_markdown

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

BlockKind = Literal["user", "assistant", "divider", "notice", "tool", "reasoning", "diff", "raw"]

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
) -> Text:
    """把块序列渲染成 Rich ``Text``（纯函数，可完全单测）。

    ★ 两个展开开关是**正交**的（D125）：``expand_tools`` 管工具卡与 diff（``Ctrl+O``），
    ``expand_reasoning`` 管思考链（``Ctrl+T``）。**拆开的原因**是这两类内容的
    "想看程度"完全不同 —— 用户经常只想看"到底执行了什么"，而思考链是冗长的内心独白；
    一个开关管两件事时，用户会被迫同时收下不想要的那一半。
    """
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
        if block.kind in _MODEL_BLOCK_KINDS:
            # 若前一个块不是模型块，说明本块是一个新模型回合的起点，输出统一角色头部 ✦ Logox
            if prev is None or prev.kind not in _MODEL_BLOCK_KINDS:
                out.append_text(Text("✦ Logox\n", style=f"bold {success_color}"))

            if block.kind == "assistant":
                # 若前序也是模型块（如思考或工具卡片），在回答正文前保留一行空行呼吸分段
                if prev is not None and prev.kind in _MODEL_BLOCK_KINDS:
                    out.append("\n")
                for line in render_markdown(block.text, sub_context.width, sub_context):
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


@dataclass
class RenderResult:
    """一次渲染的产物 + **工作量**（D56 的度量口径）。

    这里**没有预先拼好的整段文本**（D126），而是三段：

    ``head_text``（折叠提示这类插在最前面的东西，可能为空）→
    ``prefix_text``（命中缓存的那一段）→ ``tail_text``（本帧真正重渲染的那一段）。

    两个理由，都是实测出来的：

    1. **拼接是 O(整个会话)**：``Text.append_text`` 要复制 plain 与全部 spans，
       80 条消息的会话里每帧约 6ms —— 而它唯一的用武之地是"增量切分不成立时
       退回整体切分"那条罕见路径。所以它变成 :attr:`text` 这个**按需拼接**的属性。
    2. **上层需要知道"前缀是哪一段"**：它要只切尾部、复用前缀的行。
       如果只给它一段拼好的文本，那一步就必然又是 O(整个会话)
       —— 症状是"会话越长，敲一个字越卡"。

    不变量：``text.plain == head_text.plain + (prefix_text or "").plain + tail_text.plain``。
    """

    prefix_blocks: int = 0
    tail_rendered_blocks: int = 0
    tail_chars: int = 0
    prefix_hit: bool = False
    #: 本帧是否**重建**了缓存（缓存没命中 / 落后 / 渲染参数变了）
    cache_rebuilt: bool = False
    #: 位于前缀之前的内容（折叠提示）；非空时前缀**不是**整段文本的开头
    head_text: Text = field(default_factory=Text)
    #: 命中的前缀文本（**就是缓存里那个对象**，身份稳定 ⇒ 上层可以据此复用行）
    prefix_text: Text | None = None
    #: 本次真正重新渲染的那一段（含最后一块与活跃状态行）
    tail_text: Text = field(default_factory=Text)
    #: 渲染行与 Block 的区间映射 [(start_line, end_line, block)]
    block_ranges: list[tuple[int, int, Block]] = field(default_factory=list)

    @property
    def text(self) -> Text:
        """整段文本（头部 + 前缀 + 尾部）。**按需拼接**，见类 docstring。"""
        out = Text()
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
class TimelineRenderCache:
    """前缀缓存：**覆盖了多少块**与**那段文本**必须成对出现，因此放在同一个对象里。

    ⚠️ 这个类是一次**真实缺陷**的直接产物：第一版把 ``cached_blocks`` 与
    ``cached_text`` 当成两个独立字段，于是出现"块数说 30、文本只有 29"的错配，
    屏幕上就少显示了一条用户消息——**没有任何报错**，只是内容悄悄不对。
    把两者绑成一个不可分割的值之后，这种错配从"可能发生"变成"无法表达"。

    ``expand_tools`` / ``expand_reasoning`` 与 ``width`` 也在这里：它们一变，
    所有块的渲染结果都变，缓存必须整体作废。

    ⚠️ **拆成两个开关时最容易漏的就是这里**（D125-d）。忘了加第二维会怎样：
    按 ``Ctrl+T`` 后 ``matches`` 认为"渲染参数没变" → **直接复用旧画面** →
    症状是"**按键毫无反应，且没有任何报错**"。这比崩溃难查得多，所以
    ``tests/tui/test_timeline_cache.py`` 与 ``test_render_inline_app.py``
    各有一条用例专门按这个键、断言画面真的变了。
    """

    blocks: list[Block] = field(default_factory=list)
    text: Text | None = None
    expand_tools: bool = False
    expand_reasoning: bool = False
    #: ★ D176：双轨标记开关。**必须进缓存键** —— 漏了就会"按 Ctrl+B 毫无反应且不报错"（D125-d 同型）
    show_track: bool = True
    width: int = 0
    block_ranges: list[tuple[int, int, Block]] = field(default_factory=list)

    @property
    def covers(self) -> int:
        return len(self.blocks)

    def matches(
        self, *, expand_tools: bool, expand_reasoning: bool, show_track: bool = True, width: int
    ) -> bool:
        """渲染参数是否与本缓存一致（块**内容**是否变过由 `blocks` 的身份比对判断）。"""
        return (
            self.show_track == show_track
            and self.text is not None
            and self.expand_tools == expand_tools
            and self.expand_reasoning == expand_reasoning
            and self.width == width
        )


def render_cached(
    blocks: list[Block],
    cache: TimelineRenderCache,
    *,
    context: CardContext,
    expand_tools: bool = False,
    expand_reasoning: bool = False,
    #: ★ D176：是否绘制双轨标记；由 `TimelineComponent.render` 传入（纯函数不读外部状态）
    show_track: bool = True,
    hidden_count: int = 0,
    folded: bool = False,
    active_status: ActiveStatus | None = None,
    now: float | None = None,
) -> RenderResult:
    """渲染块序列并复用**前缀缓存**（D56 的核心）；返回结果与**更新后的缓存**。

    **不变量**：缓存里的块必须已经写完、不会再变。因此缓存只覆盖 ``blocks[:-1]``
    ——最后一块永远重渲染，于是"正在流式的正文"与"运行中的工具卡片"天然落在尾部，
    无需猜测谁会变。

    缓存有效性同时检查三件事，缺一不可：

    1. **参数**：宽度与展开状态与缓存时一致（`cache.matches`）；
    2. **身份**：缓存覆盖的每个块仍**是**当前列表里的同一个对象（块只被追加、
       不被替换，所以 `is` 比对既零成本又精确）；
    3. **覆盖范围**：缓存块数必须与当前可缓存块数**相等**——少了就说明缓存落后了
       （这一步正是上面那个真实缺陷的成因），多了就说明块被清空了。

    任何一条不满足都退回全量渲染。**宁可慢一帧，也不能显示过时的内容。**

    ⚠️ **缓存的重建发生在本函数内部**（D126 起），而不是留给调用方。
    两个好处，都是实测出来的：

    1. **不再重复渲染一次前缀**：以前调用方在缓存失效那一帧要自己再调一次
       ``render_blocks(blocks[:-1])``，而本函数刚刚才渲染过 ``blocks``；
    2. **同一帧里就能拿到稳定的前缀对象**：上层要用它去复用"已切好的行"，
       拿不到就得等下一帧，而那一帧又要多切一次整段文本（= 用户按下第一个键时的卡顿）。
    """
    reusable = max(0, len(blocks) - 1)  # 最后一块永不缓存
    reuse = cache.covers if cache.covers <= reusable else 0

    if reuse and not cache.matches(
            expand_tools=expand_tools,
            expand_reasoning=expand_reasoning,
            show_track=show_track,
            width=context.width
    ,
        ):
        reuse = 0
    if reuse and any(cache.blocks[index] is not blocks[index] for index in range(reuse)):
        reuse = 0
    if reuse and reuse != reusable:
        # 缓存落后（块数对不上）→ 必须重渲染，否则新块既不在缓存里、也不会被渲染出来
        reuse = 0

    rebuilt = reuse != reusable
    cache_ranges: list[tuple[int, int, Block]] = []
    if rebuilt:
        # ⚠️ ``blocks`` 与 ``text`` 必须**同生共死**：分开更新过一次，
        # 症状是屏幕上少一条消息且毫无报错（见 `TimelineRenderCache`）。
        cache.blocks = blocks[:reusable]
        cache.text = (
            render_blocks(
                cache.blocks,
                context,
                expand_tools=expand_tools,
                expand_reasoning=expand_reasoning,
                show_track=show_track,
                block_ranges=cache_ranges,
            )
            if cache.blocks
            else None
        )
        cache.block_ranges = cache_ranges
        cache.expand_tools = expand_tools
        cache.expand_reasoning = expand_reasoning
        # ★ D176：重建后也要**记下**双轨开关 —— 漏了这一步的后果是「切回去时被当成没变而直接复用缓存」
        #   （按 Ctrl+B 第二次毫无反应）。
        cache.show_track = show_track
        cache.width = context.width
        reuse = reusable

    head = Text()
    if folded and hidden_count > 0:
        # 折叠提示行**不参与缓存**：它不对应任何 Block，长度随 fold 状态变化
        head.append(
            f"—— 已折叠 {hidden_count} 块历史（Ctrl+H 展开）——\n\n",
            style=context.palette.text_faint,
        )

    tail_blocks = blocks[reuse:]
    prev = blocks[reuse - 1] if reuse > 0 else None
    tail_ranges: list[tuple[int, int, Block]] = []
    tail_text = render_blocks(
        tail_blocks,
        context,
        expand_tools=expand_tools,
        expand_reasoning=expand_reasoning,
        # ★ D176：尾部也必须拿到开关（漏了它 ⇒ 助手消息的 `▎` 永远不变）
        show_track=show_track,
        prev_block=prev,
        active_status=active_status,
        now=now,
        block_ranges=tail_ranges,
    )

    head_lines = head.plain.count("\n")
    all_ranges: list[tuple[int, int, Block]] = []
    if reuse and cache.block_ranges:
        for s, e, b in cache.block_ranges:
            all_ranges.append((s + head_lines, e + head_lines, b))
    offset = head_lines + (cache.text.plain.count("\n") if reuse and cache.text else 0)
    for s, e, b in tail_ranges:
        all_ranges.append((s + offset, e + offset, b))

    return RenderResult(
        prefix_blocks=reuse,
        tail_rendered_blocks=len(tail_blocks),
        tail_chars=len(tail_text.plain),
        # "命中"的语义是**真的复用了上次缓存的文本**，而不是"缓存覆盖了 n块"：
        # 重建那一帧也覆盖了 ``reusable`` 块，但它是刚算出来的。
        prefix_hit=bool(reuse) and not rebuilt,
        cache_rebuilt=rebuilt,
        head_text=head,
        # 注意：是**缓存里那个原对象**（不是被复制过的一份）——
        # 身份稳定是上层复用"已切好的行"的前提（见 `RenderResult` 的说明）。
        prefix_text=cache.text if reuse else None,
        tail_text=tail_text,
        block_ranges=all_ranges,
    )


# --------------------------------------------------------------------------- #
# D139：展示提示（`ToolDisplay`）的收口
# --------------------------------------------------------------------------- #

#: 有**自己的渲染器**的展示类型 —— 出现这些时，卡片不再重复铺一遍纯文本输出。
#: 依据 UI-SPEC §5.6：「结果主体（调用该工具的 `DisplayHint` 渲染器）」。
_RICH_DISPLAY_KINDS = frozenset({"diff", "lines", "table", "error"})


def _is_rich_display(display: object | None) -> bool:
    """这个展示提示是否"自带渲染器"（是 ⇒ 卡片不再铺原始文本，避免重复）。"""
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
        #: 展开开关**两个正交维度**（D125）：工具卡+diff / 思考链。
        #: 都是**会话内 UI 状态**，不进持久化 —— 所以 `/resume` 载入历史后回到默认折叠。
        self.expand_tools = False
        #: ★ D176：是否绘制**双轨标记**（用户消息 `▌` / 模型输出 `▎`）。
        #: 它纯粹是视觉分区：终端里占了格子的字形**一定会被复制**，无法标记为「装饰」，
        #: 所以给它一个开关 —— 要复制干净文本时按 `Ctrl+B` 关掉（默认开）。
        #: 与 `expand_tools` 同类：**只影响渲染**，不进持久化。
        self.show_track = True
        self.expand_reasoning = False
        self._pending_delta: list[str] = []
        #: 推理增量缓冲（M4 新增）：真实模型一次思考有几百个片段，
        #: 逐个覆盖推理块文本是 O(n²)，因此与正文一样只在帧级合并（D56）
        self._pending_reasoning: list[str] = []
        #: 工具调用名（``ToolCallStarted`` 只带 call_id）
        self._call_names: dict[str, str] = {}
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

        **谁改了数据谁负责调用它**——这是前缀缓存能成立的全部依据。
        ``layout=True`` 表示块的**数量**变了（需要重算布局），
        ``layout=False`` 表示只改了块的内容（只需重绘）。
        """
        self._dirty = True
        if layout:
            self._dirty_layout = True

    def has_pending(self) -> bool:
        """本帧是否有东西要渲染（含"只改了显示状态"的情况）。"""
        return self._dirty or bool(self._pending_delta) or bool(self._pending_reasoning)

    def take_dirty_layout(self) -> bool:
        """取走"需要重算布局"标记（取走即清零，与 `_dirty` 分开跑）。"""
        value = self._dirty_layout
        self._dirty_layout = False
        return value

    # -- 基础写入 ------------------------------------------------------- #

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
        """流式正文增量：只进缓冲，**不产生块、也不失效缓存**（节流的关键）。"""
        self._pending_delta.append(text)
        self._dirty = True

    def add_reasoning_delta(self, text: str) -> None:
        """流式推理增量：同样只进缓冲（真实模型一次思考有几百个片段）。"""
        self._pending_reasoning.append(text)
        self._dirty = True

    def flush_delta(self) -> Block | None:
        """把缓冲的增量落成块。返回新产生的**正文**块（推理另见 flush_reasoning）。

        ⚠️ **必须与紧邻的上一个正文块合并，不能另起一块**（D76）。

        这是用户第三次报障的真正原因（原话："有没有可能是流式输出的问题呢？"
        ——**他猜对了**）。一次请求里正文会被 flush 好几次（每帧一次），
        而每一帧都 ``add_assistant()`` 出一块。屏幕上于是变成：

            我是 Logox，                      ← 第 1 帧的块，自己占一行
            一个在终端里工作的编码助手。至于底层是哪个模型，…

        第一块只有十几格就断了，因为**它是另一块**，而不是同一段文字的第二行。
        折行器再对也没用——它根本没机会把两块连起来。

        症状与"软换行没重排"（D68）**看起来一样、成因完全不同**：
        D68 是同一块内部没重排，D76 是文字被切成了多块。
        两者都会让用户看到"莫名其妙的换行"。
        """
        self.flush_reasoning()
        if not self._pending_delta:
            return None
        text = "".join(self._pending_delta)
        self._pending_delta.clear()
        last = self.blocks[-1] if self.blocks else None
        if last is not None and last.kind == "assistant":
            # 同一段正文的后续帧 → 续写。**不能新建块**，否则渲染层会把它当独立段落。
            last.text += text
            self.invalidate(layout=False)
            return last
        return self.add_assistant(text)

    def flush_reasoning(self) -> Block | None:
        """把缓冲的推理增量**累加**进当前推理块。

        ⚠️ 这里是 **append 而不是覆盖**，而且**当前块内的文本要一起算**：
        一次请求会 flush 很多帧（每帧一次），若每帧只覆盖成"本帧的增量"，
        帧 1 的推理就丢了——实测症状是"推理块只有最后一句"。
        正确做法是把"当前块已有的文本 + 本帧增量"一起写回去。
        """
        if not self._pending_reasoning:
            return None
        added = "".join(self._pending_reasoning)
        self._pending_reasoning.clear()
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
        self._call_names[call_id] = name
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
        """``Ctrl+T``：切换**思考链**的展开（D125）。

        与 :meth:`toggle_expand_tools` 是**两个独立开关**，互不影响 ——
        用户经常只想核对"执行了什么"，而思考链是冗长的内心独白。
        （例外处理与 `Ctrl+O` 一致）
        """
        if self._clear_block_expand_overrides(("reasoning",)):
            self.expand_reasoning = False
        else:
            self.expand_reasoning = not self.expand_reasoning
        self.invalidate(layout=False)

    def _clear_block_expand_overrides(self, kinds: tuple[str, ...]) -> bool:
        """把指定的块打回"跟随全局开关"（`expanded=None`）；返回**是否真的清了东西**。

        块级覆盖的**唯一**来源是 D40 的"失败自动展开"；用户一按全局键，
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
            self._clear_block_expand_overrides(("reasoning",))
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

