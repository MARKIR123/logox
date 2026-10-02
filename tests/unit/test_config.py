"""配置 / 状态 / 主题的单元测试（MODULE_config.md §8 的 T-01 – T-36）。

标准库 ``unittest``（D45）。全部用例都在临时目录内运行，**绝不触碰真实的
``~/.logox``**（通过 ``LogoxPaths.at(tmp)`` 重定向）。
"""

from __future__ import annotations

import tomllib
import unittest
from pathlib import Path
from unittest import mock

import logox
from logox.config import writer
from logox.config.loader import ConfigBundle, load, render_issues
from logox.config.schema import (
    SCHEMA_VERSION,
    STATUS_ITEM_KEYS,
    LastUsed,
    LogoxConfig,
    StateFile,
)
from logox.config.state import LayeredStateStore, StateStore
from logox.config.theme import (
    BUILTIN_THEMES,
    contrast_ratio,
    discover_themes,
    load_theme,
    mix,
    validate_contrast,
)
from logox.errors import ConfigValidationError, ConfigWriteError, ThemeError, TomlWriteError
from logox.paths import LogoxPaths
from tests.unit.support import make_temp_dir, remove_temp_dir

VALID_BASE = """\
schema_version = 1

[provider]
name = "openai-compatible"
model = "test-model"
"""


def config_document(**overrides: object) -> str:
    """用**我们自己的 writer** 生成配置文本，避免手写时重复声明 TOML 表。

    TOML 不允许在同一文件里两次声明 ``[provider]``，而直接拼接字符串极易踩到
    （例如 ``VALID_BASE + '[provider]\\n...'``）。这里用 dict 合并 + writer 输出，
    从根上避免该类错误。
    """
    document: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "provider": {"name": "openai-compatible", "model": "test-model"},
    }
    for key, value in overrides.items():
        existing = document.get(key)
        if isinstance(value, dict) and isinstance(existing, dict):
            document[key] = {**existing, **value}
        else:
            document[key] = value
    return writer.dumps(document)


def palette(**overrides: str) -> dict[str, str]:
    """一个对比度合格的主题色板（用于主题相关用例）。"""
    values = {
        "bg_base": "#1e1e2e",
        "bg_raised": "#181825",
        "bg_overlay": "#313244",
        "overlay_scrim": "#11111b",
        "text_primary": "#cdd6f4",
        "text_muted": "#a6adc8",
        "text_faint": "#6c7086",
        "accent": "#89b4fa",
        "success": "#a6e3a1",
        "warning": "#f9e2af",
        "danger": "#f38ba8",
        "info": "#89dceb",
        "border_subtle": "#313244",
        "border_strong": "#585b70",
        # D152-a：输入框专属（**必需** token，缺一个就会被 ThemeError 拒绝）。
        # 这里显式列出而不是给默认值 —— 见 `config/schema.py::ThemePalette` 的说明。
        "input_border": "#7f849c",
        "input_text": "#cdd6f4",
        "input_hint": "#6c7086",
        # D161：接通后升为**必需**（它们现在真的会被画出来，所以要过对比度校验）
        "tool_output_fg": "#a6adc8",
        "thinking_text": "#a6adc8",
        "thinking_off": "#686b86",
        "thinking_low": "#89b4fa",
        "thinking_medium": "#89c8f2",
        "thinking_high": "#aea6dd",
        "diff_add_fg": "#a6e3a1",
        "diff_add_bg": "#323c3f",
        "diff_del_fg": "#f38ba8",
        "diff_del_bg": "#3e2e40",
    }
    values.update(overrides)
    return values


def theme_document(**overrides: object) -> str:
    document: dict[str, object] = {
        "schema_version": 1,
        "name": "test-theme",
        "label": "Test",
        "variant": "dark",
        "palette": palette(),
    }
    document.update(overrides)
    return writer.dumps(document)


class ConfigTestCase(unittest.TestCase):
    """公共脚手架：临时用户目录 + 临时项目目录。"""

    def setUp(self) -> None:
        self.root = make_temp_dir("config-")
        self.addCleanup(remove_temp_dir, self.root)
        self.paths = LogoxPaths.at(self.root / "userhome")
        self.paths.ensure_dirs()
        self.project = self.root / "proj"
        (self.project / ".logox").mkdir(parents=True)
        self.bare = self.root / "bare"
        self.bare.mkdir()

    # -- 辅助 ----------------------------------------------------------- #

    def write(self, path: Path, text: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def user_config(self, text: str) -> Path:
        return self.write(self.paths.config, text)

    def project_config(self, text: str) -> Path:
        return self.write(self.project / ".logox" / "config.toml", text)

    def load(self, cwd: Path | None = None, **kwargs: object) -> ConfigBundle:
        kwargs.setdefault("env", {})
        kwargs.setdefault("paths", self.paths)
        return load(cwd or self.project, **kwargs)  # type: ignore[arg-type]

    def fields(self, bundle: ConfigBundle, severity: str | None = None) -> list[str]:
        return [
            issue.field or "<root>"
            for issue in bundle.issues
            if severity is None or issue.severity == severity
        ]


# --------------------------------------------------------------------------- #
# 加载与合并
# --------------------------------------------------------------------------- #


class ConfigLoadTests(ConfigTestCase):
    def test_t01_empty_dirs_yield_defaults(self) -> None:
        """T-01：无任何配置文件时得到全默认值。

        唯一的问题是「没告诉我用哪个模型」——这是**有意为之**：全新安装必须先
        明确告知用户如何配置模型，而不是默默用一个猜来的默认值。

        ⚠️ **F-21（隔离修复）**：测试的临时目录建在 ``<repo>/.test-tmp/`` 下，
        而 ``nearest_project()`` 会**逐级向上找项目** —— 于是它找到了**仓库自己**的
        ``.logox/state.toml``（开发者跑过 logox 就有），把 ``thinking_effort`` 之类的
        "上次使用"值叠了进来。症状是这条用例**在开发机上永远失败**，而断言本身没错。

        这里显式传一个**空 state**，把"环境里的 state.toml"排除在外 ——
        被测的是"没有任何配置文件时的默认值"，而不是"我这台机器上恰好跑过什么"。
        """
        bundle = self.load(self.bare, state=StateFile())
        self.assertEqual(bundle.config, LogoxConfig())
        self.assertEqual([source.scope for source in bundle.sources], ["defaults"])
        self.assertEqual(self.fields(bundle, "error"), ["provider.model"])

    def test_t01b_state_is_a_source_only_when_it_actually_supplies_values(self) -> None:
        """★ **F-21 收尾（D136）**：`state.toml` **只在真的提供了值时**才算一个来源。

        两个方向都要守：

        * **空的** state（`StateFile()`）⇒ 不该出现在来源列表里。
          为什么重要：测试临时目录建在仓库内，于是 `nearest_project()` 会向上找到
          **仓库自己的** `.logox/state.toml`（开发者跑过 logox 就有）——
          一个空状态把"无配置 → 只有 defaults"断言顶成假失败，本项目因此常年挂着一条红灯。
        * **有值的** state ⇒ 必须出现在来源列表里，并且 `origin` 指到它
          （否则用户"上次选的模型为什么生效了"就查不出来）。
        """
        empty = self.load(self.bare, state=StateFile())
        self.assertEqual([source.scope for source in empty.sources], ["defaults"])

        with_values = self.load(
            self.bare, state=StateFile(last=LastUsed(model="last-used-model"))
        )
        self.assertIn("state", [source.scope for source in with_values.sources])
        self.assertEqual(with_values.config.provider.model, "last-used-model")

    def test_t02_project_overrides_global_and_origin_is_recorded(self) -> None:
        """T-02：项目级覆盖全局；``origin`` 指出值来自哪个文件。"""
        self.user_config(VALID_BASE + '\n[ui]\ntheme = "logox-light"\n')
        self.project_config('[ui]\ntheme = "logox-contrast"\n')

        bundle = self.load()
        self.assertEqual(bundle.config.ui.theme, "logox-contrast")
        origin = bundle.origin_of("ui.theme")
        self.assertIsNotNone(origin)
        assert origin is not None
        self.assertIn("project", origin)
        self.assertIn(".logox", origin)

    def test_t03_deep_merge_keeps_untouched_keys(self) -> None:
        """T-03：三层嵌套 table 部分覆盖 → 未覆盖的键保留上层值。"""
        self.user_config(
            VALID_BASE
            + "\n[ui]\ntheme = \"logox-light\"\nsidebar_width = 40\nstream_fps = 20\n"
        )
        self.project_config('[ui]\nsidebar_width = 24\n')

        config = self.load().config
        self.assertEqual(config.ui.sidebar_width, 24, "近者覆盖")
        self.assertEqual(config.ui.theme, "logox-light", "未覆盖的键保留")
        self.assertEqual(config.ui.stream_fps, 20, "同层未覆盖的键保留")

    def test_t04_arrays_replace_entirely_not_append(self) -> None:
        """T-04：数组是**整体替换**，绝不拼接（避免"删不掉旧元素"）。"""
        self.user_config(VALID_BASE + '\n[tools]\nenabled = ["read", "write"]\n')
        self.project_config('[tools]\nenabled = ["grep"]\n')
        self.assertEqual(self.load().config.tools.enabled, ["grep"])

    def test_t05_env_overrides_files(self) -> None:
        """T-05：``LOGOX__UI__THEME`` 覆盖所有文件来源。"""
        self.user_config(VALID_BASE + '\n[ui]\ntheme = "logox-contrast"\n')
        bundle = self.load(env={"LOGOX__UI__THEME": "logox-light"})
        self.assertEqual(bundle.config.ui.theme, "logox-light")
        self.assertIn("env", bundle.origin_of("ui.theme") or "")

    def test_t06_cli_overrides_everything(self) -> None:
        """T-06：命令行优先级最高。"""
        self.user_config(VALID_BASE)
        bundle = self.load(
            env={"LOGOX__PROVIDER__MODEL": "from-env"},
            cli_overrides={"provider": {"model": "from-cli"}},
        )
        self.assertEqual(bundle.config.provider.model, "from-cli")

    def test_env_value_typed_parsing(self) -> None:
        self.user_config(VALID_BASE)
        bundle = self.load(env={"LOGOX__UI__STREAM_FPS": "25", "LOGOX__UI__ANIMATIONS": "false"})
        self.assertEqual(bundle.config.ui.stream_fps, 25)
        self.assertIs(bundle.config.ui.animations, False)

    # -- 校验与回退 ----------------------------------------------------- #

    def test_t07_bad_field_reports_precise_path(self) -> None:
        """T-07：类型错误必须给出**精确字段路径**，且只影响该字段。"""
        self.user_config(config_document(provider={"temperature": "abc"}))
        bundle = self.load()
        self.assertIn("provider.temperature", self.fields(bundle, "error"))
        self.assertEqual(bundle.config.provider.temperature, 0.2, "出错字段回退默认值")
        self.assertEqual(bundle.config.provider.model, "test-model", "其他字段不受影响")

    def test_t08_all_errors_collected_at_once(self) -> None:
        """T-08：**一次列出全部**字段错误，不是只报第一个。"""
        self.user_config(
            "schema_version = 1\n"
            "[provider]\n"
            'name = "openai-compatible"\n'
            'model = "m"\n'
            'temperature = "abc"\n'
            "max_tokens = -5\n"
            "[ui]\n"
            "sidebar_width = 999\n"
        )
        bundle = self.load()
        errors = self.fields(bundle, "error")
        self.assertIn("provider.temperature", errors)
        self.assertIn("provider.max_tokens", errors)
        self.assertIn("ui.sidebar_width", errors)

    def test_t09_unknown_field_is_warning_not_silent(self) -> None:
        """T-09 / E-5：未知字段报 **warning**（不阻断），但绝不静默忽略。"""
        self.user_config(VALID_BASE + "\n[ui]\ntema = \"logox-dark\"\n")
        bundle = self.load()
        self.assertIn("ui.tema", self.fields(bundle, "warning"))
        self.assertEqual(self.fields(bundle, "error"), [])
        self.assertIn("未知字段", render_issues(bundle.warnings))

    def test_t10_plaintext_api_key_rejected_without_echo(self) -> None:
        """T-10 / E-6：明文密钥被拒绝，**且异常消息里绝不出现该值**。"""
        secret = "sk-super-secret-value-1234567890"
        self.user_config(config_document(provider={"api_key": secret}))
        bundle = self.load()

        rendered = render_issues(bundle.issues)
        self.assertIn("api_key", rendered)
        self.assertNotIn(secret, rendered, "绝不能回显用户写的密钥")
        self.assertNotIn(secret[:12], rendered)
        self.assertIn("api_key_env", rendered, "必须给出正确做法")

    def test_t11_unsupported_schema_version_file_ignored(self) -> None:
        """T-11：``schema_version`` 不匹配 → 忽略该文件，其余来源仍生效。"""
        self.user_config("schema_version = 99\n[provider]\nmodel = 'x'\n")
        self.project_config(VALID_BASE)
        bundle = self.load()
        self.assertEqual(bundle.config.provider.model, "test-model")
        self.assertTrue(any(issue.field == "schema_version" for issue in bundle.issues))

    def test_t12_syntax_error_reports_line_number(self) -> None:
        """T-12 / E-2：语法错误要能指出**行列**（尽力从 tomllib 消息中提取）。"""
        self.user_config("schema_version = 1\n[provider\nmodel = 'x'\n")
        bundle = self.load()
        syntax = [issue for issue in bundle.issues if "语法错误" in issue.message]
        self.assertEqual(len(syntax), 1)
        self.assertIsNotNone(syntax[0].line, "应能提取到行号（CPython 的 tomllib 会附带位置）")

    def test_t13_interceptor_hook_event_explains_v1_limitation(self) -> None:
        """T-13 / E-16：拦截型钩子被拒绝，且消息说明「v1 仅支持观察型」。"""
        self.user_config(
            VALID_BASE
            + "\n[[hooks.entries]]\nevent = \"pre_tool_use\"\ncommand = \"echo hi\"\n"
        )
        bundle = self.load()
        rendered = render_issues(bundle.errors)
        self.assertIn("hooks.entries[0].event", rendered)
        self.assertIn("观察型", rendered)

    def test_observable_hook_event_accepted(self) -> None:
        self.user_config(
            VALID_BASE
            + "\n[[hooks.entries]]\nevent = \"post_tool_use\"\nmatcher = \"edit|write\"\ncommand = \"ruff format\"\n"
        )
        bundle = self.load()
        self.assertEqual(self.fields(bundle, "error"), [])
        self.assertEqual(bundle.config.hooks.entries[0].event, "post_tool_use")

    # -- 语义约束 ------------------------------------------------------- #

    def test_undefined_provider_lists_options(self) -> None:
        self.user_config('schema_version = 1\n[provider]\nname = "nope"\nmodel = "m"\n')
        bundle = self.load()
        rendered = render_issues(bundle.errors)
        self.assertIn("provider.name", rendered)
        self.assertIn("openai-compatible", rendered, "必须列出可用值")

    def test_unknown_theme_lists_available(self) -> None:
        self.user_config(VALID_BASE + '\n[ui]\ntheme = "not-a-theme"\n')
        bundle = self.load()
        rendered = render_issues(bundle.errors)
        self.assertIn("ui.theme", rendered)
        for name in BUILTIN_THEMES:
            self.assertIn(name, rendered)

    def test_unknown_tool_name_is_warning(self) -> None:
        self.user_config(VALID_BASE + '\n[tools]\nenabled = ["read", "teleport"]\n')
        bundle = self.load()
        self.assertIn("tools.enabled", self.fields(bundle, "warning"))

    # -- 状态叠加、严格模式、路径 --------------------------------------- #

    def test_t36_state_last_effort_overrides_config(self) -> None:
        """T-36：``state.toml`` 的「上次使用」优先于 config（否则 ``/effort`` 重启即失效）。"""
        from logox.config.schema import LastUsed

        self.user_config(config_document(provider={"thinking_effort": "low"}))
        bundle = self.load(state=StateFile(last=LastUsed(effort="medium")))
        self.assertEqual(bundle.config.provider.thinking_effort, "medium")
        self.assertIn("state", bundle.origin_of("provider.thinking_effort") or "")

    def test_strict_config_raises_on_any_issue(self) -> None:
        """D44 ①：``--strict-config`` 在存在任何 issue 时抛错。"""
        self.user_config(VALID_BASE + "\n[ui]\ntema = 1\n")
        with self.assertRaises(ConfigValidationError):
            self.load(strict=True)

    def test_t29_nested_directory_finds_ancestor_project(self) -> None:
        """T-29 / D44 ①：从深层子目录启动时仍能找到祖先的项目配置。"""
        self.write(self.project / ".logox" / "config.toml", VALID_BASE + '\n[ui]\ntheme = "logox-light"\n')
        deep = self.project / "a" / "b" / "c"
        deep.mkdir(parents=True)

        bundle = self.load(deep)
        self.assertEqual(bundle.config.ui.theme, "logox-light")
        origins = [source for source in bundle.sources if source.scope == "project"]
        self.assertEqual(len(origins), 1)
        self.assertEqual(origins[0].depth, 0, "最近的祖先 depth 为 0")

    def test_t28_paths_with_cjk_and_spaces(self) -> None:
        """T-28 / E-14：含中文与空格的路径必须正常工作。"""
        weird = self.root / "我的 项目" / "子 目录"
        (weird / ".logox").mkdir(parents=True)
        self.write(weird / ".logox" / "config.toml", VALID_BASE + '\n[ui]\ntheme = "logox-light"\n')

        bundle = self.load(weird)
        self.assertEqual(bundle.config.ui.theme, "logox-light")

    def test_no_project_directory_does_not_create_one(self) -> None:
        """不在用户目录里凭空创建 ``.logox``。（同样隔离环境里的 state.toml，见 T-01）"""
        self.load(self.bare, state=StateFile())
        self.assertFalse((self.bare / ".logox").exists())

    # -- 报告渲染 ------------------------------------------------------- #

    def test_render_issues_has_four_elements(self) -> None:
        """§9：错误信息必须含 文件路径 + 字段路径 + 原因（+ 可用值）。"""
        self.user_config(VALID_BASE + '\n[ui]\ntheme = "nope"\n')
        rendered = render_issues(self.load().issues)
        self.assertIn(str(self.paths.config), rendered)  # 文件路径
        self.assertIn("[ui.theme]", rendered)  # 字段路径
        self.assertIn("不存在", rendered)  # 原因
        self.assertIn("logox-dark", rendered)  # 可用值

    def test_defensive_returns_defaults_when_nothing_strippable(self) -> None:
        """兜底：无法定位的问题不得导致死循环，而应整体回退默认值。

        模拟"摘除失败"（例如模型级校验失败的 ``loc`` 为空）——此时循环必须立刻
        退出，而不是无限重试。
        """
        from logox.config import loader as loader_module

        self.user_config(config_document(provider={"temperature": "abc"}))

        with mock.patch.object(loader_module, "_strip", return_value=False):
            bundle = self.load()

        self.assertEqual(bundle.config, LogoxConfig())
        self.assertTrue(any("回退" in issue.message for issue in bundle.issues))


# --------------------------------------------------------------------------- #
# 极简 TOML writer（T-14 – T-19）
# --------------------------------------------------------------------------- #


class TomlWriterTests(unittest.TestCase):
    def test_t14_exact_output(self) -> None:
        """T-14：输出逐字节等于期望文本。"""
        text = writer.dumps({"a": 1, "b": 1.0, "c": True, "d": "x"})
        self.assertEqual(text, 'a = 1\nb = 1.0\nc = true\nd = "x"\n')

    def test_t15_bool_is_not_serialized_as_int(self) -> None:
        """T-15：``bool`` 必须先于 ``int`` 判定（否则 ``True`` 会写成 ``1``）。"""
        text = writer.dumps({"flag": True, "count": 1})
        self.assertEqual(text, "flag = true\ncount = 1\n")

    def test_t16_escaping_and_control_characters(self) -> None:
        """T-16：转义必需字符；无法转义的控制字符必须报错，不得静默丢弃。"""
        self.assertEqual(writer.dumps({"s": 'a"b\\c'}), 's = "a\\"b\\\\c"\n')
        self.assertEqual(writer.dumps({"s": "a\nb\tc"}), 's = "a\\nb\\tc"\n')
        with self.assertRaises(TomlWriteError) as ctx:
            writer.dumps({"s": "bad\x07bell"})
        self.assertIn("U+0007", str(ctx.exception))

    def test_t17_cjk_is_written_raw(self) -> None:
        """T-17：中文原样输出 UTF-8，不转 ``\\uXXXX``（保证文件可读）。"""
        text = writer.dumps({"name": "中文 路径\\测试"})
        self.assertIn("中文", text)
        self.assertNotIn("\\u", text)

    def test_t18_nested_tables_and_array_tables_roundtrip(self) -> None:
        """T-18：顺序正确（标量先、子表后），且可被 ``tomllib`` 无损解析。"""
        data = {
            "top": 1,
            "table": {"x": 1, "sub": {"y": "z"}},
            "items": [{"name": "a", "value": 1}, {"name": "b", "value": 2}],
        }
        text = writer.dumps(data)
        self.assertEqual(tomllib.loads(text), data)
        # 顶层标量必须出现在任何表之前，否则会被归入后开的表。
        self.assertLess(text.index("top = 1"), text.index("[table]"))

    def test_array_table_items_keep_full_path_for_nested_tables(self) -> None:
        """数组表项内部的子表必须写成 ``[parent.child]``，否则生成非法 TOML。"""
        data = {"servers": [{"name": "fs", "env": {"A": "1"}}]}
        text = writer.dumps(data)
        self.assertIn("[[servers]]", text)
        self.assertIn("[servers.env]", text)
        self.assertEqual(tomllib.loads(text), data)

    def test_t19_property_roundtrip(self) -> None:
        """T-19：随机嵌套结构 dumps → loads 语义等价。"""
        samples = [
            {"a": {"b": {"c": [1, 2, 3]}}},
            {"list_of_tables": [{"k": "值", "n": 2.5}]},
            {"empty_list": [], "empty_string": "", "zero": 0, "neg": -3},
            {"unicode_key 中文": "ok", "quote\"key": 1},
        ]
        for sample in samples:
            with self.subTest(sample=sample):
                self.assertEqual(tomllib.loads(writer.dumps(sample)), sample)

    def test_none_skips_key(self) -> None:
        self.assertEqual(writer.dumps({"a": None, "b": 1}), "b = 1\n")

    def test_unsupported_types_raise(self) -> None:
        """E-18 / E-19：不支持的类型必须报错，绝不静默降级。"""
        with self.assertRaises(TomlWriteError):
            writer.dumps({"when": object()})
        with self.assertRaises(TomlWriteError):
            writer.dumps({"nested": [[1, 2]]})
        with self.assertRaises(TomlWriteError):
            writer.dumps({"ratio": float("inf")})

    def test_empty_mapping_yields_empty_text(self) -> None:
        self.assertEqual(writer.dumps({}), "")


# --------------------------------------------------------------------------- #
# StateStore（T-20 – T-25）
# --------------------------------------------------------------------------- #


class StateStoreTests(ConfigTestCase):
    def store(self) -> StateStore:
        return StateStore(self.paths.state)

    def test_t20_update_writes_atomically_and_leaves_no_tmp(self) -> None:
        """T-20：写回正确，且不留下 ``.tmp`` 残骸。"""
        store = self.store()

        def transform(state: StateFile) -> StateFile:
            state.last.model = "m1"
            return state

        store.update(transform)

        self.assertEqual(store.read().last.model, "m1")
        self.assertFalse((self.paths.state.parent / (self.paths.state.name + ".tmp")).exists())
        parsed = tomllib.loads(self.paths.state.read_text(encoding="utf-8"))
        self.assertEqual(parsed["schema_version"], SCHEMA_VERSION)

    def test_t21_failed_replace_keeps_original_intact(self) -> None:
        """T-21：写入中途失败时**原文件内容不变**（原子替换的意义所在）。"""
        store = self.store()
        store.write(StateFile())
        original = self.paths.state.read_text(encoding="utf-8")

        with mock.patch("os.replace", side_effect=OSError("被占用")), self.assertRaises(ConfigWriteError):
            store.write(StateFile(last={"model": "should-not-land"}))  # type: ignore[arg-type]

        self.assertEqual(self.paths.state.read_text(encoding="utf-8"), original)

    def test_t22_external_modification_is_merged_not_overwritten(self) -> None:
        """T-22 / E-10：检测到外部并发修改 → 重读重算，**不覆盖别人的写入**。"""
        store = self.store()
        store.write(StateFile())
        injected = False

        def transform(state: StateFile) -> StateFile:
            nonlocal injected
            if not injected:
                injected = True
                # 模拟另一个进程在我们读取之后写入
                self.paths.state.write_text(
                    writer.dumps(
                        {
                            "schema_version": SCHEMA_VERSION,
                            "last": {"provider": "external-provider", "model": None},
                        }
                    ),
                    encoding="utf-8",
                )
            state.last.model = "mine"
            return state

        store.update(transform)

        final = store.read()
        self.assertEqual(final.last.provider, "external-provider", "外部写入必须保留")
        self.assertEqual(final.last.model, "mine", "本次修改也要生效")

    def test_t23_persistent_conflict_raises_without_corruption(self) -> None:
        """T-23 / E-23：持续冲突时抛错，且不破坏现有文件。

        注意"持续冲突"必须是**每次都写入不同的内容**——如果外部进程反复写入同一份
        字节，内容比对会正确地认为"没有变化"，那不是冲突而是收敛。
        """
        store = self.store()
        store.write(StateFile())
        rounds = 0

        def always_conflict(state: StateFile) -> StateFile:
            nonlocal rounds
            rounds += 1
            self.paths.state.write_text(
                writer.dumps(
                    {"schema_version": SCHEMA_VERSION, "last": {"provider": f"flapping-{rounds}"}}
                ),
                encoding="utf-8",
            )
            state.last.model = "mine"
            return state

        with self.assertRaises(ConfigWriteError):
            store.update(always_conflict)

        self.assertEqual(rounds, 3, "应重试 3 次后放弃")
        self.assertEqual(store.read().last.provider, "flapping-3", "外部内容不得被破坏")

    def test_t24_corrupt_state_backed_up_then_rebuilt(self) -> None:
        """T-24 / E-11：损坏的状态文件**先备份再重建**，绝不静默丢弃用户数据。"""
        self.paths.state.write_text("这不是 TOML {{{", encoding="utf-8")
        store = self.store()

        with self.assertRaises(ConfigValidationError):
            store.read()

        rebuilt = store.read_or_rebuild()
        self.assertEqual(rebuilt, StateFile())
        self.assertTrue((self.paths.state.parent / (self.paths.state.name + ".bak")).exists())

    def test_missing_state_file_is_not_an_error(self) -> None:
        self.assertEqual(self.store().read(), StateFile())

    def test_t25_learn_permission_deduplicates(self) -> None:
        """T-25：重复学习同一条规则时不重复写入。"""
        store = self.store()
        self.assertTrue(store.learn_permission("allow", "shell:git status"))
        self.assertFalse(store.learn_permission("allow", "shell:git status"), "第二次应跳过写盘")
        self.assertEqual(store.read().permissions.allow, ["shell:git status"])

    def test_learn_permission_rejects_unknown_kind(self) -> None:
        with self.assertRaises(ValueError):
            self.store().learn_permission("maybe", "x")

    def test_set_status_item_validates_key(self) -> None:
        """T-32 相关：11 个合法键全部可写，未知键直接报错。"""
        store = self.store()
        for key in STATUS_ITEM_KEYS:
            store.set_status_item(key, True)
        state = store.read()
        self.assertEqual(sorted(state.ui.status_items), sorted(STATUS_ITEM_KEYS))

        with self.assertRaises(ValueError):
            store.set_status_item("typo_item", True)

    def test_cache_shell_backend(self) -> None:
        class Info:
            backend = "gitbash"
            executable = r"C:\Program Files\Git\bin\bash.exe"
            version = "5.2.37"
            detected_at = 1730000000.0

        store = self.store()
        store.cache_shell_backend(Info())
        cached = store.read().shell
        self.assertEqual(cached.backend, "gitbash")
        self.assertEqual(cached.version, "5.2.37")

    def test_aupdate_works_in_async_context(self) -> None:
        import asyncio

        async def scenario() -> None:
            store = self.store()
            await store.aupdate(self._set_theme)
            self.assertEqual(store.read().last.theme, "logox-light")

        asyncio.run(scenario())

    @staticmethod
    def _set_theme(state: StateFile) -> StateFile:
        state.last.theme = "logox-light"
        return state

    def test_state_file_never_touches_user_config(self) -> None:
        """D30 红线：写状态**绝不**改动用户手写的 config.toml。"""
        user_text = VALID_BASE + "\n# 我的注释：不要动我\n[ui]\ntheme = \"logox-light\"\n"
        self.user_config(user_text)
        before = self.paths.config.read_bytes()

        self.store().set_theme("logox-contrast")

        self.assertEqual(self.paths.config.read_bytes(), before, "用户文件必须逐字节不变")


# --------------------------------------------------------------------------- #
# 主题（T-26 / T-27）
# --------------------------------------------------------------------------- #


class ThemeTests(ConfigTestCase):
    def test_t26_missing_token_is_reported_by_name(self) -> None:
        """T-26 / E-12：缺少 token → 拒绝加载并**列出缺失项**。"""
        document = theme_document()
        broken = document.replace('danger = "#f38ba8"\n', "")
        path = self.write(self.paths.themes / "broken.toml", broken)

        with self.assertRaises(ThemeError) as ctx:
            load_theme(path)
        self.assertIn("danger", str(ctx.exception))

    def test_invalid_color_format_rejected(self) -> None:
        """E-13：只接受 ``#RRGGBB``，不接受颜色名或三字符写法。"""
        path = self.write(self.paths.themes / "bad.toml", theme_document(palette=palette(accent="red")))
        with self.assertRaises(ThemeError):
            load_theme(path)

        path2 = self.write(self.paths.themes / "bad2.toml", theme_document(palette=palette(accent="#FFF")))
        with self.assertRaises(ThemeError):
            load_theme(path2)

    def test_valid_theme_loads(self) -> None:
        path = self.write(self.paths.themes / "ok.toml", theme_document())
        theme = load_theme(path)
        self.assertEqual(theme.name, "test-theme")
        self.assertEqual(theme.variant, "dark")

    def test_t27_high_contrast_theme_must_meet_7_to_1(self) -> None:
        """T-27：高对比主题必须达到 7:1，否则校验失败并给出具体数值。"""
        weak = palette(bg_base="#000000", text_primary="#555555", text_muted="#444444")
        theme = load_theme(
            self.write(self.paths.themes / "weak.toml", theme_document(variant="high_contrast", palette=weak))
        )
        problems = validate_contrast(theme)
        self.assertTrue(problems)
        self.assertTrue(any("7" in problem for problem in problems))

    def test_normal_theme_contrast_check_passes_for_reference_palette(self) -> None:
        theme = load_theme(self.write(self.paths.themes / "ref.toml", theme_document()))
        self.assertEqual(validate_contrast(theme), [])

    def test_discover_themes_prefers_later_directory(self) -> None:
        """同名主题：**用户目录优先**（dirs 按优先级从低到高传入）。"""
        builtin = self.root / "builtin-themes"
        user = self.paths.themes
        self.write(builtin / "shared.toml", theme_document(name="builtin"))
        self.write(user / "shared.toml", theme_document(name="user"))

        found = discover_themes([builtin, user])
        self.assertEqual(found["shared"], user / "shared.toml")

    def test_contrast_ratio_helpers(self) -> None:
        self.assertAlmostEqual(contrast_ratio("#ffffff", "#000000"), 21.0, places=1)
        self.assertAlmostEqual(contrast_ratio("#ffffff", "#ffffff"), 1.0, places=5)
        self.assertEqual(mix("#000000", "#ffffff", 0.5), "#808080")

    def test_theme_with_unknown_variant_rejected(self) -> None:
        path = self.write(self.paths.themes / "v.toml", theme_document(variant="neon"))
        with self.assertRaises(ThemeError):
            load_theme(path)


# --------------------------------------------------------------------------- #
# 默认值一致性（T-30 – T-35）
# --------------------------------------------------------------------------- #


def flatten(data: object, prefix: str = "") -> dict[str, object]:
    result: dict[str, object] = {}
    if isinstance(data, dict):
        for key, value in data.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(value, dict) and value:
                result.update(flatten(value, path))
            elif isinstance(value, (list, tuple)) and len(value) == 0:
                continue  # 空集合无法从 TOML 中区分"未写"与"写空"
            elif isinstance(value, dict):
                continue  # 空表同理
            else:
                result[path] = value
    return result


class DefaultsDriftTests(unittest.TestCase):
    def test_t30_defaults_toml_matches_schema(self) -> None:
        """T-30：``defaults.toml`` 与 ``schema.py`` 的默认值**逐字段一致**（守护不漂移）。"""
        defaults_path = Path(logox.__file__).parent / "config" / "defaults.toml"
        documented = flatten(tomllib.loads(defaults_path.read_text(encoding="utf-8")))
        actual = flatten(LogoxConfig().model_dump())

        for path, expected in actual.items():
            with self.subTest(field=path):
                self.assertIn(path, documented, f"{path} 未记录在 defaults.toml")
                self.assertEqual(documented[path], expected, f"{path} 的默认值不一致")

        for path in documented:
            with self.subTest(field=path):
                self.assertIn(path, actual, f"defaults.toml 里的 {path} 在 schema 中不存在")

    def test_defaults_toml_is_serializable_by_our_writer(self) -> None:
        """defaults.toml 必须能被我们自己的 writer 语义等价地重写（往返保护）。"""
        defaults_path = Path(logox.__file__).parent / "config" / "defaults.toml"
        parsed = tomllib.loads(defaults_path.read_text(encoding="utf-8"))
        self.assertEqual(tomllib.loads(writer.dumps(parsed)), parsed)


class StatusItemConfigTests(ConfigTestCase):
    def test_t31_timing_enabled_with_all_fields_off_is_valid(self) -> None:
        """T-31：``timing`` 开启但三字段全关是合法配置（UI 需整项跳过，不输出空段）。"""
        self.user_config(
            VALID_BASE
            + "\n[ui.status_items]\ntiming = true\n"
            + "\n[ui.timing_fields]\ntotal = false\nllm = false\ntool = false\n"
        )
        bundle = self.load()
        self.assertEqual(self.fields(bundle, "error"), [])
        self.assertTrue(bundle.config.ui.status_items.timing)
        self.assertFalse(bundle.config.ui.timing_fields.total)

    def test_t32_all_status_item_keys_recognized_with_documented_defaults(self) -> None:
        """T-32 / D42：11 个状态项全部被识别；默认**仅开 4 项**。"""
        self.user_config(VALID_BASE)
        items = self.load().config.ui.status_items
        self.assertEqual(len(STATUS_ITEM_KEYS), 11)
        self.assertEqual(
            sorted(items.enabled_keys()),
            ["cache", "context", "model", "throughput"],
            "D42：默认只开 model / context / throughput / cache",
        )
        for key in STATUS_ITEM_KEYS:
            self.assertIsInstance(getattr(items, key), bool)

    def test_t33_unknown_timing_field_is_warning(self) -> None:
        """T-33：``ui.timing_fields`` 的未知键 → warning + 忽略。"""
        self.user_config(VALID_BASE + "\n[ui.timing_fields]\ntotal = true\ntypo = true\n")
        bundle = self.load()
        self.assertIn("ui.timing_fields.typo", self.fields(bundle, "warning"))

    def test_t34_invalid_thinking_effort_lists_allowed_values(self) -> None:
        """T-34：非法档位报错并列出合法值。"""
        self.user_config(config_document(provider={"thinking_effort": "extreme"}))
        bundle = self.load()
        rendered = render_issues(bundle.errors)
        self.assertIn("provider.thinking_effort", rendered)
        for allowed in ("off", "low", "medium", "high", "auto"):
            self.assertIn(allowed, rendered)

    def test_t35_thinking_effort_valid_even_if_model_lacks_thinking(self) -> None:
        """T-35：config 层不越界判断模型能力——模型是否支持思考由适配器处理。"""
        self.user_config(config_document(provider={"model": "plain-model", "thinking_effort": "high"}))
        bundle = self.load()
        self.assertEqual(self.fields(bundle, "error"), [])
        self.assertEqual(bundle.config.provider.thinking_effort, "high")


# --------------------------------------------------------------------------- #
# LayeredStateStore（D120 全局偏好共享与权限隔离）
# --------------------------------------------------------------------------- #


class LayeredStateStoreTests(ConfigTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.project_state_path = self.project / ".logox" / "state.toml"
        self.global_store = StateStore(self.paths.state)
        self.project_store = StateStore(self.project_state_path)
        self.layered = LayeredStateStore(self.project_store, self.global_store)

    def test_read_inherits_global_preferences_when_project_empty(self) -> None:
        """全局偏好在新项目（空状态）中无缝继承。"""
        self.global_store.set_last_model(provider="deepseek", model="deepseek-chat")
        self.global_store.set_theme("logox-dark")
        self.global_store.set_effort("high")

        merged = self.layered.read()
        self.assertEqual(merged.last.provider, "deepseek")
        self.assertEqual(merged.last.model, "deepseek-chat")
        self.assertEqual(merged.last.theme, "logox-dark")
        self.assertEqual(merged.last.effort, "high")

    def test_read_project_overrides_global_preferences(self) -> None:
        """项目级特有偏好优先覆盖全局偏好。"""
        self.global_store.set_last_model(provider="deepseek", model="deepseek-chat")
        self.project_store.set_last_model(model="deepseek-reasoner")

        merged = self.layered.read()
        self.assertEqual(merged.last.provider, "deepseek", "未覆盖的字段保留全局值")
        self.assertEqual(merged.last.model, "deepseek-reasoner", "项目显式配置优先覆盖")

    def test_permissions_are_strictly_isolated_to_project(self) -> None:
        """权限隔离（D120 红线）：learn_permission 绝不写全局，读取绝不继承全局。"""
        self.global_store.learn_permission("allow", "shell:rm -rf /")

        learned = self.layered.learn_permission("allow", "shell:git status")
        self.assertTrue(learned)

        # 1. 验证全局 store 绝没有被写入该项目的权限
        global_state = self.global_store.read()
        self.assertNotIn("shell:git status", global_state.permissions.allow)

        # 2. 验证当前项目读取到的权限严格采用项目级，且没有穿透进全局的危险命令
        merged = self.layered.read()
        self.assertEqual(merged.permissions.allow, ["shell:git status"])
        self.assertNotIn("shell:rm -rf /", merged.permissions.allow, "全局危险命令绝不得穿透进项目")

    def test_preferences_write_back_updates_both_stores(self) -> None:
        """修改偏好（模型/主题/思考）时双写全局与项目，确保下次换项目免配。"""
        self.layered.set_last_model(provider="openai", model="gpt-4o")
        self.layered.set_theme("latte")
        self.layered.set_effort("medium")
        self.layered.set_status_item("model", False)
        self.layered.set_focus_mode(True)

        # 检查全局
        g = self.global_store.read()
        self.assertEqual(g.last.provider, "openai")
        self.assertEqual(g.last.model, "gpt-4o")
        self.assertEqual(g.last.theme, "latte")
        self.assertEqual(g.last.effort, "medium")
        self.assertFalse(g.ui.status_items.get("model"))
        self.assertTrue(g.ui.focus_mode)

        # 检查项目
        p = self.project_store.read()
        self.assertEqual(p.last.provider, "openai")
        self.assertEqual(p.last.model, "gpt-4o")
        self.assertEqual(p.last.theme, "latte")
        self.assertEqual(p.last.effort, "medium")
        self.assertFalse(p.ui.status_items.get("model"))
        self.assertTrue(p.ui.focus_mode)

    def test_models_cache_hierarchy(self) -> None:
        """端点模型列表缓存：优先项目，缺失回退全局。"""
        self.global_store.set_models("deepseek", ["deepseek-chat", "deepseek-coder"])
        self.assertEqual(
            self.layered.cached_models("deepseek"), ["deepseek-chat", "deepseek-coder"]
        )

        # 项目有局部专属缓存时优先
        self.project_store.set_models("deepseek", ["deepseek-custom"])
        self.assertEqual(self.layered.cached_models("deepseek"), ["deepseek-custom"])

    def test_same_path_handles_gracefully(self) -> None:
        """当项目路径恰好与全局路径一致时，双写降级为单写，无冲突。"""
        same_store = LayeredStateStore(self.global_store, self.global_store)
        same_store.set_last_model(provider="deepseek", model="deepseek-chat")
        self.assertEqual(same_store.read().last.model, "deepseek-chat")


if __name__ == "__main__":
    unittest.main()
