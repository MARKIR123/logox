"""状态字形管线（D200 / MODULE_03 §5.2）。

这一组防的是同一类缺陷：**配置项存在，但不生效**。

主题文件里的 `[glyphs]` 段被文档承诺了（`UI-SPEC.md` 的字形表、`THEME-GUIDE.md` 的模板
都列了九项），内置三主题也都写着它 —— 而链断在**三处**：`CardContext` 没拿到 glyphs、
`StatusContext` 没拿到 tool_glyph、`glyph_set()` 零调用者。任何一处单独修都看不到效果，
所以断言必须**从主题文件追到渲染出来的文本**（两端各看一眼是发现不了的）。

用户裁定：`running` 由 `⏺`（U+23FA）改为 `✻`（U+273B）。
"""

from __future__ import annotations

import unittest
from pathlib import Path

from logox.config.schema import ThemeGlyphs
from logox.tui.content.cards import render_tool_summary
from logox.tui.content.status import StatusContext, StatusItems, TimingFields, build_status_line
from logox.tui.render.app import StatusComponent, TimelineComponent
from logox.tui.theme import glyph_set, load_theme

RUNNING = "✻"
OLD = "⏺"


class RunningGlyphDefaultTests(unittest.TestCase):
    """① 默认值与三个内置主题必须一致（否则"图标自己变回去"）。"""

    def test_schema_default_is_the_new_glyph(self) -> None:
        self.assertEqual(ThemeGlyphs().running, RUNNING)

    def test_builtin_themes_agree_with_the_schema_default(self) -> None:
        for name in ("logox-dark", "logox-light", "logox-contrast"):
            theme = load_theme(name)
            self.assertEqual(theme.glyphs.running, RUNNING, f"{name} 的 running 没跟上默认值")

    def test_old_glyph_is_gone_from_source_and_themes(self) -> None:
        """`⏺` 不该再出现在界面字形的位置上（文档里提到历史值是允许的）。"""
        root = Path(__file__).resolve().parents[2]
        for rel in (
            "src/logox/tui/themes/logox-dark.toml",
            "src/logox/tui/themes/logox-light.toml",
            "src/logox/tui/themes/logox-contrast.toml",
        ):
            text = (root / rel).read_text(encoding="utf-8")
            self.assertNotIn(f'running = "{OLD}"', text, rel)


class CardGlyphWiringTests(unittest.TestCase):
    """② 断点 ①：卡片上下文真的拿到了主题字形。"""

    def test_running_summary_uses_the_theme_glyph(self) -> None:
        line = render_tool_summary(
            state="running",
            name="shell",
            args_summary="pytest -q",
            duration_ms=1200,
            error_kind=None,
            glyphs={"running": "X"},
        )
        self.assertTrue(line.startswith("X "), line)
        self.assertNotIn(OLD, line)

    def test_running_summary_falls_back_to_the_new_default(self) -> None:
        """没有字形集时（例如裸用纯渲染函数）回退值也必须是新字形。"""
        line = render_tool_summary(
            state="running", name="shell", args_summary="", duration_ms=None, error_kind=None, glyphs={}
        )
        self.assertTrue(line.startswith(f"{RUNNING} "), line)

    def test_timeline_component_carries_glyphs_into_its_render_context(self) -> None:
        """**真的渲染一张运行中的卡片**，断言屏幕上就是主题字形。

        ⚠️ 这条是本组里最容易写假的一条：只断言 `timeline.glyphs == {...}` 的话，
        **即使 `render()` 忘了把 glyphs 传进 `CardContext`（也就是本来那个缺陷）也照样绿**。
        所以断言必须落在渲染输出上 —— 我第一次就写成了查字段，红检时它没变红。
        """
        from logox.kernel import events as ev

        palette = load_theme("logox-dark").palette
        timeline = TimelineComponent(palette, glyphs={"running": "Z"})
        timeline.ingest(ev.ToolCallRequested(session_id="s", call_id="c1", name="shell"))

        rendered = "\n".join(row.plain for row in timeline.render(100))

        self.assertIn("Z shell", rendered, "卡片没拿到主题字形")
        self.assertNotIn(OLD, rendered)


class StatusGlyphWiringTests(unittest.TestCase):
    """③ 断点 ②：状态栏的生成中指示与工具项都跟着主题走。"""

    def test_tool_glyph_comes_from_the_glyphs_dict(self) -> None:
        component = StatusComponent(load_theme("logox-dark").palette, glyphs={"running": "Y"})
        self.assertEqual(component.tool_glyph, "Y")

    def test_tool_glyph_falls_back_to_the_new_default(self) -> None:
        component = StatusComponent(load_theme("logox-dark").palette)
        self.assertEqual(component.tool_glyph, RUNNING)

    def test_status_component_render_uses_the_injected_glyph(self) -> None:
        """宿主接线也必须可用：只测属性和内容函数会漏掉 render 内的故障。"""
        from logox.tui.metrics import SessionMetrics

        component = StatusComponent(load_theme("logox-dark").palette, glyphs={"running": "Y"})
        component.metrics = SessionMetrics()
        component.metrics.model = "m"
        component.metrics.provider = "p"
        component.metrics.running_tool = "shell"

        row = component.render(120)[0].plain
        self.assertIn("Y", row)
        self.assertNotIn(OLD, row)

    def test_status_context_uses_tool_glyph_over_its_own_default(self) -> None:
        """直接构造 context 时，传入的 `tool_glyph` 必须赢过 dataclass 默认值。"""
        from logox.tui.metrics import SessionMetrics

        palette = load_theme("logox-dark").palette
        context = StatusContext(
            palette=palette,
            items=StatusItems(),
            timing_fields=TimingFields(),
            width=120,
            tool_glyph="Y",
        )
        metrics = SessionMetrics()
        metrics.model = "m"
        metrics.provider = "p"
        metrics.running_tool = "shell"
        line = build_status_line(metrics, context).plain
        self.assertIn("Y", line, "注入的字形没出现在状态栏上")
        self.assertNotIn(OLD, line)


class IconSetFallbackTests(unittest.TestCase):
    """④ 断点 ③：`ui.icon_set` 的 ASCII / Nerd 降级路径真的可达。"""

    def test_ascii_icon_set_downgrades_every_glyph(self) -> None:
        theme = load_theme("logox-dark")
        ascii_glyphs = glyph_set(theme, icon_set="ascii")
        self.assertEqual(ascii_glyphs["running"], ">")
        self.assertEqual(ascii_glyphs["success"], "+")
        # 降级后**不能**残留任何非 ASCII 字形
        for name, glyph in ascii_glyphs.items():
            self.assertTrue(glyph.isascii(), f"{name} 没被降级：{glyph!r}")

    def test_no_icon_set_override_keeps_the_theme_glyphs(self) -> None:
        theme = load_theme("logox-dark")
        same = glyph_set(theme, icon_set=None)
        self.assertEqual(same["running"], RUNNING)
        self.assertNotIn("set", same, "字形集里不该带上 set 这个配置键")


class ThemeSwitchGlyphTests(unittest.TestCase):
    """⑤ 换主题（`/theme` 与 `/reload`）必须同时换字形，并且作废卡片缓存。"""

    def _app(self, tmp: Path, icon_set: str = "unicode"):
        from types import SimpleNamespace

        from logox.config.schema import LogoxConfig, ProviderConfig, UiConfig
        from logox.paths import LogoxPaths
        from logox.tui.render.app import InlineApp
        from logox.tui.render.terminal import FakeTerminal

        config = LogoxConfig(
            provider=ProviderConfig(name="openai-compatible", model="gpt-4o"),
            ui=UiConfig(icon_set=icon_set),  # type: ignore[arg-type]
        )
        runtime = SimpleNamespace(
            config=config,
            paths=LogoxPaths.at(tmp),
            reducer=None,
            model="m",
            provider_name="p",
            session_replayer=None,
        )
        return InlineApp(runtime=runtime, terminal=FakeTerminal(columns=100, rows=30))

    def test_apply_theme_refreshes_glyphs_and_invalidates_cache(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            app = self._app(Path(tmp))
            before = app.timeline.cache_invalidations

            # 造一个"自定义主题"：只改 running 字形，其余照抄 dark
            from logox.config import writer
            from logox.config.theme import load_theme as load_file

            src = load_theme("logox-dark")
            custom = src.model_copy(update={"name": "custom-glyph"})
            custom.glyphs = src.glyphs.model_copy(update={"running": "W"})
            #: 主题文件的写入走**生产路径**（`config.writer` + `load_theme(path)` 校验），
            #: 而不是手写 TOML —— 否则测试可能在写一个生产根本读不进来的形状。
            target = Path(tmp) / "themes" / "custom-glyph.toml"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(writer.dumps(custom.model_dump()), encoding="utf-8")
            app.themes_dir = target.parent

            app.apply_theme("custom-glyph")
            self.assertEqual(load_file(target).glyphs.running, "W", "主题文件本身没写对")

            self.assertEqual(app.timeline.glyphs["running"], "W", "卡片字形没换")
            self.assertEqual(app.status.tool_glyph, "W", "状态栏字形没换")
            self.assertGreater(
                app.timeline.cache_invalidations, before, "字形变了但卡片缓存没作废"
            )

    def test_icon_set_from_config_reaches_the_components(self) -> None:
        """⑥ `ui.icon_set = "ascii"` 必须在**启动路径**上就生效（不只是函数能调）。"""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            app = self._app(Path(tmp), icon_set="ascii")
            self.assertEqual(app.timeline.glyphs["running"], ">")
            self.assertEqual(app.status.tool_glyph, ">")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
