"""tests/unit/test_plugins.py - 单入口插件系统单元测试。

测试覆盖：
1. PluginContext 沙箱门面（工具注册、事件订阅、命令注册、日志隔离）；
2. PluginManager 插件发现机制（配置路径、项目级、用户级及优先级去重）；
3. 单入口 register(ctx) 契约执行；
4. 异常隔离机制（语法错误、缺少 register、运行时抛错均安全捕获至 failed_plugins）；
5. 成功加载插件的完整联动。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from logox.config.schema import PluginsConfig
from logox.kernel.bus import EventBus
from logox.kernel.events import SessionStart
from logox.plugins import PluginContext, PluginManager
from logox.tools.base import Tool, ToolContext, ToolResult


class DummyTool(Tool):
    """测试用虚拟工具。"""

    name = "dummy_plugin_tool"
    description = "Dummy tool from plugin"

    async def execute(self, arguments: dict, ctx: ToolContext) -> ToolResult:
        return ToolResult.success("dummy output")


class DummyRegistry:
    """测试用轻量工具注册表。"""

    def __init__(self) -> None:
        self.tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self.tools[tool.name] = tool


class PluginSystemTests(unittest.IsolatedAsyncioTestCase):
    """单入口插件系统核心逻辑测试。"""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.cwd = Path(self.tmp_dir.name).resolve()
        self.user_dir = self.cwd / "user_home"
        self.user_dir.mkdir(parents=True, exist_ok=True)
        self.bus = EventBus(session_id="sess-plugin")
        self.tool_reg = DummyRegistry()

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_plugin_context_facade(self) -> None:
        """测试 PluginContext 门面各项能力。"""
        custom_commands: dict = {}
        ctx = PluginContext(
            tool_registry=self.tool_reg,  # type: ignore[arg-type]
            bus=self.bus,
            commands_registry=custom_commands,
        )

        # 1. 注册工具
        tool = DummyTool()
        ctx.register_tool(tool)
        self.assertIn("dummy_plugin_tool", self.tool_reg.tools)

        # 2. 注册斜杠命令
        def _dummy_cmd(arg: str) -> str:
            return f"cmd:{arg}"

        ctx.register_slash_command("mycmd", _dummy_cmd, "My description")
        self.assertIn("mycmd", custom_commands)
        self.assertEqual(custom_commands["mycmd"][1], "My description")

        # 3. 获取独立 Logger
        logger = ctx.get_logger("test_p")
        self.assertEqual(logger.name, "logox.plugin.test_p")

    def test_discover_plugins_precedence(self) -> None:
        """测试插件文件发现优先级：项目级覆盖用户级。"""
        project_plugins = self.cwd / ".logox" / "plugins"
        project_plugins.mkdir(parents=True, exist_ok=True)
        user_plugins = self.user_dir / "plugins"
        user_plugins.mkdir(parents=True, exist_ok=True)

        # 用户级有 a.py 和 b.py
        (user_plugins / "a.py").write_text("# user a", encoding="utf-8")
        (user_plugins / "b.py").write_text("# user b", encoding="utf-8")

        # 项目级有 a.py（应覆盖用户级 a.py）和 c.py
        (project_plugins / "a.py").write_text("# project a", encoding="utf-8")
        (project_plugins / "c.py").write_text("# project c", encoding="utf-8")

        mgr = PluginManager(
            config=PluginsConfig(enabled=True),
            cwd=self.cwd,
            user_dir=self.user_dir,
        )
        discovered = mgr.discover_plugin_files()
        filenames = [p.name for p in discovered]

        # 应该包含 a.py, b.py, c.py，且 a.py 指向项目级
        self.assertEqual(len(discovered), 3)
        self.assertIn("a.py", filenames)
        self.assertIn("b.py", filenames)
        self.assertIn("c.py", filenames)

        a_path = next(p for p in discovered if p.name == "a.py")
        self.assertEqual(a_path.resolve(), (project_plugins / "a.py").resolve())

    def test_plugins_disabled_returns_empty(self) -> None:
        """当 plugins.enabled 为 False 时不发现任何插件。"""
        project_plugins = self.cwd / ".logox" / "plugins"
        project_plugins.mkdir(parents=True, exist_ok=True)
        (project_plugins / "hello.py").write_text("print(1)", encoding="utf-8")

        mgr = PluginManager(
            config=PluginsConfig(enabled=False),
            cwd=self.cwd,
            user_dir=self.user_dir,
        )
        self.assertEqual(mgr.discover_plugin_files(), [])

    def test_load_plugin_success(self) -> None:
        """正常加载符合 register(ctx) 契约的插件。"""
        plugin_file = self.cwd / ".logox" / "plugins" / "my_plugin.py"
        plugin_file.parent.mkdir(parents=True, exist_ok=True)
        code = (
            "def register(ctx):\n"
            "    ctx.register_slash_command('foo', lambda: 'bar', 'foo command')\n"
        )
        plugin_file.write_text(code, encoding="utf-8")

        mgr = PluginManager(
            config=PluginsConfig(enabled=True),
            cwd=self.cwd,
            user_dir=self.user_dir,
        )
        ctx = PluginContext(
            tool_registry=self.tool_reg,  # type: ignore[arg-type]
            bus=self.bus,
            commands_registry=mgr.custom_commands,
        )
        loaded = mgr.load_all(ctx)

        self.assertEqual(loaded, 1)
        self.assertIn("my_plugin", mgr.loaded_plugins)
        self.assertEqual(len(mgr.failed_plugins), 0)
        self.assertIn("foo", mgr.custom_commands)

    def test_load_plugin_missing_register_contract(self) -> None:
        """插件未导出 register 函数时记录失败，不影响系统。"""
        plugin_file = self.cwd / ".logox" / "plugins" / "no_register.py"
        plugin_file.parent.mkdir(parents=True, exist_ok=True)
        plugin_file.write_text("x = 100\n", encoding="utf-8")

        mgr = PluginManager(
            config=PluginsConfig(enabled=True),
            cwd=self.cwd,
            user_dir=self.user_dir,
        )
        ctx = PluginContext(
            tool_registry=self.tool_reg,  # type: ignore[arg-type]
            bus=self.bus,
            commands_registry=mgr.custom_commands,
        )
        loaded = mgr.load_all(ctx)

        self.assertEqual(loaded, 0)
        self.assertIn("no_register", mgr.failed_plugins)
        self.assertIn("未定义 register(ctx) 函数", mgr.failed_plugins["no_register"])

    def test_load_plugin_syntax_error_isolated(self) -> None:
        """插件脚本存在语法错误时被安全捕获隔离。"""
        plugin_file = self.cwd / ".logox" / "plugins" / "bad_syntax.py"
        plugin_file.parent.mkdir(parents=True, exist_ok=True)
        plugin_file.write_text("def broken_syntax(:\n", encoding="utf-8")

        mgr = PluginManager(
            config=PluginsConfig(enabled=True),
            cwd=self.cwd,
            user_dir=self.user_dir,
        )
        ctx = PluginContext(
            tool_registry=self.tool_reg,  # type: ignore[arg-type]
            bus=self.bus,
            commands_registry=mgr.custom_commands,
        )
        loaded = mgr.load_all(ctx)

        self.assertEqual(loaded, 0)
        self.assertIn("bad_syntax", mgr.failed_plugins)

    def test_load_plugin_runtime_exception_isolated(self) -> None:
        """插件在 register 执行期间抛出异常时被隔离，其他插件仍正常加载。"""
        plugins_dir = self.cwd / ".logox" / "plugins"
        plugins_dir.mkdir(parents=True, exist_ok=True)

        bad_file = plugins_dir / "exploding.py"
        bad_file.write_text(
            "def register(ctx):\n    raise RuntimeError('boom!')\n",
            encoding="utf-8",
        )

        good_file = plugins_dir / "healthy.py"
        good_file.write_text(
            "def register(ctx):\n    ctx.register_slash_command('healthy', lambda: 'ok')\n",
            encoding="utf-8",
        )

        mgr = PluginManager(
            config=PluginsConfig(enabled=True),
            cwd=self.cwd,
            user_dir=self.user_dir,
        )
        ctx = PluginContext(
            tool_registry=self.tool_reg,  # type: ignore[arg-type]
            bus=self.bus,
            commands_registry=mgr.custom_commands,
        )
        loaded = mgr.load_all(ctx)

        self.assertEqual(loaded, 1)
        self.assertIn("healthy", mgr.loaded_plugins)
        self.assertIn("exploding", mgr.failed_plugins)
        self.assertIn("boom!", mgr.failed_plugins["exploding"])
        self.assertIn("healthy", mgr.custom_commands)

    async def test_plugin_event_subscription(self) -> None:
        """插件通过 ctx.subscribe 成功监听事件总线事件。"""
        plugins_dir = self.cwd / ".logox" / "plugins"
        plugins_dir.mkdir(parents=True, exist_ok=True)

        event_flag = self.cwd / "event_received.txt"
        sub_file = plugins_dir / "sub_plugin.py"
        sub_code = (
            "from pathlib import Path\n"
            "from logox.kernel.events import SessionStart\n"
            "def register(ctx):\n"
            "    async def _on_start(ev):\n"
            f"        Path(r'{event_flag}').write_text(ev.model, encoding='utf-8')\n"
            "    ctx.subscribe(SessionStart, _on_start)\n"
        )

        sub_file.write_text(sub_code, encoding="utf-8")

        mgr = PluginManager(
            config=PluginsConfig(enabled=True),
            cwd=self.cwd,
            user_dir=self.user_dir,
        )
        ctx = PluginContext(
            tool_registry=self.tool_reg,  # type: ignore[arg-type]
            bus=self.bus,
            commands_registry=mgr.custom_commands,
        )
        mgr.load_all(ctx)

        # 发布事件
        await self.bus.publish(
            SessionStart(
                session_id="sess-plugin",
                cwd=str(self.cwd),
                model="deepseek-v3",
                provider="mock",
            )
        )

        self.assertTrue(event_flag.exists())
        self.assertEqual(event_flag.read_text(encoding="utf-8"), "deepseek-v3")


if __name__ == "__main__":
    unittest.main()
