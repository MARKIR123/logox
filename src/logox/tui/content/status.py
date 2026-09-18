"""状态行的**纯逻辑**（D16 / D38 / D39 / D42 / UI-SPEC §5.1）。

为什么它从 `widgets/status_bar.py` 里搬出来
=========================================

理由与 :mod:`logox.tui.content.overlay` 相同：原文件顶部 import 了 Textual，而
D80 之后的纯净终端流渲染器要用**同一套**裁剪算法。这套算法是 UI-SPEC §5.1
专门设计的（"超宽时先合并 usage+cache，再按优先级摘除"），重写一遍必然走样，
所以按分层约定拆开：

=============================== ==============================================
``logox.tui.content.status``       **纯逻辑**：度量 → 一行文本（本文件）
``logox.tui.widgets.status_bar`` Textual 包装（固定最后一行），转发到本文件
=============================== ==============================================

三个不能违背的规则
------------------
* **不直接读内核状态**（R3 红线）：数据只来自 ``SessionMetrics``。
* **右端运行提示与告警标记永不裁剪**——它们是"现在在干什么"的唯一常驻线索。
* **无数据就整项不显示**，不显示假的 ``0``（D39：``cache`` 未上报时不显示 ``cache 0%``）。
"""

from __future__ import annotations

from dataclasses import dataclass

from rich.cells import cell_len
from rich.text import Text

from logox.config.schema import StatusItems, ThemePalette, TimingFields
from logox.tui.format import (
    EMPTY,
    format_clock,
    format_cost,
    format_ratio,
    format_tokens,
)
from logox.tui.metrics import SessionMetrics

__all__ = [
    "CONTEXT_DANGER",
    "CONTEXT_WARN",
    "SEPARATOR",
    "TRIM_PRIORITY",
    "StatusContext",
    "build_status_line",
    "status_item_keys",
]

SEPARATOR = " · "
"""状态项之间的分隔符（UI-SPEC §5.1）。"""

#: 裁剪优先级：数字越大**越先被裁掉**。见 UI-SPEC §5.1 的表。
TRIM_PRIORITY: dict[str, int] = {
    "session": 13,
    "git": 12,
    "permission": 11,
    "timing": 10,
    "usage": 9,
    "turn": 8,
    "tool": 7,
    "cache": 6,
    "throughput": 5,
    "context": 4,
    "model": 3,
}

#: 上下文占用率的告警阈值（UI-SPEC §5.1）
CONTEXT_WARN = 0.75
CONTEXT_DANGER = 0.90

#: 缓存命中率低于此值时提示（通常意味着前缀频繁变动或刚压缩过）
CACHE_WARN = 0.30


def status_item_keys() -> list[str]:
    """按优先级（最后被裁的在前）返回全部状态项 key。"""
    return sorted(TRIM_PRIORITY, key=lambda key: TRIM_PRIORITY[key])


@dataclass(frozen=True)
class StatusContext:
    """渲染状态栏所需的全部外部输入（**不含任何内核对象**）。"""

    palette: ThemePalette
    items: StatusItems
    timing_fields: TimingFields
    width: int
    tool_glyph: str = "⏺"
    git_branch: str | None = None
    session_label: str | None = None
    queue_label: str | None = None


def _context_style(palette: ThemePalette, ratio: float) -> str:
    if ratio >= CONTEXT_DANGER:
        return palette.danger
    if ratio >= CONTEXT_WARN:
        return palette.warning
    return palette.text_muted


def _render_item(
    key: str, metrics: SessionMetrics, context: StatusContext
) -> tuple[Text, bool] | None:
    """渲染单个状态项。返回 ``(Text, 是否需要关注)``；``None`` 表示**无数据、整项不显示**。"""
    palette = context.palette
    muted = palette.text_muted
    faint = palette.text_faint

    if key == "model":
        # D42：`<模型名> · <思考档位>`；**非 off 档位均显示**
        model = metrics.model or EMPTY
        effort = metrics.thinking_effort
        text = Text()
        text.append(model, style=palette.accent)
        if effort and effort != "off":
            text.append(f" · {effort}", style=muted)
        return text, False

    if key == "context":
        ratio = metrics.context_ratio
        if metrics.context_window > 0:
            tokens_str = format_tokens(metrics.context_tokens)
            win_str = format_tokens(metrics.context_window)
            ctx_text = f"ctx {tokens_str}/{win_str} ({format_ratio(ratio)})"
        else:
            ctx_text = f"ctx {format_ratio(ratio)}"
        text = Text(ctx_text, style=_context_style(palette, ratio))
        return text, ratio >= CONTEXT_WARN

    if key == "throughput":
        if metrics.throughput is None:
            return None  # 没算出来就不显示，不显示 0 tok/s
        return Text(f"{metrics.throughput:.0f} tok/s", style=muted), False

    if key == "cache":
        ratio = metrics.cache_ratio
        if ratio is None:
            return None  # 厂商未上报缓存字段 → 整项不显示
        style = palette.warning if ratio < CACHE_WARN else muted
        cache_str = f"cache {format_ratio(ratio)}"
        if metrics.cached_input and metrics.cached_input > 0:
            cache_str += f" ({format_tokens(metrics.cached_input)})"
        return Text(cache_str, style=style), ratio < CACHE_WARN

    if key == "turn":
        return Text(f"#{metrics.turn}", style=muted), False

    if key == "usage":
        text = Text()
        text.append(f"{format_tokens(metrics.total_tokens)} tok", style=muted)
        if metrics.cost_usd:
            text.append(f" · {format_cost(metrics.cost_usd)}", style=muted)
        return text, False

    if key == "tool":
        if metrics.running_tool is None:
            return None
        return Text(f"{context.tool_glyph} {metrics.running_tool}", style=palette.accent), False

    if key == "timing":
        fields = context.timing_fields
        parts: list[str] = []
        if fields.total:
            parts.append(f"Σ {format_clock(metrics.total_ms)}")
        if fields.llm:
            parts.append(f"llm {format_clock(metrics.llm_ms)}")
        if fields.tool:
            parts.append(f"tool {format_clock(metrics.tool_ms)}")
        if not parts:
            return None  # 三个字段全关 → 整项跳过，不留下孤立的分隔符
        return Text(" · ".join(parts), style=faint), False

    if key == "permission":
        if metrics.pending_permissions <= 0:
            return None
        return (
            Text(f"ask · {metrics.pending_permissions} pending", style=palette.warning),
            metrics.pending_permissions > 0,
        )

    if key == "git":
        if not context.git_branch:
            return None
        return Text(context.git_branch, style=faint), False

    if key == "session":
        if not context.session_label:
            return None
        return Text(context.session_label, style=faint), False

    return None


def _right_hint(metrics: SessionMetrics, context: StatusContext) -> Text:
    """右端运行提示——**永不裁剪**（D39 / R4）。"""
    palette = context.palette
    text = Text()
    if metrics.busy:
        text.append(f"{context.tool_glyph} esc to interrupt", style=palette.accent)
    if context.queue_label:
        if text.plain:
            text.append(SEPARATOR, style=palette.text_faint)
        text.append(context.queue_label, style=palette.text_muted)
    if metrics.retry is not None:
        if text.plain:
            text.append(SEPARATOR, style=palette.text_faint)
        text.append(
            f"retry {metrics.retry.attempt} in {metrics.retry.delay_s:.0f}s", style=palette.warning
        )
    return text


def build_status_line(metrics: SessionMetrics, context: StatusContext) -> Text:
    """把度量压进一行——**含宽度不足时的裁剪与合并**（UI-SPEC §5.1 算法）。

    步骤：渲染全部启用项 → 超宽则先合并 ``usage`` 与 ``cache`` → 仍超宽则按
    优先级从高编号开始逐个摘除 → 右端运行提示与告警标记始终保留。
    """
    palette = context.palette
    enabled = set(context.items.enabled_keys())
    right = _right_hint(metrics, context)
    right_width = cell_len(right.plain)
    # 左端还要给告警标记留位
    alert = "?" if metrics.pending_permissions > 0 else ""
    budget = max(0, context.width - right_width - (cell_len(SEPARATOR) if right_width else 0) - (2 if alert else 0))

    # 1) 渲染全部启用项
    rendered: list[tuple[str, Text, bool]] = []
    for key in status_item_keys():
        if key not in enabled:
            continue
        item = _render_item(key, metrics, context)
        if item is None:
            continue
        rendered.append((key, item[0], item[1]))

    def total_width(items: list[tuple[str, Text, bool]]) -> int:
        if not items:
            return 0
        return sum(cell_len(text.plain) for _, text, _ in items) + cell_len(SEPARATOR) * (len(items) - 1)

    # 2) 先尝试合并 usage + cache（它们描述同一件事：token 用量）
    keys = {key for key, _, _ in rendered}
    if total_width(rendered) > budget and {"usage", "cache"} <= keys:
        merged: list[tuple[str, Text, bool]] = []
        for key, text, alerting in rendered:
            if key == "usage":
                ratio = metrics.cache_ratio
                combined = Text()
                combined.append(f"{format_tokens(metrics.total_tokens)} tok", style=palette.text_muted)
                if ratio is not None:
                    combined.append(f" (cache {format_ratio(ratio)})", style=palette.text_muted)
                if metrics.cost_usd:
                    combined.append(f" · {format_cost(metrics.cost_usd)}", style=palette.text_muted)
                merged.append(("usage", combined, alerting))
            elif key == "cache":
                continue  # 已并入 usage
            else:
                merged.append((key, text, alerting))
        rendered = merged

    # 3) 仍超宽 → 按优先级从高编号开始摘除（数字大的先被裁）
    for key in sorted(keys, key=lambda item: -TRIM_PRIORITY[item]):
        if total_width(rendered) <= budget:
            break
        rendered = [entry for entry in rendered if entry[0] != key]

    # 4) 组装
    line = Text()
    if alert:
        line.append(f"{alert} ", style=palette.accent)
    for index, (_, text, _alerting) in enumerate(rendered):
        if index:
            line.append(SEPARATOR, style=palette.text_faint)
        line.append_text(text)

    if right_width:
        gap = context.width - cell_len(line.plain) - right_width - (1 if not alert else 0)
        if gap > 0:
            line.append(" " * gap)
        elif line.plain:
            line.append(SEPARATOR, style=palette.text_faint)
        line.append_text(right)

    if not line.plain:
        # 状态项被裁光时也必须能看到"在干什么"（E-4）
        line.append_text(right if right.plain else Text("logox", style=palette.text_muted))
    return line
