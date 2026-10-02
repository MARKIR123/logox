"""斜杠命令的流程测试（新界面，D80 §9 第 6 步）。

这里测的是**业务流程**，不是渲染：什么时候问什么、取消怎么办、失败怎么办。
所以用一个**假 host**（直接把脚本好的答案交给命令层）而不是终端——
于是每条流程都能在几毫秒内跑完，而且断言的是"世界发生了什么"（内核换了没有、
`state.toml` 写了没有、密钥落盘了没有），不是"屏幕上出现了什么"。

按键与渲染由 `test_render_overlay.py`（组件）与 `test_render_inline_app.py`
（整机接线）分别覆盖。三者拼起来才是完整的一条链。
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

from logox.app import ReloadItem, ResourceReloadReport
from logox.tui.commands import ResolvedCommand
from logox.tui.render.commands import EFFORT_LEVELS, CommandRunner
from logox.tui.theme import load_theme


class FakeDiscovery:
    def __init__(self, *, models: list[str] | None = None, error: str = "") -> None:
        self.models = models or []
        self.error = error
        self.attempted = True

    @property
    def ok(self) -> bool:
        return not self.error

    def summary(self) -> str:
        return f"抓到 {len(self.models)} 个" if self.ok else f"失败：{self.error}"


class FakeProvider:
    def __init__(self, name: str) -> None:
        self.name = name
        self.models: list[str] = []

    def set_models(self, models: list[str]) -> None:
        self.models = models


class FakeKernel:
    def __init__(self) -> None:
        self.models: list[str] = []
        self.thinking: list[str] = []
        self.providers: list[Any] = []

    def set_model(self, model: str) -> None:
        self.models.append(model)

    def set_thinking(self, effort: str) -> None:
        self.thinking.append(effort)

    def set_provider(self, provider: object) -> None:
        self.providers.append(provider)


class FakeState:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def set_last_model(self, provider: str | None = None, model: str | None = None) -> None:
        self.calls.append(("last_model", (provider, model)))

    def set_theme(self, theme: str) -> None:
        self.calls.append(("theme", theme))

    def set_effort(self, effort: str) -> None:
        self.calls.append(("effort", effort))

    def set_models(self, provider: str, models: list[str], **_: Any) -> None:
        self.calls.append(("models", (provider, models)))


class FakeRuntime:
    """只实现命令层真正用到的那几个方法（**窄接口**的价值就在这里）。"""

    def __init__(self, *, env_file: Path | None = None) -> None:
        self.kernel = FakeKernel()
        self.state_store = FakeState()
        self.registry = object()  # 非 None 即可（命令层只判断"有没有"）
        self.provider_name = "deepseek"
        self.model = "deepseek-flash"
        self.needs_login = True
        self.cwd = Path(".")
        self.tools = ["read"]
        self.env_file = env_file
        self.warnings: list[str] = []
        self.config = None
        self._providers = {
            "deepseek": ("DEEPSEEK_API_KEY", ["deepseek-flash", "deepseek-v4-pro"]),
            "ollama": ("", ["llama3"]),
        }
        self._models: dict[str, list[str]] = {}
        self.build_error: str = ""
        self.discovery = FakeDiscovery(models=["deepseek-flash", "deepseek-v4-pro"])
        self.refresh_calls: list[tuple[str, str | None]] = []
        #: `/reload`：记录被调用次数，并交出一份可脚本化的报告
        self.reloaded = 0
        self.reload_report = ResourceReloadReport(
            items=[ReloadItem("项目记忆", detail="AGENTS.md · 3,680 tokens")],
            prefix_changed=False,
            system_tokens_before=4_980,
            system_tokens_after=4_980,
            not_reloaded=["Python 代码（改 .py 仍需重启）"],
        )

    def reload_resources(self) -> Any:
        self.reloaded += 1
        return self.reload_report

    def available_providers(self) -> list[str]:
        return list(self._providers)

    def provider_details(self, name: str) -> dict[str, Any]:
        env_name, models = self._providers.get(name, ("", []))
        has_key = getattr(self, "configured_keys", {}).get(name, False)
        return {
            "name": name,
            "api_key_env": env_name,
            "base_url": "",
            "models": list(models),
            "has_key": has_key,
        }

    def build_provider(self, name: str, *, api_key: str | None = None) -> Any:
        if self.build_error:
            raise RuntimeError(self.build_error)
        return FakeProvider(name)

    def default_model_for(self, name: str) -> str:
        return (self.provider_details(name)["models"] or [""])[0]

    def list_models(self, name: str, *, api_key: str | None = None) -> list[str]:
        return list(self._models.get(name) or self.provider_details(name)["models"])

    async def refresh_models(
        self, name: str, *, api_key: str | None = None, timeout_s: float | None = None
    ) -> Any:
        self.refresh_calls.append((name, api_key))
        if self.discovery.ok:
            self._models[name] = list(self.discovery.models)
        return self.discovery

    def list_sessions(self, cwd: Any = None) -> list[Any]:
        from logox.paths import LogoxPaths
        from logox.store.manager import SessionManager

        paths = getattr(self, "paths", None)
        base_dir = (
            paths.sessions
            if paths and hasattr(paths, "sessions")
            else LogoxPaths.default().sessions
        )
        mgr = SessionManager(base_dir)
        return mgr.list_sessions(cwd or self.cwd)


class FakeHost:
    """假界面：把脚本好的答案交给命令层，并记录"提示了什么"。"""

    def __init__(self, runtime: FakeRuntime, answers: list[Any] | None = None) -> None:
        self.runtime = runtime
        self.theme = load_theme("logox-dark")
        self.effort = "auto"
        self.content_width = 100
        self.content_rows = 20
        #: 依次交给 `push_overlay` 的返回值（可调用 = 按组件类型决定）
        self.answers: list[Any] = list(answers or [])
        self.overlays: list[Any] = []
        self.notices: list[str] = []
        self.stopped = False
        self.cleared = False
        self.refreshed = 0
        self.applied_theme: str | None = None
        self.events: list[str] = []
        #: 是否正在生成回复（`/reload` 据此拒绝执行）
        self.busy = False

    async def push_overlay(self, component: Any, *, max_rows: int | None = None) -> Any:
        self.overlays.append(component)
        if not self.answers:
            # 只读面板（`/help`、`/status`、`/debug`）会一直等到用户关掉它。
            # 假 host 的等价物是"立刻关掉"。而**选择器 / 输入框 / 确认框**如果
            # 没有脚本答案，多半意味着流程问了一个测试没预料到的问题——
            # 那时静默返回 None 会让断言以"已取消"的形式通过，掩盖真正的缺陷。
            if type(component).__name__ == "PanelComponent":
                return None
            raise AssertionError("命令问了更多问题，但脚本里没有答案了")
        answer = self.answers.pop(0)
        if callable(answer):
            return answer(component)
        return answer

    def notice(self, message: str, *, token: str = "text_muted") -> None:
        self.notices.append(message)

    def refresh_status(self) -> None:
        self.refreshed += 1

    def clear_timeline(self) -> None:
        self.cleared = True

    def apply_theme(self, name: str) -> str:
        self.applied_theme = name
        return name

    def apply_effort(self, effort: str) -> None:
        self.effort = effort

    def recent_events(self) -> list[str]:
        return self.events

    def stop(self) -> None:
        self.stopped = True

    def switch_session(self, file_path: Any) -> int:
        self.switched_session = file_path
        return 3

    def new_session(self) -> Any:
        self.new_session_called = True
        return Path("fake_new.jsonl")

    def delete_session(self, file_path: Any, *, soft: bool = True) -> Any:
        self.deleted_session = file_path
        return file_path

    def is_current_session(self, file_path: Any) -> bool:
        return getattr(self, "current_session", None) == file_path

    @property
    def text(self) -> str:
        return "\n".join(self.notices)


def command(text: str) -> ResolvedCommand:
    from logox.tui.commands import resolve

    resolved = resolve(text)
    assert resolved is not None, f"解析失败：{text}"
    return resolved


class CommandTestCase(unittest.IsolatedAsyncioTestCase):
    def make(self, answers: list[Any] | None = None) -> tuple[CommandRunner, FakeHost, FakeRuntime]:
        runtime = FakeRuntime(env_file=Path(tempfile.gettempdir()) / "logox-test-nonexistent.env")
        host = FakeHost(runtime, answers)
        return CommandRunner(host), host, runtime

    async def run_command(self, runner: CommandRunner, text: str) -> None:
        await runner.run(command(text))


class EffortLevelsTests(unittest.TestCase):
    def test_matches_the_schema(self) -> None:
        """★ 界面里的档位清单必须与配置模型一致，否则 `/effort high` 会被内核拒绝。"""
        from typing import get_args

        from logox.config.schema import ProviderConfig

        allowed = set(get_args(ProviderConfig.model_fields["thinking_effort"].annotation))
        self.assertEqual(set(EFFORT_LEVELS), allowed)


class LoginFlowTests(CommandTestCase):
    async def test_full_flow_swaps_provider_and_persists(self) -> None:
        """★ 完整流程：选供应商 → 输密钥 → 抓模型 → 确认保存 → 装进内核。"""
        runner, host, runtime = self.make(
            answers=[
                lambda component: _pick(component, "deepseek"),
                "sk-test-123",
                False,  # 不写盘（仅本次）
            ]
        )
        await self.run_command(runner, "/login")

        self.assertEqual(runtime.provider_name, "deepseek")
        self.assertEqual(runtime.model, "deepseek-flash", "应当切到该供应商的默认模型")
        self.assertFalse(runtime.needs_login)
        self.assertEqual(len(runtime.kernel.providers), 1, "新 provider 没有装进内核")
        self.assertEqual(runtime.refresh_calls, [("deepseek", "sk-test-123")])
        self.assertIn(("last_model", ("deepseek", "deepseek-flash")), runtime.state_store.calls)
        self.assertIn("已登录 deepseek", host.text)

    async def test_cancel_at_the_provider_step_changes_nothing(self) -> None:
        """★ Esc 取消**不能留下半成品状态**（这是多步交互最容易写出的缺陷）。"""
        runner, host, runtime = self.make(answers=[None])
        await self.run_command(runner, "/login")

        self.assertEqual(runtime.provider_name, "deepseek")
        self.assertEqual(runtime.kernel.providers, [])
        self.assertEqual(runtime.state_store.calls, [])
        self.assertTrue(runtime.needs_login)
        self.assertIn("已取消登录", host.text)

    async def test_cancel_at_the_key_step_changes_nothing(self) -> None:
        runner, host, runtime = self.make(
            answers=[lambda component: _pick(component, "deepseek"), None]
        )
        await self.run_command(runner, "/login")

        self.assertEqual(runtime.kernel.providers, [])
        self.assertEqual(runtime.state_store.calls, [])
        self.assertIn("已取消登录", host.text)

    async def test_a_bad_key_leaves_the_current_session_untouched(self) -> None:
        """★ **先验证再替换**：构造失败时当前连接原封不动。

        用户不会因为一次输错密钥就丢掉正在用的东西。
        """
        runner, host, runtime = self.make(
            answers=[lambda component: _pick(component, "deepseek"), "sk-bad"]
        )
        runtime.build_error = "鉴权失败"
        await self.run_command(runner, "/login")

        self.assertEqual(runtime.kernel.providers, [], "验证失败却换了 provider")
        self.assertEqual(runtime.state_store.calls, [])
        self.assertIn("无法使用 deepseek", host.text)

    async def test_remember_yes_writes_the_key_file(self) -> None:
        """★ 选"记住"才落盘，而且要**同时**放进进程环境（本次立刻可用）。"""
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            runner, host, runtime = self.make(
                answers=[
                    lambda component: _pick(component, "deepseek"),
                    "sk-remember-me",
                    True,  # 记住
                ]
            )
            runtime.env_file = env_file
            await self.run_command(runner, "/login")

            self.assertTrue(env_file.is_file(), "选了记住却没写文件")
            self.assertIn("sk-remember-me", env_file.read_text(encoding="utf-8"))
            self.assertEqual(os.environ.get("DEEPSEEK_API_KEY"), "sk-remember-me")
            self.assertIn("已记住密钥", host.text)
            del os.environ["DEEPSEEK_API_KEY"]

    async def test_remember_no_does_not_write_the_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            runner, _host, runtime = self.make(
                answers=[
                    lambda component: _pick(component, "deepseek"),
                    "sk-only-now",
                    False,  # 仅本次
                ]
            )
            runtime.env_file = env_file
            await self.run_command(runner, "/login")

            self.assertFalse(env_file.exists(), "选了仅本次却写了文件")
            self.assertEqual(os.environ.get("DEEPSEEK_API_KEY"), "sk-only-now")
            del os.environ["DEEPSEEK_API_KEY"]

    async def test_cancel_at_the_write_step_keeps_the_key_in_memory_only(self) -> None:
        """★ ``None``（取消）与 ``False``（明确选"否"）必须区别对待。"""
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            runner, host, runtime = self.make(
                answers=[
                    lambda component: _pick(component, "deepseek"),
                    "sk-cancel-write",
                    None,  # Esc
                ]
            )
            runtime.env_file = env_file
            await self.run_command(runner, "/login")

            self.assertFalse(env_file.exists())
            self.assertEqual(os.environ.get("DEEPSEEK_API_KEY"), "sk-cancel-write")
            self.assertIn("已取消写入", host.text)
            del os.environ["DEEPSEEK_API_KEY"]

    async def test_discovery_failure_does_not_block_login(self) -> None:
        """★ 抓不到模型列表**不是**登录失败：回退到本地预设表，并说明原因。"""
        runner, host, runtime = self.make(
            answers=[lambda component: _pick(component, "deepseek"), "sk-x", False]
        )
        runtime.discovery = FakeDiscovery(error="连接超时")
        await self.run_command(runner, "/login")

        self.assertEqual(runtime.provider_name, "deepseek")
        self.assertIn("连接超时", host.text, "抓取失败的原因必须说出来")
        self.assertIn("已登录", host.text)

    async def test_provider_without_a_key_skips_the_prompt(self) -> None:
        """不需要密钥的供应商（本地端点）**不该**弹出密钥输入框。"""
        runner, host, runtime = self.make(answers=[lambda component: _pick(component, "ollama")])
        await self.run_command(runner, "/login")

        self.assertEqual([type(item).__name__ for item in host.overlays], ["PickerComponent"])
        self.assertEqual(runtime.provider_name, "ollama")

    async def test_the_provider_list_marks_the_current_one(self) -> None:
        runner, host, _runtime = self.make(answers=[None])
        await self.run_command(runner, "/login")
        picker = host.overlays[0]
        hints = {choice.label: choice.hint for choice in picker.state.choices}
        self.assertIn("当前", hints["deepseek"])
        self.assertIn("DEEPSEEK_API_KEY", hints["deepseek"])
        self.assertIn("无需密钥", hints["ollama"])

    async def test_no_registry_explains_itself(self) -> None:
        runner, host, runtime = self.make()
        runtime.registry = None
        await self.run_command(runner, "/login")
        self.assertIn("注册表", host.text)
        self.assertEqual(host.overlays, [])

    async def test_login_reuses_existing_key_without_prompt(self) -> None:
        """★ D190：已存在密钥凭据时直接复用，不弹输入框也不问记住。"""
        runner, host, runtime = self.make(
            answers=[
                lambda component: _pick(component, "deepseek"),
            ]
        )
        runtime.configured_keys = {"deepseek": True}
        await self.run_command(runner, "/login")

        self.assertEqual(runtime.provider_name, "deepseek")
        self.assertEqual(runtime.refresh_calls, [("deepseek", None)])
        self.assertIn("复用已保存的密钥凭据（DEEPSEEK_API_KEY）", host.text)
        self.assertIn("已登录 deepseek", host.text)

    async def test_login_reset_forces_prompt_even_if_key_exists(self) -> None:
        """★ D190：使用 --reset 显式强制重设密钥。"""
        runner, host, runtime = self.make(
            answers=[
                lambda component: _pick(component, "deepseek"),
                "sk-new-key-123",
                False,
            ]
        )
        runtime.configured_keys = {"deepseek": True}
        await self.run_command(runner, "/login --reset")

        self.assertEqual(runtime.provider_name, "deepseek")
        self.assertEqual(runtime.refresh_calls, [("deepseek", "sk-new-key-123")])
        self.assertIn("已登录 deepseek", host.text)



class ModelCommandTests(CommandTestCase):
    async def test_direct_argument_switches_without_a_popup(self) -> None:
        """带参数直接切（留给"我知道要哪个"与脚本化），**不弹浮层**。"""
        runner, host, runtime = self.make()
        await self.run_command(runner, "/model deepseek-v4-pro")

        self.assertEqual(runtime.kernel.models, ["deepseek-v4-pro"])
        self.assertEqual(runtime.model, "deepseek-v4-pro")
        self.assertEqual(host.overlays, [], "带参数时不该弹浮层")
        self.assertIn(("last_model", (None, "deepseek-v4-pro")), runtime.state_store.calls)

    async def test_popup_selection_switches(self) -> None:
        runner, host, runtime = self.make(
            answers=[lambda component: _pick(component, "deepseek-v4-pro")]
        )
        await self.run_command(runner, "/model")

        self.assertEqual(runtime.model, "deepseek-v4-pro")
        self.assertEqual(host.overlays[0].state.title, "选择模型 · deepseek")

    async def test_cancel_keeps_the_model(self) -> None:
        runner, host, runtime = self.make(answers=[None])
        await self.run_command(runner, "/model")
        self.assertEqual(runtime.model, "deepseek-flash")
        self.assertEqual(runtime.kernel.models, [])
        self.assertIn("已取消", host.text)

    async def test_no_known_models_says_how_to_proceed(self) -> None:
        runner, host, runtime = self.make()
        runtime._providers["deepseek"] = ("DEEPSEEK_API_KEY", [])
        await self.run_command(runner, "/model")
        self.assertIn("/model <模型名>", host.text, "要告诉用户还能怎么办")

    async def test_kernel_rejection_is_reported(self) -> None:
        runner, host, runtime = self.make()

        def explode(model: str) -> None:
            raise ValueError("模型名非法")

        runtime.kernel.set_model = explode  # type: ignore[method-assign]
        await self.run_command(runner, "/model bad-model")
        self.assertIn("无法切换", host.text)
        self.assertEqual(runtime.model, "deepseek-flash", "失败了就不该改显示")


class ThemeCommandTests(CommandTestCase):
    async def test_popup_selection_applies_and_persists(self) -> None:
        runner, host, runtime = self.make(
            answers=[lambda component: _pick(component, "logox-light")]
        )
        await self.run_command(runner, "/theme")

        self.assertEqual(host.applied_theme, "logox-light")
        self.assertIn(("theme", "logox-light"), runtime.state_store.calls)

    async def test_unknown_theme_lists_the_available_ones(self) -> None:
        runner, host, _runtime = self.make()
        await self.run_command(runner, "/theme nope")
        self.assertIn("不存在", host.text)
        self.assertIn("logox-dark", host.text)
        self.assertIsNone(host.applied_theme)

    async def test_broken_theme_does_not_crash_the_session(self) -> None:
        """主题文件坏了 → 一行提示，会话继续（P-5）。"""
        runner, host, _runtime = self.make()

        def explode(name: str) -> str:
            raise RuntimeError("主题文件损坏")

        host.apply_theme = explode  # type: ignore[method-assign]
        await self.run_command(runner, "/theme logox-light")
        self.assertIn("加载失败", host.text)


class EffortCommandTests(CommandTestCase):
    async def test_direct_argument_applies(self) -> None:
        runner, host, runtime = self.make()
        await self.run_command(runner, "/effort high")
        self.assertEqual(host.effort, "high")
        self.assertIn(("effort", "high"), runtime.state_store.calls)

    async def test_invalid_level_is_rejected_loudly(self) -> None:
        """★ 非法档位要**明确报错、不静默忽略、不写盘**（E-29）。"""
        runner, host, runtime = self.make()
        await self.run_command(runner, "/effort turbo")
        self.assertIn("未知档位", host.text)
        self.assertEqual(host.effort, "auto")
        self.assertEqual(runtime.state_store.calls, [])

    async def test_popup_selection(self) -> None:
        runner, host, _runtime = self.make(answers=[lambda component: _pick(component, "low")])
        await self.run_command(runner, "/effort")
        self.assertEqual(host.effort, "low")


class MiscCommandTests(CommandTestCase):
    async def test_help_opens_a_panel_listing_new_ui_commands(self) -> None:
        runner, host, _runtime = self.make()
        await self.run_command(runner, "/help")
        panel = host.overlays[0]
        body = panel.text.plain
        self.assertIn("Logox 帮助", body)
        self.assertIn("/login", body)
        # 命令清单来自 `tui/commands.py`（**只有一份**），所以帮助里必然有它
        self.assertIn("当前可用", body)
        self.assertIn("还没实现", body, "未实现的命令必须被明确标出来")

    async def test_status_lists_session_facts(self) -> None:
        runner, host, _runtime = self.make()
        await self.run_command(runner, "/status")
        body = host.overlays[0].text.plain
        self.assertIn("deepseek", body)
        self.assertIn("deepseek-flash", body)
        self.assertIn("尚未登录", body)

    async def test_debug_shows_events(self) -> None:
        runner, host, _runtime = self.make()
        host.events = ["  1. SessionStart", "  2. UserPromptSubmit"]
        await self.run_command(runner, "/debug")
        self.assertIn("SessionStart", host.overlays[0].text.plain)

    async def test_clear_empties_the_timeline(self) -> None:
        runner, host, _runtime = self.make()
        await self.run_command(runner, "/clear")
        self.assertTrue(host.cleared)

    async def test_exit_stops(self) -> None:
        for name in ("/exit", "/quit", "/q"):
            runner, host, _runtime = self.make()
            await self.run_command(runner, name)
            self.assertTrue(host.stopped, f"{name} 没有退出")

    async def test_unimplemented_commands_are_named_honestly(self) -> None:
        """★ 还没实现的命令必须**明说**，而不是假装可用（D47 第三条要求）。

        这些命令描述的正是被删掉的侧栏面板（D85）与后续里程碑的能力。
        它们在 `/help` 里被标成"还没实现"，打进去也会得到同一句话——
        用户能立刻知道"这个功能是没做，还是我打错了"。
        """
        for name in ("files", "memory"):
            runner, host, _runtime = self.make()
            await self.run_command(runner, f"/{name}")
            self.assertIn("还没有实现", host.text, f"/{name} 的提示不诚实")
            self.assertIn(name, host.text, "提示里要带上命令名，用户才知道是哪一条")

    async def test_planned_commands_say_they_are_not_implemented(self) -> None:
        runner, host, _runtime = self.make()
        # ⚠️ 用**仍在 planned 里**的命令；`/compact` 已于 D161 落地，改用 `/files`
        await self.run_command(runner, "/files")
        self.assertIn("还没有实现", host.text)

    async def test_compact_is_available_now(self) -> None:
        """D161：`/compact` 已从 planned 移到 ready（它做的事现在真的会发生）。"""
        from logox.tui.commands import AVAILABLE_COMMANDS, PLANNED_COMMANDS

        self.assertIn("compact", AVAILABLE_COMMANDS)
        self.assertNotIn("compact", PLANNED_COMMANDS)

    async def test_unknown_command_lists_what_is_available(self) -> None:
        runner, host, _runtime = self.make()
        await self.run_command(runner, "/nosuchthing")
        self.assertIn("未知命令", host.text)
        self.assertIn("/help", host.text, "要列出可用命令，而不是只说无效")

    async def test_a_failing_command_does_not_propagate(self) -> None:
        """★ 命令流程内部炸了也不能把会话带走——只留一行提示。"""
        runner, host, _runtime = self.make()
        host.push_overlay = _explode  # type: ignore[method-assign]
        await self.run_command(runner, "/help")  # 不该抛出去
        self.assertIn("执行失败", host.text)

    async def test_resume_when_no_sessions_shows_notice(self) -> None:
        runner, host, runtime = self.make()
        with tempfile.TemporaryDirectory() as tmp:
            runtime.cwd = Path(tmp)
            runtime.paths = type("Paths", (), {"sessions": Path(tmp) / "sessions"})()
            await self.run_command(runner, "/resume")
            self.assertIn("没有历史会话", host.text)

    async def test_resume_displays_formatted_sessions_and_switches(self) -> None:
        from logox.store.manager import SessionManager
        runner, host, runtime = self.make()
        with tempfile.TemporaryDirectory() as tmp:
            runtime.cwd = Path(tmp)
            sessions_dir = Path(tmp) / "sessions"
            runtime.paths = type("Paths", (), {"sessions": sessions_dir})()
            mgr = SessionManager(sessions_dir)
            _s1 = mgr.create_session(runtime.cwd, initial_title="修复权限弹窗")
            time.sleep(0.02)
            s2 = mgr.create_session(runtime.cwd, initial_title="重构状态栏度量")

            # 模拟用户在弹出的 Picker 中选中第一个（最近活跃的 s2）
            def _choose_first(component: Any) -> Any:
                # 校验格式：必须严格为 [序号] 会话标题摘要 · 对话轮次
                labels = [c.label for c in component.state.choices]
                self.assertIn("[1] 重构状态栏度量 · 0 轮", labels[0])
                self.assertIn("[2] 修复权限弹窗 · 0 轮", labels[1])
                return component.state.choices[0]

            host.answers = [_choose_first]
            await self.run_command(runner, "/resume")
            self.assertEqual(host.switched_session, s2.file_path)
            self.assertIn("已恢复历史会话", host.text)

    async def test_resume_direct_number_argument(self) -> None:
        from logox.store.manager import SessionManager
        runner, host, runtime = self.make()
        with tempfile.TemporaryDirectory() as tmp:
            runtime.cwd = Path(tmp)
            sessions_dir = Path(tmp) / "sessions"
            runtime.paths = type("Paths", (), {"sessions": sessions_dir})()
            mgr = SessionManager(sessions_dir)
            s1 = mgr.create_session(runtime.cwd, initial_title="任务1")
            time.sleep(0.02)
            _s2 = mgr.create_session(runtime.cwd, initial_title="任务2")

            # /resume 2 直接切到第二个
            await self.run_command(runner, "/resume 2")
            self.assertEqual(host.switched_session, s1.file_path)
            self.assertIn("已恢复历史会话 [2]", host.text)

    async def test_new_command_creates_new_session(self) -> None:
        runner, host, _runtime = self.make()
        await self.run_command(runner, "/new")
        self.assertTrue(getattr(host, "new_session_called", False))
        self.assertIn("已开启全新会话", host.text)

    async def test_resume_delete_session_via_callback(self) -> None:
        from logox.store.manager import SessionManager
        runner, host, runtime = self.make()
        with tempfile.TemporaryDirectory() as tmp:
            runtime.cwd = Path(tmp)
            sessions_dir = Path(tmp) / "sessions"
            runtime.paths = type("Paths", (), {"sessions": sessions_dir})()
            mgr = SessionManager(sessions_dir)
            s1 = mgr.create_session(runtime.cwd, initial_title="要删除的任务")

            def _delete_choice(component: Any) -> Any:
                self.assertTrue(getattr(component.state, "allow_delete", False))
                self.assertIsNotNone(component.on_delete)
                keep_open = component.on_delete(component.state.choices[0])
                self.assertTrue(keep_open)
                return None  # 取消关闭

            host.answers = [_delete_choice]
            await self.run_command(runner, "/resume")
            self.assertEqual(host.deleted_session, s1.file_path)
            self.assertIn("已将历史会话移入回收站", host.text)

    async def test_mcp_command_when_no_servers(self) -> None:
        runner, host, runtime = self.make()
        runtime.list_mcp_servers = lambda: []
        await self.run_command(runner, "/mcp")
        self.assertIn("未配置任何外部 MCP 服务", host.text)

    async def test_mcp_command_shows_panel(self) -> None:
        from logox.mcp.models import McpConnectionState, McpServerStatus

        runner, host, runtime = self.make()
        runtime.list_mcp_servers = lambda: [
            McpServerStatus(
                name="github",
                command="npx @mcp/server-github",
                state=McpConnectionState.CONNECTED,
                tool_count=5,
                tools=["create_issue", "list_repos"],
                description="GitHub 工具集",
            )
        ]
        await self.run_command(runner, "/mcp")
        self.assertEqual(len(host.overlays), 1)
        panel = host.overlays[0]
        self.assertIn("github", panel.text.plain)
        self.assertIn("CONNECTED", panel.text.plain)
        self.assertIn("create_issue", panel.text.plain)


class ReloadCommandTests(CommandTestCase):
    """`/reload`（MODULE_08 §5.2 的七条验收条件）。"""

    async def test_reports_each_item_and_repaints_theme(self) -> None:
        """① 逐项报告；主题文件重读 + 视口重绘（同名文件改了内容也生效）。"""
        runner, host, runtime = self.make()
        await self.run_command(runner, "/reload")

        self.assertEqual(runtime.reloaded, 1)
        self.assertIn("项目记忆", host.text)
        self.assertIn("AGENTS.md", host.text)
        # 主题走界面侧重读：`load_theme` 每次都读文件，这是"切走再切回"之外唯一路径
        self.assertEqual(host.applied_theme, "logox-dark")
        self.assertIn("主题", host.text)

    async def test_unchanged_prefix_is_not_reported_as_a_cost(self) -> None:
        """② 没变时**不能**提示"前缀重算"——否则每次都说要花钱，等于噪音。"""
        runner, host, _ = self.make()
        await self.run_command(runner, "/reload")
        self.assertIn("系统提示未变", host.text)
        self.assertNotIn("按全价重算", host.text)

    async def test_prefix_change_is_announced_with_numbers(self) -> None:
        """③ 变了就必须说出来，并且带上数字。"""
        runner, host, runtime = self.make()
        runtime.reload_report = ResourceReloadReport(
            items=[ReloadItem("项目记忆", detail="AGENTS.md · 4,100 tokens")],
            prefix_changed=True,
            system_tokens_before=4_980,
            system_tokens_after=5_400,
        )
        await self.run_command(runner, "/reload")
        self.assertIn("按全价重算", host.text)
        self.assertIn("4,980", host.text)
        self.assertIn("5,400", host.text)

    async def test_refuses_while_generating(self) -> None:
        """④ 正在生成时拒绝执行：`reload_resources` **一次都不能被调用**。"""
        runner, host, runtime = self.make()
        host.busy = True
        await self.run_command(runner, "/reload")

        self.assertEqual(runtime.reloaded, 0, "生成中不该动系统提示")
        self.assertIn("正在回答", host.text)
        self.assertIsNone(host.applied_theme, "被拒绝时不该顺手换主题")

    async def test_failure_of_one_item_is_visible_and_not_fatal(self) -> None:
        """⑤ 单项失败要说清原因，其他项照常、会话不崩。"""
        runner, host, runtime = self.make()
        runtime.reload_report = ResourceReloadReport(
            items=[
                ReloadItem("项目记忆", detail="AGENTS.md · 3,680 tokens"),
                ReloadItem("技能包", error="PermissionError: 拒绝访问"),
            ],
        )
        await self.run_command(runner, "/reload")

        self.assertIn("AGENTS.md", host.text, "另一项仍应报告成功")
        self.assertIn("PermissionError", host.text)
        self.assertEqual(host.applied_theme, "logox-dark", "主题环节不该被上游失败带走")

    async def test_restart_only_items_are_listed(self) -> None:
        """⑥ 诚实清单：不重载的东西必须说出来，而不是让用户以为全生效了。"""
        runner, host, _ = self.make()
        await self.run_command(runner, "/reload")
        self.assertIn("仍需重启", host.text)
        self.assertIn("Python 代码", host.text)

    async def test_runtime_without_reload_support_says_so(self) -> None:
        """运行时没这个能力时给明确提示，而不是静默成功。"""
        runner, host, runtime = self.make()
        runtime.reload_resources = None  # type: ignore[assignment]
        await self.run_command(runner, "/reload")
        self.assertIn("不支持资源重载", host.text)


class ReloadRegistrationTests(unittest.TestCase):
    """⑦ `/reload` 必须只出现在**可用**清单里（补全与 `/help` 都从这里现算）。"""

    def test_registered_as_available_not_planned(self) -> None:
        from logox.tui.commands import AVAILABLE_COMMANDS, PLANNED_COMMANDS, resolve

        self.assertIn("reload", AVAILABLE_COMMANDS)
        self.assertNotIn("reload", PLANNED_COMMANDS)
        resolved = resolve("/reload")
        assert resolved is not None
        self.assertTrue(resolved.is_ready)


async def _explode(*_args: Any, **_kwargs: Any) -> Any:
    raise RuntimeError("浮层炸了")


def _pick(component: Any, value: str) -> Any:
    """从选择器里挑一个（模拟用户选中 ``value``）。"""
    from logox.tui.content.overlay import Choice

    for choice in component.state.choices:
        if choice.value == value:
            return choice
    return Choice(value=value, label=value)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
