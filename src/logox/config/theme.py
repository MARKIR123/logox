"""主题文件的发现、加载与对比度校验（UI-SPEC §8 / D35 / D43）。

职责
----
* 发现 ``themes/*.toml``（用户目录优先于内置目录，同名时用户胜出）
* 把 TOML 加载成 :class:`~logox.config.schema.ThemeFile`，失败时抛
  :class:`~logox.errors.ThemeError`（E-12/E-13：**拒绝加载并列出缺失项**，
  不做静默兜底——部分正确的配色比明确的失败更难排查）
* 按 §3.3 校验对比度

不负责
------
不渲染任何东西、不把颜色应用到组件（那是 ``tui/`` 的职责）。

> **内置主题文件（``logox-dark`` / ``logox-light`` / ``logox-contrast`` 的
> ``.toml``）在 M1.5 视觉骨架阶段交付**。本模块在 M1 只提供机制与校验，
> 因此不会把尚未与 Catppuccin 官方色板逐值核对过的十六进制值硬编码进代码。
"""

from __future__ import annotations

import tomllib
from collections.abc import Iterable, Sequence
from pathlib import Path

from pydantic import ValidationError

from logox.config.schema import SCHEMA_VERSION, ThemeFile
from logox.errors import ThemeError

__all__ = [
    "BUILTIN_THEMES",
    "contrast_ratio",
    "discover_themes",
    "load_theme",
    "mix",
    "relative_luminance",
    "validate_contrast",
]

BUILTIN_THEMES: tuple[str, ...] = ("logox-dark", "logox-light", "logox-contrast")

# UI-SPEC §3.3：正文 / 次要文本 / 高对比主题的最低对比度
MIN_CONTRAST_PRIMARY = 4.5
MIN_CONTRAST_MUTED = 3.0
MIN_CONTRAST_HIGH = 7.0


def discover_themes(dirs: Iterable[Path]) -> dict[str, Path]:
    """扫描若干目录下的 ``*.toml``，返回 ``主题名 → 文件路径``。

    ``dirs`` **按优先级从低到高**传入：后出现的目录覆盖先出现的同名主题，
    因此调用方应按 ``(内置目录, 用户目录)`` 的顺序传入。
    """
    found: dict[str, Path] = {}
    for directory in dirs:
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.toml")):
            found[path.stem] = path
    return found


def load_theme(path: Path) -> ThemeFile:
    """加载并校验一个主题文件。

    :raises ThemeError: 文件不可读 / TOML 语法错误 / schema 不匹配 / 缺少 token。
        错误消息会**列出全部缺失或非法的 token**，而不是只说"加载失败"。
    """
    path = Path(path)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ThemeError(path, f"无法读取主题文件：{exc}") from exc

    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise ThemeError(path, f"主题文件不是有效的 UTF-8：{exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ThemeError(path, f"主题文件 TOML 语法错误：{exc}") from exc

    version = data.get("schema_version", SCHEMA_VERSION)
    if version != SCHEMA_VERSION:
        raise ThemeError(path, f"schema_version 为 {version}，本版本只支持 {SCHEMA_VERSION}（不做自动迁移）")

    try:
        return ThemeFile.model_validate(data)
    except ValidationError as exc:
        raise ThemeError(path, _format_theme_errors(exc)) from exc


def load_theme_by_name(name: str, dirs: Sequence[Path]) -> ThemeFile:
    """按名称在候选目录中查找并加载主题（用户目录优先）。"""
    available = discover_themes(dirs)
    if name not in available:
        known = sorted({*available, *BUILTIN_THEMES})
        raise ThemeError(Path(name), f"主题 {name!r} 不存在；可用主题：{'、'.join(known)}")
    return load_theme(available[name])


def _format_theme_errors(exc: ValidationError) -> str:
    parts: list[str] = []
    missing: list[str] = []
    for error in exc.errors():
        loc = ".".join(str(item) for item in error.get("loc", ()))
        if error.get("type") == "missing":
            missing.append(loc)
            continue
        parts.append(f"{loc or '<root>'}：{error.get('msg', '')}")
    if missing:
        parts.insert(0, f"缺少必需的 token：{'、'.join(missing)}")
    return "；".join(parts) if parts else "主题校验失败"


# --------------------------------------------------------------------------- #
# 颜色工具与对比度校验
# --------------------------------------------------------------------------- #


def _rgb(color: str) -> tuple[int, int, int]:
    text = color.lstrip("#")
    if len(text) != 6:
        raise ValueError(f"仅接受 #RRGGBB 形式的颜色，收到 {color!r}")
    return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)


def relative_luminance(color: str) -> float:
    """WCAG 相对亮度。"""
    channels = []
    for value in _rgb(color):
        srgb = value / 255.0
        channels.append(srgb / 12.92 if srgb <= 0.04045 else ((srgb + 0.055) / 1.055) ** 2.4)
    red, green, blue = channels
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def contrast_ratio(foreground: str, background: str) -> float:
    """WCAG 对比度（1.0 – 21.0）。"""
    lighter = max(relative_luminance(foreground), relative_luminance(background))
    darker = min(relative_luminance(foreground), relative_luminance(background))
    return (lighter + 0.05) / (darker + 0.05)


def mix(base: str, accent: str, ratio: float) -> str:
    """按比例混合两色（UI-SPEC §8.1：diff 行背景 = ``mix(base, 语义色, 0.15)``）。"""
    base_rgb = _rgb(base)
    accent_rgb = _rgb(accent)
    # strict=True：两个 RGB 三元组长度必然相等，长度不等说明上游 _rgb 坏了。
    # 用默认的静默截断会把一个真 bug 变成"颜色稍微不对"，极难发现。
    blended = tuple(round(b + (a - b) * ratio) for b, a in zip(base_rgb, accent_rgb, strict=True))
    return "#" + "".join(f"{value:02x}" for value in blended)


def validate_contrast(theme: ThemeFile) -> list[str]:
    """按 UI-SPEC §3.3 校验对比度，返回问题清单（空 = 通过）。

    高对比主题（``variant == "high_contrast"``）要求正文 ≥ 7:1；其余主题
    正文 ≥ 4.5:1、次要文本 ≥ 3:1。
    """
    problems: list[str] = []
    palette = theme.palette
    high = theme.variant == "high_contrast"
    primary_min = MIN_CONTRAST_HIGH if high else MIN_CONTRAST_PRIMARY

    primary = contrast_ratio(palette.text_primary, palette.bg_base)
    if primary < primary_min:
        problems.append(
            f"text_primary/bg_base 对比度 {primary:.2f}:1 低于要求的 {primary_min}:1"
            + ("（高对比主题）" if high else "")
        )

    muted = contrast_ratio(palette.text_muted, palette.bg_base)
    if muted < MIN_CONTRAST_MUTED:
        problems.append(f"text_muted/bg_base 对比度 {muted:.2f}:1 低于要求的 {MIN_CONTRAST_MUTED}:1")

    # -- D152-b：输入框专属 token 的**下限校验** --
    #
    # 为什么必须进校验，而不是"把内置主题的值改亮就行"：
    # 只改内置值的话，**下一个自定义主题照样能配出看不见的输入框**，而症状
    # （"输入框看不清"）与原因（"主题配错了"）之间的因果链很长 —— 用户只会再报一次同样的障。
    # 写进校验后，错误在**加载时**就带着 token 名与实测对比度被拒绝。
    #
    # `input_text` 是**正文**，与 text_primary 同级要求；
    # `input_border` 是"聚焦框"（UI-SPEC:102 的措辞），取次要级 3.0 ——
    # 而它原来借用的 border_subtle 只有 1.30:1，正是用户报障的那个数。
    input_text_ratio = contrast_ratio(palette.input_text, palette.bg_base)
    if input_text_ratio < primary_min:
        problems.append(
            f"input_text/bg_base 对比度 {input_text_ratio:.2f}:1 低于要求的 {primary_min}:1"
            + ("（高对比主题）" if high else "")
        )

    input_border_ratio = contrast_ratio(palette.input_border, palette.bg_base)
    if input_border_ratio < MIN_CONTRAST_MUTED:
        problems.append(
            f"input_border/bg_base 对比度 {input_border_ratio:.2f}:1 低于要求的 "
            f"{MIN_CONTRAST_MUTED}:1（输入框框线是**聚焦框**，不能弱到看不见）"
        )

    # -- D161：思考与工具输出的专属 token（接通后补的校验）--
    #
    # 为什么这批也要校验：它们**刚刚才从"死配置"变成"真的会被画出来"**。
    # 在接通之前，`thinking_off` 只有 1.91:1（dark）/ 1.54:1（light）而**没有任何人受影响**——
    # 因为没人读它。接通之后同一个值会真的显示出来：一个 1.54:1 的档位词＝看不见的字。
    # **值没变，但后果变了** —— 这正是"接通"必须配套"加校验"的原因。
    #
    # 阈值口径：
    #   * `tool_output_fg` 是**工具结果正文**（用户展开卡片就想读它）→ 正文级
    #   * `thinking_text` 是展开的思考正文，读它是"可选行为" → 次要级
    #   * 4 个档位色是状态行的词 → 次要级
    tool_output_ratio = contrast_ratio(palette.tool_output_fg, palette.bg_base)
    if tool_output_ratio < primary_min:
        problems.append(
            f"tool_output_fg/bg_base 对比度 {tool_output_ratio:.2f}:1 低于要求的 {primary_min}:1"
            + ("（高对比主题）" if high else "")
            + "（工具结果正文，用户展开卡片就是为了读它）"
        )

    for token in ("thinking_text", "thinking_off", "thinking_low", "thinking_medium", "thinking_high"):
        ratio = contrast_ratio(getattr(palette, token), palette.bg_base)
        if ratio < MIN_CONTRAST_MUTED:
            problems.append(
                f"{token}/bg_base 对比度 {ratio:.2f}:1 低于要求的 {MIN_CONTRAST_MUTED}:1"
                "（它会作为**文字**画在屏幕上：思考正文或状态行的档位词）"
            )

    for name in ("accent", "success", "warning", "danger", "info"):
        ratio = contrast_ratio(getattr(palette, name), palette.bg_base)
        if ratio < MIN_CONTRAST_MUTED:
            problems.append(f"{name}/bg_base 对比度 {ratio:.2f}:1 低于要求的 {MIN_CONTRAST_MUTED}:1")

    return problems
