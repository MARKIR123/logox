"""差分渲染器（D80 / MODULE_tui_render §4）。

这是整个重写的核心，也是"流畅"的来源。算法与 Pi 的 ``TUI.doRender`` 一致：

1. **把组件渲染成行**（``render(width) -> list[Text]``）；
2. **与上一帧逐行比较**，找出 ``first_changed`` / ``last_changed``；
3. **只重画那一段**：移到 ``first_changed``，从那里清到屏幕末尾，写回去。

为什么"逐行比较"值得
--------------------
比较是 O(行数) 次**字符串比较**，重画是 O(行数 × 宽度) 次终端写入——
便宜一个数量级。而 AI agent 界面最常见的动作是**末尾追加**，
于是"变化区间"通常只有最后几行。

三种策略（与 Pi 一致）
----------------------
====================== ============================================================
条件                    策略
====================== ============================================================
首帧                    直接输出全部行（**不清屏**，假定屏幕干净）
宽度或高度变了          清屏 + 全量重画（折行位置全变，无法增量）
内容变短了              清屏 + 全量重画（否则旧行会残留在下面）
通常情况                移到 ``first_changed``，清到末尾，重画变化段
====================== ============================================================

不闪靠"同步输出"
----------------
每一帧的字节都包在 ``\\x1b[?2026h ... \\x1b[?2026l`` 里。终端收到**完整一对**
才整体刷新，因此看不到中间态。不支持的终端会忽略这两个序列——功能不受影响。

与 Textual 的关键差别（这才是重写的意义）
----------------------------------------
Textual 走**备用屏**（``\\x1b[?1049h``）：拿走回滚历史，换来"画面完全由应用控制"。
本渲染器走**主屏**：内容进入终端自己的回滚历史，于是
**滚动、选中、复制全部由终端免费提供**——这三件事我们前后打了 8 轮补丁。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from rich.text import Text

from logox.tui.render.ansi import strip_ansi, text_to_ansi, visible_width
from logox.tui.render.component import Component, Container, fit_lines
from logox.tui.render.keys import Key
from logox.tui.render.terminal import Terminal

__all__ = ["MIN_RENDER_INTERVAL_MS", "OverlayHandle", "Screen"]

#: 帧率上限（≈60fps）。Pi 用同一量级。
#:
#: 为什么需要它：流式增量一回合几百条，不节流的话每一条都触发一次渲染。
#: 节流之后的语义是"**最多**每 16ms 重画一次"，而不是"每 16ms 一定重画一次"
#: （真正决定要不要画的是"内容有没有变"）。
MIN_RENDER_INTERVAL_MS = 16.0

#: 同步输出：一对序列之间是一次原子刷新
SYNC_BEGIN = "\x1b[?2026h"
SYNC_END = "\x1b[?2026l"

#: 清屏（含清回滚历史）—— 只在宽度/高度变化或内容变短时用
CLEAR_ALL = "\x1b[2J\x1b[H\x1b[3J"

#: 当前帧丢弃旧帧的**不变量**：一帧的字节必须完整成对
ASSERT_SYNC_PAIRS = True


@dataclass
class OverlayHandle:
    """一个浮层的句柄（对应 Pi 的 ``OverlayHandle``）。"""

    component: Component
    options: dict[str, Any] = field(default_factory=dict)
    hidden: bool = False
    #: 浮层是否**自己**吃按键（``non_capturing=True`` 表示不吃，如纯展示的调试面板）
    non_capturing: bool = False

    def hide(self) -> None:
        self.hidden = True

    def set_hidden(self, hidden: bool) -> None:
        self.hidden = hidden

    def is_hidden(self) -> bool:
        return self.hidden


class Screen:
    """行式差分渲染器。

    持有终端、根组件、浮层栈与焦点；对外只暴露"加组件 / 收按键 / 请求重绘"。
    """

    def __init__(self, terminal: Terminal, *, min_interval_ms: float = MIN_RENDER_INTERVAL_MS) -> None:
        self.terminal = terminal
        self.min_interval_ms = min_interval_ms
        self.root = Container()
        #: 每帧已渲染的行 —— **带 ANSI 样式的字符串**（不是纯文本，见 `text_to_ansi`）
        self._lines: list[str] = []
        #: ``self._lines`` 每一行对应的**源行对象**（与它同一下标、同生同死）。
        #:
        #: 用来跳过多余的序列化：组件如果返回的是**同一个** `Text` 对象（时间线的前缀
        #: 就是这么做的，见 `render/app.py` 的 `TimelineComponent`），它的 ANSI 字节
        #: 一定与上一帧逐字节相同，没必要再算一遍。
        #:
        #: ⚠️ 这条优化依赖一个**组件契约**：`render()` 返回的 `Text` 一经交出，
        #: 组件**不得再原地修改**（要改就返回新对象）。原地改的话我们会发出旧字节，
        #: 而症状是"屏幕上还是旧内容"——所以 ``ansi_hits`` 这个计数器是给测试用的，
        #: 它让"到底复用了几行"变成可断言的事实，而不是一个假设。
        self._serialized_rows: list[Text] = []
        #: 上一帧的行（**比较的基准**）
        self._previous: list[str] = []
        self._previous_width = 0
        self._previous_height = 0
        #: 上一帧视口的**首行在缓冲区里的行号**（见 `_render_lines` 的说明）
        self._previous_viewport_top = 0
        #: 硬件光标当前在缓冲区的哪一行（**实际位置**，与"内容末行"不是一回事）
        self._hardware_cursor_row = 0
        #: 历史上渲染过的最大行数（判断"终端工作区"用）
        self._max_lines_rendered = 0
        #: 重绘请求（节流用）
        self._render_requested = False
        self._last_render_at = 0.0
        #: **是否已经有一帧排在调度器里**（D133）
        #:
        #: 这是"按住一个键不会卡"的关键：一场输入里可能有几十个按键，
        #: 每个按键都调一次 `request_render`，但它们**合并成同一帧**。
        #: （Pi 的做法完全相同：`renderRequested` 去重 + `process.nextTick`。）
        self._frame_pending = False
        #: 这一帧是否要求**跳过 16ms 节流**（`force=True` 提升的优先级）
        self._force_requested = False
        #: 已排队的那一帧当时是用多少延迟排的（用来判断“能不能再提前”）
        self._pending_delay_s = 0.0
        self._defer_handle: Any = None
        #: "稍后再画一次"的调度器，由装配根注入（``loop.call_later``）。
        #: 签名 ``(delay_seconds, callback) -> handle``。为 ``None`` 时立刻重绘。
        self.on_defer: Callable[[float, Callable[[], None]], Any] | None = None
        self.overlays: list[OverlayHandle] = []
        self.focus: Component | None = None
        #: 原始输入回调（由上层注入：`Screen` 不解析键位，见 `_on_input`）
        self.on_input: Callable[[str], None] | None = None
        self._running = False
        #: IME 光标的位置 ``(行, 列)``（来自组件的零宽标记；``None`` = 本帧没有）
        self._cursor_pos: tuple[int, int] | None = None
        #: 硬件光标**当前**的列，以及它是否可见。
        #:
        #: 为什么要记账：每帧都无条件地"移动光标 + 显示/隐藏"会写出十几个字节，
        #: 于是"内容没变就一个字节都不写"这条性质就没了。记下当前位置之后，
        #: 位置没变就跳过——**这才是"空闲时零开销"**。
        self._cursor_col = 0
        self._cursor_visible = False
        #: 是否把硬件光标显示出来并移到输入位置。
        #:
        #: 为什么要显示：编辑器**自己不画光标**（它只发一个零宽标记，
        #: 见 `components/editor.py`）。不显示硬件光标的话，用户会看到
        #: "输入框里没有光标"——以为程序卡住了。显示真光标还有个好处：
        #: 它是终端原生的，会自然闪烁，而且中文输入法的候选窗会跟着它走。
        self.show_hardware_cursor = True
        #: 统计（测试与 /debug 用）。``ansi_hits`` / ``ansi_misses`` 是 D126 的度量：
        #: 每帧重新序列化的行数应当只与**变化量**成正比，而不是与会话长度成正比。
        self.stats = {
            "frames": 0,
            "full_redraws": 0,
            "skipped": 0,
            "ansi_hits": 0,
            "ansi_misses": 0,
        }

    # ------------------------------------------------------------------ #
    # 组件与焦点
    # ------------------------------------------------------------------ #

    def add(self, component: Component) -> None:
        self.root.add(component)

    def remove(self, component: Component) -> None:
        self.root.remove(component)

    def set_focus(self, component: Component | None) -> None:
        """切换焦点组件，并**同步它自己的 ``focused`` 标记**（对齐 Pi 的 ``setFocus``）。

        为什么必须同步：编辑器只在 ``focused`` 为真时才发 IME 光标标记。
        浮层打开时不把它置假的话，**两个组件都会发标记**，而渲染器只能取其中一个
        （从下往上找，取到编辑器的）——于是浮层里的输入框拿不到硬件光标，
        中文输入法候选窗会飘到输入框那一行去。
        """
        previous = self.focus
        if previous is not None and previous is not component:
            _set_focused(previous, False)
        self.focus = component
        if component is not None:
            _set_focused(component, True)

    def focused_editor(self, component: Component) -> None:
        """把焦点交回某个组件（浮层关闭时用）。"""
        self.set_focus(component)

    # ------------------------------------------------------------------ #
    # 浮层（对齐 Pi 的 showOverlay）
    # ------------------------------------------------------------------ #

    def show_overlay(self, component: Component, **options: Any) -> OverlayHandle:
        handle = OverlayHandle(component=component, options=options)
        self.overlays.append(handle)
        if not options.get("non_capturing", False):
            self.set_focus(component)
        self.request_render(force=True)
        return handle

    def hide_overlay(self) -> None:
        """隐藏最上面那一个可见浮层。"""
        for handle in reversed(self.overlays):
            if not handle.hidden:
                handle.hidden = True
                self.set_focus(None)
                self.request_render(force=True)
                return

    def pop_overlay(self, handle: OverlayHandle) -> None:
        """把某个浮层**从栈里摘掉**（关闭时用；区别于"临时隐藏"）。"""
        if handle in self.overlays:
            self.overlays.remove(handle)
        self.request_render(force=True)

    def has_overlay(self) -> bool:
        return any(self._is_visible(handle) for handle in self.overlays)

    def _is_visible(self, handle: OverlayHandle) -> bool:
        if handle.hidden:
            return False
        predicate = handle.options.get("visible")
        if callable(predicate):
            return bool(predicate(self.terminal.columns, self.terminal.rows))
        return True

    # ------------------------------------------------------------------ #
    # 输入
    # ------------------------------------------------------------------ #

    def handle_key(self, key: Key) -> None:
        """把按键交给焦点组件；若焦点组件未消费，则向上冒泡给根组件。"""
        target = self.focus if self.focus is not None else self.root
        consumed = target.handle_input(key)
        if not consumed and target is not self.root:
            consumed = self.root.handle_input(key)
        if consumed:
            # ★ 动静分流（D123）+ **合并成一帧**（D133）：
            #
            # * `force=True` 的作用是"**把这一帧的延迟压到 0**"（不被 16ms 节流推到下一格）；
            # * 而**渲染本身**仍由 `request_render` 排到事件循环的下一轮 —— 于是
            #   "读线程一次交上来一个批次（最多 32 个按键）"只会画**一帧**。
            #
            # ⚠️ 这里**不能**改成"就在这一层画"：以前就是那样，后果是 32 个按键画 32 帧、
            # 而且全在读线程上做 I/O，把控制台输入缓冲拖爆（用户："松开 a 还在继续打"）。
            self.request_render(force=True)

    def start(self) -> None:
        self._running = True
        self.terminal.start(self._on_input, self.request_render)
        self.request_render(force=True)

    def stop(self) -> None:
        self._running = False
        self._cancel_deferred()
        self._park_cursor()
        self.terminal.stop()

    def _park_cursor(self) -> None:
        """把光标停到**界面下方**，让 shell 的提示符接着往下写。

        为什么必须做：界面的光标停在输入框里（那是 IME 候选窗需要的）。
        直接退出的话，shell 提示符会**盖在输入框那一行上**。
        （对齐 Pi 的 ``TUI.stop()``。）
        """
        if not self._lines:
            return
        target_row = len(self._lines)  # 内容末行的**下一行**
        delta = target_row - self._hardware_cursor_row
        buffer = ""
        if delta > 0:
            buffer += f"\x1b[{delta}B"
        elif delta < 0:
            buffer += f"\x1b[{-delta}A"
        buffer += "\r\n"
        self.terminal.write(buffer)
        self._hardware_cursor_row = target_row
        self._cursor_col = 0

    def _on_input(self, data: str) -> None:
        """终端原始字节 → 交给上面的层去解析。

        `Screen` **不认识**键位语义（那是 `keys.py` 与 `app.py` 的事），
        这里只做转发。"谁负责解析"只有一处，符合本模块"每层只做一件事"的分工。
        """
        handler = self.on_input
        if callable(handler):
            handler(data)

    # ------------------------------------------------------------------ #
    # 渲染
    # ------------------------------------------------------------------ #

    def request_render(self, *, force: bool = False) -> None:
        """请求一帧重绘。

        ★ D133（参考实现 Pi）：**不在调用栈里同步渲染，且同一轮里的多次请求合并成一帧。**

        为什么这是本轮最重要的一条：读线程一次会交上来**一个批次**（最多 32 条按键记录），
        而每个按键都会走到这里。以前 `force=True` 是**同步 `render_now()`**，于是：

        ===================== ====================================================
        一个批次 32 个按键     旧：**32 次**全帧计算 + 32 次终端写+flush
                             新：**1 次**
        ===================== ====================================================

        而写终端要 flush（Windows 上很贵）。读线程被渲染阻塞 → 控制台的输入缓冲
        越积越多 → 用户松开 `a` 之后**还会继续打一会儿**、退格**多删几个**；
        中文输入法提交“如果”时被拆成两帧，看着就是一个字一个字蹦。

        Pi 的 `requestRender` 正是这个形状：`renderRequested` 去重 + `process.nextTick`
        把真正的绘制推到下一轮（**连 `force` 也不例外**）。

        ⚠️ **节流只能"推迟"，不能"丢掉"**：被推迟的那一帧一定会在后面的时间片兑现
        （这条是从"打字快会吞字"那个缺陷里换来的，别改回去）。

        ``force=True`` 的语义：**跳过 16ms 节流**（浮层、退出前清屏这类"必须尽快看到"的
        场景）—— 但仍然是"排到下一轮"，不是"现在就在这一层调用栈里画"。

        ⚠️ 没有调度器时（测试、或还没 `run()` 的应用）**立刻画**：保留旧语义、
        也让"渲染策略"这种纯逻辑不依赖终端状态。
        """
        self._render_requested = True

        if self.on_defer is None:
            # 没有调度器 → 立刻画（测试与未启动的应用走这条）
            self._render_requested = False
            self.render_now()
            return

        if force:
            self._force_requested = True
            if self._frame_pending:
                if self._pending_delay_s <= 0.0:
                    # ★ 已经是最快那一档 → **直接合并**（一个输入批次里每个按键都 force，
                    #   如果每次都快排一遍，32 个按键就会排 32 帧 —— 那就白改了）。
                    return
                # 排队中的那一帧不够快（带着一个节流延迟）→ 取消它、用 0 延迟重排。
                # 仍然是**一帧**：被取消的那个不会再跑。
                self._cancel_deferred()
                self._force_requested = True  # ⚠️ 必须在 cancel 之后：它会清掉这个标记
        elif self._frame_pending:
            return  # ★ 合并：这一轮里不管请求多少次，都只画一帧

        now = time.monotonic() * 1000.0
        elapsed = now - self._last_render_at
        if force or elapsed >= self.min_interval_ms:
            delay_s = 0.0
        else:
            delay_s = max(0.0, (self.min_interval_ms - elapsed) / 1000.0)

        self._frame_pending = True
        self._pending_delay_s = delay_s
        self._defer_handle = self.on_defer(delay_s, self._run_pending_frame)

    def _run_pending_frame(self) -> None:
        """兑现已排队的那一帧（定时器到点时调用）。

        ⚠️ 这里**不再重新检查节流窗口**：`on_defer(delay, …)` 的契约就是"到点叫我"，
        叫了就该画。再检查一遍的后果是——用假调度器（测试）时回调会被无限顺延，
        一帧都画不出来。
        """
        self._frame_pending = False
        self._defer_handle = None
        self._pending_delay_s = 0.0
        self._render_requested = False
        self._force_requested = False
        self.render_now()

    def _cancel_deferred(self) -> None:
        """撤销排队中的那一帧（已经画过一帧时它就没意义了）。"""
        handle = self._defer_handle
        self._defer_handle = None
        self._frame_pending = False
        self._pending_delay_s = 0.0
        self._force_requested = False
        if handle is not None:
            cancel = getattr(handle, "cancel", None)
            if callable(cancel):
                cancel()

    def render_now(self) -> None:
        """**立刻**渲染一帧。

        语义是"**现在就画**"，不是"如果需要就画"——所以它不检查
        ``_render_requested``，也不检查 ``_running``。
        这样测试可以完全绕过节流与终端（直接调它），
        而节流的判断集中在 :meth:`request_render` 一处。

        算法与 Pi 的 ``TUI.doRender`` 一致。三个必须一起理解的状态：

        * ``_hardware_cursor_row`` —— 终端光标**实际**在第几行；
        * ``_previous_viewport_top`` —— 上一帧可见区域的**首行行号**；
        * ``_max_lines_rendered`` —— 终端"工作区"的历史最大值。

        为什么需要"视口"这个概念：主屏模式下内容会**自然向上滚动**
        （滚出去的行进了终端的回滚缓冲，用户能滚回去看）。所以"第 0 行"
        是缓冲区里的绝对行号，而屏幕上看得见的只有最后 ``height`` 行。
        一旦要改的行已经滚出可视区（``first < viewport_top``），
        **增量重画就不再可能**（那几行已经不在屏幕上了），只能整屏重画。
        漏掉这个判断的症状是：内容一长，界面就开始花。
        """
        self._render_requested = False
        self._last_render_at = time.monotonic() * 1000.0

        width = max(1, self.terminal.columns)
        height = max(1, self.terminal.rows)

        width_changed = self._previous_width != 0 and self._previous_width != width
        height_changed = self._previous_height != 0 and self._previous_height != height

        # ① 组件 → 行（Text），并在这一层**兜底硬切**超宽行
        #    （超宽会让终端折行 → 下面所有行错位，见 component.py 的说明）
        raw = fit_lines(self.root.render(width), width)
        raw = self._composite_overlays(raw, width, height)
        # ② 找出（并摘掉）IME 光标标记 —— 必须在序列化**之前**做
        self._cursor_pos = self._extract_cursor(raw, height)
        # ③ 行 → 带 ANSI 的字符串。
        #    ⚠️ 这一步以前是直接取 `line.plain`，于是**主题的 53 个颜色全部失效**：
        #    界面上全是一个颜色的字，但宽度断言照样通过，所以没有任何报错。
        #
        #    ⚠️ 另一个坑是"每帧重算全部行"（D126）—— 序列化要做的是"把 spans 展开成
        #    逐字符样式再渲染"，代价与**总字符数**成正比。会话一长，敲一个字就要
        #    重算几千行的字节（实测 2000 行 ≈ 33ms），而这几千行里变了的通常只有 1 行。
        #    所以这里按**对象身份**复用：同一行对象 → 字节一定一样。
        new_lines = self._serialize_rows(raw)
        self._lines = new_lines
        self._serialized_rows = raw
        self.stats["frames"] += 1

        prev_viewport_top = self._previous_viewport_top
        viewport_top = prev_viewport_top

        # 首帧：直接输出，**不清屏**（假定屏幕是干净的，且不能抹掉用户的历史）
        if not self._previous and not width_changed and not height_changed:
            self._full_render(new_lines, width, height, clear=False)
            return

        # 宽度变了：折行位置全变，增量重画没有意义
        if width_changed:
            self._full_render(new_lines, width, height, clear=True)
            return

        # 高度变了：可见区域变了，不整屏重画的话视口会对不齐
        if height_changed:
            self._full_render(new_lines, width, height, clear=True)
            return

        # ④ 找变化区间（逐行字符串比较：比"重画"便宜一个数量级）
        first, last = self._diff(new_lines)
        appended = len(new_lines) > len(self._previous)
        if appended:
            if first == -1:
                first = len(self._previous)
            last = len(new_lines) - 1

        if first == -1:
            # 内容没变 —— 一个字节都不写（这就是"流畅"的来源）
            self.stats["skipped"] += 1
            self._position_hardware_cursor(self._cursor_pos, len(new_lines))
            self._previous_height = height
            return

        # ⑤ 变化全在"被删掉的行"里（内容变短）
        if first >= len(new_lines):
            if len(self._previous) > len(new_lines):
                self._write_deletion(new_lines, width, height, viewport_top)
            self._previous = new_lines
            self._previous_width, self._previous_height = width, height
            self._position_hardware_cursor(self._cursor_pos, len(new_lines))
            return

        # ⑥ 要改的行已经滚出可视区 → 只能整屏重画（见 docstring）
        if first < prev_viewport_top:
            self._full_render(new_lines, width, height, clear=True)
            return

        self._write_incremental(
            new_lines, first, last, appended, width, height, viewport_top
        )
        self._previous = new_lines
        self._previous_width, self._previous_height = width, height

    def _serialize_rows(self, rows: list[Text]) -> list[str]:
        """把行对象序列化成 ANSI 字节，**按身份复用上一帧的结果**（D126）。

        为什么按下标 + 身份、而不是用字典按内容做键：时间线是**只追加**的，
        所以"第 i 行还是上一帧那个对象"几乎是恒真的，一次 `is` 比对就够；
        而按内容做键要为每一行算哈希（几千行 × 上百字符）——反而更贵。

        下标错位（内容缩水、宽度变化）时全部当作未命中，只是慢一帧，不会错。
        """
        previous_rows = self._serialized_rows
        previous_lines = self._lines
        limit = min(len(previous_rows), len(previous_lines))
        out: list[str] = []
        hits = 0
        for index, row in enumerate(rows):
            if index < limit and previous_rows[index] is row:
                out.append(previous_lines[index])
                hits += 1
            else:
                out.append(text_to_ansi(row))
        self.stats["ansi_hits"] += hits
        self.stats["ansi_misses"] += len(rows) - hits
        return out

    # -- 内部：三种写帧策略 ---------------------------------------------- #

    def _write_incremental(
        self,
        new_lines: list[str],
        first: int,
        last: int,
        appended: bool,
        width: int,
        height: int,
        viewport_top: int,
    ) -> None:
        """只重画变化区间（绝大多数帧走的就是这里）。"""
        buffer = [SYNC_BEGIN]
        prev_viewport_bottom = viewport_top + height - 1

        # "纯追加"是 AI 对话最常见的形态：新行紧接在旧行下面。
        # 这种情况下**不要**把光标移回上一行——直接在末尾写新行，
        # 让终端自己往上滚。滚出去的内容自然进入回滚缓冲（这是我们要的）。
        append_start = appended and first == len(self._previous) and first > 0
        move_target = first - 1 if append_start else first

        if move_target > prev_viewport_bottom:
            # 目标行在可视区下方：先滑到底部，再用换行把它"顶"上来。
            # 每写一个换行，屏幕就上滚一行，视口首行的行号也随之 +1。
            current_screen_row = max(
                0, min(height - 1, self._hardware_cursor_row - viewport_top)
            )
            move_to_bottom = height - 1 - current_screen_row
            if move_to_bottom > 0:
                buffer.append(f"\x1b[{move_to_bottom}B")
            scroll = move_target - prev_viewport_bottom
            buffer.append("\r\n" * scroll)
            viewport_top += scroll
            self._hardware_cursor_row = move_target

        # 把光标移到目标行。用**相对**移动（上/下若干行）而不是绝对定位，
        # 因为主屏里"绝对第几行"会随滚动而变。
        line_diff = (move_target - viewport_top) - (self._hardware_cursor_row - viewport_top)
        if line_diff > 0:
            buffer.append(f"\x1b[{line_diff}B")
        elif line_diff < 0:
            buffer.append(f"\x1b[{-line_diff}A")
        buffer.append("\r\n" if append_start else "\r")

        render_end = min(last, len(new_lines) - 1)
        for index in range(first, render_end + 1):
            if index > first:
                buffer.append("\r\n")
            # ⚠️ 必须先清掉这一行：新内容比旧的短时，不清就会留下旧字符的尾巴
            buffer.append("\x1b[2K")
            buffer.append(new_lines[index])

        final_cursor_row = render_end

        # 内容变短：把多出来的行清掉，再把光标移回内容末尾
        shrunk = len(self._previous) > len(new_lines)
        if shrunk:
            if render_end < len(new_lines) - 1:
                move_down = len(new_lines) - 1 - render_end
                buffer.append(f"\x1b[{move_down}B")
                final_cursor_row = len(new_lines) - 1
            extra = len(self._previous) - len(new_lines)
            # 防御性底行收缩守卫（D105）：如果光标已经在终端最底部（height - 1），
            # 向下移光标（\x1b[1B）会被终端忽略，但向上移光标（\x1b[1A）却会生效，
            # 导致硬件光标向上漂移 1 行，进而触发双状态栏与表格错位 bug。
            current_screen_row = final_cursor_row - viewport_top
            can_clear = min(extra, max(0, height - 1 - current_screen_row))
            if can_clear > 0:
                buffer.append("\x1b[1B")
                for index in range(can_clear):
                    buffer.append("\r\x1b[2K")
                    if index < can_clear - 1:
                        buffer.append("\x1b[1B")
                buffer.append(f"\x1b[{can_clear}A")

        buffer.append(SYNC_END)
        self.terminal.write("".join(buffer))

        self._hardware_cursor_row = final_cursor_row
        # 收缩那一支最后停在行首；否则光标在内容末行的末尾
        self._cursor_col = 0 if shrunk or not new_lines else visible_width(new_lines[final_cursor_row])
        self._max_lines_rendered = max(self._max_lines_rendered, len(new_lines))
        self._previous_viewport_top = max(viewport_top, final_cursor_row - height + 1)
        self._position_hardware_cursor(self._cursor_pos, len(new_lines))

    def _write_deletion(
        self,
        new_lines: list[str],
        width: int,
        height: int,
        viewport_top: int,
    ) -> None:
        """变化全在"被删掉的行"里：屏幕上没有新内容要写，只需要擦掉多余的旧行。"""
        target_row = max(0, len(new_lines) - 1)
        if target_row < viewport_top or len(self._previous) - len(new_lines) > height:
            # 内容缩到可视区之上（或缩得太多）→ 位置已经无从推算，整屏重画
            self._full_render(new_lines, width, height, clear=True)
            return
        extra = len(self._previous) - len(new_lines)

        buffer = [SYNC_BEGIN]
        line_diff = target_row - self._hardware_cursor_row
        if line_diff > 0:
            buffer.append(f"\x1b[{line_diff}B")
        elif line_diff < 0:
            buffer.append(f"\x1b[{-line_diff}A")
        buffer.append("\r")
        if extra > 0:
            buffer.append("\x1b[1B")
        for index in range(extra):
            buffer.append("\r\x1b[2K")
            if index < extra - 1:
                buffer.append("\x1b[1B")
        if extra > 0:
            buffer.append(f"\x1b[{extra}A")
        buffer.append(SYNC_END)
        self.terminal.write("".join(buffer))
        self._hardware_cursor_row = target_row
        self._cursor_col = 0  # 这一支最后停在行首（见上面的 `\r`）
        self._previous_viewport_top = viewport_top

    def _diff(self, new_lines: list[str]) -> tuple[int, int]:
        """逐行比较，返回 ``(first_changed, last_changed)``；没变则 ``(-1, -1)``。"""
        old = self._previous
        first = -1
        last = -1
        for index in range(max(len(old), len(new_lines))):
            old_text = old[index] if index < len(old) else ""
            new_text = new_lines[index] if index < len(new_lines) else ""
            if old_text != new_text:
                if first == -1:
                    first = index
                last = index
        return first, last

    def _full_render(
        self, lines: list[str], width: int, height: int, *, clear: bool
    ) -> None:
        """整屏重画（首帧、宽高变化、变更落在视口之上时用）。"""
        self.stats["full_redraws"] += 1
        buffer = [SYNC_BEGIN]
        if clear:
            buffer.append(CLEAR_ALL)
        buffer.append("\r\n".join(lines))
        buffer.append(SYNC_END)
        self.terminal.write("".join(buffer))

        self._hardware_cursor_row = max(0, len(lines) - 1)
        self._cursor_col = visible_width(lines[-1]) if lines else 0
        if clear:
            self._max_lines_rendered = len(lines)
        else:
            self._max_lines_rendered = max(self._max_lines_rendered, len(lines))
        buffer_length = max(height, len(lines))
        self._previous_viewport_top = max(0, buffer_length - height)
        # ⚠️ 这里必须更新"上一帧"——否则下一帧会以为这是首帧，**再整屏写一遍**，
        # 而第二次是从当前光标处开始写的，于是内容被**重复贴在同一行上**。
        self._previous = lines
        self._previous_width = width
        self._previous_height = height
        self._position_hardware_cursor(self._cursor_pos, len(lines))

    # -- 内部：硬件光标 -------------------------------------------------- #

    def _position_hardware_cursor(self, cursor_pos: tuple[int, int] | None, total: int) -> None:
        """把硬件光标移到 IME 标记处（没有标记就藏起来）。

        这一步与"画内容"是**两件事**：内容里那个标记是零宽的，
        终端看不见它；真正决定中文输入法候选窗出现在哪里的，是硬件光标。

        位置没变就**什么都不写**——空闲时零开销靠的就是这个判断。
        """
        want_visible = bool(cursor_pos) and total > 0 and self.show_hardware_cursor
        if not want_visible:
            if self._cursor_visible:
                self.terminal.write("\x1b[?25l")
                self._cursor_visible = False
            return
        assert cursor_pos is not None  # want_visible 为真 ⇒ 一定有值
        row = max(0, min(cursor_pos[0], total - 1))
        col = max(0, cursor_pos[1])
        if row == self._hardware_cursor_row and col == self._cursor_col and self._cursor_visible:
            return
        delta = row - self._hardware_cursor_row
        buffer = ""
        if delta > 0:
            buffer += f"\x1b[{delta}B"
        elif delta < 0:
            buffer += f"\x1b[{-delta}A"
        buffer += f"\x1b[{col + 1}G"  # 列是 1 起算
        buffer += "\x1b[?25h"
        self.terminal.write(buffer)
        self._hardware_cursor_row = row
        self._cursor_col = col
        self._cursor_visible = True

    # ------------------------------------------------------------------ #
    # 调试
    # ------------------------------------------------------------------ #

    def frame_text(self) -> str:
        """当前帧的**纯文本**（测试断言"屏幕上是什么"用）。"""
        return "\n".join(strip_ansi(line) for line in self._lines)

    def assert_fits(self, width: int) -> None:
        """自检：交付给终端的行必须都不超宽。

        这是最后一道防线。**不加断言的话，超宽行的症状是"整个界面错位"**，
        而那时已经离成因很远了。
        """
        for index, line in enumerate(self._lines):
            actual = visible_width(line)
            if actual > width:
                raise AssertionError(f"第 {index} 行交给终端时超宽：{actual} > {width}：{line!r}")

    def _composite_overlays(self, lines: list[Text], width: int, height: int) -> list[Text]:
        """把可见浮层按锚点贴到基础行上（行级覆盖）。

        为什么不做逐格 alpha 混合：终端里没有"半透明"，浮层本来就是**盖住**下面。
        Pi 也是这么做的（``compositeLineAt``）。
        """
        result = list(lines)
        for handle in self.overlays:
            if not self._is_visible(handle):
                continue
            result = self._composite_one(result, handle, width, height)
        return result

    def _composite_one(
        self, base: list[Text], handle: OverlayHandle, width: int, height: int
    ) -> list[Text]:
        options = handle.options
        overlay_width = self._resolve_width(options.get("width"), width, options)
        rows = fit_lines(handle.component.render(overlay_width), overlay_width)

        row = self._resolve_row(options, width, height, len(rows), len(base))
        col = self._resolve_col(options, width, overlay_width)
        # ★ 浮层默认**不透明**：它盖住的那一行，两侧的旧内容必须被擦掉。
        #
        # 为什么非做不可（实测截图发现的）：浮层是"横向拼贴"到某一行上的，
        # 于是浮层左右两侧会**留着时间线的字**——帮助面板右边露出半句回答，
        # 看起来像排版坏了。而浮层本来就是模态的（抢占焦点、Esc 才关），
        # "盖住"才是它该有的语义（终端里没有半透明）。
        opaque = bool(options.get("opaque", True))

        result = list(base)
        # 浮层比基础内容高时要补几行空行（底部锚定下这只会在"内容很短"时发生）
        while len(result) < row + len(rows):
            result.append(Text())
        for offset, line in enumerate(rows):
            target = row + offset
            if target >= len(result):
                break
            result[target] = _overlay_line(result[target], line, col, width, opaque=opaque)
        return result

    @staticmethod
    def _resolve_width(spec: Any, width: int, options: dict[str, Any]) -> int:
        if isinstance(spec, str) and spec.endswith("%"):
            resolved = int(width * float(spec[:-1]) / 100.0)
        elif isinstance(spec, int):
            resolved = spec
        else:
            resolved = min(width, 80)
        minimum = options.get("min_width")
        if isinstance(minimum, int):
            resolved = max(resolved, minimum)
        return max(1, min(resolved, width))

    @staticmethod
    def _resolve_row(options: dict[str, Any], width: int, height: int, rows: int, base: int) -> int:
        """算出浮层贴在**第几行**（相对于这一帧的行号）。

        ``anchor="bottom"``（我们的默认）把浮层贴在**内容末尾之上**——像 `fzf`
        那样紧挨着输入框，而且**帧的长度不会因为浮层而增长**。

        为什么不用"屏幕正中间"（Pi 的做法）：Pi 为了居中会把帧**补齐到整个终端
        高度**（``compositeOverlays`` 里的 ``Math.max(result.length, termHeight, ...)``），
        于是打开一个浮层会写进几十行空白。我们是行式主屏渲染，帧一变长就会与
        "纯追加"的滚动逻辑纠缠（旧内容被顶上去、进回滚缓冲），关掉浮层时还要
        把这些空行清掉。底部锚定没有这些问题。
        """
        margin = options.get("margin", 1)
        margin_top = margin if isinstance(margin, int) else int(margin.get("top", 0))

        if options.get("anchor") == "bottom":
            # 贴在基础内容之后（与 AppLayout 配合实现防遮挡停靠布局）；若显式关闭 push 则回退到直接覆盖
            if options.get("push", True):
                return base
            return max(0, base - rows)

        available = max(0, height - rows - margin_top * 2)

        row = options.get("row")
        if isinstance(row, str) and row.endswith("%"):
            resolved = margin_top + int(available * float(row[:-1]) / 100.0)
        elif isinstance(row, int):
            resolved = row
        else:
            resolved = margin_top + available // 2  # 默认居中
        return max(margin_top, min(resolved, max(0, height - rows - margin_top)))

    @staticmethod
    def _resolve_col(options: dict[str, Any], width: int, overlay_width: int) -> int:
        margin = options.get("margin", 1)
        margin_left = margin if isinstance(margin, int) else int(margin.get("left", 0))

        col = options.get("col")
        if isinstance(col, str) and col.endswith("%"):
            resolved = int(width * float(col[:-1]) / 100.0)
        elif isinstance(col, int):
            resolved = col
        else:
            resolved = (width - overlay_width) // 2  # 默认居中
        return max(margin_left, min(resolved, max(0, width - overlay_width)))

    @staticmethod
    def _extract_cursor(lines: list[Text], height: int) -> tuple[int, int] | None:
        """找出 IME 光标标记的 ``(行, 列)``，并把它从这一帧里**摘掉**。

        标记是零宽的 APC 序列，**不能留在写给终端的字节里**（终端不认识它，
        某些终端还会把它显示出来）。摘掉之后我们再单独把硬件光标移过去。

        只在**末尾若干行**里找（可见视口）：上面的行要么不在屏幕上，
        要么根本不该有输入光标。找不到返回 ``None``。
        """
        from logox.tui.render.components.text import CURSOR_MARKER

        marker_length = len(CURSOR_MARKER)
        viewport_top = max(0, len(lines) - height)
        for row in range(len(lines) - 1, viewport_top - 1, -1):
            line = lines[row]
            index = line.plain.find(CURSOR_MARKER)
            if index == -1:
                continue
            col = visible_width(line.plain[:index])
            lines[row] = _remove_marker(line, index, marker_length)
            return row, col
        return None


def _set_focused(component: Any, value: bool) -> None:
    """尽力同步组件的 ``focused`` 标记（**鸭子类型**，没有该属性就跳过）。

    只有"可聚焦"的组件才有这个属性（编辑框、浮层）。这里刻意不做 isinstance 检查：
    组件协议本来就只有三个方法，加一个可选属性是为了 IME 光标，不该变成硬性要求。
    """
    if hasattr(component, "focused"):
        component.focused = value


def _remove_marker(line: Text, index: int, marker_length: int) -> Text:
    """把 ``index`` 处的零宽标记从这一行删掉，**样式原样保留**。

    为什么要这么小心：直接 ``Text(plain[:i] + plain[i+n:])`` 会丢掉 ``spans``，
    于是那一行**颜色全没了**。而这个函数正好用在输入框那一行——
    症状就是"打字的时候输入框忽然变白"。所以这里逐段搬移样式，
    并把跨过标记的 span 拆成左右两段（标记本身零宽，不需要样式）。
    """
    out = Text(style=line.style)
    out.append(line.plain[:index] + line.plain[index + marker_length :])
    for span in line.spans:
        start, end = span.start, span.end
        if end <= index:
            out.stylize(span.style, start, end)
        elif start >= index + marker_length:
            out.stylize(span.style, start - marker_length, end - marker_length)
        else:
            if start < index:
                out.stylize(span.style, start, index)
            if end > index + marker_length:
                out.stylize(span.style, index, end - marker_length)
    return out


def _overlay_line(base: Text, overlay: Text, col: int, width: int, *, opaque: bool = True) -> Text:
    """把 ``overlay`` 贴到 ``base`` 的 ``col`` 列处（**按 cell 宽度**对齐）。

    中文占 2 格，所以必须按 cell 切而不是按字符切——按字符切会让浮层
    在含中文的行上错位。这正是本项目已经踩过的"宽度口径"问题。

    ``opaque=True``（默认）时，浮层左右两侧的**旧内容被空格替换**——终端里没有
    半透明，模态浮层就该盖住下面。不透明的话，帮助面板右边会露出半句回答
    （实测截图发现）。
    """
    base_plain = base.plain
    overlay_width = visible_width(overlay.plain)
    if opaque:
        # 两侧都留白：浮层声明它占多少格，那之外的部分一律不显示
        head = " " * col
        tail = " " * max(0, width - col - overlay_width)
        piece = Text(head, style=base.style)
        piece.append_text(overlay)
        piece.append(tail, style=base.style)
        missing = width - visible_width(piece.plain)
        if missing > 0:
            piece.append(" " * missing)
        return piece

    head = _take_cells(base_plain, col)
    tail_start = col + overlay_width
    tail = _drop_cells(base_plain, tail_start)

    piece = Text(head, style=base.style)
    piece.append_text(overlay)
    piece.append(tail, style=base.style)
    # 底部对齐到整宽：浮层盖住的那一段必须完全替换，不能露出底色
    missing = width - visible_width(piece.plain)
    if missing > 0:
        piece.append(" " * missing)
    return piece


def _take_cells(text: str, count: int) -> str:
    """取前 ``count`` 格（中文算 2 格，不会切半个字）。"""
    used = 0
    out: list[str] = []
    for char in text:
        char_width = visible_width(char)
        if used + char_width > count:
            break
        out.append(char)
        used += char_width
    return "".join(out)


def _drop_cells(text: str, count: int) -> str:
    """丢掉前 ``count`` 格，返回剩余部分。"""
    used = 0
    for index, char in enumerate(text):
        char_width = visible_width(char)
        if used + char_width > count:
            return text[index:]
        used += char_width
    return ""
