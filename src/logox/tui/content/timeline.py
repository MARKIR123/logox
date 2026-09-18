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
from logox.tui.content.cards import (
    CardContext,
    DiffHunk,
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

    prefix = Text("▎ ", style=success_color)
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
    expand_all: bool = False,
    prev_block: Block | None = None,
    active_status: ActiveStatus | None = None,
    now: float | None = None,
) -> Text:
    """把块序列渲染成 Rich ``Text``（纯函数，可完全单测）。"""
    palette = context.palette
    width = max(20, context.width)
    out = Text()
    success_color = str(getattr(palette, "success", "") or "green")
    prefix = Text("▎ ", style=success_color)
    sub_context = CardContext(
        palette=palette,
        glyphs=context.glyphs,
        width=max(18, width - 2),
        diff_context_lines=context.diff_context_lines,
    )

    for index, block in enumerate(blocks):
        prev = blocks[index - 1] if index > 0 else prev_block
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
                    expanded=block.expanded if block.expanded is not None else expand_all,
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
                    payload=block.payload,
                    expanded=block.expanded if block.expanded is not None else expand_all,
                    indent=0,
                )
                out.append_text(_prefix_lines(card, prefix))
                out.append("\n")
            elif block.kind == "diff":
                card = render_diff(
                    path=block.path,
                    stat=block.change_stat,
                    hunks=block.hunks,
                    context=sub_context,
                    expanded=block.expanded if block.expanded is not None else True,
                    indent=0,
                )
                out.append_text(_prefix_lines(card, prefix))
                out.append("\n")
        elif block.kind == "user":
            user_fg = str(getattr(palette, "user_message_fg", "") or getattr(palette, "text_primary", ""))
            accent = str(getattr(palette, "accent", "") or "cyan")
            out.append_text(
                quote_block(
                    [Text(line, style=f"bold {user_fg}") for line in fmt.wrap_cells(block.text, width - 2).split("\n")],
                    marker="▌",
                    marker_style=f"bold {accent}",
                    indent=0,
                )
            )
            out.append("\n")
        elif block.kind in ("divider", "notice"):
            out.append(fmt.wrap_cells(block.text, width), style=getattr(palette, block.token))
            out.append("\n")
        else:  # raw
            out.append(fmt.clip(block.text, width), style=getattr(palette, block.token))
            out.append("\n")

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
        )
        out.append_text(spinner_text)

    return out


@dataclass
class RenderResult:
    """一次渲染的产物 + **工作量**（D56 的度量口径）。"""

    text: Text
    prefix_blocks: int = 0
    tail_rendered_blocks: int = 0
    tail_chars: int = 0
    prefix_hit: bool = False


@dataclass
class TimelineRenderCache:
    """前缀缓存：**覆盖了多少块**与**那段文本**必须成对出现，因此放在同一个对象里。

    ⚠️ 这个类是一次**真实缺陷**的直接产物：第一版把 ``cached_blocks`` 与
    ``cached_text`` 当成两个独立字段，于是出现"块数说 30、文本只有 29"的错配，
    屏幕上就少显示了一条用户消息——**没有任何报错**，只是内容悄悄不对。
    把两者绑成一个不可分割的值之后，这种错配从"可能发生"变成"无法表达"。

    ``expand_all`` 与 ``width`` 也在这里：它们一变，所有块的渲染结果都变，
    缓存必须整体作废。
    """

    blocks: list[Block] = field(default_factory=list)
    text: Text | None = None
    expand_all: bool = False
    width: int = 0

    @property
    def covers(self) -> int:
        return len(self.blocks)

    def matches(self, *, expand_all: bool, width: int) -> bool:
        """渲染参数是否与本缓存一致（块**内容**是否变过由 `blocks` 的身份比对判断）。"""
        return self.text is not None and self.expand_all == expand_all and self.width == width


def render_cached(
    blocks: list[Block],
    cache: TimelineRenderCache,
    *,
    context: CardContext,
    expand_all: bool = False,
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
    """
    reusable = max(0, len(blocks) - 1)  # 最后一块永不缓存
    reuse = cache.covers if cache.covers <= reusable else 0

    if reuse and not cache.matches(expand_all=expand_all, width=context.width):
        reuse = 0
    if reuse and any(cache.blocks[index] is not blocks[index] for index in range(reuse)):
        reuse = 0
    if reuse and reuse != reusable:
        # 缓存落后（块数对不上）→ 必须重渲染，否则新块既不在缓存里、也不会被渲染出来
        reuse = 0

    out = Text()
    if folded and hidden_count > 0:
        # 折叠提示行**不参与缓存**：它不对应任何 Block，长度随 fold 状态变化
        out.append(f"—— 已折叠 {hidden_count} 块历史（Ctrl+H 展开）——\n\n", style=context.palette.text_faint)

    if reuse and cache.text is not None:
        out.append_text(cache.text)
    else:
        out.append_text(render_blocks(blocks[:reuse], context, expand_all=expand_all))

    tail_blocks = blocks[reuse:]
    prev = blocks[reuse - 1] if reuse > 0 else None
    tail_text = render_blocks(
        tail_blocks,
        context,
        expand_all=expand_all,
        prev_block=prev,
        active_status=active_status,
        now=now,
    )
    out.append_text(tail_text)

    return RenderResult(
        text=out,
        prefix_blocks=reuse,
        tail_rendered_blocks=len(tail_blocks),
        tail_chars=len(tail_text.plain),
        prefix_hit=bool(reuse),
    )


class TimelineBuffer:
    """事件 → 显示块的纯状态机。"""

    def __init__(self, *, fold_threshold: int = FOLD_THRESHOLD) -> None:
        self.blocks: list[Block] = []
        self.fold_threshold = fold_threshold
        self.folded = False
        self.expand_all = False
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

    def start_tool(self, *, call_id: str, name: str, args_summary: str = "") -> Block:
        self._call_names[call_id] = name
        block = Block(kind="tool", name=name, args_summary=args_summary, state="running", text=call_id)
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
    ) -> None:
        block = self._tool_block(call_id)
        if block is None:
            return
        block.state = "ok" if ok else "error"
        block.duration_ms = duration_ms
        block.error_kind = error_kind
        block.change_stat = change_stat
        if not ok:
            # D40：失败时**自动展开**（否则用户不知道为何没改成）
            block.expanded = True
        # 改的是**已有块**，且失败时会展开 → 必须失效（T-22/T-23）
        self.invalidate(layout=False)

    def attach_diff(self, *, call_id: str, path: str, hunks: list[DiffHunk], stat: ChangeStat | None = None) -> None:
        """把 diff 挂到某个工具卡片之后（折叠态只显示徽标）。"""
        block = self._tool_block(call_id)
        if stat is not None and block is not None and block.change_stat is None:
            block.change_stat = stat
        self.blocks.append(Block(kind="diff", path=path, hunks=hunks, change_stat=stat, expanded=False))
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

    def toggle_expand_all(self) -> None:
        self.expand_all = not self.expand_all
        self.invalidate(layout=False)  # 每张卡片的渲染结果都变了（T-25）

    def set_expand_all(self, value: bool) -> None:
        if self.expand_all != value:
            self.expand_all = value
            self.invalidate(layout=False)

    def _strip_turn_summary_from_blocks(self) -> None:
        """从时间线末尾的 assistant 块中剥离意外残留的 <turn_summary> 标签。"""
        import re
        pattern = re.compile(r"<turn_summary.*?>.*?(?:</turn_summary>|$)", re.DOTALL | re.IGNORECASE)
        for block in reversed(self.blocks):
            if block.kind == "assistant":
                if "<turn_summary" in block.text.lower():
                    block.text = pattern.sub("", block.text).rstrip()
                    self.invalidate(layout=True)
                break

    # -- 事件摄入 ------------------------------------------------------- #

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
            )
            self.active_status = ActiveStatus(kind="thinking", label="处理结果中", started_at=time.time())
            return True
        if isinstance(event, ev.TurnFinished):
            self.clear_active_status()
            self._strip_turn_summary_from_blocks()
            return True
        if isinstance(event, ev.SessionStart):
            if not getattr(event, "resumed", False) and not self.blocks:
                self.add_divider(
                    f"—— 会话开始 · {event.model} · shell={event.shell_backend} · "
                    f"记忆文件 {len(event.memory_sources)} 份 ——"
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

