"""装配根与启动期失败出口的用例（MODULE_tui_integration §7.3）。

**这些用例都不需要网络**：缺密钥、无模型、安全闸门、主题损坏四类失败都发生在
"构造 Provider / 加载主题"这一步，而 Provider 的构造只读环境变量、不发请求。

为什么值得专门测"失败时说了什么"：M3 §10 缺陷 4 的教训——本地端点（Ollama /
LM Studio）配置完全正确却报 auth 错误，报错指向性极差。**启动失败的信息质量，
直接决定用户能不能自己解决它。**
"""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from logox.app import StartupError, build_runtime, render_startup_error
from logox.config.schema import LogoxConfig, ProviderConfig, ProviderInstanceConfig, UiConfig
from logox.paths import LogoxPaths
from tests.unit.support import make_temp_dir, remove_temp_dir


def _paths(root: Path) -> LogoxPaths:
    return LogoxPaths.at(root)


def _bundle(config: LogoxConfig, sources: list[object] | None = None) -> object:
    """最小 bundle：`build_runtime` 只用到 `.config`。"""
    return SimpleNamespace(config=config, sources=sources or [])


class ProviderFailureTests(unittest.TestCase):
    """T-30 / T-31：Provider 与模型两类失败。"""

    def setUp(self) -> None:
        self.root = make_temp_dir("m4-app")
        # `addCleanup` 而不是 `tearDown`：即使用例中途断言失败/抛异常也一定会执行，
        # 而且**在 tearDown 之后**才跑（unittest 的保证）。仓库里其它模块用的是同一写法。
        self.addCleanup(remove_temp_dir, self.root)
        self.paths = _paths(self.root)

    def test_t30_missing_api_key_still_starts_and_says_what_to_do(self) -> None:
        """★ 缺密钥时**必须能启动**——否则 `/login`（用来配密钥的命令）永远执行不到。

        这是"先有鸡还是先有蛋"：M4 最初让缺密钥直接返回 ``StartupError``，
        于是一个还没配置任何密钥的新用户**根本进不去界面**，也就无法用 `/login` 自救。

        现在的语义：缺密钥 → 启动 + 一个``占位适配器``（一被调用就给出可行动的提示）
        + 一条明确的启动警告。真正会失败的（未知 provider、没有模型）才拦在启动期。
        """
        from logox.app import Runtime

        config = LogoxConfig(
            provider=ProviderConfig(name="openai-compatible", model="gpt-4o"),
        )
        with mock.patch.dict("os.environ", {}, clear=True):
            outcome = build_runtime(_bundle(config), self.root, self.paths)

        self.assertIsInstance(outcome, Runtime, "缺密钥不得阻止启动")
        assert isinstance(outcome, Runtime)
        provider = outcome.kernel._provider  # noqa: SLF001
        self.assertTrue(getattr(provider, "is_placeholder", False), "应当是占位适配器")
        self.assertEqual(getattr(provider, "missing_env", ""), "OPENAI_API_KEY", "必须点名缺的是哪个变量")

    def test_t30b_placeholder_error_message_is_actionable(self) -> None:
        """占位适配器被调用时给出的错误，必须包含**变量名**与**下一步**。"""
        import asyncio

        from logox.app import Runtime
        from logox.errors import ErrorCategory
        from logox.providers.base import ChatRequest

        config = LogoxConfig(provider=ProviderConfig(name="openai-compatible", model="gpt-4o"))
        with mock.patch.dict("os.environ", {}, clear=True):
            outcome = build_runtime(_bundle(config), self.root, self.paths)
        assert isinstance(outcome, Runtime)
        provider = outcome.kernel._provider  # noqa: SLF001

        async def collect() -> list[object]:
            return [event async for event in provider.stream(ChatRequest(model="gpt-4o"))]

        events = asyncio.run(collect())
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertIs(event.category, ErrorCategory.AUTH)  # type: ignore[attr-defined]
        self.assertIn("OPENAI_API_KEY", event.message)  # type: ignore[attr-defined]
        self.assertIn("/login", event.message, "必须告诉用户下一步是运行 /login")  # type: ignore[attr-defined]

    def test_t31_no_model_lists_what_is_available(self) -> None:
        """没有可用模型：给出配置文件路径与该 provider 的已知模型。

        用 `lm-studio` 构造：它的预设 `models` 为空（本地端点的模型列表由端点自己回答），
        因此"没有可用模型"这条路才走得通。

        **这一条仍然拦在启动期**：没有模型时连"选哪个模型"都无从谈起，
        进界面只会让用户对着一句"模型不说话"发呆。它与"缺密钥"不同——
        缺密钥有 `/login` 这条自助路径，缺模型没有。
        """
        config = LogoxConfig(provider=ProviderConfig(name="lm-studio", model=""))
        outcome = build_runtime(_bundle(config), self.root, self.paths)

        self.assertIsInstance(outcome, StartupError)
        assert isinstance(outcome, StartupError)
        self.assertEqual(outcome.kind, "model")
        self.assertIn("lm-studio", outcome.message)
        self.assertIn("model", outcome.hint)
        self.assertIn("config.toml", outcome.hint)

    def test_unknown_provider_is_reported_as_provider_kind(self) -> None:
        config = LogoxConfig(provider=ProviderConfig(name="nosuchprovider", model="m"))
        outcome = build_runtime(_bundle(config), self.root, self.paths)
        self.assertIsInstance(outcome, StartupError)
        assert isinstance(outcome, StartupError)
        self.assertEqual(outcome.kind, "provider")

    def test_stale_model_name_is_noticed_but_never_silently_replaced(self) -> None:
        """★ 模型名不在已知清单里时：**提醒，但不擅自改**（D67 后续）。

        这条针对一个实测过的坑：用户 `state.toml` 里存着**早就退役的**模型名
        （`deepseek-chat`），每次请求都失败，而设置看着一切正常。

        **为什么不自动回退**：已知清单不是权威清单——实测 DeepSeek 的 `/models`
        端点并不返回全部可调用模型（用户真正在用的多模态模型就不在里面但能调通）。
        自动回退会把用户有意指定的自建/中转模型名悄悄换掉，正是本项目一直在防的
        "静默改配置"。所以这里断言两件事：**有提醒**、**模型没被改**。
        """
        from logox.app import Runtime

        config = LogoxConfig(provider=ProviderConfig(name="deepseek", model="deepseek-chat"))
        with mock.patch.dict("os.environ", {"DEEPSEEK_API_KEY": "sk-test"}, clear=False):
            outcome = build_runtime(_bundle(config), self.root, self.paths)

        self.assertIsInstance(outcome, Runtime)
        assert isinstance(outcome, Runtime)
        self.assertEqual(outcome.model, "deepseek-chat", "不得擅自替换用户指定的模型名")
        self.assertTrue(
            any("deepseek-chat" in warning for warning in outcome.warnings),
            f"必须给出可见提醒，实际警告：{outcome.warnings}",
        )

    def test_deepseek_default_model_is_the_multimodal_one(self) -> None:
        """没配模型时，DeepSeek 的默认必须是**能看图**那个（实测结论见 registry 注释）。"""
        from logox.app import Runtime
        from logox.providers.registry import BUILTIN_SPECS

        self.assertEqual(
            BUILTIN_SPECS["deepseek"].default_model,
            "deepseek-v4.1-flash-expires-on-0910",
        )
        config = LogoxConfig(provider=ProviderConfig(name="deepseek", model=""))
        with mock.patch.dict("os.environ", {"DEEPSEEK_API_KEY": "sk-test"}, clear=False):
            outcome = build_runtime(_bundle(config), self.root, self.paths)
        self.assertIsInstance(outcome, Runtime)
        assert isinstance(outcome, Runtime)
        self.assertEqual(outcome.model, "deepseek-v4.1-flash-expires-on-0910")
        self.assertFalse(
            any("不在已知清单" in warning for warning in outcome.warnings),
            "默认模型自己不该触发'名字可疑'的提醒",
        )

    def test_config_file_can_override_the_provider_entirely(self) -> None:
        """用户自定义 provider 走 `[providers.<name>]`——装配根必须把覆盖传下去。"""
        from logox.app import Runtime

        config = LogoxConfig(
            provider=ProviderConfig(name="my-local", model="qwen3:8b"),
            providers={
                "my-local": ProviderInstanceConfig(kind="openai_compat", base_url="http://127.0.0.1:1234/v1")
            },
        )
        outcome = build_runtime(_bundle(config), self.root, self.paths)
        self.assertIsInstance(outcome, Runtime)
        assert isinstance(outcome, Runtime)
        self.assertEqual(outcome.provider_name, "my-local")
        self.assertEqual(outcome.model, "qwen3:8b")


class ToolGateTests(unittest.TestCase):
    """T-32：M3 的安全闸门在装配根这一层也必须生效（E-18）。"""

    def setUp(self) -> None:
        self.root = make_temp_dir("m4-gate")
        # `addCleanup` 而不是 `tearDown`：即使用例中途断言失败/抛异常也一定会执行，
        # 而且**在 tearDown 之后**才跑（unittest 的保证）。仓库里其它模块用的是同一写法。
        self.addCleanup(remove_temp_dir, self.root)
        self.paths = _paths(self.root)

    def test_t32_writer_without_permissions_fails_at_assembly(self) -> None:
        """注册表里出现写工具且决策器放行一切 → **装配期失败**，不是运行期。"""
        from logox.errors import LogoxError
        from logox.kernel.registry import ToolRegistry
        from logox.tools.base import ToolResult, ToolSpec
        from tests.unit.kernel_support import StubArgs

        class Writer:
            spec = ToolSpec(name="write", params=StubArgs, readonly=False)

            async def run(self, args: object, ctx: object) -> ToolResult:  # pragma: no cover
                return ToolResult(ok=True)

        registry = ToolRegistry()
        registry.register(Writer())
        with self.assertRaises(LogoxError) as ctx:
            registry.assert_no_writers()
        message = str(ctx.exception)
        self.assertIn("write", message)
        self.assertIn("LOGOX_ALLOW_UNSAFE_TOOLS", message, "必须告诉用户逃生舱在哪")

    def test_allow_unsafe_flag_lets_assembly_through(self) -> None:
        """`LOGOX_ALLOW_UNSAFE_TOOLS=1` 时放行（仅调试用），并打醒目的警告。"""
        from logox.app import Runtime

        config = LogoxConfig(provider=ProviderConfig(name="ollama", model="qwen3:8b"))
        outcome = build_runtime(_bundle(config), self.root, self.paths, allow_unsafe_tools=True)
        self.assertIsInstance(outcome, Runtime)


class ThemeFallbackTests(unittest.TestCase):
    """T-33：主题损坏**不阻断启动**（UI-SPEC §8.2 / P-5）。"""

    def setUp(self) -> None:
        self.root = make_temp_dir("m4-theme")
        # `addCleanup` 而不是 `tearDown`：即使用例中途断言失败/抛异常也一定会执行，
        # 而且**在 tearDown 之后**才跑（unittest 的保证）。仓库里其它模块用的是同一写法。
        self.addCleanup(remove_temp_dir, self.root)
        self.paths = _paths(self.root)

    def test_t33_bad_theme_falls_back_and_warns(self) -> None:
        from logox.app import Runtime

        config = LogoxConfig(
            provider=ProviderConfig(name="ollama", model="qwen3:8b"),
            ui=UiConfig(theme="definitely-not-a-theme"),
        )
        outcome = build_runtime(_bundle(config), self.root, self.paths)

        self.assertIsInstance(outcome, Runtime, "主题坏了也必须能启动")
        assert isinstance(outcome, Runtime)
        self.assertEqual(outcome.theme.name, "logox-dark")
        self.assertTrue(outcome.warnings, "必须留下一条警告（界面上要显示）")
        self.assertIn("definitely-not-a-theme", outcome.warnings[0])


class RuntimeAssemblyTests(unittest.TestCase):
    """T-34：正常装配的形状。"""

    def setUp(self) -> None:
        self.root = make_temp_dir("m4-asm")
        # `addCleanup` 而不是 `tearDown`：即使用例中途断言失败/抛异常也一定会执行，
        # 而且**在 tearDown 之后**才跑（unittest 的保证）。仓库里其它模块用的是同一写法。
        self.addCleanup(remove_temp_dir, self.root)
        self.paths = _paths(self.root)

    def _build(self, **kwargs: object) -> object:
        config = LogoxConfig(
            provider=ProviderConfig(name="ollama", model="qwen3:8b", thinking_effort="low"),
            **kwargs,  # type: ignore[arg-type]
        )
        return build_runtime(_bundle(config), self.root, self.paths)

    def test_t34_runtime_shape_matches_config(self) -> None:
        from logox.app import Runtime
        from logox.kernel.loop import KernelLoop

        outcome = self._build()
        assert isinstance(outcome, Runtime)
        self.assertIsInstance(outcome.kernel, KernelLoop)
        self.assertEqual(outcome.model, "qwen3:8b")
        self.assertEqual(outcome.provider_name, "ollama")
        self.assertEqual(
            outcome.tools,
            ["edit", "glob", "grep", "read", "shell", "write"],
            "M5 注册全量工具：read, write, edit, glob, grep, shell",
        )
        self.assertFalse(outcome.warnings)
        self.assertEqual(outcome.theme.name, "logox-dark")
        self.assertIsNotNone(outcome.state_store, "状态写回的落点必须装好（D30）")

    def test_runtime_bus_session_id_is_usable(self) -> None:
        from logox.app import Runtime

        outcome = self._build()
        assert isinstance(outcome, Runtime)
        self.assertTrue(outcome.bus.session_id)
        self.assertFalse(outcome.bus.closed)

    def test_state_store_points_at_state_toml_only(self) -> None:
        """D30 红线：装配根给界面的写入口**只能是** `state.toml`。"""
        from logox.app import Runtime

        outcome = self._build()
        assert isinstance(outcome, Runtime)
        assert outcome.state_store is not None
        self.assertEqual(outcome.state_store.path.name, "state.toml")
        self.assertNotIn("config.toml", str(outcome.state_store.path))

    def test_thinking_effort_reaches_the_kernel(self) -> None:
        """受配置控制的档位必须在装配时就传给内核（D42）。"""
        from logox.app import Runtime

        outcome = self._build()
        assert isinstance(outcome, Runtime)
        self.assertEqual(outcome.kernel._thinking.effort, "low")  # noqa: SLF001 - 装配结果断言


class DevModeTests(unittest.TestCase):
    """**体验模式**（`LOGOX_SCRIPTED_PROVIDER=1`，MODULE §14.9）。

    它让"没有任何模型端点的人"也能看到完整链路，因此必须保证三件事：
    ① 开关只替换 **provider**，其余（真内核类、真工具、真总线）一模一样；
    ② **关掉开关时行为完全不变**——不能因为加了体验模式就悄悄改了生产路径；
    ③ stderr 上有醒目横幅，避免有人以为自己在看真实模型的输出。
    """

    def setUp(self) -> None:
        self.root = make_temp_dir("m4-dev")
        # `addCleanup` 而不是 `tearDown`：即使用例中途断言失败/抛异常也一定会执行
        self.addCleanup(remove_temp_dir, self.root)
        self.paths = _paths(self.root)

    def _build(self, flag: str | None) -> object:
        env = {} if flag is None else {"LOGOX_SCRIPTED_PROVIDER": flag}
        with mock.patch.dict("os.environ", env, clear=False) as patched:
            if flag is None:
                patched.pop("LOGOX_SCRIPTED_PROVIDER", None)
            config = LogoxConfig(provider=ProviderConfig(name="ollama", model="qwen3:8b"))
            return build_runtime(_bundle(config), self.root, self.paths)

    def test_dev_mode_swaps_only_the_provider(self) -> None:
        from logox.app import Runtime
        from logox.devsetup import MODEL_NAME, LogoxDevProvider
        from logox.kernel.loop import KernelLoop

        outcome = self._build("1")
        self.assertIsInstance(outcome, Runtime)
        assert isinstance(outcome, Runtime)
        self.assertIsInstance(outcome.kernel._provider, LogoxDevProvider)  # noqa: SLF001
        # 其余全部照旧：真内核类、真工具注册表、真总线
        self.assertIsInstance(outcome.kernel, KernelLoop)
        self.assertEqual(outcome.tools, ["edit", "glob", "grep", "read", "shell", "write"])
        self.assertEqual(outcome.model, MODEL_NAME)
        self.assertEqual(outcome.provider_name, "logox-dev", "状态栏要显示真实在跑的那个 provider")

    def test_dev_mode_prints_a_banner_to_stderr(self) -> None:
        """必须让人一眼看出"这不是真实模型输出"。"""
        import contextlib
        import io

        captured = io.StringIO()
        with contextlib.redirect_stderr(captured):
            self._build("1")
        self.assertIn("体验模式", captured.getvalue())
        self.assertIn("预置脚本", captured.getvalue())

    def test_dev_mode_provider_replays_a_real_tool_call(self) -> None:
        """★ 体验模式的脚本必须真的驱动出一次工具调用，且工具**真的访问了文件系统**。

        否则"体验模式证明了链路是通的"就只是一句空话。
        """
        import asyncio

        from logox.app import Runtime
        from logox.devsetup import LogoxDevProvider
        from logox.kernel.turn import TurnStatus

        outcome = self._build("1")
        assert isinstance(outcome, Runtime)
        provider = outcome.kernel._provider  # noqa: SLF001
        assert isinstance(provider, LogoxDevProvider)
        provider._delay_s = 0.0  # noqa: SLF001 - 测试不需要演示节奏

        turn = asyncio.run(outcome.kernel.submit("你好"))
        self.assertIs(turn.status, TurnStatus.DONE)
        self.assertEqual(turn.tool_call_count, 1, "脚本第一轮应当请求一次工具")

        names = [
            block.name
            for message in outcome.kernel.history
            for block in message.blocks
            if type(block).__name__ == "ToolUseBlock"
        ]
        self.assertEqual(names, ["read"])
        results = [
            block.content
            for message in outcome.kernel.history
            for block in message.blocks
            if type(block).__name__ == "ToolResultBlock"
        ]
        self.assertTrue(results, "工具结果必须回灌进历史")
        # 本用例的临时目录里**没有** hello.py，所以真工具应返回"文件不存在"——
        # 这恰好证明它真的去访问了文件系统（而不是被 mock 掉的）。
        self.assertIn("hello.py", results[0])
        self.assertTrue(results[0].strip(), "错误信息不能是空的")

    def test_production_path_unchanged_when_flag_absent(self) -> None:
        """★ 关掉开关时**必须完全不变**：仍然按配置构造 provider。"""
        from logox.app import Runtime
        from logox.devsetup import LogoxDevProvider

        outcome = self._build(None)
        self.assertIsInstance(outcome, Runtime)
        assert isinstance(outcome, Runtime)
        self.assertNotIsInstance(outcome.kernel._provider, LogoxDevProvider)  # noqa: SLF001
        self.assertEqual(outcome.provider_name, "ollama")
        self.assertEqual(outcome.model, "qwen3:8b")

    def test_only_the_exact_value_one_enables_it(self) -> None:
        """开关只认 `"1"`：`LOGOX_SCRIPTED_PROVIDER=0` 不得意外开启体验模式。"""
        from logox.app import Runtime
        from logox.devsetup import LogoxDevProvider

        outcome = self._build("0")
        self.assertIsInstance(outcome, Runtime)
        assert isinstance(outcome, Runtime)
        self.assertNotIsInstance(outcome.kernel._provider, LogoxDevProvider)  # noqa: SLF001


class StartupErrorMessageTests(unittest.TestCase):
    """渲染出来的失败信息必须是**给人看**的：无堆栈、有下一步。"""

    def test_error_rendering_has_no_traceback_and_has_next_steps(self) -> None:
        text = render_startup_error(
            StartupError(kind="auth", message="缺少密钥", hint="设置 OPENAI_API_KEY", exit_code=1)
        )
        self.assertIn("缺少密钥", text)
        self.assertIn("设置 OPENAI_API_KEY", text)
        self.assertIn("--chat", text, "应告诉用户还有文本模式可用")
        for marker in ("Traceback", 'File "', "  at "):
            self.assertNotIn(marker, text, "启动失败不得输出堆栈")

    def test_error_without_hint_is_still_readable(self) -> None:
        text = render_startup_error(StartupError(kind="provider", message="出问题了"))
        self.assertIn("出问题了", text)
        self.assertNotIn("怎么办", text)
