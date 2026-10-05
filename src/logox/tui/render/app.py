"""纯净终端流：把内核接到自研渲染器上（D80 / D81）。

这是什么
--------
**不用 Textual、不要侧栏、走主屏**的界面。对齐 Pi 的架构：组件只有三个方法、
渲染是行式差分、滚动与选中交给终端。

三条与旧实现的关键差别
----------------------
1. **主屏**：内容写进终端自己的回滚历史 → 滚动、选中、复制**全部由终端提供**
   （旧实现走备用屏，为此写了虚拟化 + 选区 + 滚动条，共约 700 行）。
2. **行式差分渲染**：内容没变就一个字节都不写；只有尾部变化时只重画尾部。
3. **垂直堆叠**：时间线（自动高）+ 输入框 + 状态行。**没有侧栏**（D81 裁定）。

这个文件负责什么
----------------
只有三件事：**订阅事件**（喂给时间线 / 度量）、**驱动帧**、**转发按键**。
命令的业务流程在 `commands.py`，渲染算法在 `screen.py`，按键解析在 `keys.py`。
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from rich.text import Text

from logox.config.schema import StatusItems, TimingFields
from logox.kernel import events as ev
from logox.permission_types import PermissionAsk, PermissionChoice
from logox.tui.content.cards import CardContext
from logox.tui.content.status import StatusContext, build_status_line
from logox.tui.content.timeline import (
    Block,
    TimelineBuffer,
    TimelineRenderCache,
    render_cached,
)
from logox.tui.metrics import MetricsReducer
from logox.tui.render.ansi import split_styled_lines
from logox.tui.render.commands import CommandRunner
from logox.tui.render.component import Component
from logox.tui.render.components.editor import BoxedEditor, Editor
from logox.tui.render.components.overlay import PermissionComponent
from logox.tui.render.keys import (
    DISABLE_KITTY_KEYBOARD,
    DISABLE_MODIFY_OTHER_KEYS,
    ENABLE_KITTY_KEYBOARD,
    ENABLE_MODIFY_OTHER_KEYS,
    ESC_TIMEOUT_MS,
    KITTY_QUERY,
    Key,
    KeyParser,
    strip_kitty_responses,
)
from logox.tui.render.screen import CLEAR_ALL, Screen
from logox.tui.render.terminal import Terminal, make_terminal
from logox.tui.theme import load_theme

__all__ = ["InlineApp", "run_inline"]

#: 状态行占 1 行，输入框至少 1 行
RESERVED_ROWS = 2

#: `/debug` 保留多少条事件（越多越占内存，50 条够看"刚才发生了什么"）
DEBUG_EVENT_LIMIT = 50

#: ``Ctrl+C`` 两下退出的时间窗口（秒）。
#:
#: 为什么不是"按一下就直接退"：``Ctrl+C`` 是终端里"复制"的常用键，
#: 而我们收走了按键——一次误按就等于丢掉整个会话。2 秒是"同一动作"的量级
#: （人按两下键的间隔通常远小于它，而"再想想"通常会超过）。
CTRL_C_WINDOW_S = 2.0

#: 请求终端开启**括号粘贴**（``CSI ? 2004 h``）：粘贴的内容会被 ``ESC[200~`` /
#: ``ESC[201~`` 包起来，于是"粘贴一大段"与"手打一大段"可以区分。
#: 没有它的话，粘贴内容里的换行会被当成"逐条回车提交"——**一次粘贴发几十条消息**。
ENABLE_BRACKETED_PASTE = "\x1b[?2004h"
DISABLE_BRACKETED_PASTE = "\x1b[?2004l"

#: 问完 Kitty 协议后等多久还没回应，就退回到 xterm 的 ``modifyOtherKeys``（秒）。
#:
#: 150ms 是 Pi 用的值：太短会在慢速链路上误判（例如经过 tmux），
#: 太长会让 ``Shift+Enter`` 在启动后的一小段时间里失效。
KITTY_FALLBACK_DELAY_S = 0.15


class TimelineComponent:
    """时间线把可变 Block 转成不可变 Rich 行，并复用未变排版。

    每帧仍核验所有块的实际显示字段，覆盖老工具完成、原地改正文、展开
    与主题/宽度变化。核验后复用共同片段前缀、行区间和已切好的行，输入
    不重新排版历史，流式只重新拼装变化后缀。完整 Text 访问仅作兼容。
    内容键遍历仍随历史长度增长，不能承诺整帧成本恒定。
    """

    def __init__(self, palette: Any, *, max_height: int = 0, glyphs: dict[str, str] | None = None) -> None:
        self.palette = palette
        #: 状态字形（`✓` / `✗` / `✻` …）。**必须由装配处注入**——
        #: D200 之前这里没有这个字段，于是卡片永远走 `cards.py` 里的字面量 fallback，
        #: 主题文件里的 `[glyphs]` 写了也不生效（配置存在 ≠ 配置生效）。
        self.glyphs: dict[str, str] = dict(glyphs or {})
        self.buffer = TimelineBuffer()
        self.max_height = max_height
        self._cache = TimelineRenderCache()
        #: 渲染行与 Block 的区间映射 [(start_line, end_line, block)]
        self.block_ranges: list[tuple[int, int, Block]] = []
        #: 前缀**行**缓存：`_prefix_rows` 对应的源文本（身份比对用）
        self._prefix_rows: list[Text] = []
        self._prefix_rows_key: Text | None = None
        self._block_rows: dict[int, tuple[Text | list[Text], list[Text]]] = {}
        self._segments: list[tuple[Block | None, Text | list[Text]]] = []
        self._segment_offsets: list[int] = [0]
        self._segment_rows: list[Text] = []
        #: 度量（测试与 /debug 用）：命中次数、作废次数、上一帧耗时
        self.prefix_hits = 0
        self.cache_invalidations = 0
        self.prefix_row_reuses = 0

    @property
    def is_active(self) -> bool:
        """是否处于活跃生成/工具调用状态。"""
        return self.buffer.active_status is not None

    def ingest(self, event: ev.AnyEvent) -> bool:
        return self.buffer.ingest(event)

    def render(self, width: int) -> list[Text]:
        context = CardContext(palette=self.palette, width=width, glyphs=self.glyphs)
        blocks: list[Block] = self.buffer.visible_blocks
        if not blocks and not self.buffer.active_status:
            # 空会话且无活跃状态**一行都不出**：`render_blocks([])` 会返回一个空串，
            # 于是启动时输入框上面会多出一条莫名的空行（看着像 bug 而不是留白）。
            self.block_ranges = []
            self._cache = TimelineRenderCache()
            self._block_rows.clear()
            self._segments = []
            self._segment_offsets = [0]
            self._segment_rows = []
            return []

        # 缓存的有效性判断与重建**全在 `render_cached` 里**（D126）：
        # 这样"重建"那一帧也能立刻拿到稳定的前缀对象，上层才能只切尾部。
        result = render_cached(
            blocks,
            self._cache,
            context=context,
            expand_tools=self.buffer.expand_tools,
            expand_reasoning=self.buffer.expand_reasoning,
            # ★ D176：双轨标记的显示开关（`Ctrl+B`）
            show_track=self.buffer.show_track,
            active_status=self.buffer.active_status,
            segmented=True,
        )
        self.block_ranges = result.block_ranges
        if result.cache_rebuilt:
            self.cache_invalidations += 1
        else:
            self.prefix_hits += 1

        rows = self._split_rows(result)
        # 只保留**尾部**若干行（`max_height` > 0 时）：主屏下"上面滚掉"是自然的，
        # 不需要虚拟化。默认 0 = 不裁剪，全部交给终端滚动（会话历史进回滚缓冲）。
        if self.max_height and len(rows) > self.max_height:
            rows = rows[-self.max_height :]
        return rows

    def toggle_card_at_line(self, line_idx: int) -> bool:
        """根据渲染行号切换对应卡片的独立展开/收起状态 (3A)。

        若命中了 tool / reasoning / diff 块，将其 expanded 状态翻转，
        并调用 self.invalidate() 使缓存失效，返回 True；
        未命中可折叠卡片则返回 False。
        """
        target_block: Block | None = None
        for s_line, e_line, block in self.block_ranges:
            if s_line <= line_idx < e_line:
                target_block = block
                break

        if target_block is None or target_block.kind not in ("tool", "reasoning", "diff", "anamnesis"):
            return False

        if target_block.kind in {"anamnesis", "reasoning"}:
            cur = target_block.expanded if target_block.expanded is not None else self.buffer.expand_reasoning
            target_block.expanded = not cur
        elif target_block.kind == "tool":
            cur = target_block.expanded if target_block.expanded is not None else self.buffer.expand_tools
            target_block.expanded = not cur
            try:
                buf_idx = self.buffer.blocks.index(target_block)
                if buf_idx + 1 < len(self.buffer.blocks):
                    next_block = self.buffer.blocks[buf_idx + 1]
                    if next_block.kind == "diff":
                        next_block.expanded = not cur
            except ValueError:
                pass
        elif target_block.kind == "diff":
            cur = target_block.expanded if target_block.expanded is not None else self.buffer.expand_tools
            target_block.expanded = not cur

        self.buffer.invalidate(layout=False)
        return True

    def _split_rows(self, result: Any) -> list[Text]:
        """优先按块复用不可变行，正文和样式都相同时保留旧行身份。

        分块入口避免合并整个历史；完整 Text 入口保留旧前缀兼容路径，
        当前缀不是完整换行边界时回退到完整切分，避免断行错误。
        """
        if result.segments is not None:
            segments = result.segments
            if segments is self._segments:
                self.prefix_row_reuses += len(segments)
                rows = self._segment_rows
                return [] if len(rows) == 1 and not rows[0].plain else rows
            common = 0
            for old, new in zip(self._segments, segments, strict=False):
                if old[0] is not new[0] or old[1] is not new[1]:
                    break
                common += 1
            rows = self._segment_rows[:self._segment_offsets[common]]
            offsets = self._segment_offsets[:common + 1]
            suffix_keys: set[int] = set()
            self.prefix_row_reuses += common
            for block, text in segments[common:]:
                key = id(block) if block is not None else id(text)
                suffix_keys.add(key)
                previous = self._block_rows.get(key)
                if (isinstance(text, list) and not text) or (isinstance(text, Text) and not text.plain):
                    offsets.append(len(rows))
                    self._block_rows.pop(key, None)
                    continue
                if previous is not None and previous[0] is text:
                    piece_rows = previous[1]
                    self.prefix_row_reuses += 1
                else:
                    if isinstance(text, list):
                        piece_rows = text
                    else:
                        piece_rows = split_styled_lines(text) if text.plain != "\n" else [Text()]
                    if previous is not None:
                        old_rows = previous[1]
                        # 新列表可替换相同行；不能原地修改下层缓存拥有的行列表。
                        piece_rows = list(piece_rows)
                        for index in range(min(len(old_rows), len(piece_rows))):
                            if piece_rows[index] == old_rows[index]:
                                piece_rows[index] = old_rows[index]
                rows.extend(piece_rows)
                offsets.append(len(rows))
                self._block_rows[key] = (text, piece_rows)
            for block, text in self._segments[common:]:
                key = id(block) if block is not None else id(text)
                if key not in suffix_keys:
                    self._block_rows.pop(key, None)
            self._segments = segments
            self._segment_offsets = offsets
            self._segment_rows = rows
            if len(rows) == 1 and not rows[0].plain:
                return []
            return rows
        prefix_text = result.reusable_prefix
        if prefix_text is None:
            # 没有可复用的前缀（有折叠头部、或还没攒出前缀）→ 整段重切
            self._prefix_rows = []
            self._prefix_rows_key = None
            return split_styled_lines(result.text)

        if prefix_text is not self._prefix_rows_key:
            self._prefix_rows = split_styled_lines(prefix_text)
            self._prefix_rows_key = prefix_text
        else:
            self.prefix_row_reuses += 1

        if self._prefix_rows and not prefix_text.plain.endswith("\n"):
            # 前缀没以换行结尾 → 它的末行与尾部首行是同一行，增量拼接不成立
            return split_styled_lines(result.text)
        return [*self._prefix_rows, *split_styled_lines(result.tail_text)]

    def handle_input(self, key: Key) -> bool:
        """时间线自己不抢键，只处理两个**全局展开开关**（D125）。

        这里是**输入框不消费时的冒泡落点**（`Screen.handle_key` 的冒泡链路）。
        与 `InlineApp._dispatch` 那一条路径**必须同时维护** —— 只改一处的话，
        症状是"某个状态下按键失效"（取决于按键是先到编辑器还是先到应用级）。
        """
        if key.ctrl and key.name == "o":
            self.buffer.toggle_expand_tools()
            return True
        if key.ctrl and key.name == "b":
            # ★ D176：切换**双轨标记**（`▌` / `▎`）—— 要拖选复制干净文本时关掉它。
            #   终端里「占了格子的字形一定会被复制」，所以唯一可靠的办法是让标记**消失**
            #   （不是换成空格 —— 那样复制出来仍会多两个空格）。
            self.buffer.toggle_track()
            self.invalidate()
            return True
        if key.ctrl and key.name == "t":
            self.buffer.toggle_expand_reasoning()
            return True
        return False

    def invalidate(self) -> None:
        self._cache = TimelineRenderCache()
        self._block_rows.clear()
        self._segments = []
        self._segment_offsets = [0]
        self._segment_rows = []
        self.cache_invalidations += 1

    def step(self) -> bool:
        """从平滑器按自适应打字机速率释出一步增量。返回是否有新文本落块。"""
        committed = False
        if self.buffer.step_reasoning() is not None:
            committed = True
        if self.buffer.step_delta(include_reasoning=False) is not None:
            committed = True
        return committed

    def flush(self) -> bool:
        """把缓冲里的**流式增量全部**落成块（终态瞬时排空）。返回是否真的落了东西。

        ⚠️ 为什么必须有人定期调它（这是实测踩到的一个严重缺陷）：
        `TimelineBuffer.add_delta()` **只把文本放进缓冲**，不产生块也不失效缓存
        ——那正是节流能成立的原因（一回合几百条增量，逐条重渲染会很慢）。
        代价是：**没人调 flush 就什么都不会显示**。

        新界面最初就漏了这一步，症状是"**整段回答在回合结束时一次性蹦出来**"，
        流式效果完全消失。而所有单测都是绿的——因为它们直接驱动组件，
        不经过"事件 → 缓冲 → 定时落块"这条真实链路（`tests/e2e/test_tui_loop.py`
        里的流式断言抓住了它）。
        """
        committed = False
        if self.buffer.flush_delta() is not None:
            committed = True
        if self.buffer.flush_reasoning() is not None:
            committed = True
        return committed


class StatusComponent:
    """底部状态行（对齐 Pi 的 ``FooterComponent`` 的**信息密度**）。

    只放"每一眼都想要"的东西：模型、思考档位、token、费用、上下文占用。
    侧栏里那些（文件列表、MCP、memory）已按 D81 裁定改为按需呈现。

    **内容由 `logox.tui.content.status` 算**（从旧 Textual 组件里搬出来的纯逻辑）：
    它的裁剪算法是 UI-SPEC §5.1 专门设计的（超宽时先合并 ``usage``+``cache``，
    再按优先级一项项摘除）。重写一遍必然走样，所以两条界面路径共用同一份。
    """

    def __init__(
        self,
        palette: Any,
        *,
        items: StatusItems | None = None,
        timing_fields: TimingFields | None = None,
        glyphs: dict[str, str] | None = None,
    ) -> None:
        self.palette = palette
        self.items = items or StatusItems()
        self.timing_fields = timing_fields or TimingFields()
        #: 状态字形集（D200：由装配处注入，`tool_glyph` 从中取）。
        self.glyphs: dict[str, str] = dict(glyphs or {})
        self._metrics: Any = None

    @property
    def tool_glyph(self) -> str:
        """"运行中"字形（生成中提示与工具项共用）。

        ⚠️ 回退值必须与 :class:`~logox.config.schema.ThemeGlyphs` 的默认一致 ——
        两条路给出不同图标时，状态栏会与工具卡片不一致，而那时看代码是看不出来的。
        """
        return self.glyphs.get("running", "✻")

    @property
    def metrics(self) -> Any:
        if callable(self._metrics):
            return self._metrics()
        return self._metrics

    @metrics.setter
    def metrics(self, value: Any) -> None:
        self._metrics = value

    def render(self, width: int) -> list[Text]:
        metrics = self.metrics
        if metrics is None:  # pragma: no cover - 装配根总会给
            return [Text()]
        context = StatusContext(
            palette=self.palette,
            items=self.items,
            timing_fields=self.timing_fields,
            width=max(1, width),
            tool_glyph=self.tool_glyph,
        )
        return [build_status_line(metrics, context)]

    def handle_input(self, key: Key) -> bool:
        return False

    def invalidate(self) -> None:
        return None


class AppLayout:
    """纯流式布局（Flow Layout，对齐 Claude Code 与 Pi Agent）：

    内容自上而下自然流式排列：时间线紧贴对话流，输入框与状态栏紧随时间线正下方。
    彻底移除人造空行 padding，坚决杜绝因屏幕物理边界下移失效导致的光标漂移与残影。
    超出一屏时，依靠终端原生回滚缓冲自然滚动。
    激活浮层（如 /resume 弹窗）时，让出常规输入框和状态行。
    """

    def __init__(
        self,
        terminal: Terminal,
        timeline: TimelineComponent,
        editor: Component,
        status: StatusComponent,
        *,
        screen: Any = None,
    ) -> None:
        self.terminal = terminal
        self.timeline = timeline
        self.editor = editor
        self.status = status
        self.screen = screen
        #: `/` 补全提示框（D162）。`None` = 不显示。它**不是浮层**，见 `render()` 的说明。
        self.completion: Any | None = None

    def render(self, width: int) -> list[Text]:
        tl_rows = self.timeline.render(width)

        # 检查是否有激活的可见浮层（对标 Pi 的独占提示框：**模态**弹窗时让出常规输入区）
        has_active_overlay = False
        if self.screen is not None and hasattr(self.screen, "overlays"):
            for h in self.screen.overlays:
                if not getattr(h, "hidden", False):
                    has_active_overlay = True
                    break

        if has_active_overlay:
            return tl_rows

        # ★ D162 修正：`/` 补全提示框**画在这里** —— 时间线与输入框之间。
        #   为什么不做成浮层：`AppLayout` 一见到浮层就会让出输入区（上面那段），
        #   于是列表正好落在输入框原来的位置上，把它遮住（用户报障）。
        #   作为布局的一部分则天然满足"在输入框上方、且不遮挡它"：
        #   帧在中间长高 ⇒ 时间线尾部滚进回滚缓冲，**输入框位置不动**。
        completion_rows = self.completion.render(width) if self.completion is not None else []

        ed_rows = self.editor.render(width)
        st_rows = self.status.render(width)
        return tl_rows + completion_rows + ed_rows + st_rows

    def handle_input(self, key: Key) -> bool:
        if self.editor.handle_input(key):
            return True
        return self.timeline.handle_input(key)

    def invalidate(self) -> None:
        self.timeline.invalidate()
        self.editor.invalidate()
        self.status.invalidate()
        if self.completion is not None and hasattr(self.completion, "invalidate"):
            self.completion.invalidate()


def user_themes_dir(runtime: Any) -> Path | None:
    """用户主题目录（``~/.logox/themes``）；取不到时返回 ``None``（= 只有内置主题）。

    为什么集中成**一个**函数（D152-c）：它被**四处**需要 ——
    装配根的一次主题加载、界面的两次（构造 + `/theme` 热切换）、以及 `/theme` 的列表。
    任何一处漏传都会造出"只有用户会撞上"的不一致：列表里有它但选不中、
    或选得中但列表里没有。这类症状很难自查，所以来源必须唯一。

    ``runtime`` 用 ``getattr`` 逐层探测：测试里的假 runtime 可能没有 ``paths``，
    而"探不到"的正确语义正是"没有用户主题目录"（退回内置），不是报错。
    """
    themes = getattr(getattr(runtime, "paths", None), "themes", None)
    return Path(themes) if themes is not None else None


class InlineApp:
    """把内核事件接到渲染器上。"""

    def __init__(
        self,
        *,
        runtime: Any,
        terminal: Terminal | None = None,
        theme_name: str | None = None,
        session_start: Any | None = None,
    ) -> None:
        self.runtime = runtime
        self.terminal = terminal or make_terminal()
        #: 用户主题目录（`~/.logox/themes`）。**只认用户目录，不读项目级**（D152-c）。
        #: 属性名刻意不叫 `user_themes_dir` —— 那会和上面的模块级函数同名，
        #: 后来的人写 `self.user_themes_dir(...)` 想调用函数就会撞上"不可调用"。
        self.themes_dir = user_themes_dir(runtime)
        config = getattr(runtime, "config", None)
        # 主题名：配置里写的优先（用户可能选过 logox-light），否则用主题默认
        chosen = theme_name or (
            getattr(getattr(config, "ui", None), "theme", "") or "logox-dark"
        )
        try:
            self.theme = load_theme(chosen, self.themes_dir)
        except Exception:  # 主题坏了不该让界面起不来（装配根已经警告过一次）
            self.theme = load_theme("logox-dark", self.themes_dir)
        palette = self.theme.palette
        #: 状态字形集（D200）。**唯一来源**：主题的 `[glyphs]` 段，再按 `ui.icon_set` 降级。
        #: 装配处算一次、注入两个组件，`/theme` 与 `/reload` 换主题时用同一个方法重算 ——
        #: 三条路径（启动 / `/theme` / `/reload`）因此不可能给出不同图标。
        glyphs = self._glyphs_for(self.theme)

        self.screen = Screen(self.terminal)
        self.timeline = TimelineComponent(palette, glyphs=glyphs)
        replayer = getattr(runtime, "session_replayer", None)
        if callable(replayer):
            try:
                replayer(self.timeline)
            except Exception as exc:
                self.timeline.buffer.add_notice(f"载入历史会话失败：{exc}", token="warning")

        core_editor = Editor(
            on_submit=self._on_submit,
            # 空输入时右侧的指路牌：键位在状态行被度量挤掉了，这里补回可发现性
            hint="Enter 发送 · /help 键位 · Ctrl+C 中断 · Ctrl+D 退出",
            hint_style=str(palette.input_hint),
        )
        self.editor = BoxedEditor(
            core_editor,
            # ★ D152-a：输入框三个元素各用**专属** token。
            #   此前它们是借来的（border_subtle / text_primary / text_faint），
            #   而 border_subtle 是共享的装饰色（对 bg_base 仅 1.30:1）——
            #   既看不清，又"改输入框 = 帮助分隔线/工具卡竖线/浮层底边一起变"，
            #   谈不上微调。现在这三个值可以独立调。
            border_style=str(palette.input_border),
            text_style=str(palette.input_text),
        )
        self.status = StatusComponent(
            palette,
            items=getattr(getattr(config, "ui", None), "status_items", None),
            timing_fields=getattr(getattr(config, "ui", None), "timing_fields", None),
            glyphs=glyphs,
        )

        # 度量归约器（把事件压成状态行要的那些数字）。**阻塞投递**：
        # 纯算术，很快，而且状态行落后一帧会让人以为"卡住了"。
        self.reducer = getattr(runtime, "reducer", None) or MetricsReducer()
        self.status.metrics = lambda: self.reducer.metrics
        #: 会话开始事件（由装配根构造）。在 `run()` 里、**应用自己的事件循环内**发布
        #: ——跨事件循环发布会让总线内部的非阻塞消费者静默失灵。
        self.session_start = session_start
        self.effort = str(getattr(getattr(config, "provider", None), "thinking_effort", "auto") or "auto")
        # ★ 构造时就把"当前是谁"填进度量：状态行第一帧就该显示模型名，
        # 而不是等 `SessionStart` 走完一遍总线（那会先闪一下空模型名）。
        self.reducer.metrics.model = str(getattr(runtime, "model", "") or "")
        self.reducer.metrics.provider = str(getattr(runtime, "provider_name", "") or "")
        self.reducer.metrics.thinking_effort = self.effort
        #: 最近的事件（`/debug` 用）
        self._debug_log: list[str] = []
        #: 上一次用户在弹窗输入框中填写的具体补充要求
        self.last_feedback = ""

        self.root = AppLayout(self.terminal, self.timeline, self.editor, self.status, screen=self.screen)
        self.screen.add(self.root)
        self.screen.set_focus(self.editor)

        # 原始输入交给本应用解析（`Screen` 只管转发）
        self.screen.on_input = self._on_raw_input
        self.commands = CommandRunner(self)

        self.keys = KeyParser()
        self._pending_escape_handle: asyncio.TimerHandle | None = None
        self._busy = False
        #: 上一次空闲时按 Ctrl+C 的时间（两下退出用；见 `_interrupt`）
        self._ctrl_c_armed_at: float | None = None
        # ★ D162：`/` 命令补全（状态在 app，编辑器不认识它）
        self._completion: Any | None = None  # 只存状态；提示框挂在 self.root.completion 上
        #: `run()` 启动的后台任务（退出时要取消，否则 sleep 会在关闭的循环上抛）
        self._tickers: list[Any] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        #: 退出事件（由 run() 初始化，使事件循环可以无损休眠，并在 stop() 时即刻唤醒）
        self._stop_event: asyncio.Event | None = None
        #: 正在跑的交互命令名（`/login` / `/model`）——防止叠出两层浮层
        self._interactive_running: set[str] = set()
        #: Kitty 协议探测状态（见 `_enable_keyboard_protocols`）
        self._kitty_active = False
        self._modify_other_keys_active = False
        self._kitty_timer: asyncio.TimerHandle | None = None
        #: 事件循环所在线程的 id（D133：用来判断"现在是不是在读线程上"）
        self._loop_thread_id: int | None = None
        #: 退订句柄。**在构造时订阅**而不是在 `run()` 里——
        #: 这样"事件能不能到达界面"是**构造后立刻可测**的（测试不必先跑起事件循环）。
        self._unsubscribe: list[Any] = []
        if getattr(runtime, "bus", None) is not None:
            self._unsubscribe.append(
                runtime.bus.subscribe(ev.Event, self._on_event, name="inline-app")
            )
            self._unsubscribe.append(
                runtime.bus.subscribe(ev.Event, self.reducer.handle, name="inline-metrics")
            )
        # ★ 把"怎么问用户"注册给装配根里的权限决策器（见 `logox/app.py` 的
        # `UiPermissionDecider`）。**界面不 import 内核**，内核也不知道界面——
        # 两边只通过 `PermissionAsk` / `PermissionChoice` 这个纯数据形状说话。
        #
        # 构造时就注册（而不是等 `run()`）：这样"注册有没有发生"是构造后立刻可测的，
        # 漏注册的症状是**工具静默被拒绝**，而那时用户只会觉得"这工具坏了"。
        if getattr(runtime, "permission_decider", None) is not None:
            runtime.permission_decider.prompter = self
        self.anamnesis = getattr(runtime, "anamnesis", None)
        self._anamnesis_block: Block | None = None
        self._anamnesis_restore_task: asyncio.Task | None = None
        self._anamnesis_restore_capture: tuple[str, Any] | None = None
        if self.anamnesis is not None:
            self.anamnesis.on_event = self._on_anamnesis
            self.anamnesis.foreground_busy = lambda: self._busy or self.screen.has_overlay() or bool(self._interactive_running)

    # ------------------------------------------------------------------ #
    # 事件 → 界面
    # ------------------------------------------------------------------ #

    async def _on_event(self, event: ev.AnyEvent) -> None:
        """总线订阅者：把事件喂给时间线，然后请求重绘。

        ⚠️ 这里**只做"显示"**，不做任何业务判断——那是内核的事（D7 事件总线内核）。
        """
        if isinstance(event, ev.TurnFinished):
            self._busy = False
            if self.anamnesis is not None:
                self.anamnesis.note_foreground_state(False)
        elif isinstance(event, ev.UserPromptSubmit):
            if self.anamnesis is not None:
                self.anamnesis.note_submission(event.ts)
                self.anamnesis.note_foreground_state(True)
        elif isinstance(event, (ev.ModelRequestStarted, ev.ToolCallRequested)):
            self._busy = True
        needs_render = self.timeline.ingest(event)
        self._remember_for_debug(event)
        if isinstance(event, ev.UserPromptSubmit):
            # 阻塞订阅者在内核构建上下文前兑现回显，只使用已有事件避免重复消息。
            self.screen.render_now()
        elif isinstance(event, (ev.TurnFinished, ev.ModelRequestFinished, ev.ErrorOccurred)):
            # 回合结束/请求结束/出错时**立刻**落一次：否则最后几个增量要等下一次
            # 定时 tick 才出现，而如果那之后没有任何事件，它就永远留在缓冲里。
            self.timeline.flush()
            self.screen.request_render()
        elif needs_render:
            self.screen.request_render()

    async def _on_anamnesis(self, event: Any) -> None:
        from logox.tui.content.anamnesis import AnamesisCard

        if event.session_id and self.anamnesis is not None and event.session_id != self.anamnesis.current_session():
            return
        capture = self._anamnesis_restore_capture
        if capture is not None and (not event.session_id or event.session_id == capture[0]):
            capture[1].append(event)
        block = next((b for b in reversed(self.timeline.buffer.blocks)
                      if b.kind == "anamnesis" and b.card.run_id == event.run_id), None)
        if block is None:
            block = Block(kind="anamnesis", card=AnamesisCard(event.run_id))
            self.timeline.buffer.blocks.append(block)
        if event.sequence and event.sequence <= block.card.sequence:
            return
        previous_phase = block.card.phase
        block.card.ingest(event)
        self._anamnesis_block = block
        if event.kind in {"completed", "failed", "paused"} and previous_phase not in {"completed", "failed", "paused", "interrupted"}:
            message = {"completed": "✓ 入梦已完成", "failed": "✗ 入梦失败", "paused": "Ⅱ 入梦已暂停"}[event.kind]
            if event.reason:
                message += "：" + event.reason[:300]
            self.timeline.buffer.add_notice(message, token="warning" if event.kind == "failed" else "success")
        self.timeline.buffer.invalidate()
        self.screen.request_render()

    async def _restore_anamnesis(self) -> None:
        from logox.tui.content.anamnesis import AnamesisCard

        if self.anamnesis is None:
            return
        owner = self.anamnesis.current_session()
        if not owner:
            return
        from collections import deque

        capture: tuple[str, Any] = (owner, deque(maxlen=512))
        self._anamnesis_restore_capture = capture
        try:
            previews = await self.anamnesis.history(owner)
            if owner != self.anamnesis.current_session():
                return
            for preview in previews:
                block = next((b for b in self.timeline.buffer.blocks
                              if b.kind == "anamnesis" and b.card.run_id == preview["run_id"]), None)
                card = AnamesisCard.from_preview(preview, active=self.anamnesis.is_active
                                                and self.anamnesis.status().run_id == preview["run_id"])
                for event in capture[1]:
                    if event.run_id == card.run_id and event.sequence > card.sequence:
                        card.ingest(event)
                if block is not None and block.card.sequence > card.sequence:
                    continue
                if block is None:
                    block = self.timeline.buffer.add_anamnesis_reference(card.run_id)
                block.card = card
                self._anamnesis_block = block
            self.timeline.buffer.invalidate()
            self.screen.request_render()
        except (ValueError, OSError) as exc:
            self.notice(f"入梦历史恢复失败：{exc}", token="warning")
        finally:
            if self._anamnesis_restore_capture is capture:
                self._anamnesis_restore_capture = None

    def _schedule_anamnesis_restore(self) -> None:
        if self.anamnesis is None:
            return
        if self._anamnesis_restore_task is not None:
            self._anamnesis_restore_task.cancel()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(self._restore_anamnesis())
        else:
            self._anamnesis_restore_task = loop.create_task(self._restore_anamnesis())

    def _remember_for_debug(self, event: ev.AnyEvent) -> None:
        """留一份事件摘要给 ``/debug`` 看。

        这是"事件总线内核"最直接的证据：**内核为了这一步一行代码都没加**——
        `/debug` 只是又一个订阅者。
        """
        kind = type(event).__name__
        if len(self._debug_log) >= DEBUG_EVENT_LIMIT:
            del self._debug_log[0]
        self._debug_log.append(f"{len(self._debug_log) + 1:>3}. {kind}")

    def recent_events(self) -> list[str]:
        return list(self._debug_log)

    async def ask_permission(self, ask: PermissionAsk) -> PermissionChoice:
        """（``PermissionPrompter`` 的实现）弹权限弹窗，返回用户的选择。

        这是**新界面对"工具权限确认"的全部实现**：内核的 ``PermissionDecider``
        返回 ``ask`` 时，装配根转到这个方法，用户看到的是一张
        UI-SPEC §5.8 规定的四选项弹窗。

        ⚠️ 两处"宁可不问"的保守处理：

        1. **终端太矮就不问**（连四个选项都放不下时）。画一张按钮看不见的弹窗
           会让这个回合**永远卡在等待授权上**——用户不知道要按什么键，
           也没有任何提示告诉他为什么没反应。那种情况下"拒绝 + 说明原因"
           是唯一能让用户继续往下走的选择。
        2. 任何异常都降级成**拒绝**（由调用方兜住）：权限路径上的失败
           绝不能变成"默认放行"。
        """
        component = PermissionComponent(
            ask, self.theme.palette, max_rows=self.content_rows
        )
        if self.content_rows and component.minimum_rows(self.content_width) > self.content_rows:
            self.notice(
                f"终端太小（{self.terminal.rows} 行），放不下授权弹窗 → 已按**拒绝**处理；"
                "放大终端后重试即可",
                token="warning",
            )
        self.last_feedback = ""
        choice = await self.push_overlay(component)
        feedback = getattr(component, "input_value", "").strip()
        if feedback:
            self.last_feedback = feedback
            self.notice(f"已拒绝调用，补充要求：{feedback}", token="warning")
            return PermissionChoice.DENY
        # 浮层被别的路径关掉时可能返回 None —— 那不是"允许"
        return choice if isinstance(choice, PermissionChoice) else PermissionChoice.DENY

    def _on_submit(self, text: str) -> None:
        """用户按下 Enter：交给内核（**不 await**，否则界面会被这一回合占住）。"""
        if text.startswith("/"):
            self._spawn(self._run_command(text))
            self.screen.request_render(force=True)
            return
        self._busy = True
        if self.anamnesis is not None:
            self.anamnesis.note_activity("submit")
            self.anamnesis.note_foreground_state(True)
        self._spawn(self._start_turn(text))
        self.screen.request_render(force=True)

    async def _start_turn(self, text: str) -> None:
        try:
            await self.runtime.kernel.start(text)
        except Exception as exc:  # pragma: no cover - 竞态兜底
            self._busy = False
            if self.anamnesis is not None:
                self.anamnesis.note_foreground_state(False)
            self.timeline.buffer.add_notice(f"无法提交：{exc}", token="danger")
            self.screen.request_render(force=True)

    async def _run_command(self, text: str) -> None:
        """解析并执行一条斜杠命令（**异步**：`/login` 这类要弹浮层等用户）。"""
        from logox.tui.commands import resolve

        command = resolve(text)
        if command is None:
            return
        await self.commands.run(command)
        self.screen.request_render(force=True)

    def _spawn(self, coro: Any) -> Any:
        """在运行中的事件循环里排一个任务（没有循环时直接跑完）。

        返回 Task（调用方可以取消它），没有事件循环时返回 ``None``。
        """
        if self._loop is not None:
            return self._loop.create_task(coro)
        asyncio.run(coro)  # pragma: no cover - 仅在没有事件循环时（测试直接调用）
        return None

    # ------------------------------------------------------------------ #
    # 浮层（`CommandHost` 的实现）
    # ------------------------------------------------------------------ #

    @property
    def content_width(self) -> int:
        """可用内容宽度：终端宽度减两侧留白（浮层与帮助按它排版）。"""
        return max(20, self.terminal.columns - 2)

    @property
    def content_rows(self) -> int:
        """可用内容高度：终端高度减状态行与输入框，再留一点余量。"""
        return max(6, self.terminal.rows - RESERVED_ROWS - 2)

    async def push_overlay(self, component: Component) -> Any:
        """显示一个浮层，等它交出结果，然后关掉并把焦点还给输入框。

        实现方式：组件拿到一个 ``on_done`` 回调，回调把结果放进 ``Future``；
        这里 ``await`` 那个 Future。**组件完全不需要知道 asyncio 的存在**——
        它只管"我得出结果了"，谁在等、等到之后干什么都与它无关。

        ⚠️ **没有事件循环时直接报错，而不是等下去**：等下去就是永久卡死
        （按键永远送不进这个 Future），而症状是"界面完全没反应"。
        测试里实测到过这个挂起——它比一条明确的错误难查得多。
        """
        if self._loop is None:
            raise RuntimeError("交互命令需要运行中的事件循环：请先 app.run()（或测试里设置 app._loop）")
        future: asyncio.Future[Any] = self._loop.create_future()

        def done(result: Any) -> None:
            if not future.done():
                future.set_result(result)

        component.on_done = done  # type: ignore[attr-defined]
        handle = self.screen.show_overlay(
            component,
            width=self.terminal.columns,
            col=0,
            margin=0,
            anchor="bottom",
            non_capturing=False,
        )
        self.screen.request_render(force=True)
        try:
            return await future
        finally:
            self.screen.pop_overlay(handle)
            # 焦点还给输入框：否则浮层关掉之后按键会继续送给一个已经关掉的组件
            self.screen.set_focus(self.editor)
            self.screen.request_render(force=True)

    @property
    def busy(self) -> bool:
        """是否正在生成回复（`/reload` 据此拒绝执行，Q3-A）。"""
        return self._busy

    def notice(self, message: str, *, token: str = "text_muted") -> None:
        """在时间线上写一行提示（命令的结果**必须看得见**）。"""
        self.timeline.buffer.add_notice(message, token=token)
        self.screen.request_render(force=True)

    def clear_timeline(self) -> None:
        self.timeline.buffer.blocks.clear()
        self.timeline.invalidate()
        self._debug_log.clear()
        self.screen.request_render(force=True)

    def refresh_status(self) -> None:
        """把内核/运行时里的当前值同步回来，再重画（模型、档位、provider 变了之后）。"""
        metrics = self.reducer.metrics
        metrics.model = getattr(self.runtime, "model", metrics.model)
        metrics.provider = getattr(self.runtime, "provider_name", metrics.provider)
        metrics.thinking_effort = self.effort
        self.screen.request_render(force=True)

    def _glyphs_for(self, theme: Any) -> dict[str, str]:
        """从主题算出血形集（D200）。

        ``glyph_set()`` 本来就写好了 `ui.icon_set` 的 ASCII / Nerd 降级，但**从没被调用过**
        —— 于是"终端显示不了 unicode 字形"这条退路实际上是死的。这里给它接上唯一的调用方。

        为什么不把 `icon_set` 存成字段：它是**配置**，不是状态；换主题时按当前配置重算是
        正确的行为，缓存成字段就多了一份可能与配置不一致的事实。
        """
        from logox.tui.theme import glyph_set

        config = getattr(self.runtime, "config", None)
        icon_set = getattr(getattr(config, "ui", None), "icon_set", None)
        return glyph_set(theme, icon_set=icon_set)

    def apply_theme(self, name: str) -> str:
        """换主题。**失败时抛异常**，由命令层解释成一行提示（不崩、不退出）。

        ⚠️ 换主题要同步**三处**输入框颜色（D152-a）：
        ``input_border`` / ``input_text`` / ``input_hint``。
        漏掉任何一处，那个元素就会**停留在旧主题的颜色**上 ——
        在 `logox-dark → logox-light` 这种深浅互换里，后果是"白字白底、完全看不见"，
        而它不会报错、测试也未必抓得到（旧色在旧主题里是合法的）。

        为什么 hint 要绕一层：它在**内层** ``Editor`` 上（`BoxedEditor` 是装饰器，
        只代理协议方法，不代理这个字段）。

        ⚠️ 还要同步**字形集**（D200）：不同主题可以配不同 `[glyphs]`（例如对比度主题
        用更粗的箭头）。和颜色同理 —— 漏掉它，图标就停留在旧主题的字形上，
        而且那时**屏幕上一切正常**，只是与你改的主题文件不符。
        """
        theme = load_theme(name, self.themes_dir)
        self.theme = theme
        palette = theme.palette
        self.timeline.palette = palette
        glyphs = self._glyphs_for(theme)
        self.timeline.glyphs = glyphs
        self.timeline.invalidate()  # 卡片颜色与字形都变了 → 缓存必须作废
        self.status.palette = palette
        self.status.glyphs = glyphs

        if hasattr(self.editor, "border_style"):
            self.editor.border_style = str(palette.input_border)
        if hasattr(self.editor, "text_style"):
            self.editor.text_style = str(palette.input_text)
        inner = getattr(self.editor, "inner", None)
        if inner is not None and hasattr(inner, "hint_style"):
            inner.hint_style = str(palette.input_hint)

        self.screen.request_render(force=True)
        return theme.name

    def apply_effort(self, effort: str) -> None:
        """把档位设进内核（**可选能力，探测而非强制**）并更新显示。"""
        kernel = getattr(self.runtime, "kernel", None)
        setter = getattr(kernel, "set_thinking", None)
        if callable(setter):
            setter(effort)
        self.effort = effort
        self.reducer.metrics.thinking_effort = effort
        self.screen.request_render(force=True)

    def switch_session(self, file_path: Path | str) -> int:
        """热切换会话：清空时间线、委托装配根重构历史，并刷新终端渲染。"""
        self.clear_timeline()
        count = 0
        switcher = getattr(self.runtime, "switch_session", None)
        if callable(switcher):
            count = switcher(file_path, timeline=self.timeline)
        self._schedule_anamnesis_restore()
        self.screen.request_render(force=True)
        return count

    def new_session(self) -> Path:
        """开启全新会话：清空时间线、委托装配根重置历史与创建新会话，并刷新终端渲染。"""
        self.clear_timeline()
        creator = getattr(self.runtime, "create_new_session", None)
        new_path = Path.cwd()
        if callable(creator):
            info = creator()
            new_path = getattr(info, "file_path", info)
        self.screen.request_render(force=True)
        return Path(new_path)

    def is_current_session(self, file_path: Path | str) -> bool:
        """检查指定路径是否为当前会话。"""
        checker = getattr(self.runtime, "is_current_session", None)
        if callable(checker):
            return bool(checker(file_path))
        cur = getattr(self.runtime, "resume_file", None)
        if not cur:
            return False
        return Path(cur).resolve() == Path(file_path).resolve()

    def delete_session(self, file_path: Path | str, *, soft: bool = True) -> Path:
        """删除指定会话文件（D104）。"""
        deleter = getattr(self.runtime, "delete_session", None)
        if callable(deleter):
            return deleter(file_path, soft=soft)
        raise RuntimeError("当前运行时未装配会话删除服务")

    async def rewind(self, to_turn: int, *, force: bool = False) -> Any:
        """执行时空穿梭回滚：委托装配根还原物理文件、截断历史并更新渲染。"""
        rewinder = getattr(self.runtime, "rewind", None)
        if callable(rewinder):
            res = await rewinder(to_turn, force=force, timeline=self.timeline)
            await self._restore_anamnesis()
            self.screen.request_render(force=True)
            return res
        return None


    async def ask_continuation(self, turn: Any, iteration: int) -> bool:
        """（HITL 人在回路轮次续期）当达到 50 步工具循环预算时，弹出 Pi 风格独占提示框询问是否继续。"""
        from logox.tui.content.overlay import Choice, PickerState
        from logox.tui.render.components.overlay import PickerComponent

        title = "人在回路确认 (HITL)"
        choices = [
            Choice(value="continue", label="[1] 继续执行 50 步工具调用"),
            Choice(value="stop", label="[2] 终止执行并让助手回答"),
        ]
        state = PickerState(
            title=title,
            choices=choices,
            footer="↑↓ 切换 · Enter 确认 · Esc 终止",
        )
        component = PickerComponent(state, self.theme.palette)
        res = await self.push_overlay(component)
        if res is None:
            return False
        return res.value == "continue"

    # ------------------------------------------------------------------ #
    # 输入
    # ------------------------------------------------------------------ #

    def _on_raw_input(self, data: str) -> None:
        """终端字节 → 按键 → 组件。

        ★ D133：**读线程只负责把字节交过来，解析/派发/渲染全在事件循环线程做**。

        为什么必须这样：渲染要写终端并 `flush`（Windows 上很贵）。以前它是**在读线程里**
        同步做的 —— 一旦某个批次要画很多帧，读线程就被阻塞，而控制台的输入缓冲是
        **有限且会堆积的**：用户按住 `a` 时事件持续进队，写屏却跟不上，于是
        "松开手之后还会继续打一会儿"、退格"多删几个"。

        交回事件循环还顺带修掉一个更隐蔽的问题：**两个线程同时渲染同一个终端**
        （读线程走 `request_render(force=True)`，事件循环走定时器/事件）——
        两边各写半帧就可能把画面撕开。现在只有一条线程写终端。

        ⚠️ 只在**真的跑起来之后**才转投（`run()` 里记录线程号）：测试与
        `app.send()` 这类同步入口仍然直接走完，语义不依赖事件循环。
        """
        loop = self._loop
        if (
            loop is not None
            and self._loop_thread_id is not None
            and threading.get_ident() != self._loop_thread_id
        ):
            # 转投到事件循环：那里才是解析、派发与渲染的唯一线程
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(self._on_raw_input, data)
                return
        self._parse_and_dispatch(data)

    def _parse_and_dispatch(self, data: str) -> None:
        """真的解析并派发一批字节（**只在事件循环线程上跑**）。"""
        # 终端的"协议回应"必须**先**挑出来：它不是用户按的键。
        # 漏掉这一步的症状是——启动时输入框里凭空多出几个字符。
        # ⚠️ 只删这一段、不丢整包：回应可能与用户的第一次按键挤在同一次读取里。
        data, saw_response = strip_kitty_responses(data)
        if saw_response:
            self._activate_kitty()
        if not data:
            return
        for key in self.keys.feed(data):
            self._dispatch(key)
        if self.keys.pending == "\x1b":
            self._schedule_escape_flush()
        # ⚠️ 这里**不能**再无条件请求一次重绘（D126）。
        #
        # 以前这一行是在的，后果是**每敲一个字要画两帧**：
        # `Key 被编辑器消费 → handle_key 已经请求了一帧（force=True）`，
        # 紧接着这一行又请求第二帧（完全相同的内容）。
        # 单帧 50ms 的会话里，这意味着一次按键要付出 100ms —— 用户感受到的
        # "输入卡顿"有一半是这一帧白画的。
        #
        # （D133 之后多次请求会被**合并成同一帧**，所以这一行的危害已经变小；
        #   但仍然不恢复它 —— "谁改数据谁请求重绘"这条纪律更清楚。）
        #
        # 那为什么不干脆删掉、改由"谁状态变了谁请求重绘"：因为确实有几条路
        # 只改内容、不画屏（Ctrl+C 的提示、浮层 "/login" 流程里的提示行）。
        # 所以规矩改成：**这些路径自己负责请求重绘**（见 `_interrupt`），
        # 而这里不再当"背锅侠"——否则它每帧都会背一次。
        #
        # 注意：
        #   * 没有被任何组件消费的按键（比如空闲时的 `Esc`）本来就不改内容，不需要画；
        #   * `keys.feed` 返回空（半个转义序列到了、剩下的还在路上）也不需要画。
        # 两者都由"谁改数据谁 `request_render`"这条已有的纪律兜住。

    def _dispatch(self, key: Key) -> None:
        if self.anamnesis is not None:
            editing = key.printable or key.name in {"paste", "backspace", "delete", "enter"}
            editing = editing or (key.ctrl and key.name in {"u", "k", "w", "y"})
            self.anamnesis.note_activity("input" if editing else "view")
        if key.ctrl and key.name == "c":
            self._interrupt()
            return
        if key.ctrl and key.name == "d" and not self.screen.has_overlay():
            # 若当前有激活的浮层（如 /resume 历史会话列表），优先把 Ctrl+D 交由浮层处理
            self.stop()
            return
        if key.ctrl and key.name == "o":
            # D125：`Ctrl+O` 只切**工具卡 + diff**（归一类"动作产物"）；
            # 思考链改用 `Ctrl+T`，见下面那条。
            self.timeline.buffer.toggle_expand_tools()
            self.screen.request_render(force=True)
            return
        if key.ctrl and key.name == "b":
            # ★ D176：双轨标记开关（与 Ctrl+O / Ctrl+T 同型：组件与 app **都**处理，
            #   因为焦点在时间线组件上时按键先到它那里）
            self.timeline.buffer.toggle_track()
            self.screen.request_render(force=True)
            return
        if key.ctrl and key.name == "t":
            # 普通思考与工具开关独立；入梦卡片与普通思考共用 Ctrl+T。
            self.timeline.buffer.toggle_expand_reasoning()
            self.screen.request_render(force=True)
            return
        # `Esc` 的优先级：**先关浮层，再中断生成**。
        #
        # 顺序反过来会很难受：浮层开着时按 Esc 应该是"关掉它"（用户明确在等这个），
        # 而不是把后台正在跑的那一回合顺手停掉。
        # 这一条与状态行右端的提示（"esc to interrupt"）是配套的——提示里
        # 写着一个按了没反应的键，比少写一个键糟糕得多。
        if key.name == "escape" and not self.screen.has_overlay() and (self._busy or self.timeline.is_active):
            self.runtime.kernel.cancel()
            self._busy = False
            self.timeline.buffer.clear_active_status()
            self.timeline.buffer.add_notice("已中断", token="warning")
            self.screen.request_render(force=True)
            return
        # ★ D162：补全列表**不抢焦点**（抢了用户就打不了字），所以它的按键
        #   必须在**交给编辑器之前**在这里拦下来。`↑↓` 到底归谁，只有在这一处才看得清。
        if self._completion is not None and self._handle_completion_key(key):
            return

        self.screen.handle_key(key)
        # 编辑器内容可能刚变（输入/退格/粘贴）⇒ 重算候选（纯计算，代价可忽略）
        self._refresh_completion()

    # ------------------------------------------------------------------ #
    # `/` 命令补全（D162）
    # ------------------------------------------------------------------ #

    def _handle_completion_key(self, key: Key) -> bool:
        """列表开着时消费补全相关按键；返回 True 表示已处理（不再透传）。"""
        state = self._completion
        assert state is not None
        if key.name == "escape":
            self._close_completion()
            return True
        if key.name in ("up", "down"):
            # ★ D175：复用 picker 的状态机（`move` 环绕、返回 None）——
            #   不能再按返回值判断"有没有变化"，直接刷新即可（列表本来就要重画窗口）。
            state.move(-1 if key.name == "up" else 1)
            self._show_completion()
            return True
        if key.name in ("tab", "enter"):
            # 接受的文本 = `/命令名 `（带尾随空格：它让"光标前不得有空白"的触发判据失效，
            # 于是列表自然收起，不需要额外的"已接受"状态）
            current = state.current
            text = f"/{current.value} " if current is not None else ""
            if text:
                apply_to_editor = getattr(self.editor, "apply_completion", None)
                if callable(apply_to_editor):
                    apply_to_editor(text)
            self._close_completion()
            if key.name == "tab":
                self.screen.request_render(force=True)
                return True
            # `Enter`：**先补全、再提交**（否则 `/mo` + Enter 会得到"未知命令 /mo"）。
            # 关闭列表后按 Enter 才是"按字面提交"—— 见设计文档 §3.4（与 Pi 的字面行为有差异）
            self._on_submit(self.editor.text)
            return True
        return False

    def _refresh_completion(self) -> None:
        """重算候选并显示/隐藏列表（**纯计算**，每帧最多一次）。"""
        from logox.tui.content import completion as completion_module

        # 模态浮层（如 `/model` 选择框）开着时让位，避免两个交互框叠在一起。
        # （补全自己**不是浮层**，所以这里可以直接用 `has_overlay()`，不必再排除自己。）
        if self.screen is not None and self.screen.has_overlay():
            self._close_completion()
            return
        editor = self.editor
        cursor_getter = getattr(editor, "cursor", None)
        if cursor_getter is None:
            self._close_completion()
            return
        row, col = cursor_getter
        state = completion_module.completion_for(editor.text, row=row, col=col)
        if state is None:
            self._close_completion()
            return
        self._completion = state
        self._show_completion()

    def _show_completion(self) -> None:
        """把候选提示框挂到布局上（**不是浮层** —— 见 `AppLayout.render` 的说明）。"""
        from logox.tui.render.components.completion import CompletionComponent

        if self._completion is None:
            return
        self.root.completion = CompletionComponent(self._completion, self.theme.palette)
        self.screen.request_render(force=True)

    def _close_completion(self) -> None:
        if getattr(self.root, "completion", None) is not None:
            self.root.completion = None
            self.screen.request_render(force=True)
        self._completion = None


    def _interrupt(self) -> None:
        """``Ctrl+C``：正在跑就打断；空闲时要**按两下**才退出（D69）。

        ⚠️ 为什么必须是两下（这是用户亲自提的要求，别再改回去）：
        ``Ctrl+C`` 是终端里"复制"的常用键。而我们在原始模式里把按键收了过来，
        所以**一次误按就等于退出整个会话**——用户丢掉的是正在看的回答。
        两下退出把"复制"与"退出"分开：第一下只是提醒，第二下才真退。

        窗口是 :data:`CTRL_C_WINDOW_S`，时间用 ``monotonic`` 量（墙上时钟会被
        系统对时/夏令时改动影响，用它算间隔会得到负数或巨大的值）。
        """
        if self._busy or self.timeline.is_active:
            self.runtime.kernel.cancel()
            self._busy = False
            self.timeline.buffer.clear_active_status()
            self._ctrl_c_armed_at = None
            self.timeline.buffer.add_notice("已中断", token="warning")
            # 见 `_on_raw_input` 的说明：改内容的路径**自己负责**请求重绘。
            self.screen.request_render(force=True)
            return
        now = time.monotonic()
        armed = self._ctrl_c_armed_at
        if armed is not None and (now - armed) <= CTRL_C_WINDOW_S:
            self.stop()
            return
        self._ctrl_c_armed_at = now
        self.timeline.buffer.add_notice(
            "再按一次 Ctrl+C 退出（Ctrl+D 也可以；Ctrl+C 的第一次按键不算数，"
            "是为了不误伤终端的「复制」）",
            token="text_faint",
        )
        self.screen.request_render(force=True)

    def _schedule_escape_flush(self) -> None:
        """孤立的 ``ESC`` 要等一小会儿才能确定（见 `keys.ESC_TIMEOUT_MS`）。"""
        if self._loop is None:
            return
        if self._pending_escape_handle is not None:
            self._pending_escape_handle.cancel()
        self._pending_escape_handle = self._loop.call_later(
            ESC_TIMEOUT_MS / 1000.0, self._flush_escape
        )

    def _flush_escape(self) -> None:
        self._pending_escape_handle = None
        key = self.keys.flush_pending_escape()
        if key is not None:
            self._dispatch(key)
            self.screen.request_render()

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    async def run(self) -> None:
        """跑起来：清屏、协商键盘协议、公示会话、开始画、等 `stop()`。"""
        self._loop = asyncio.get_running_loop()
        # ★ 记下事件循环线程：读线程交上来的字节要转投到这里（D133）
        self._loop_thread_id = threading.get_ident()
        self._clear_screen()
        self._enable_keyboard_protocols()
        self.screen.start()
        self._report_terminal_warnings()
        if self.session_start is not None:
            # ★ **必须在应用自己的事件循环内发布**（这里正是那个循环）。
            # 若在 `cli.py` 里用 `asyncio.run` 发一次再启动应用，那是**另一个循环**——
            # 而总线内部的非阻塞消费者任务绑定在创建它的循环上，跨循环会**静默失灵**：
            # 时间线拿不到 SessionStart，界面看起来正常，只是少了一行。
            self._spawn(self.runtime.bus.publish(self.session_start))
        self._stop_event = asyncio.Event()
        self._tickers = self.start_tickers()
        # ★ D188：**后台**抓一次本地（免鉴权）端点的模型清单。
        #   为什么放在这里：只有本应用的循环能安全跑异步（理由同上面 SessionStart）；
        #   为什么用后台：`/model` 弹窗必须保持"零网络、点开即出"（D65 的不变量），
        #   而本机端点又不值得让启动多等一秒。
        #   ★ 挂进 `_tickers` 是为了退出时能**取消**它：否则一个还没回来的请求
        #   会在关闭时留下"Task was destroyed but it is pending"这类噪声。
        refresher = getattr(self.runtime, "refresh_local_models", None)
        if callable(refresher):
            refresh_task = self._spawn(refresher())
            if refresh_task is not None:
                self._tickers.append(refresh_task)
        try:
            if self.anamnesis is not None:
                await self._restore_anamnesis()
                await self.anamnesis.start_background()
            await self._stop_event.wait()
        finally:
            if self.anamnesis is not None:
                if self._anamnesis_restore_task is not None:
                    self._anamnesis_restore_task.cancel()
                    await asyncio.gather(self._anamnesis_restore_task, return_exceptions=True)
                await self.anamnesis.aclose()
            for ticker in self._tickers:
                ticker.cancel()
            self._tickers = []
            self._restore_screen()

    def _clear_screen(self) -> None:
        """进界面时**清空终端**（屏幕 + 回滚缓冲），像 Claude Code / Pi agent 那样。

        为什么要清：用户打开一个 agent 界面时想要的是"一块干净的工作区"，
        而不是在 shell 的历史输出末尾接上一段新内容。清掉之后，界面从第一行开始写，
        下面所有内容都是这次会话自己的——往上滚也只滚自己的历史。

        **这三个序列不是猜的**，与 Pi 的主屏渲染器**逐字节相同**
        （``@earendil-works/pi-tui/dist/tui-main-screen.js``）：:

            output.append("\\x1b[2J\\x1b[H\\x1b[3J");  // Clear screen, home, then clear scrollback

        顺序也有讲究：先 ``2J`` 清可见屏 → ``H`` 把光标放回左上 →
        再 ``3J`` 清回滚缓冲（``3J`` 不动光标，所以顺序错了会留下残影）。

        ⚠️ **代价要说明**：``\\x1b[3J`` 会连**回滚缓冲**一起清掉，也就是
        **进入 logox 之前那些输出就再也翻不回来了**。这是用户明确要求的取舍
        （"就像 ClaudeCode Pi agent 的做法一样"）。
        如果哪天想保留进界面之前的输出，把 :data:`CLEAR_ALL` 换成
        ``CLEAR_VIEWPORT``（原地擦除当前屏、不动回滚缓冲）即可。

        为什么放在 ``run()`` 的最开头（而不是并进首帧的同步输出块）：
        清屏必须**早于**任何界面字节，否则"要清的内容"会和新界面同时存在一瞬。
        而它与随后的首帧之间只隔着两次**非阻塞**的 ``terminal.write``
        （Kitty 协议是"先问后启用"，不等待回应），没有肉眼可见的空窗——
        所以**没有为此改渲染路径**（没有证据就不动已经绿了的代码）。
        """
        self.terminal.write(CLEAR_ALL)

    def start_tickers(self) -> list[Any]:
        """启动随界面运行的后台任务（目前只有流式合并 ticker）。

        为什么做成公开方法：**测试要驱动的是真实链路**，而"流式增量谁来落块"
        正是这条链路上最容易漏的一环（漏了整段回答会在回合结束时一次性蹦出来）。
        如果只有 `run()` 里才能启动它，所有测试就都测不到那条路径。

        它同时接上**延迟重绘的调度器**：节流挡下的那一次重绘需要一个定时器补画，
        否则快速输入会被静默吞掉（见 `Screen.request_render` 的说明）。
        """
        if self._loop is None:
            return []
        loop = self._loop

        def _threadsafe_defer(delay: float, callback: Callable[[], None]) -> Any:
            if not loop.is_running():
                return None

            class _DeferHandle:
                def __init__(self) -> None:
                    self._cancelled = False
                    self._timer_handle: asyncio.TimerHandle | None = None

                def _schedule(self) -> None:
                    if not self._cancelled:
                        self._timer_handle = loop.call_later(delay, self._run)

                def _run(self) -> None:
                    if not self._cancelled:
                        callback()

                def cancel(self) -> None:
                    self._cancelled = True
                    if self._timer_handle is not None:
                        loop.call_soon_threadsafe(self._timer_handle.cancel)

            defer_handle = _DeferHandle()
            loop.call_soon_threadsafe(defer_handle._schedule)
            return defer_handle

        self.screen.on_defer = _threadsafe_defer
        return [self._loop.create_task(self._stream_ticker())]

    async def _stream_ticker(self) -> None:
        """按 ``ui.stream_fps`` 把流式增量通过平滑器微步步进并渲染（D186）。

        为什么需要它：内核每收到一个增量就发一条 `ModelDelta`，一回合几百条。
        若每条都逐帧重渲染，一旦超出视口高，每一帧都会触发整屏 ANSI 重绘，
        瞬间塞爆 Windows ConPTY 管道，导致 Python 底层 sys.stdout.flush() 卡死数秒；
        平滑器按 30~60 FPS 弹性释出字符，少积压时逐字吐出呈现极佳打字机质感，
        大突发时自适应提速平滑追平。
        """
        config = getattr(self.runtime, "config", None)
        fps = int(getattr(getattr(config, "ui", None), "stream_fps", 30) or 30)
        interval = 1.0 / max(5, min(60, fps))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + interval
        while self.screen._running:  # noqa: SLF001
            await asyncio.sleep(max(0.0, deadline - loop.time()))
            now = loop.time()
            deadline += interval
            if deadline <= now:
                deadline = now + interval
            committed = self.timeline.step()
            is_active = self.timeline.is_active
            anamnesis_changed = False
            if self.anamnesis is not None and self.anamnesis.is_active and self._anamnesis_block is not None:
                anamnesis_changed = self._anamnesis_block.card.tick()
                if anamnesis_changed:
                    self.timeline.buffer.invalidate(layout=False)
            if committed or is_active or anamnesis_changed:
                self.screen.request_render()

    def _report_terminal_warnings(self) -> None:
        """把终端驱动的降级警告显示到时间线上。

        为什么非显示不可：`Win32Terminal` 在旧版 Windows 上**不会抛异常**，
        只是记一条警告并继续（"启动就崩"比"方向键不工作"更糟）。
        于是"为什么方向键没反应"这件事**只能靠这条警告解释**——
        不显示的话，用户看到的是一个没人解释的坏掉的方向键。
        ``start()`` 之后才有内容，所以只能在这里读。
        """
        for warning in getattr(self.terminal, "warnings", []) or []:
            self.timeline.buffer.add_notice(warning, token="warning")
        if getattr(self.terminal, "warnings", None):
            self.screen.request_render(force=True)

    def stop(self) -> None:
        """退出：退订、停终端、恢复屏幕。**幂等**（可以安全地重复调用）。

        幂等很关键：`stop()` 会被"用户按 Ctrl+D"、"`/exit`"、"读线程结束"
        三条路调用，而其中第一条是在**读线程**里发生的。
        """
        if self.anamnesis is not None:
            self.anamnesis.request_close()
        self.timeline.buffer.clear_active_status()
        for handle in self._unsubscribe:
            self.runtime.bus.unsubscribe(handle)
        self._unsubscribe.clear()
        # 注销权限提问者：退出之后没有人能回答弹窗，决策器必须回到"拒绝"这条安全路径
        decider = getattr(self.runtime, "permission_decider", None)
        if decider is not None and getattr(decider, "prompter", None) is self:
            decider.prompter = None
        self._cancel_kitty_timer()
        self.screen.stop()
        if self._stop_event is not None:
            if self._loop is not None and self._loop.is_running():
                self._loop.call_soon_threadsafe(self._stop_event.set)
            else:
                self._stop_event.set()

    # -- 键盘协议协商 ---------------------------------------------------- #

    def _enable_keyboard_protocols(self) -> None:
        """打开括号粘贴，并**先问**终端支不支持 Kitty 键盘协议。

        为什么要"先问"：``Shift+Enter`` 在传统协议里与 ``Enter`` 是**同一个字节**，
        所以"在输入框里换行"这个动作需要终端配合。可选的配合方式有两种：

        ==================== ==========================================================
        Kitty 键盘协议       给每个按键附带修饰位，最准确。支持的终端：kitty / WezTerm / Ghostty。
        xterm 修改其他键      ``CSI 27 ; <修饰> ; <码点> ~``。xterm / tmux / Windows Terminal 支持。
        ==================== ==========================================================

        直接发"启用 Kitty"是不行的——**不支持的终端会静默忽略**，于是我们错以为
        拿到了修饰位。先发查询（``CSI ? u``），有回应才启用；150ms 没回应就退到
        xterm 那条路。这与 Pi 的做法一致（``queryAndEnableKittyProtocol``）。
        """
        self.terminal.write(ENABLE_BRACKETED_PASTE)
        self.terminal.write(KITTY_QUERY)
        if self._loop is None:  # pragma: no cover - 只在没有事件循环时（测试）
            return
        self._kitty_timer = self._loop.call_later(KITTY_FALLBACK_DELAY_S, self._enable_fallback_keys)

    def _activate_kitty(self) -> None:
        """终端回应了查询 → 启用 Kitty 键盘协议。"""
        if self._kitty_active:
            return
        self._cancel_kitty_timer()
        self._kitty_active = True
        self.terminal.write(ENABLE_KITTY_KEYBOARD)

    def _enable_fallback_keys(self) -> None:
        """没有 Kitty 回应 → 退到 xterm 的 ``modifyOtherKeys`` 模式 2。"""
        self._kitty_timer = None
        if self._kitty_active or self._modify_other_keys_active:
            return
        self._modify_other_keys_active = True
        self.terminal.write(ENABLE_MODIFY_OTHER_KEYS)

    def _cancel_kitty_timer(self) -> None:
        if self._kitty_timer is not None:
            self._kitty_timer.cancel()
            self._kitty_timer = None

    def _restore_screen(self) -> None:
        """退出时把终端恢复原状。

        ⚠️ **每一步都不能漏**，漏了会污染用户的 shell：

        * 括号粘贴不关 → 之后在 shell 里粘贴的内容会带上不可见的包装序列；
        * ``modifyOtherKeys`` 不关 → 之后用户按 ``Shift+Enter``，shell 收到的是 ``CSI 27;2;13~``
          这串乱码，而不是回车；
        * Kitty 协议不关 → 同理。
        """
        self._cancel_kitty_timer()
        if self._kitty_active:
            self.terminal.write(DISABLE_KITTY_KEYBOARD)
            self._kitty_active = False
        if self._modify_other_keys_active:
            self.terminal.write(DISABLE_MODIFY_OTHER_KEYS)
            self._modify_other_keys_active = False
        self.terminal.write(DISABLE_BRACKETED_PASTE)
        self.terminal.write("\x1b[?25h")  # 兜底：确保退出后光标是可见的

    # -- 测试入口 ------------------------------------------------------- #

    def frame_text(self) -> str:
        self.screen.render_now()
        return self.screen.frame_text()

    def send(self, data: str) -> None:
        """投递**原始字节**（走真实的解析路径，与终端推来的东西完全一样）。"""
        self._on_raw_input(data)
        self.screen.render_now()

    def press(self, *keys: Key | str) -> None:
        """投递**已解析的按键**（测试与脚本化用：`Escape`、`Enter` 这类没有独立字节形态的键）。

        ⚠️ 刻意**不**用 `KeyParser` 合成字节：``Escape`` 在真实终端里要等 30ms
        超时才能确定（见 `ESC_TIMEOUT_MS`），测试里没必要为这个等。

        传单个可打印字符（``press("3")``）时会自动补上 ``char`` —— 否则
        ``Key("3")`` 既不是"字符 3"也不是任何具名键，**按下去什么都不会发生**，
        而这种"静默无效"在测试里表现为**挂起**（实测踩到：预览脚本因此卡死）。
        """
        for key in keys:
            if isinstance(key, str):
                key = Key(key, char=key) if len(key) == 1 and key.isprintable() else Key(key)
            self._dispatch(key)
        self.screen.render_now()

    async def submit(self, text: str) -> None:
        """（测试用）执行一句"用户输入"并**等它跑完**（命令的浮层流程也能等到）。"""
        if text.startswith("/"):
            await self._run_command(text)
        else:
            await self._start_turn(text)


def run_inline(
    runtime: Any,
    *,
    terminal: Terminal | None = None,
    session_start: Any | None = None,
) -> int:
    """同步入口：给 CLI 用。"""
    app = InlineApp(runtime=runtime, terminal=terminal, session_start=session_start)
    try:
        asyncio.run(app.run())
    except KeyboardInterrupt:  # pragma: no cover - 用户强杀
        return 130
    finally:
        # 恢复终端状态**绝不能漏**：漏了用户的光标与回显就乱了。
        # `Screen.stop()` 已经调过 `terminal.stop()`，这里只兜底一次（幂等）。
        app.terminal.stop()
        if hasattr(runtime, "close") and callable(runtime.close):
            runtime.close()
    return 0
