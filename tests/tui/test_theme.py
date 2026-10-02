"""主题加载与 CSS 变量注入的测试（MODULE_tui.md §8 的 ``test_theme.py``）。

覆盖：三套内置主题可加载、色板完整、CSS 变量命名、字形集降级、
以及**对比度达标**（UI-SPEC §3.3 的硬要求）。
"""

from __future__ import annotations

import tomllib
import unittest
from pathlib import Path
from unittest import mock

from logox.config.schema import PALETTE_TOKENS, ThemeFile
from logox.config.theme import (
    BUILTIN_THEMES,
    MIN_CONTRAST_HIGH,
    MIN_CONTRAST_MUTED,
    MIN_CONTRAST_PRIMARY,
    contrast_ratio,
    discover_themes,
    validate_contrast,
)
from logox.config.theme import (
    load_theme as load_theme_file,
)
from logox.errors import ThemeError
from logox.tui import theme as tui_theme
from tests.unit.support import make_temp_dir, remove_temp_dir


class BuiltinThemeTests(unittest.TestCase):
    def test_all_three_builtin_themes_exist_and_load(self) -> None:
        available = tui_theme.list_themes()
        for name in BUILTIN_THEMES:
            with self.subTest(theme=name):
                self.assertIn(name, available, f"{name} 应存在于内置主题目录")
                loaded = tui_theme.load_theme(name)
                self.assertEqual(loaded.name, name)

    def test_variants_match_decision_d43(self) -> None:
        """D43：深色 = Mocha，浅色 = Latte，高对比 = 自研。"""
        self.assertEqual(tui_theme.load_theme("logox-dark").variant, "dark")
        self.assertEqual(tui_theme.load_theme("logox-light").variant, "light")
        self.assertEqual(tui_theme.load_theme("logox-contrast").variant, "high_contrast")

    def test_palette_is_complete(self) -> None:
        for name in BUILTIN_THEMES:
            with self.subTest(theme=name):
                theme = tui_theme.load_theme(name)
                self.assertEqual(tui_theme.missing_tokens(theme), [])
                self.assertEqual(len(theme.palette.as_dict()), len(PALETTE_TOKENS))

    def test_dark_theme_matches_catppuccin_mocha(self) -> None:
        """色值必须与 Catppuccin 官方色板一致（D43 要求逐值校验）。"""
        theme = tui_theme.load_theme("logox-dark")
        self.assertEqual(theme.palette.bg_base, "#1e1e2e")  # mocha base
        self.assertEqual(theme.palette.text_primary, "#cdd6f4")  # mocha text
        self.assertEqual(theme.palette.accent, "#89b4fa")  # mocha blue

    def test_light_theme_matches_catppuccin_latte(self) -> None:
        theme = tui_theme.load_theme("logox-light")
        self.assertEqual(theme.palette.bg_base, "#eff1f5")  # latte base
        self.assertEqual(theme.palette.text_primary, "#4c4f69")  # latte text
        self.assertEqual(theme.palette.accent, "#1e66f5")  # latte blue

    def test_theme_files_are_generated_by_our_own_writer(self) -> None:
        """主题文件必须能被标准库 ``tomllib`` 解析（= 我们的 writer 输出合法 TOML）。"""
        for name in BUILTIN_THEMES:
            path = tui_theme.builtin_themes_dir() / f"{name}.toml"
            with self.subTest(theme=name):
                parsed = tomllib.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(parsed["schema_version"], 1)
                self.assertIn("palette", parsed)


class ContrastTests(unittest.TestCase):
    def test_every_builtin_theme_passes_ui_spec_contrast(self) -> None:
        """UI-SPEC §3.3 硬要求；``validate_contrast`` 返回空表示通过。"""
        for name in BUILTIN_THEMES:
            with self.subTest(theme=name):
                theme = tui_theme.load_theme(name)
                self.assertEqual(validate_contrast(theme), [])

    def test_high_contrast_theme_meets_seven_to_one(self) -> None:
        theme = tui_theme.load_theme("logox-contrast")
        ratio = contrast_ratio(theme.palette.text_primary, theme.palette.bg_base)
        self.assertGreaterEqual(ratio, MIN_CONTRAST_HIGH)

    def test_dark_and_light_body_text_meet_four_and_a_half(self) -> None:
        for name in ("logox-dark", "logox-light"):
            with self.subTest(theme=name):
                theme = tui_theme.load_theme(name)
                ratio = contrast_ratio(theme.palette.text_primary, theme.palette.bg_base)
                self.assertGreaterEqual(ratio, MIN_CONTRAST_PRIMARY)

    def test_muted_text_meets_three_to_one(self) -> None:
        for name in BUILTIN_THEMES:
            with self.subTest(theme=name):
                theme = tui_theme.load_theme(name)
                ratio = contrast_ratio(theme.palette.text_muted, theme.palette.bg_base)
                self.assertGreaterEqual(ratio, MIN_CONTRAST_MUTED)

    def test_diff_foreground_readable_on_its_own_background(self) -> None:
        """diff 行是正文，前景对**自己的背景**也要达到 3:1。"""
        for name in BUILTIN_THEMES:
            with self.subTest(theme=name):
                palette = tui_theme.load_theme(name).palette
                self.assertGreaterEqual(
                    contrast_ratio(palette.diff_add_fg, palette.diff_add_bg), MIN_CONTRAST_MUTED
                )
                self.assertGreaterEqual(
                    contrast_ratio(palette.diff_del_fg, palette.diff_del_bg), MIN_CONTRAST_MUTED
                )


class EffortAndPayloadTokenTests(unittest.TestCase):
    """★ D161：接通后的 6 个 token 必须真的达标 —— 并与契约（必需）保持一致。

    背景：这 6 个 token 此前**没有任何代码读取**，所以它们的取值好不好看**无人受影响**。
    接通之后同一批值会真的画到屏幕上：`thinking_off` 当时是 1.91:1（dark）/ 1.54:1（light）
    —— 一个看不见的档位词。**值没变，但后果变了。**
    """

    TOKENS = (
        "tool_output_fg",
        "thinking_text",
        "thinking_off",
        "thinking_low",
        "thinking_medium",
        "thinking_high",
    )

    def test_all_six_are_required_tokens(self) -> None:
        """被校验的 token 必须**必需** —— 否则默认值可以悄悄绕过校验。

        这也是一条契约守卫：`tool_output_fg` 要 4.5:1，而"一个默认值同时满足深浅两种底色"
        在数学上不可能，所以它只能是必需项（推导见 `ThemePalette` 的注释）。
        """
        from logox.config.schema import ThemePalette

        required = {n for n, f in ThemePalette.model_fields.items() if f.is_required()}
        for token in self.TOKENS:
            with self.subTest(token=token):
                self.assertIn(token, required, f"{token} 必须显式声明（它要过对比度校验）")

    def test_all_six_meet_their_thresholds(self) -> None:
        for name in BUILTIN_THEMES:
            with self.subTest(theme=name):
                palette = tui_theme.load_theme(name).palette
                # tool_output_fg 是"工具结果正文" → 正文级；其余是次要文本
                tool_ratio = contrast_ratio(palette.tool_output_fg, palette.bg_base)
                minimum = (
                    MIN_CONTRAST_HIGH
                    if tui_theme.load_theme(name).variant == "high_contrast"
                    else MIN_CONTRAST_PRIMARY
                )
                self.assertGreaterEqual(
                    tool_ratio,
                    minimum,
                    f"{name}: tool_output_fg 对 bg_base 只有 {tool_ratio:.2f}:1",
                )
                for token in self.TOKENS[1:]:
                    ratio = contrast_ratio(getattr(palette, token), palette.bg_base)
                    self.assertGreaterEqual(
                        ratio,
                        MIN_CONTRAST_MUTED,
                        f"{name}: {token} 对 bg_base 只有 {ratio:.2f}:1（会画成文字）",
                    )

    def test_dimming_the_effort_colours_is_caught(self) -> None:
        """把 `thinking_off` 配回旧的暗值，体检必须报出来（**先红后绿的那一半**）。"""
        theme = tui_theme.load_theme("logox-dark")
        broken = theme.model_copy(
            update={"palette": theme.palette.model_copy(update={"thinking_off": "#494b5e"})}
        )
        problems = validate_contrast(broken)
        self.assertTrue(
            any("thinking_off" in problem for problem in problems),
            f"旧值 #494b5e（1.91:1）必须被体检报出，实际返回 {problems}",
        )

    def test_dim_tool_output_is_caught_at_body_level(self) -> None:
        """`tool_output_fg` 要按**正文级**判，不是次要级。

        用一个"过了次要级但不达正文级"的值来区分 —— 中性灰 `#808080` 实测约 3.7:1：
        若阈值被误写成 3.0，这条会静默通过。
        """
        theme = tui_theme.load_theme("logox-dark")
        borderline = "#808080"
        ratio = contrast_ratio(borderline, theme.palette.bg_base)
        self.assertGreater(ratio, MIN_CONTRAST_MUTED, "该值应当已过次要级（否则用例失去区分度）")
        self.assertLess(ratio, MIN_CONTRAST_PRIMARY, "该值应当未达正文级（否则用例失去区分度）")

        broken = theme.model_copy(
            update={"palette": theme.palette.model_copy(update={"tool_output_fg": borderline})}
        )
        self.assertTrue(
            any("tool_output_fg" in problem for problem in validate_contrast(broken)),
            "未达正文级的工具输出必须被报出",
        )


class CssVariableTests(unittest.TestCase):
    def test_variables_use_hyphenated_token_names(self) -> None:
        theme = tui_theme.load_theme("logox-dark")
        variables = tui_theme.palette_css_variables(theme)
        self.assertEqual(variables["--bg-base"], "#1e1e2e")
        self.assertEqual(variables["--text-primary"], "#cdd6f4")
        self.assertEqual(variables["--diff-add-bg"], theme.palette.diff_add_bg)
        self.assertEqual(len(variables), len(PALETTE_TOKENS))

    def test_every_token_becomes_a_variable(self) -> None:
        variables = tui_theme.palette_css_variables(tui_theme.load_theme())
        for token in PALETTE_TOKENS:
            with self.subTest(token=token):
                self.assertIn(f"--{token.replace('_', '-')}", variables)


class GlyphTests(unittest.TestCase):
    def test_default_glyphs_from_theme(self) -> None:
        glyphs = tui_theme.glyph_set(tui_theme.load_theme("logox-dark"))
        self.assertEqual(glyphs["success"], "✓")
        self.assertEqual(glyphs["error"], "✗")

    def test_ascii_fallback_has_no_unicode(self) -> None:
        """UI-SPEC §9：终端缺字形时必须能整体降级到 ASCII。"""
        glyphs = tui_theme.glyph_set(tui_theme.load_theme("logox-dark"), icon_set="ascii")
        for key, value in glyphs.items():
            with self.subTest(glyph=key):
                self.assertTrue(value.isascii(), f"{key}={value!r} 不是 ASCII")

    def test_config_override_selects_ascii(self) -> None:
        glyphs = tui_theme.glyph_set(tui_theme.load_theme("logox-dark"), icon_set="ascii")
        self.assertEqual(glyphs["ellipsis"], "...")


class ErrorPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = make_temp_dir("tui-theme-")
        self.addCleanup(remove_temp_dir, self.root)
        self.user_dir = self.root / "themes"
        self.user_dir.mkdir()

    def test_unknown_theme_lists_available(self) -> None:
        with self.assertRaises(ThemeError) as ctx:
            tui_theme.load_theme("no-such-theme")
        message = str(ctx.exception)
        self.assertIn("no-such-theme", message)
        for name in BUILTIN_THEMES:
            self.assertIn(name, message)

    def test_user_theme_overrides_builtin_with_same_name(self) -> None:
        """用户目录优先级更高：同名主题由用户版本胜出。"""
        source = (tui_theme.builtin_themes_dir() / "logox-dark.toml").read_text(encoding="utf-8")
        # 只改 label，验证加载到的确实是用户那份
        custom = source.replace('label = "Logox Dark（Catppuccin Mocha）"', 'label = "My Custom Dark"')
        self.assertNotEqual(custom, source, "替换未生效，测试本身需要更新")
        (self.user_dir / "logox-dark.toml").write_text(custom, encoding="utf-8")

        loaded = tui_theme.load_theme("logox-dark", user_themes_dir=self.user_dir)
        self.assertEqual(loaded.label, "My Custom Dark")

    def test_search_path_order_is_builtin_then_user(self) -> None:
        paths = tui_theme.theme_search_path(self.user_dir)
        self.assertEqual(paths[0], tui_theme.builtin_themes_dir())
        self.assertEqual(paths[-1], self.user_dir)

    def test_corrupt_user_theme_raises_with_detail(self) -> None:
        (self.user_dir / "broken.toml").write_text("schema_version = 1\nname = 1\n", encoding="utf-8")
        with self.assertRaises(ThemeError):
            tui_theme.load_theme("broken", user_themes_dir=self.user_dir)


class PackagePurityTests(unittest.TestCase):
    def test_tui_package_init_does_not_import_textual(self) -> None:
        """``tui/__init__.py`` 不得 import textual——否则会破坏 D29 的启动预算。"""
        import ast

        import logox.tui

        source = Path(logox.tui.__file__).read_text(encoding="utf-8")
        imported: set[str] = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                imported.add(node.module.split(".")[0])
        self.assertNotIn("textual", imported)
        self.assertNotIn("rich", imported)

    def test_theme_module_does_not_import_textual(self) -> None:
        source = Path(tui_theme.__file__).read_text(encoding="utf-8")
        self.assertNotIn("import textual", source)

    def test_builtin_themes_dir_matches_config_list(self) -> None:
        """``config.theme.BUILTIN_THEMES`` 与磁盘上的文件必须一致（守护两者不漂移）。"""
        on_disk = {path.stem for path in tui_theme.builtin_themes_dir().glob("*.toml")}
        self.assertEqual(on_disk, set(BUILTIN_THEMES))

    def test_generated_files_are_stable(self) -> None:
        """``tools/gen_themes.py --check`` 的等价断言：文件与色板一致（防止手改漂移）。"""
        import json
        import sys

        repo_root = Path(__file__).resolve().parents[2]
        sys.path.insert(0, str(repo_root / "tools"))
        try:
            import gen_themes  # type: ignore[import-not-found]
        except ImportError:  # pragma: no cover
            self.skipTest("gen_themes 不可导入")
            return

        palette = json.loads(gen_themes.PALETTE_PATH.read_text(encoding="utf-8"))
        themes, _logs = gen_themes.build_themes(palette)
        from logox.config import writer

        for name, theme in themes.items():
            with self.subTest(theme=name):
                expected = writer.dumps(theme.model_dump())
                actual = (tui_theme.builtin_themes_dir() / f"{name}.toml").read_text(encoding="utf-8")
                self.assertEqual(actual, expected, f"{name}.toml 与官方色板不一致，请运行 tools/gen_themes.py")


class ThemeFileShapeTests(unittest.TestCase):
    def test_theme_file_requires_all_tokens(self) -> None:
        """缺 token 必须被拒绝（E-12），而不是静默兜底。"""
        data = {
            "schema_version": 1,
            "name": "t",
            "palette": {"bg_base": "#000000", "text_primary": "#ffffff"},
        }
        from pydantic import ValidationError

        with self.assertRaises(ValidationError) as ctx:
            ThemeFile.model_validate(data)
        self.assertIn("accent", str(ctx.exception))


class InputThemeTokenTests(unittest.TestCase):
    """D152-a / D152-b：输入框有**专属** token，且对比度下限被校验守住了。

    要防的两个缺陷
    --------------
    1. **又变回借来的颜色**：输入框此前借 `border_subtle`（对 bg_base 仅 1.30:1），
       而那是共享的装饰色 —— 既看不清，又"改输入框 = 四处一起变"。
       用户报障原话："颜色也是灰黑色的，不清楚"。
    2. **自定义主题配出看不见的框**：只把内置值改亮是不够的，
       下一个主题照样能配出 1.30:1 —— 而症状与原因的因果链很长，用户只会再报一次。
    """

    def test_every_builtin_theme_has_dedicated_input_tokens(self) -> None:
        for name in BUILTIN_THEMES:
            with self.subTest(theme=name):
                palette = tui_theme.load_theme(name).palette
                for token in ("input_border", "input_text", "input_hint"):
                    self.assertTrue(
                        getattr(palette, token).startswith("#"),
                        f"{name} 缺少 {token}",
                    )

    def test_input_tokens_are_not_borrowed_from_shared_ones(self) -> None:
        """**细粒度是真的**：三个输入框 token 必须各自独立于共享的装饰/正文色。

        这一条就是用户"需要高细粒度的微调"的直接断言：只要 `input_border` 还被
        钉死等于 `border_subtle`，改输入框就必然牵动帮助分隔线/工具卡竖线/浮层底边。
        """
        for name in BUILTIN_THEMES:
            with self.subTest(theme=name):
                palette = tui_theme.load_theme(name).palette
                self.assertNotEqual(
                    palette.input_border,
                    palette.border_subtle,
                    "input_border 不得等于 border_subtle（那意味着「改输入框 = 四处一起变」）",
                )
                # 三个 token 互相之间也不能相同：它们承担三种不同的可读性要求
                self.assertEqual(
                    len({palette.input_border, palette.input_text, palette.input_hint}),
                    3,
                    "input_border / input_text / input_hint 必须是三个不同的值",
                )

    def test_input_border_is_visible_in_every_builtin_theme(self) -> None:
        """框线必须**看得见**：对 `bg_base` ≥ 次要阈值（3.0）。

        旧值 `border_subtle` 在深色主题上只有 **1.30:1** —— 那就是用户说的"不清楚"。
        """
        for name in BUILTIN_THEMES:
            with self.subTest(theme=name):
                palette = tui_theme.load_theme(name).palette
                ratio = contrast_ratio(palette.input_border, palette.bg_base)
                self.assertGreaterEqual(
                    ratio,
                    MIN_CONTRAST_MUTED,
                    f"{name}: input_border 对 bg_base 仅 {ratio:.2f}:1，低到看不见",
                )

    def test_contrast_check_catches_an_invisible_input_border(self) -> None:
        """**先红后绿的那一半**：把框线配成 1.30:1，体检必须报出来。

        没有这一条的话，"写进校验"只是一句声明 —— 校验到底有没有覆盖到新 token，
        谁也说不清。
        """
        theme = tui_theme.load_theme("logox-dark")
        broken = theme.model_copy(
            update={"palette": theme.palette.model_copy(update={"input_border": "#313244"})}
        )
        problems = validate_contrast(broken)
        self.assertTrue(
            any("input_border" in problem for problem in problems),
            f"看不见的框线必须被体检报出，实际返回 {problems}",
        )

    def test_input_text_must_meet_the_body_text_threshold(self) -> None:
        """`input_text` 是**正文**，与 `text_primary` 同级要求（不是次要阈值）。"""
        theme = tui_theme.load_theme("logox-dark")
        broken = theme.model_copy(
            update={"palette": theme.palette.model_copy(update={"input_text": "#313244"})}
        )
        self.assertTrue(any("input_text" in problem for problem in validate_contrast(broken)))


class UserThemeDirectoryTests(unittest.TestCase):
    """D152-c：主题只认「内置 + `~/.logox/themes`」，**不认项目级覆盖**。"""

    def test_user_theme_is_listed_and_loadable(self) -> None:
        """用户放进去的主题必须**既能被列出、也能被加载**（列表与加载同一份事实）。"""
        import shutil

        from logox.tui.theme import builtin_themes_dir

        tmp = Path(make_temp_dir("user-themes"))
        self.addCleanup(lambda: remove_temp_dir(str(tmp)))
        source = builtin_themes_dir() / "logox-dark.toml"
        shutil.copy(source, tmp / "my-theme.toml")
        text = (tmp / "my-theme.toml").read_text(encoding="utf-8")
        (tmp / "my-theme.toml").write_text(
            text.replace("logox-dark", "my-theme").replace("#7f849c", "#ff00ff"),
            encoding="utf-8",
        )

        available = tui_theme.list_themes(tmp)
        self.assertIn("my-theme", available)
        loaded = tui_theme.load_theme("my-theme", tmp)
        self.assertEqual(loaded.name, "my-theme")
        self.assertEqual(loaded.palette.input_border, "#ff00ff", "用户值必须真的生效")

    def test_user_dir_is_searched_after_builtin(self) -> None:
        """优先级：用户目录**覆盖**内置同名主题（排在后面 = 后者胜出）。"""
        paths = tui_theme.theme_search_path(Path("/tmp/whatever"))
        self.assertEqual(paths[0], tui_theme.builtin_themes_dir())
        self.assertEqual(paths[-1], Path("/tmp/whatever"))

    def test_no_project_level_theme_source(self) -> None:
        """**反向守卫**：项目级 `.logox/themes/` 不得再被算作可用主题。

        去掉它的理由（用户裁定）：主题是"用户对界面的偏好"，不是"项目对代码的规范"。
        它同时修掉一个**双事实来源**：从前项目级主题会让 `ui.theme` 通过**校验**，
        而真正加载时失败（`load_theme` 只搜内置 + 用户目录）。
        """
        import shutil

        from logox.config.loader import _available_themes
        from logox.paths import LogoxPaths

        tmp = Path(make_temp_dir("proj-theme"))
        self.addCleanup(lambda: remove_temp_dir(str(tmp)))
        # 造一个"看起来像项目"的目录，里面有 .logox/themes/ 与 .git
        project = tmp / "proj"
        (project / ".logox" / "themes").mkdir(parents=True, exist_ok=True)
        (project / ".git").mkdir(exist_ok=True)
        shutil.copy(
            tui_theme.builtin_themes_dir() / "logox-dark.toml",
            project / ".logox" / "themes" / "project-only.toml",
        )

        home = LogoxPaths.at(tmp / "home")
        available = _available_themes(home, project)
        self.assertNotIn(
            "project-only",
            available,
            "项目级主题必须**不再**出现在可用主题里（否则校验通过、加载失败）",
        )
        self.assertTrue(set(BUILTIN_THEMES).issubset(available), "内置主题必须仍在")


class FineGrainedTokensDoNotLeakTests(unittest.TestCase):
    """细粒度的**另一面**：改输入框不能牵动别人。"""

    def test_editor_uses_input_tokens_while_others_keep_theirs(self) -> None:
        from logox.tui.render.ansi import text_to_ansi
        from logox.tui.render.components.editor import BoxedEditor, Editor

        palette = tui_theme.load_theme("logox-dark").palette
        core = Editor(hint="提示", hint_style=str(palette.input_hint))
        core.set_text("abc")
        rows = BoxedEditor(
            core,
            border_style=str(palette.input_border),
            text_style=str(palette.input_text),
        ).render(30)

        ansi = text_to_ansi(rows[1])
        # 正文与框线各自是自己的 token（#7f849c → 127;132;156）
        self.assertIn("\x1b[38;2;127;132;156m│ ", ansi, "框线必须是 input_border")
        self.assertIn("abc", ansi)
        self.assertNotIn(
            "\x1b[38;2;49;50;68m",
            ansi,
            "不得再出现 border_subtle(#313244) —— 那是旧的「借来的」框线色",
        )


if __name__ == "__main__":
    unittest.main()

    def test_discover_themes_reports_stems(self) -> None:
        found = discover_themes([tui_theme.builtin_themes_dir()])
        self.assertEqual(set(found), set(BUILTIN_THEMES))

    def test_load_theme_file_uses_our_loader(self) -> None:
        """``tui.theme.load_theme`` 必须复用 ``config.theme`` 的校验，而不是另起一套。"""
        with mock.patch.object(tui_theme, "_load_theme_file", wraps=load_theme_file) as patched:
            tui_theme.load_theme("logox-dark")
        self.assertTrue(patched.called)


if __name__ == "__main__":
    unittest.main()
