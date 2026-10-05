"""内置/用户主题发现、加载与状态字形覆盖。

当前 Rich/ANSI 组件直接使用语义配色；palette_css_variables 保留为已导出
的兼容转换接口，不参与当前 TUI 渲染。主题文件校验复用 config/theme.py。
"""

from __future__ import annotations

from pathlib import Path

from logox.config.schema import PALETTE_TOKENS, ThemeFile
from logox.config.theme import BUILTIN_THEMES, discover_themes
from logox.config.theme import load_theme as _load_theme_file
from logox.errors import ThemeError

__all__ = [
    "BUILTIN_THEMES",
    "DEFAULT_THEME",
    "builtin_themes_dir",
    "glyph_set",
    "list_themes",
    "load_theme",
    "palette_css_variables",
    "theme_search_path",
]

DEFAULT_THEME = "logox-dark"

BUILTIN_THEMES_DIR = Path(__file__).parent / "themes"
"""内置主题文件目录（生成自 Catppuccin 官方色板，见 ``tools/gen_themes.py``）。"""


def builtin_themes_dir() -> Path:
    return BUILTIN_THEMES_DIR


def theme_search_path(user_themes_dir: Path | None = None) -> list[Path]:
    """主题搜索路径，**按优先级从低到高**（后者覆盖同名主题）。"""
    paths = [BUILTIN_THEMES_DIR]
    if user_themes_dir is not None:
        paths.append(Path(user_themes_dir))
    return paths


def list_themes(user_themes_dir: Path | None = None) -> dict[str, Path]:
    """列出可用主题（``名称 → 文件路径``）。"""
    return discover_themes(theme_search_path(user_themes_dir))


def load_theme(name: str = DEFAULT_THEME, user_themes_dir: Path | None = None) -> ThemeFile:
    """按名称加载主题。

    :raises ThemeError: 主题不存在或校验失败（含缺失 token 的清单）。
    """
    available = list_themes(user_themes_dir)
    path = available.get(name)
    if path is None:
        known = "、".join(sorted({*available, *BUILTIN_THEMES}))
        raise ThemeError(Path(name), f"主题 {name!r} 不存在；可用主题：{known}")
    return _load_theme_file(path)


def palette_css_variables(theme: ThemeFile) -> dict[str, str]:
    """把语义 token 转成 CSS 变量（``--bg-base`` 这样的连字符命名）。"""
    return {f"--{token.replace('_', '-')}": value for token, value in theme.palette.as_dict().items()}


def glyph_set(theme: ThemeFile, *, icon_set: str | None = None) -> dict[str, str]:
    """取字形集。

    ``icon_set`` 参数用于**配置覆盖**（``ui.icon_set``）：终端缺字形时降级到 ASCII，
    这是 UI-SPEC §9 的降级路径的一部分。
    """
    glyphs = theme.glyphs
    if icon_set is None or icon_set == glyphs.set:
        return glyphs.model_dump(exclude={"set"})

    if icon_set == "ascii":
        return {
            "running": ">",
            "success": "+",
            "error": "x",
            "denied": "!",
            "cancelled": "#",
            "thinking": ">",
            "user": ">",
            "assistant": "*",
            "ellipsis": "...",
        }
    if icon_set == "nerd":  # pragma: no cover - 依赖 Nerd Font 字形，不默认启用
        return {**glyphs.model_dump(exclude={"set"}), "running": "\uf04b", "success": "\uf00c", "error": "\uf00d"}
    return glyphs.model_dump(exclude={"set"})


def missing_tokens(theme: ThemeFile) -> list[str]:
    """返回缺失的语义 token（正常情况为空——``ThemeFile`` 已强制全部必填）。"""
    present = set(theme.palette.as_dict())
    return [token for token in PALETTE_TOKENS if token not in present]
