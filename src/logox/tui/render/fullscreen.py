"""全屏备用屏模式引擎（D179 / D180 / 09_tui_fullscreen）。

这是什么
--------
基于终端备用屏（Alternate Screen，``\\x1b[?1049h``）的双模并存架构（方案 B）。
对标 Pi Agent（``@earendil-works/pi-tui`` 的 ``tui-alt-screen.js``）：
1. **备用屏与鼠标捕获**：进入时切换至独立备用屏并开启 SGR 1006 鼠标跟踪协议，
   退出时原子还原终端与光标，彻底杜绝回显乱码；
2. **视口与停靠区解耦**：终端垂直切分为上方的对话消息视口（ScrollView）
   与下方的固定停靠区（Dock：候选框 + 输入框 + 状态栏）；
3. **独立滚动**：用户使用鼠标滚轮或 PageUp/PageDown 向上查阅长历史消息时，
4. **滚动与输入解耦**：用户在输入框打字、编辑或换行时，视口保持当前滚动位置，方便对照历史上下文输入；
   用户回车提交消息（Submit）或显式按 Esc / End 时，视口才归零回到底部。
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import re
import sys
import time
from typing import Any

from rich.cells import cell_len
from rich.text import Text

from logox.tui.render.app import InlineApp, StatusComponent, TimelineComponent
from logox.tui.render.component import Component, fit_lines
from logox.tui.render.keys import Key
from logox.tui.render.terminal import Terminal, make_terminal

__all__ = [
    "FullscreenApp",
    "FullscreenLayout",
    "cell_to_char_index",
    "copy_to_system_clipboard",
    "extract_clean_selection",
    "run_fullscreen",
    "strip_track_prefix",
]

#: 进入备用屏并清屏归位光标
ENTER_ALT_SCREEN = "\x1b[?1049h\x1b[H\x1b[2J"
#: 退出备用屏并恢复光标
EXIT_ALT_SCREEN = "\x1b[?1049l\x1b[?25h"

#: 开启 SGR 1006 鼠标坐标与拖拽跟踪 (1000h 按键 + 1002h 拖拽移动 + 1006h SGR 扩展)
ENABLE_MOUSE_SGR = "\x1b[?1000h\x1b[?1002h\x1b[?1006h"
#: 关闭鼠标跟踪
DISABLE_MOUSE_SGR = "\x1b[?1002l\x1b[?1000l\x1b[?1006l"

#: 全屏模式标准清理序列
FULLSCREEN_CLEANUP = f"{DISABLE_MOUSE_SGR}{EXIT_ALT_SCREEN}"


def copy_to_system_clipboard(text: str) -> bool:
    """Windows 原生剪贴板 API 注入，带 OSC 52 跨平台后备。

    采用标准库 ctypes 直接操作 Win32 API（严格零外部依赖，坚守 D28）。
    显式声明 restype 和 argtypes 避免 64 位指针截断。
    若非 Windows 或遇到剪贴板被抢占，自动回退到终端标准 OSC 52 转义序列。
    """
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32

            kernel32.GlobalAlloc.restype = ctypes.c_void_p
            kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
            kernel32.GlobalLock.restype = ctypes.c_void_p
            kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
            kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
            user32.OpenClipboard.argtypes = [ctypes.c_void_p]
            user32.SetClipboardData.argtypes = [wintypes.UINT, ctypes.c_void_p]

            CF_UNICODETEXT = 13
            GMEM_MOVEABLE = 0x0002
            GMEM_ZEROINIT = 0x0040

            if user32.OpenClipboard(None):
                try:
                    user32.EmptyClipboard()
                    data = (text + "\0").encode("utf-16le")
                    h_mem = kernel32.GlobalAlloc(GMEM_MOVEABLE | GMEM_ZEROINIT, len(data))
                    if h_mem:
                        ptr = kernel32.GlobalLock(h_mem)
                        if ptr:
                            ctypes.memmove(ptr, data, len(data))
                            kernel32.GlobalUnlock(h_mem)
                            user32.SetClipboardData(CF_UNICODETEXT, h_mem)
                            return True
                finally:
                    user32.CloseClipboard()
        except Exception:
            pass

    # OSC 52 后备（跨平台 / Linux / macOS / 远程 SSH）
    try:
        import base64

        b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
        sys.stdout.write(f"\x1b]52;c;{b64}\x07")
        sys.stdout.flush()
        return True
    except Exception:
        return False


def strip_track_prefix(line: str) -> str:
    """剔除时间线行首的双轨装饰标记（▌ / ▎ 及跟随的一个空格）。"""
    return re.sub(r"^\s*[▌▎]\s?", "", line)


def cell_to_char_index(text: str, target_cell: int, *, is_end: bool = False) -> int:
    """将终端单元格列坐标 (0-based) 转换为字符串字符下标 (0-based)。

    兼容 CJK 双宽字符、全角标点与 Emoji。
    若 target_cell 小于 0 返回 0；若 target_cell 大于整行单元格总宽返回 len(text)。

    参数:
        text: 目标纯文本行
        target_cell: 终端单元格列号 (0-based)
        is_end: 若为 True，表示选区结束端点（包含 target_cell 处的字符，返回切片 end 开区间下标）
    """
    if target_cell < 0:
        return 0
    accum = 0
    for idx, ch in enumerate(text):
        w = cell_len(ch)
        if is_end:
            if accum + w > target_cell:
                return idx + 1
        else:
            if accum + w > target_cell:
                return idx
        accum += w
    return len(text)


def extract_clean_selection(
    all_lines: list[Text],
    start_line: int,
    start_col: int,
    end_line: int,
    end_col: int,
) -> str:
    """提取选区文本并剔除行首双轨字符（`▌ ` / `▎ ` / 前缀缩进）。"""
    if (start_line, start_col) > (end_line, end_col):
        start_line, end_line = end_line, start_line
        start_col, end_col = end_col, start_col

    raw_lines = []
    for idx in range(start_line, min(end_line + 1, len(all_lines))):
        line_plain = all_lines[idx].plain
        s = cell_to_char_index(line_plain, start_col, is_end=False) if idx == start_line else 0
        e = cell_to_char_index(line_plain, end_col, is_end=True) if idx == end_line else len(line_plain)
        if s > e and idx == start_line and idx == end_line:
            s, e = e, s
        raw_lines.append(line_plain[s:e])

    clean_lines = [strip_track_prefix(line) for line in raw_lines]
    return "\n".join(clean_lines)


def _emergency_restore() -> None:
    """进程异常终止时的 atexit 原子恢复钩子（防止终端被困在备用屏）。"""
    with contextlib.suppress(Exception):
        sys.stdout.write(FULLSCREEN_CLEANUP)
        sys.stdout.flush()


class FullscreenLayout:
    """全屏视口布局：

    窗口垂直划分为两部分：
    1. 上半部：消息视口（ScrollView），高度为 terminal.rows - dock_rows；
       支持 scroll_offset 虚拟滚动，滚轮或翻页键只移动此区域；
    2. 下半部：停靠区域（Dock），包含输入框、候选浮层与状态行，永远固定在最后几行。
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
        self.completion: Any | None = None

        # 滚动状态
        self.scroll_offset: int = 0  # 0 表示钉在最底部查看最新消息；> 0 表示向上回滚的历史行数
        self.auto_scroll_to_bottom: bool = True

        # 视口行缓存与几何快照（O(1) 鼠标交互关键，彻底去除每帧重复 render）
        self._cached_tl_rows: list[Text] = []
        self._cached_block_ranges: list[tuple[int, int, Any]] = []
        self._cached_width: int = 0
        self._cached_dock_rows: int = 0
        self._cached_viewport_height: int = 0

        # 鼠标与选区交互状态
        self._mouse_down_pos: tuple[int, int] | None = None
        self._mouse_down_line: int | None = None
        self._mouse_down_col: int = 0
        self._is_dragging: bool = False
        self._selection: tuple[int, int, int, int] | None = None  # (start_line, start_col, end_line, end_col)
        self._toast: str = ""
        self._toast_time: float = 0.0

        # 滚动条与对话轮次标尺状态（D183）
        self._show_scrollbar: bool = False
        self._turn_mark_rows: dict[int, int] = {}
        self._scrollbar_rows: list[tuple[Text, int, str, str, Text]] = []
        self._mark_ranges: list[tuple[int, int, Any]] | None = None
        self._mark_geometry: tuple[int, int] = (0, 0)

    def _screen_y_to_timeline_line(self, y: int, width: int) -> int | None:
        """把 1-based 终端屏幕行号映射为全量时间线的行下标 (O(1) 纯数学映射)。"""
        total_tl_rows = len(self._cached_tl_rows)
        if total_tl_rows == 0:
            self._cached_tl_rows = self.timeline.render(max(1, width - 1))
            self._cached_width = max(1, width - 1)
            total_tl_rows = len(self._cached_tl_rows)

        dock_rows = self._cached_dock_rows or self._get_dock_rows_count(width)
        viewport_height = max(1, max(1, self.terminal.rows) - dock_rows)
        if y < 1 or y > viewport_height or total_tl_rows == 0:
            return None
        if total_tl_rows <= viewport_height:
            idx = y - 1
            return idx if idx < total_tl_rows else None
        start = total_tl_rows - viewport_height - self.scroll_offset
        idx = start + (y - 1)
        return idx if 0 <= idx < total_tl_rows else None

    def _get_dock_rows_count(self, width: int) -> int:
        """计算当前底部 Dock 所占的实际行数。"""
        # 若有激活的独占模态浮层（如 /resume、HITL 确认框），Dock 由浮层全权替代
        if self.screen is not None and hasattr(self.screen, "overlays"):
            for h in self.screen.overlays:
                if not getattr(h, "hidden", False):
                    if hasattr(h, "component") and hasattr(h.component, "render"):
                        opt = h.options
                        resolve_w = getattr(self.screen, "_resolve_width", None)
                        overlay_w = resolve_w(opt.get("width"), width, opt) if resolve_w else width
                        rendered = fit_lines(h.component.render(overlay_w), overlay_w)
                        return max(1, len(rendered))
                    return 6

        completion_rows = self.completion.render(width) if self.completion is not None else []
        ed_rows = self.editor.render(width)
        st_rows = self.status.render(width)
        return len(completion_rows) + len(ed_rows) + len(st_rows)

    def _get_max_offset(self, width: int) -> int:
        """计算当前历史消息允许滚动的最大行数 (O(1))。"""
        dock_rows = self._cached_dock_rows or self._get_dock_rows_count(width)
        total_height = max(1, self.terminal.rows)
        viewport_height = max(1, total_height - dock_rows)
        total_tl_rows = len(self._cached_tl_rows)
        if total_tl_rows == 0:
            self._cached_tl_rows = self.timeline.render(max(1, width - 1))
            self._cached_width = max(1, width - 1)
            total_tl_rows = len(self._cached_tl_rows)
        return max(0, total_tl_rows - viewport_height)

    def get_viewport_anchor(self, width: int) -> tuple[Any | None, int]:
        """捕获当前视口首行锚点 (D185：Scroll Anchoring，方案 1.A + 2.A)。

        返回 (anchor_block, intra_offset)：
        - anchor_block: 当前视口首行对应的 Block 实例（若无则 None）；
        - intra_offset: 该首行在 anchor_block 内部的相对行号偏移。
        """
        dock_rows = self._cached_dock_rows or self._get_dock_rows_count(width)
        total_height = max(1, self.terminal.rows)
        viewport_height = max(1, total_height - dock_rows)

        total_tl_rows = len(self._cached_tl_rows)
        if total_tl_rows == 0:
            content_w = max(1, width - 1)
            self._cached_tl_rows = self.timeline.render(content_w)
            self._cached_width = content_w
            total_tl_rows = len(self._cached_tl_rows)

        if total_tl_rows == 0:
            return None, 0

        if total_tl_rows <= viewport_height:
            start_line = 0
        else:
            start_line = max(0, total_tl_rows - viewport_height - self.scroll_offset)

        if hasattr(self.timeline, "block_ranges") and self.timeline.block_ranges:
            for s_l, e_l, block in self.timeline.block_ranges:
                if s_l <= start_line < e_l:
                    return block, max(0, start_line - s_l)
            last_s, last_e, last_block = self.timeline.block_ranges[-1]
            return last_block, max(0, start_line - last_s)

        return None, start_line

    def restore_viewport_anchor(
        self,
        anchor_block: Any | None,
        intra_offset: int,
        width: int,
    ) -> None:
        """根据卡片展开/折叠前的锚点恢复视口位置 (D185：Scroll Anchoring，方案 1.A + 2.A)。

        重新排版后，在全新的 block_ranges 中找到同一个 anchor_block，
        计算其新的物理行号 new_start = new_b_start + intra_offset，
        并反解更新 self.scroll_offset = max(0, min(max_offset, new_total_tl_rows - viewport_height - new_start))。
        """
        self._cached_tl_rows = []
        content_w = max(1, width - 1)
        all_tl_rows = self.timeline.render(content_w)
        total_tl_rows = len(all_tl_rows)

        dock_rows = self._get_dock_rows_count(width)
        total_height = max(1, self.terminal.rows)
        viewport_height = max(1, total_height - dock_rows)

        if total_tl_rows <= viewport_height:
            content_w = max(1, width - 1)
            self.scroll_offset = 0
            self._cached_tl_rows = all_tl_rows
            self._cached_width = content_w
            self._cached_dock_rows = dock_rows
            self._cached_viewport_height = viewport_height
            self._cached_block_ranges = list(getattr(self.timeline, "block_ranges", []))
            return

        self._cached_tl_rows = all_tl_rows
        self._cached_width = content_w
        self._cached_dock_rows = dock_rows
        self._cached_viewport_height = viewport_height
        self._cached_block_ranges = list(getattr(self.timeline, "block_ranges", []))

        max_offset = max(0, total_tl_rows - viewport_height)

        if anchor_block is not None and hasattr(self.timeline, "block_ranges"):
            found = False
            for s_l, _e_l, block in self.timeline.block_ranges:
                if block is anchor_block:
                    b_height = max(1, _e_l - s_l)
                    target_start = s_l + min(intra_offset, b_height - 1)
                    new_offset = total_tl_rows - viewport_height - target_start
                    self.scroll_offset = max(0, min(max_offset, new_offset))
                    found = True
                    break
            if not found:
                self.scroll_offset = max(0, min(max_offset, self.scroll_offset))
        else:
            target_start = intra_offset
            new_offset = total_tl_rows - viewport_height - target_start
            self.scroll_offset = max(0, min(max_offset, new_offset))

    def _apply_scrollbar(
        self,
        view_rows: list[Text],
        *,
        total_tl_rows: int,
        viewport_height: int,
        width: int,
        start: int,
    ) -> list[Text]:
        """为视口右侧追加 8 级微步子字符平滑滚动条与对话轮次起点标尺（D183 / D184）。

        - 仅在内容超出一屏（total_tl_rows > viewport_height）时呈现；
        - 滑块（Thumb）采用 8 级微步子字符（Eighth-Block）平滑渲染，垂直分辨率提升 8 倍，消除阶梯顿挫；
        - 滑块颜色与默认导轨完全一致（border_subtle），消除视觉冲突；
        - 导轨上若命中用户提问行（block.kind == 'user'），格式与导轨同款（细线 │），
          颜色为用户左侧双轨蓝（bold {accent}），且在滑块内部透传显示。
        """
        if total_tl_rows <= viewport_height or not view_rows:
            self._show_scrollbar = False
            self._turn_mark_rows = {}
            self._scrollbar_rows = []
            self._mark_ranges = None
            return view_rows

        self._show_scrollbar = True
        V = len(view_rows)
        N = total_tl_rows

        # 1. 计算 8 级微步滑块几何 (Sub-Cell Eighth-Block Geometry)
        E = V * 8
        H8 = max(8, min(E, round(E * (V / N))))  # 保底至少 8 个微步（即 1 整行）
        travel8 = E - H8
        max_scroll = max(1, N - V)
        top_offset = N - V - self.scroll_offset

        if self.scroll_offset == 0:
            Y8 = travel8
        elif top_offset <= 0:
            Y8 = 0
        else:
            Y8 = max(0, min(travel8, round(top_offset * travel8 / max_scroll)))

        # 内容区间与几何未变时，轮次标尺也不需要重新遍历整个会话。
        ranges = getattr(self.timeline, "block_ranges", [])
        if self._mark_ranges is not ranges or self._mark_geometry != (N, V):
            self._turn_mark_rows = {}
            for s_l, _e_l, block in ranges:
                if getattr(block, "kind", "") == "user":
                    y_mark = min(V - 1, max(0, round(s_l * (V - 1) / (N - 1))))
                    self._turn_mark_rows.setdefault(y_mark, s_l)
            self._mark_ranges = ranges
            self._mark_geometry = (N, V)

        palette = getattr(self.timeline, "palette", None)
        accent_style = str(getattr(palette, "accent", "#89b4fa") or "#89b4fa")
        border_subtle = str(getattr(palette, "border_subtle", "#45475a") or "#45475a")
        bg_base = str(getattr(palette, "bg_base", "") or "")
        marker_style = f"bold {accent_style}"

        result: list[Text] = []
        target_len = max(0, width - 1)
        lower_blocks = [" ", "▂", "▃", "▄", "▅", "▆", "▇"]

        cached_rows = self._scrollbar_rows
        current: list[tuple[Text, int, str, str, Text]] = []
        for v_idx, row in enumerate(view_rows):
            s = max(8 * v_idx, Y8)
            e = min(8 * v_idx + 7, Y8 + H8 - 1)
            has_mark = v_idx in self._turn_mark_rows
            glyph, style = "│", marker_style if has_mark else border_subtle
            if s <= e:
                k0, k1 = s - 8 * v_idx, e - 8 * v_idx
                covered = k1 - k0 + 1
                if has_mark:
                    style = f"{marker_style} on {border_subtle}"
                elif covered == 8:
                    glyph = "█"
                elif k0 > 0 and k1 == 7:
                    glyph = lower_blocks[covered - 1]
                elif k0 == 0 and k1 < 7:
                    if covered == 4 or not bg_base:
                        glyph = "▀"
                    else:
                        glyph, style = lower_blocks[8 - covered - 1], f"{bg_base} on {border_subtle}"
                else:
                    glyph = "█"
            previous = cached_rows[v_idx] if v_idx < len(cached_rows) else None
            if previous is not None and previous[0] is row and previous[1:4] == (width, glyph, style):
                r = previous[4]
            else:
                r = row.copy()
                cur_len = cell_len(r.plain)
                if cur_len < target_len:
                    r.append(" " * (target_len - cur_len))
                elif cur_len > target_len:
                    cut = cell_to_char_index(r.plain, target_len, is_end=False)
                    r = r[:cut]
                    pad = max(0, target_len - cell_len(r.plain))
                    if pad:
                        r.append(" " * pad)
                r.append(glyph, style=style)
            current.append((row, width, glyph, style, r))
            result.append(r)
        self._scrollbar_rows = current

        return result

    def render(self, width: int) -> list[Text]:
        anchor = self._reading_anchor()
        # 1. 检查是否有激活的独占模态浮层（Pi 独占提示框规范）
        has_active_overlay = False
        overlay_rows_count = 0
        if self.screen is not None and hasattr(self.screen, "overlays"):
            for h in self.screen.overlays:
                if not getattr(h, "hidden", False):
                    has_active_overlay = True
                    if hasattr(h, "component") and hasattr(h.component, "render"):
                        opt = h.options
                        resolve_w = getattr(self.screen, "_resolve_width", None)
                        overlay_w = resolve_w(opt.get("width"), width, opt) if resolve_w else width
                        rendered = fit_lines(h.component.render(overlay_w), overlay_w)
                        overlay_rows_count = max(1, len(rendered))
                    else:
                        overlay_rows_count = 6
                    break

        total_height = max(1, self.terminal.rows)

        # 2. 如果存在浮层，Dock 让位给浮层，视口预留空间给浮层
        if has_active_overlay:
            dock_rows = overlay_rows_count
            viewport_height = max(1, total_height - dock_rows)
            content_w = max(1, width - 1)
            all_tl_rows = self.timeline.render(content_w)
            total_tl_rows = len(all_tl_rows)

            self._cached_tl_rows = all_tl_rows
            self._cached_width = content_w
            self._cached_dock_rows = dock_rows
            self._cached_viewport_height = viewport_height

            self._keep_reading_anchor(anchor, viewport_height)
            self._cached_block_ranges = list(getattr(self.timeline, "block_ranges", []))
            max_offset = max(0, total_tl_rows - viewport_height)
            self.scroll_offset = max(0, min(self.scroll_offset, max_offset))

            if total_tl_rows <= viewport_height:
                padding = [Text()] * (viewport_height - total_tl_rows)
                view_rows = all_tl_rows + padding
                start = 0
            else:
                start = total_tl_rows - viewport_height - self.scroll_offset
                end = total_tl_rows - self.scroll_offset
                view_rows = list(all_tl_rows[start:end])

            if time.time() - self._toast_time < 1.5 and view_rows:
                toast_badge = Text(" ✓ 已复制到剪贴板 ", style="bold black on #a6e3a1")
                toast_len = cell_len(toast_badge.plain)
                row = view_rows[0].copy()
                avail_w = (width - 1) if (total_tl_rows > viewport_height) else width
                if avail_w >= toast_len:
                    cut = cell_to_char_index(row.plain, avail_w - toast_len, is_end=False)
                    row = row[:cut]
                    pad = max(0, avail_w - cell_len(row.plain) - toast_len)
                    row.append(" " * pad)
                    row.append_text(toast_badge)
                    view_rows[0] = row

            view_rows = self._apply_scrollbar(
                view_rows,
                total_tl_rows=total_tl_rows,
                viewport_height=viewport_height,
                width=width,
                start=start,
            )

            # Screen._composite_overlays 会把浮层追加到 view_rows 底部，整屏正好填满 total_height
            return view_rows

        # 3. 常规停靠状态：计算底部 Dock 各行
        completion_rows = self.completion.render(width) if self.completion is not None else []
        ed_rows = self.editor.render(width)
        st_rows = self.status.render(width)
        dock_rows = len(completion_rows) + len(ed_rows) + len(st_rows)

        # 4. 计算消息视口物理高度
        viewport_height = max(1, total_height - dock_rows)

        # 5. 渲染全部时间线行（Single-Pass Viewport Render, D186）
        # 利用上一帧的 _show_scrollbar 状态预判宽度，避免每帧反复二次重绘摧毁 TimelineRenderCache
        content_w = max(1, width - 1)
        all_tl_rows = self.timeline.render(content_w)
        total_tl_rows = len(all_tl_rows)

        # 保存视口几何快照（供鼠标坐标投影与 O(1) 偏移计算）
        self._cached_tl_rows = all_tl_rows
        self._cached_width = content_w
        self._cached_dock_rows = dock_rows
        self._cached_viewport_height = viewport_height

        # 6. 边界约束 scroll_offset
        self._keep_reading_anchor(anchor, viewport_height)
        self._cached_block_ranges = list(getattr(self.timeline, "block_ranges", []))
        max_offset = max(0, total_tl_rows - viewport_height)
        self.scroll_offset = max(0, min(self.scroll_offset, max_offset))

        # 7. 视口切片提取
        if total_tl_rows <= viewport_height:
            # 消息还不足一屏：顶部或中间补空格 padding，输入框依然稳稳钉在底部
            padding = [Text()] * (viewport_height - total_tl_rows)
            view_rows = all_tl_rows + padding
            start = 0
        else:
            start = total_tl_rows - viewport_height - self.scroll_offset
            end = total_tl_rows - self.scroll_offset
            view_rows = list(all_tl_rows[start:end])

        # 8. 选区反色高亮渲染（CJK 物理单元格精确映射）
        if self._selection is not None and view_rows:
            sel_s_line, sel_s_col, sel_e_line, sel_e_col = self._selection
            for v_idx in range(len(view_rows)):
                tl_line = (start + v_idx) if total_tl_rows > viewport_height else v_idx
                if sel_s_line <= tl_line <= sel_e_line:
                    line_text = view_rows[v_idx].copy()
                    line_len = len(line_text.plain)
                    s_c = cell_to_char_index(line_text.plain, sel_s_col, is_end=False) if tl_line == sel_s_line else 0
                    e_c = cell_to_char_index(line_text.plain, sel_e_col, is_end=True) if tl_line == sel_e_line else line_len
                    s_c = max(0, min(s_c, line_len))
                    e_c = max(s_c, min(e_c, line_len))
                    if e_c > s_c:
                        line_text.stylize("reverse", s_c, e_c)
                    view_rows[v_idx] = line_text

        # 9. 复制成功 Toast 浮层渲染（右上角悬浮展示）
        if time.time() - self._toast_time < 1.5 and view_rows:
            toast_badge = Text(" ✓ 已复制到剪贴板 ", style="bold black on #a6e3a1")
            toast_len = cell_len(toast_badge.plain)
            row = view_rows[0].copy()
            avail_w = (width - 1) if (total_tl_rows > viewport_height) else width
            if avail_w >= toast_len:
                cut = cell_to_char_index(row.plain, avail_w - toast_len, is_end=False)
                row = row[:cut]
                pad = max(0, avail_w - cell_len(row.plain) - toast_len)
                row.append(" " * pad)
                row.append_text(toast_badge)
                view_rows[0] = row

        # 10. 挂载右侧滚动条与轮次标尺（D183）
        view_rows = self._apply_scrollbar(
            view_rows,
            total_tl_rows=total_tl_rows,
            viewport_height=viewport_height,
            width=width,
            start=start,
        )

        return view_rows + completion_rows + ed_rows + st_rows

    def _reading_anchor(self) -> tuple[Any | None, int, int] | None:
        if self.scroll_offset <= 0 or not self._cached_tl_rows or self._cached_viewport_height <= 0:
            return None
        start = max(0, len(self._cached_tl_rows) - self._cached_viewport_height - self.scroll_offset)
        ranges = self._cached_block_ranges or getattr(self.timeline, "block_ranges", [])
        for begin, end, block in ranges:
            if begin <= start < end:
                return block, start - begin, start
        return None, 0, start

    def _keep_reading_anchor(
        self, anchor: tuple[Any | None, int, int] | None, viewport_height: int,
    ) -> None:
        if anchor is None:
            return
        block, intra, old_start = anchor
        start = old_start
        if block is not None:
            for begin, end, current in getattr(self.timeline, "block_ranges", []):
                if current is block:
                    start = begin + min(intra, max(0, end - begin - 1))
                    break
        maximum = max(0, len(self._cached_tl_rows) - viewport_height)
        self.scroll_offset = max(0, min(maximum, len(self._cached_tl_rows) - viewport_height - start))

    def handle_input(self, key: Key) -> bool:
        width = max(1, self.terminal.columns)
        total_height = max(1, self.terminal.rows)
        dock_rows = self._cached_dock_rows or self._get_dock_rows_count(width)
        viewport_height = max(1, total_height - dock_rows)

        # 鼠标滚轮（SGR 1006）
        if key.name == "wheel_up":
            max_offset = self._get_max_offset(width)
            step = 15 if key.alt else 3
            self.scroll_offset = min(max_offset, self.scroll_offset + step)
            return True
        if key.name == "wheel_down":
            step = 15 if key.alt else 3
            self.scroll_offset = max(0, self.scroll_offset - step)
            return True

        # 鼠标左键按下（mouse_down）
        if key.name == "mouse_down":
            # 0. 拦截右侧滚动条点击（D183，方案 4.A + 轮次标尺直达）
            if self._show_scrollbar and key.x >= width and key.y <= viewport_height:
                y_click = max(0, min(viewport_height - 1, key.y - 1))
                total_tl_rows = len(self._cached_tl_rows)
                max_offset = self._get_max_offset(width)
                if y_click in self._turn_mark_rows:
                    target_s = self._turn_mark_rows[y_click]
                    self.scroll_offset = max(0, min(max_offset, total_tl_rows - viewport_height - target_s))
                else:
                    ratio = y_click / max(1, viewport_height - 1)
                    target_start = round(ratio * max(0, total_tl_rows - viewport_height))
                    self.scroll_offset = max(0, min(max_offset, total_tl_rows - viewport_height - target_start))
                self._mouse_down_pos = (key.x, key.y)
                self._selection = None
                self._is_dragging = False
                return True

            # 1. 点击发生在 Dock 区域
            if key.y > viewport_height:
                dock_y = key.y - viewport_height - 1
                completion_rows = self.completion.render(width) if self.completion is not None else []
                ed_rows = self.editor.render(width)
                if len(completion_rows) <= dock_y < len(completion_rows) + len(ed_rows):
                    editor_y = dock_y - len(completion_rows)
                    box_col = max(0, key.x - 1)
                    setter = getattr(self.editor, "set_cursor_from_cell", None)
                    if callable(setter):
                        setter(editor_y, box_col, width=width)
                    if hasattr(self.editor, "focused"):
                        self.editor.focused = True
                    return True
                return False

            # 2. 点击发生在消息视口区域
            tl_line = self._screen_y_to_timeline_line(key.y, width)
            self._mouse_down_pos = (key.x, key.y)
            self._mouse_down_line = tl_line
            self._mouse_down_col = max(0, key.x - 1)
            self._selection = None
            self._is_dragging = False
            return True

        # 鼠标左键拖拽（mouse_drag）
        if key.name == "mouse_drag":
            # 若是从滚动条开始的拖拽，支持拖动滑块快速浏览（D183）
            if self._show_scrollbar and self._mouse_down_pos is not None and self._mouse_down_pos[0] >= width:
                y_drag = max(0, min(viewport_height - 1, key.y - 1))
                total_tl_rows = len(self._cached_tl_rows)
                max_offset = self._get_max_offset(width)
                ratio = y_drag / max(1, viewport_height - 1)
                target_start = round(ratio * max(0, total_tl_rows - viewport_height))
                new_offset = max(0, min(max_offset, total_tl_rows - viewport_height - target_start))
                if new_offset != self.scroll_offset:
                    self.scroll_offset = new_offset
                    return True
                return False

            if self._mouse_down_pos is not None:
                dist = abs(key.x - self._mouse_down_pos[0]) + abs(key.y - self._mouse_down_pos[1])
                if dist >= 1:
                    self._is_dragging = True
                if self._is_dragging and self._mouse_down_line is not None:
                    clamped_y = max(1, min(key.y, viewport_height))
                    clamped_x = max(1, min(key.x, width))
                    cur_line = self._screen_y_to_timeline_line(clamped_y, width)
                    cur_col = max(0, clamped_x - 1)
                    if cur_line is None:
                        total_tl_rows = len(self._cached_tl_rows)
                        if total_tl_rows > 0:
                            cur_line = min(total_tl_rows - 1, max(0, clamped_y - 1))
                    if cur_line is not None:
                        anc_line = self._mouse_down_line
                        anc_col = self._mouse_down_col
                        if (anc_line, anc_col) <= (cur_line, cur_col):
                            new_sel = (anc_line, anc_col, cur_line, cur_col)
                        else:
                            new_sel = (cur_line, cur_col, anc_line, anc_col)
                        # 仅在选区产生实际改变时才触发重绘
                        if new_sel != self._selection:
                            self._selection = new_sel
                            return True
            return False

        # 鼠标左键释放（mouse_up）
        if key.name == "mouse_up":
            # 滚动条拖拽释放直接闭环（D183）
            if self._show_scrollbar and self._mouse_down_pos is not None and self._mouse_down_pos[0] >= width:
                self._selection = None
                self._is_dragging = False
                self._mouse_down_pos = None
                self._mouse_down_line = None
                return True

            # ★ D182：手势消除歧义（Gesture Disambiguation，方案 1A）
            # 判据：是否是真正的拖拽划选（起止不同行，或同行业起止跨度 >= 2 列）
            is_real_drag = False
            if self._is_dragging and self._selection is not None:
                s_line, s_col, e_line, e_col = self._selection
                if s_line != e_line or abs(e_col - s_col) >= 2:
                    is_real_drag = True

            if is_real_drag and self._selection is not None:
                # 拖拽划选完成：提取纯净文本写入剪贴板
                s_line, s_col, e_line, e_col = self._selection
                lines = self._cached_tl_rows or self.timeline.render(max(1, width - 1))
                clean_text = extract_clean_selection(lines, s_line, s_col, e_line, e_col)
                if clean_text.strip():
                    copy_to_system_clipboard(clean_text)
                    self._toast = "已复制到剪贴板"
                    self._toast_time = time.time()
                self._selection = None
                self._is_dragging = False
                self._mouse_down_pos = None
                self._mouse_down_line = None
                return True
            else:
                # 单击事件（或手部微抖 <= 1 列仲裁为单击）
                self._selection = None
                self._is_dragging = False
                self._mouse_down_pos = None
                self._mouse_down_line = None

                # 视口内单击
                if key.y <= viewport_height:
                    # 单击卡片：精准切换展开/折叠状态 (3A)
                    tl_line = self._screen_y_to_timeline_line(key.y, width)
                    if tl_line is not None and hasattr(self.timeline, "toggle_card_at_line"):
                        anchor_block, intra_offset = self.get_viewport_anchor(width)
                        if self.timeline.toggle_card_at_line(tl_line):
                            self.restore_viewport_anchor(anchor_block, intra_offset, width)
                            return True
                    return True
                return False

        # 键盘翻页
        if key.name == "pageup":
            max_offset = self._get_max_offset(width)
            step = max(1, viewport_height // 2)
            self.scroll_offset = min(max_offset, self.scroll_offset + step)
            return True
        if key.name == "pagedown":
            step = max(1, viewport_height // 2)
            self.scroll_offset = max(0, self.scroll_offset - step)
            return True

        # 直达会话起点与末尾
        if key.name == "home" and (key.ctrl or (self.scroll_offset > 0 and not getattr(self.editor, "text", ""))):
            self.scroll_offset = self._get_max_offset(width)
            return True
        if key.name == "end" and (key.ctrl or (self.scroll_offset > 0 and not getattr(self.editor, "text", ""))):
            self.scroll_offset = 0
            return True

        # Esc 退出历史浏览态
        if key.name == "escape" and self.scroll_offset > 0:
            self.scroll_offset = 0
            return True

        # 转发至常规输入组件
        if self.editor.handle_input(key):
            return True

        # ★ D185：输入框未消费按键时，冒泡至此的 Ctrl+O / Ctrl+T 锚定保护 (1.A + 2.A)
        if key.ctrl and key.name in ("o", "t"):
            anchor_block, intra_offset = self.get_viewport_anchor(width)
            if self.timeline.handle_input(key):
                self.restore_viewport_anchor(anchor_block, intra_offset, width)
                return True
            return False

        return self.timeline.handle_input(key)

    def invalidate(self) -> None:
        self._cached_tl_rows = []
        self._cached_width = 0
        self._cached_dock_rows = 0
        self._cached_viewport_height = 0
        self._show_scrollbar = False
        self._turn_mark_rows = {}
        self.timeline.invalidate()
        self.editor.invalidate()
        self.status.invalidate()
        if self.completion is not None and hasattr(self.completion, "invalidate"):
            self.completion.invalidate()


class FullscreenApp(InlineApp):
    """全屏备用屏模式应用（继承自 InlineApp，100% 复用核心事件机与组件体系）。"""

    def __init__(
        self,
        *,
        runtime: Any,
        terminal: Terminal | None = None,
        theme_name: str | None = None,
        session_start: Any | None = None,
    ) -> None:
        super().__init__(
            runtime=runtime,
            terminal=terminal,
            theme_name=theme_name,
            session_start=session_start,
        )
        self._restored = False
        # 替换 root 布局为 FullscreenLayout
        self.screen.remove(self.root)
        self.root = FullscreenLayout(
            self.terminal,
            self.timeline,
            self.editor,
            self.status,
            screen=self.screen,
        )
        self.screen.add(self.root)

    def _clear_screen(self) -> None:
        """进入全屏备用屏并启动鼠标捕获。"""
        self.terminal.write(f"{ENTER_ALT_SCREEN}{ENABLE_MOUSE_SGR}")

    def _restore_screen(self) -> None:
        """退出应用时恢复主屏与光标状态。"""
        self.restore_terminal()
        super()._restore_screen()

    def restore_terminal(self) -> None:
        """幂等恢复终端至主屏幕。"""
        if not self._restored:
            self._restored = True
            with contextlib.suppress(Exception):
                self.terminal.write(FULLSCREEN_CLEANUP)

    def _park_cursor(self) -> None:
        """在备用屏模式下停用光标下移（因为备用屏退出后终端自动恢复原位）。"""
        return None

    def _dispatch(self, key: Key) -> None:
        # Esc 键：若处于查看历史对白状态，优先返回底部最新对白
        if key.name == "escape" and not self.screen.has_overlay() and self.root.scroll_offset > 0:
            self.root.scroll_offset = 0
            self.screen.request_render(force=True)
            return

        # ★ D185：Ctrl+O 与 Ctrl+T 全局展开/折叠首行视口锚定（Scroll Anchoring，方案 1.A + 2.A）
        if key.ctrl and key.name in ("o", "t"):
            anchor_block, intra_offset = self.root.get_viewport_anchor(self.terminal.columns)
            if key.name == "o":
                self.timeline.buffer.toggle_expand_tools()
            else:
                self.timeline.buffer.toggle_expand_reasoning()
            self.root.restore_viewport_anchor(anchor_block, intra_offset, self.terminal.columns)
            self.screen.request_render(force=True)
            return

        # 视口滚动快捷键与鼠标交互事件拦截
        if key.name in ("wheel_up", "wheel_down", "pageup", "pagedown", "mouse_down", "mouse_drag", "mouse_up") and self.root.handle_input(key):
            self.screen.request_render(force=True)  # ★ 按下/拖拽/释放/滚轮即时响应（下一 tick 单帧合并，D181）
            return

        super()._dispatch(key)

    def _on_submit(self, text: str) -> None:
        self.root.scroll_offset = 0
        super()._on_submit(text)

    async def run(self) -> None:
        """带 fail-safe 保护的全屏运行循环。"""
        atexit.register(_emergency_restore)
        try:
            await super().run()
        finally:
            atexit.unregister(_emergency_restore)
            self.restore_terminal()


def run_fullscreen(
    runtime: Any,
    *,
    terminal: Terminal | None = None,
    session_start: Any | None = None,
) -> int:
    """全屏备用屏模式同步入口：给 CLI 使用。"""
    term = terminal or make_terminal()
    app = FullscreenApp(runtime=runtime, terminal=term, session_start=session_start)
    try:
        asyncio.run(app.run())
    except KeyboardInterrupt:
        return 130
    finally:
        term.stop()
        app.restore_terminal()
        if hasattr(runtime, "close") and callable(runtime.close):
            runtime.close()
    return 0
