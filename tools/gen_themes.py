"""从 Catppuccin **官方色板**生成 Logox 三套内置主题。

为什么用生成而不是手写
----------------------
UI-SPEC §3.2 / D43 明确要求「色值须以 Catppuccin 官方色板为准」。手抄十六进制值
无法防止抄错，也无法在官方更新色板后察觉。本脚本从
``tools/vendor/catppuccin-palette.json``（官方 ``palette.json``，已随仓库固定版本）
读取色值、映射到 Logox 的 18 个语义 token、按规则派生 diff 背景，并用 Logox 自己的
``ThemeFile`` 模型 + ``writer`` 输出 TOML——**生成物一定能被 Logox 加载**。

用法::

    python tools/gen_themes.py            # 重新生成
    python tools/gen_themes.py --check    # 只校验现有主题文件与色板一致（CI 友好）
"""

from __future__ import annotations

import argparse
import colorsys
import json
import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from logox.config import writer  # noqa: E402
from logox.config.schema import ThemeFile  # noqa: E402
from logox.config.theme import contrast_ratio, mix, relative_luminance, validate_contrast  # noqa: E402

PALETTE_PATH = REPO_ROOT / "tools" / "vendor" / "catppuccin-palette.json"
THEMES_DIR = REPO_ROOT / "src" / "logox" / "tui" / "themes"

#: diff 行背景的**最大**派生比例（UI-SPEC §8.1）
DIFF_MIX_RATIO_MAX = 0.15

#: diff 行前景对自身背景的最低对比度（UI-SPEC §3.3；diff 行是正文，理应达标）
DIFF_FG_CONTRAST = 3.0


def derive_diff_bg(base: str, foreground: str) -> tuple[str, float]:
    """派生 diff 行背景，**取满足对比度要求的最大混合比**。

    为什么不能硬编码 0.15：``mix(base, fg, 0.15)`` 在深色主题上没问题（实测加行 7.62:1），
    但在**浅色主题**上会把背景压暗，深色前景反而只有 2.77:1——低于 3:1。
    正确做法是把混合比作为**上限**，从小到大试到刚好达标：

    * 深色主题 → 会用满 0.15（背景够暗，前景够亮）
    * 浅色主题 → 自动降到更小的比例（背景更浅，深色前景才看得清）

    返回 ``(背景色, 实际使用的比例)``。
    """
    best = mix(base, foreground, DIFF_MIX_RATIO_MAX)
    best_ratio = DIFF_MIX_RATIO_MAX
    ratio = DIFF_MIX_RATIO_MAX
    while ratio > 0.02:
        candidate = mix(base, foreground, ratio)
        if contrast_ratio(foreground, candidate) >= DIFF_FG_CONTRAST:
            return candidate, ratio
        best, best_ratio = candidate, ratio
        ratio = round(ratio - 0.01, 4)
    return best, best_ratio

#: 前景色对背景的最低对比度（UI-SPEC §3.3）
TARGET_FG_CONTRAST = 3.0

#: 需要做对比度校正的前景色 token（其余是背景/边框类，不参与前景对比）
FG_TOKENS = ("accent", "success", "warning", "danger", "info", "diff_add_fg", "diff_del_fg")

#: **正文级**对比度目标（4.5）。UI-SPEC §3.3 对"正文"的要求，比次要文本高一档。
BODY_FG_CONTRAST = 4.5

#: 需要按**正文级**校正的 D79 前景 token。
#:
#: 为什么它们要更高一档：这些是"用户**主动要读**的正文"，而不是界面外壳
#: （状态行、提示语才是次要文本）。
#: 实测：`tool_output_fg` 在浅色主题上等于 `text_muted`，只有 **4.37:1** ——
#: 它一直没人管，因为它没被任何校验覆盖。
_D79_BODY_TOKENS = ("tool_output_fg",)

#: Catppuccin 色名 → Logox 语义 token（Mocha 与 Latte 共用同一套映射，
#: 这正是选 Catppuccin 的原因：同家族深浅双版语义一一对应）
SEMANTIC_MAP = {
    "bg_base": "base",
    "bg_raised": "mantle",
    "bg_overlay": "surface0",
    "overlay_scrim": "crust",
    "text_primary": "text",
    "text_muted": "subtext0",
    "text_faint": "overlay0",
    "accent": "blue",
    "success": "green",
    "warning": "yellow",
    "danger": "red",
    "info": "sky",
    "border_subtle": "surface0",
    "border_strong": "surface2",
    # -- D152-a：输入框专属 --
    # 深色取 overlay1（4.44:1）而不是 surface2（2.46:1）：后者与旧的"看不清"同档，
    # 改了等于没改；也**不**取 overlay2（5.81:1）——那会让框线比部分正文更抢眼，
    # 输入框会变成界面上最重的东西。4.44 是"一眼看得见、但不喧宾夺主"的位置。
    # 浅色取 overlay2（3.49:1）：浅色主题的可用档位整体偏窄（overlay1 只有 2.83）。
    "input_border": "overlay1",
    "input_text": "text",
    "input_hint": "overlay0",
    "diff_add_fg": "green",
    "diff_del_fg": "red",
}

#: 浅色主题需要**换档**的 token（Catppuccin 同家族深浅双版的位序并非总是一一对应）
LIGHT_OVERRIDES = {
    # 框线在深色用 overlay1（4.44:1），浅色同档只有 2.83:1 —— 掉到次要阈值 3.0 之下。
    # 升一档到 overlay2（3.49:1）刚好过线，且仍弱于正文（7.06:1）。
    "input_border": "overlay2",
}

#: 自研高对比主题（D43：以 Mocha 为骨架加深，正文对比度 ≥ 7:1）。
#: **不得仅靠颜色表意**——符号兜底由组件的 glyph 集负责（UI-SPEC §10）。
#: 这里只写 16 个基础 token；``diff_*_bg`` 由 ``finalize_palette`` 派生。
CONTRAST_PALETTE = {    "bg_base": "#000000",
    "bg_raised": "#0a0a0a",
    "bg_overlay": "#1a1a1a",
    "overlay_scrim": "#000000",
    "text_primary": "#ffffff",
    "text_muted": "#c6c6c6",
    "text_faint": "#9a9a9a",
    "accent": "#79c0ff",
    "success": "#7ee787",
    "warning": "#ffd33d",
    "danger": "#ff7b72",
    "info": "#a5d6ff",
    "border_subtle": "#4d4d4d",
    "border_strong": "#bfbfbf",
    # D152-a：输入框专属。高对比主题下"看得清"就是全部目的，
    # 所以框线不再走"弱分隔"档（原 border_subtle #4d4d4d 只有 2.48:1）。
    "input_border": "#b0b0b0",
    "input_text": "#ffffff",
    "input_hint": "#9a9a9a",
    "diff_add_fg": "#7ee787",
    "diff_del_fg": "#ff7b72",
}


def _rgb(color: str) -> tuple[int, int, int]:
    text = color.lstrip("#")
    return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)


def _srgb_to_linear(channel: float) -> float:
    """sRGB 分量 → 线性光（标准传递函数）。"""
    return channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4


def _linear_to_srgb(channel: float) -> float:
    return channel * 12.92 if channel <= 0.0031308 else 1.055 * (channel ** (1 / 2.4)) - 0.055


def mix_linear(base: str, accent: str, ratio: float) -> str:
    """在**线性光**空间按比例混合两色（用于"内容分层底色"）。

    为什么不能用 `config.theme.mix`（sRGB 空间直接插值）：
    显示器上的亮度与 sRGB 数值**不成正比**，所以直接插值会把颜色往中间灰拉——
    实测 ``mix('#1e1e2e', '#a6e3a1', 0.12)`` 得到 ``#2e363c``（饱和度 0.10），
    看上去是一块灰，看不出"这是成功"。

    线性光混合保持了观感上的色相与饱和度，因此**可以用很小的比例**
    得到"看得出颜色、但仍然是低调背景"的效果（配合 `_BG_DERIVATIONS` 的 0.03–0.08）。

    注意：`config.theme.mix` 仍然用于它原本的用途（diff 行背景），那里已经过
    UI-SPEC §8.1 的验证，**不要顺手改掉**。
    """
    base_rgb = _rgb(base)
    accent_rgb = _rgb(accent)
    out: list[int] = []
    for base_channel, accent_channel in zip(base_rgb, accent_rgb, strict=True):
        linear = _srgb_to_linear(base_channel / 255)
        target = _srgb_to_linear(accent_channel / 255)
        blended = linear + (target - linear) * ratio
        out.append(round(max(0.0, min(1.0, _linear_to_srgb(blended))) * 255))
    return "#" + "".join(f"{value:02x}" for value in out)


def _hex(rgb: tuple[float, float, float]) -> str:
    return "#" + "".join(f"{max(0, min(255, round(channel * 255))):02x}" for channel in rgb)


def ensure_contrast(color: str, background: str, target: float = TARGET_FG_CONTRAST) -> tuple[str, bool]:
    """在**保持色相与饱和度**的前提下调整明度，直到对比度达标。

    为什么需要它：Catppuccin 的 accent 色是为「强调」调的，不是为「在浅底上当文字」调的。
    实测 Latte 的 ``green``/``yellow``/``sky`` 在 ``base`` 上只有 2.96 / 2.31 / 2.47:1，
    低于 UI-SPEC §3.3 要求的 3:1。与其换色（会破坏语义），不如**只压明度**——
    色相不变，语义仍是"绿/黄/蓝"，只是变深了。

    返回 ``(颜色, 是否调整过)``。
    """
    if contrast_ratio(color, background) >= target:
        return color, False

    darker = relative_luminance(background) > 0.5  # 亮背景 → 压暗；暗背景 → 提亮
    red, green, blue = _rgb(color)
    hue, lightness, saturation = colorsys.rgb_to_hls(red / 255, green / 255, blue / 255)

    candidate = color
    for _ in range(100):
        lightness = max(0.0, lightness - 0.02) if darker else min(1.0, lightness + 0.02)
        candidate = _hex(colorsys.hls_to_rgb(hue, lightness, saturation))
        if contrast_ratio(candidate, background) >= target:
            return candidate, True
        if (darker and lightness <= 0.0) or (not darker and lightness >= 1.0):
            break
    return candidate, True


def _flavour_colors(palette: dict, flavour: str) -> dict[str, str]:
    return {name: entry["hex"] for name, entry in palette[flavour]["colors"].items()}


def map_semantics(colors: dict[str, str]) -> dict[str, str]:
    """把 Catppuccin 色名映射到 Logox 语义 token。"""
    return {token: colors[source] for token, source in SEMANTIC_MAP.items()}


def finalize_palette(resolved: dict[str, str], *, adjustments: list[str] | None = None) -> dict[str, str]:
    """对比度校正前景色，然后**在校正之后**派生 diff 行背景。

    顺序很重要：若先派生 diff 背景再校正前景色，diff 背景会用旧色算出，
    导致「同一语义在不同位置有两种色」的隐蔽不一致。
    """
    palette = dict(resolved)
    for token in FG_TOKENS:
        corrected, changed = ensure_contrast(palette[token], palette["bg_base"])
        if changed:
            if adjustments is not None:
                adjustments.append(f"{token}: {palette[token]} → {corrected}")
            palette[token] = corrected

    palette["diff_add_bg"], add_ratio = derive_diff_bg(palette["bg_base"], palette["diff_add_fg"])
    palette["diff_del_bg"], del_ratio = derive_diff_bg(palette["bg_base"], palette["diff_del_fg"])
    if adjustments is not None and (add_ratio, del_ratio) != (DIFF_MIX_RATIO_MAX, DIFF_MIX_RATIO_MAX):
        adjustments.append(
            f"diff 背景混合比下调以满足 {DIFF_FG_CONTRAST}:1 —— "
            f"add={add_ratio:.2f}, del={del_ratio:.2f}（上限 {DIFF_MIX_RATIO_MAX}）"
        )
    return palette


def build_palette(colors: dict[str, str], *, adjustments: list[str] | None = None) -> dict[str, str]:
    """Catppuccin 色名 → 语义 token → 对比度校正 → 派生 diff 背景 → 派生 D79 视觉 token。"""
    return derive_visual_tokens(
        finalize_palette(map_semantics(colors), adjustments=adjustments), colors
    )


#: D79 新增的"内容分层底色"从基础 token **派生**的比例。
#:
#: "内容分层底色"从基础 token **派生**的比例。
#:
#: 为什么派生而不是写死：Pi 的 ``dark.json`` 是给深色终端调的一套固定值。
#: 我们有三套主题（深/浅/高对比），写死会让浅色主题上的"工具成功底"变成一块脏绿。
#: 按比例混合基础色与语义色，三套主题**自动各自合适**，而且与 diff 背景的派生
#: 用的是同一套思路（UI-SPEC §8.1 已经为此建立先例）。
#:
#: ⚠️ **必须在线性光空间混合**（``mix_linear``），这是实测出来的：
#: 同样的比例在 sRGB 空间混出来是**灰泥**——深色底 ``#1e1e2e`` 混 12% 的
#: Catppuccin 绿 ``#a6e3a1`` 只得到 ``#2e363c``（饱和度 0.10，基本看不出是绿）。
#: 改到线性光空间后饱和度 0.27、色相清楚，而对比度仍然很低（"分区"而不"抢眼"）。
#:
#: 比例也因此从 sRGB 时代的 0.12–0.65 缩到 **0.03–0.08**：
#: 线性光的视觉强度高得多，同样的"看得出来"只需更小的比例。
_BG_DERIVATIONS: dict[str, tuple[str, str, float]] = {
    # token: (底色, 混入色, 比例)
    "user_message_bg": ("bg_base", "text_faint", 0.06),  # 中性偏冷的面板底
    "custom_message_bg": ("bg_base", "accent", 0.03),
    "tool_pending_bg": ("bg_base", "text_faint", 0.04),
    "tool_success_bg": ("bg_base", "success", 0.03),
    "tool_error_bg": ("bg_base", "danger", 0.04),
    "selected_bg": ("bg_base", "accent", 0.08),
}

#: 需要"在自身背景上作为文字可读"的 D79 前景 token（沿用同一套对比度校正）
_D79_FG_TOKENS = (
    "user_message_fg",
    "custom_message_fg",
    "custom_message_label",
    "tool_title_fg",
    "tool_output_fg",
    "thinking_text",
    # ★ D161：4 个档位色 + off 也被接通成"真的会画出来的文字"（状态行的档位词），
    #   所以它们也要过同一道校正 —— 实测 `thinking_off` 在**三套主题里全部低于 3.0**
    #   （1.91 / 1.54 / 2.82），因为它原本是刻意压暗的（`mix(bg_base, text_faint, 0.55)`）。
    #   接通之前这个值无害（没人读它）；接通之后它会真的显示成一个看不见的词。
    "thinking_off",
    "thinking_low",
    "thinking_medium",
    "thinking_high",
    "md_heading",
    "md_link",
    "md_code",
    "md_code_block",
    "md_quote",
    "md_list_bullet",
)


def derive_visual_tokens(
    palette: dict[str, str], colors: dict[str, str], *, syntax: str = "colour"
) -> dict[str, str]:
    """派生 D79 的视觉 token（对齐 Pi 的语义色，但**跟着主题走**）。

    分三类处理，每类的道理不同：

    1. **内容分层底色** —— 从 ``bg_base`` 混入对应语义色，比例很小。见 ``_BG_DERIVATIONS``。
    2. **前景色** —— 直接取已有语义色（``text_primary`` 等），再统一做对比度校正。
       这一步不能省：浅色主题上 ``green`` 直接当文字只有 2.96:1（见 ``ensure_contrast``）。
    3. **语法高亮** —— 与终端明暗**无关**（代码块底色是 ``mix`` 出来的、本身很淡），
       Pi 也是三套主题共用同一组 syntax 值（``dark.json`` 里直接写死）。

    ``syntax="mono"``：高对比主题**不使用彩色语法高亮**。理由是那份主题的承诺是
    "对比度 ≥ 7:1 且不靠颜色表意"；塞进 9 种语法色既难保证每一个都达标，
    也违背了它"给看不清颜色的人用"的初衷。
    """
    resolved = dict(palette)

    # ① 内容分层底色（**线性光**混合，见 mix_linear 的说明）
    for token, (base_token, accent_token, ratio) in _BG_DERIVATIONS.items():
        resolved[token] = mix_linear(resolved[base_token], resolved[accent_token], ratio)

    # ② 前景色：先指向已有语义色，再统一校正
    resolved["user_message_fg"] = resolved["text_primary"]
    resolved["custom_message_fg"] = resolved["text_primary"]
    resolved["custom_message_label"] = mix(resolved["text_primary"], resolved["accent"], 0.55)
    resolved["tool_title_fg"] = resolved["text_primary"]
    resolved["tool_output_fg"] = resolved["text_muted"]
    resolved["thinking_text"] = resolved["text_muted"]
    resolved["md_heading"] = mix(resolved["warning"], resolved["text_primary"], 0.35)
    resolved["md_link"] = resolved["accent"]
    resolved["md_code"] = resolved["info"]
    resolved["md_code_block"] = resolved["success"]
    resolved["md_quote"] = resolved["text_muted"]
    resolved["md_list_bullet"] = resolved["info"]

    # ③ 思考档位：颜色即档位（越亮越高），对齐 Pi 的 thinkingOff→High 梯度
    # ★ D161：这个块**必须在下面的校正循环之前** —— 它原本排在循环之后，
    #   于是"把 thinking_* 加进校正列表"会是**空操作**（赋值在后的会覆盖校正结果）。
    #   顺序错了不会报错，只会让人以为校正生效了。
    resolved["thinking_off"] = mix(resolved["bg_base"], resolved["text_faint"], 0.55)
    resolved["thinking_low"] = resolved["accent"]
    resolved["thinking_medium"] = mix(resolved["accent"], resolved["info"], 0.5)
    resolved["thinking_high"] = mix(resolved["accent"], resolved["danger"], 0.35)

    # 底色类 token 不参与"当文字用"的对比度校正（它们就是背景）
    for token in _D79_FG_TOKENS:
        target = BODY_FG_CONTRAST if token in _D79_BODY_TOKENS else TARGET_FG_CONTRAST
        corrected, _changed = ensure_contrast(resolved[token], resolved["bg_base"], target)
        resolved[token] = corrected

    # Markdown 的"辅助色"（不需要高对比，只要看得见）
    resolved["md_code_block_border"] = mix(resolved["bg_base"], resolved["text_faint"], 0.85)
    resolved["md_table_border"] = mix(resolved["bg_base"], resolved["text_faint"], 0.85)
    resolved["md_quote_border"] = resolved["text_faint"]
    resolved["md_hr"] = mix(resolved["bg_base"], resolved["text_faint"], 0.70)
    resolved["md_link_url"] = resolved["text_faint"]

    # ③ 思考档位：颜色即档位（越亮越高），对齐 Pi 的 thinkingOff→High 梯度
    # ★ D161：已**上移**到校正循环之前（见那里的说明）—— 留此注释只为指明去向。

    # ④ 语法高亮
    if syntax == "mono":
        for token in SYNTAX_TOKENS:
            resolved[token] = resolved["md_code_block"]
    else:
        resolved.update(SYNTAX_TOKENS)
    return resolved


#: 代码语法高亮（对齐 Pi 的 ``syntax*``）。取自 VS Code 深色主题的经典取值，
#: 在深/浅两套底色上都还能看清——因为代码块底色本身极淡，不是纯黑或纯白。
SYNTAX_TOKENS = {
    "syntax_comment": "#6a9955",
    "syntax_keyword": "#569cd6",
    "syntax_function": "#dcdcaa",
    "syntax_variable": "#9cdcfe",
    "syntax_string": "#ce9178",
    "syntax_number": "#b5cea8",
    "syntax_type": "#4ec9b0",
    "syntax_operator": "#d4d4d4",
    "syntax_punctuation": "#d4d4d4",
}


def build_themes(palette: dict) -> tuple[dict[str, ThemeFile], dict[str, list[str]]]:
    mocha = _flavour_colors(palette, "mocha")
    latte = _flavour_colors(palette, "latte")

    logs: dict[str, list[str]] = {"logox-dark": [], "logox-light": [], "logox-contrast": []}
    themes = {
        "logox-dark": ThemeFile(
            name="logox-dark",
            label="Logox Dark（Catppuccin Mocha）",
            variant="dark",
            palette=build_palette(mocha, adjustments=logs["logox-dark"]),  # type: ignore[arg-type]
        ),
        "logox-light": ThemeFile(
            name="logox-light",
            label="Logox Light（Catppuccin Latte）",
            variant="light",
            # ★ LIGHT_OVERRIDES：浅色主题需要**换档**的 token。
            #   在**映射之后**覆盖色名，而不是给 SEMANTIC_MAP 加三元逻辑 ——
            #   后者会让"深浅共用一套映射"这个选 Catppuccin 的理由失效。
            palette={
                **build_palette(latte, adjustments=logs["logox-light"]),  # type: ignore[arg-type]
                **{token: latte[source] for token, source in LIGHT_OVERRIDES.items()},
            },
        ),
        "logox-contrast": ThemeFile(
            name="logox-contrast",
            label="Logox High Contrast",
            variant="high_contrast",
            palette=derive_visual_tokens(  # type: ignore[arg-type]
                finalize_palette(CONTRAST_PALETTE, adjustments=logs["logox-contrast"]),
                {},
                syntax="mono",
            ),
        ),
    }
    return themes, logs


def report(theme: ThemeFile) -> list[str]:
    problems = validate_contrast(theme)
    palette = theme.palette
    lines = [
        f"  {theme.name:<16} variant={theme.variant:<14} "
        f"primary/bg={contrast_ratio(palette.text_primary, palette.bg_base):5.2f}:1  "
        f"muted/bg={contrast_ratio(palette.text_muted, palette.bg_base):5.2f}:1"
    ]
    for token in ("accent", "success", "warning", "danger", "info"):
        ratio = contrast_ratio(getattr(palette, token), palette.bg_base)
        lines.append(f"      {token:<8} {ratio:5.2f}:1")
    lines.append(
        f"      diff_fg/bg  add={contrast_ratio(palette.diff_add_fg, palette.diff_add_bg):4.2f}:1"
        f"  del={contrast_ratio(palette.diff_del_fg, palette.diff_del_bg):4.2f}:1"
    )
    lines.extend(f"    ✗ {problem}" for problem in problems)
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="从 Catppuccin 官方色板生成 Logox 内置主题")
    parser.add_argument("--check", action="store_true", help="只校验现有文件，不写入")
    args = parser.parse_args(argv)

    palette = json.loads(PALETTE_PATH.read_text(encoding="utf-8"))
    themes, logs = build_themes(palette)
    THEMES_DIR.mkdir(parents=True, exist_ok=True)

    print(f"Catppuccin 色板版本：{palette.get('version')}")
    failed = False
    for name, theme in themes.items():
        text = writer.dumps(theme.model_dump())
        path = THEMES_DIR / f"{name}.toml"
        if args.check:
            existing = path.read_text(encoding="utf-8") if path.is_file() else ""
            status = "一致" if existing == text else "**不一致**"
            print(f"  {name:<16} {status}")
            failed |= existing != text
        else:
            path.write_text(text, encoding="utf-8")
            print(f"  写入 {path.relative_to(REPO_ROOT)}")
        for entry in logs[name]:
            print(f"      对比度校正：{entry}")

    print("\n对比度核对（UI-SPEC §3.3）：")
    for theme in themes.values():
        for line in report(theme):
            print(line)
        if validate_contrast(theme):
            failed = True

    if failed:
        print("\n存在不合格项。", file=sys.stderr)
        return 1
    print("\n全部合格。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
