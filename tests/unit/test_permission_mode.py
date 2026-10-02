"""权限运行模式（default vs creative）与项目级免打扰单元测试（D130 / MODULE_permission_mode.md）。

验证核心契约：
1. 默认防护 (default)：未授权写/改与常规命令必须触发 HITL 审批（ASK），复合命令降级拦截；
2. 创造免打扰 (creative)：常规文件操作与复合命令免打扰直接放行（ALLOW）；
3. 坚守安全红线：任何模式下一票否决绝对黑名单（DENY），跨沙箱越界与敏感文件始终直通 HITL 审批（ASK）；
4. 持久化与状态仓：遵循 D30 读写分离，仅写 state.toml；
5. Slash 命令 /mode：支持参数直切与无参数弹窗交互；
6. 状态栏呈现：creative 醒目警示色呈现。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from logox.config.schema import StateFile
from logox.config.state import StateStore
from logox.permissions.engine import PermissionEngine
from logox.permissions.models import Decision, PermissionMode, PermissionRule, RiskLevel, RuleScope
from logox.tui.commands import ResolvedCommand
from logox.tui.content.overlay import Choice
from logox.tui.content.status import StatusContext, _render_item
from logox.tui.metrics import MetricsReducer, SessionMetrics
from logox.tui.render.commands import CommandRunner
from logox.tui.theme import load_theme


class PermissionEngineModeTests(unittest.TestCase):
    """引擎裁决在 default 与 creative 模式下的行为对比测试。"""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmpdir.name).resolve()
        # 创建工作区内部常规文件与敏感文件
        (self.workspace / "src").mkdir(parents=True)
        (self.workspace / "src" / "main.py").write_text("print('hello')", encoding="utf-8")
        (self.workspace / ".env").write_text("SECRET=123", encoding="utf-8")

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_default_mode_requires_hitl_for_unlisted_operations(self) -> None:
        """default 模式：未白名单的写文件与常规命令必须触发 ASK。"""
        engine = PermissionEngine(self.workspace, mode=PermissionMode.DEFAULT)
        self.assertEqual(engine.mode, PermissionMode.DEFAULT)

        # 写入未授权文件
        eval_edit = engine.evaluate("fs_edit", {"path": "src/main.py", "content": "x"})
        self.assertEqual(eval_edit.decision, Decision.ASK)
        self.assertEqual(eval_edit.risk_level, RiskLevel.NORMAL)

        # 常规未白名单命令（非内置只读命令）
        eval_shell = engine.evaluate("shell", {"command": "python script.py"})
        self.assertEqual(eval_shell.decision, Decision.ASK)

        # 复合命令降级拦截为 ASK
        eval_compound = engine.evaluate("shell", {"command": "pytest && echo ok"})
        self.assertEqual(eval_compound.decision, Decision.ASK)
        self.assertIn("复合操作符", eval_compound.reason)

    def test_creative_mode_auto_allows_normal_operations_and_compound_commands(self) -> None:
        """creative 模式：常规写操作与复合命令自动放行 (ALLOW)。"""
        engine = PermissionEngine(self.workspace, mode=PermissionMode.CREATIVE)
        self.assertEqual(engine.mode, PermissionMode.CREATIVE)

        # 常规写文件：自动放行
        eval_edit = engine.evaluate("fs_edit", {"path": "src/main.py", "content": "x"})
        self.assertEqual(eval_edit.decision, Decision.ALLOW)
        self.assertIn("创造模式", eval_edit.reason)

        # 常规 shell：自动放行
        eval_shell = engine.evaluate("shell", {"command": "python script.py"})
        self.assertEqual(eval_shell.decision, Decision.ALLOW)
        self.assertIn("创造模式", eval_shell.reason)

        # 复合 shell：在 creative 模式下不降级，直接放行
        eval_compound = engine.evaluate("shell", {"command": "pytest && echo ok"})
        self.assertEqual(eval_compound.decision, Decision.ALLOW)

    def test_creative_mode_preserves_hardcoded_blacklist_deny(self) -> None:
        """安全红线：系统绝对自毁命令在 creative 模式下仍坚决 DENY。"""
        engine = PermissionEngine(self.workspace, mode=PermissionMode.CREATIVE)

        for dangerous_cmd in ["rm -rf /", "rm -rf /*", "Remove-Item -Recurse -Force C:\\"]:
            eval_res = engine.evaluate("shell", {"command": dangerous_cmd})
            self.assertEqual(
                eval_res.decision,
                Decision.DENY,
                f"高危自毁命令 {dangerous_cmd} 必须 DENY",
            )
            self.assertIn("系统高危自毁绝对黑名单", eval_res.reason)

    def test_creative_mode_preserves_path_sandbox_cross_boundary_ask(self) -> None:
        """安全红线：跨工作区越界在 creative 模式下坚决拦截提权进入 ASK。"""
        engine = PermissionEngine(self.workspace, mode=PermissionMode.CREATIVE)

        eval_res = engine.evaluate("fs_edit", {"path": "../outside.txt", "content": "hack"})
        self.assertEqual(eval_res.decision, Decision.ASK)
        self.assertEqual(eval_res.risk_level, RiskLevel.HIGH_CROSS_BOUNDARY)

    def test_creative_mode_preserves_sensitive_files_ask(self) -> None:
        """安全红线：修改 .env 等核心敏感文件在 creative 模式下坚决拦截提权进入 ASK。"""
        engine = PermissionEngine(self.workspace, mode=PermissionMode.CREATIVE)

        eval_res = engine.evaluate("fs_edit", {"path": ".env", "content": "HACKED"})
        self.assertEqual(eval_res.decision, Decision.ASK)
        self.assertEqual(eval_res.risk_level, RiskLevel.HIGH_SENSITIVE)

    def test_creative_mode_respects_explicit_deny_rules(self) -> None:
        """显式拒绝规则一票否决：在 creative 模式下依然一票否决。"""
        engine = PermissionEngine(self.workspace, mode=PermissionMode.CREATIVE)
        engine.learn_rule(
            PermissionRule(
                tool_name="shell",
                pattern="pip install *",
                decision=Decision.DENY,
                scope=RuleScope.SESSION,
            )
        )

        eval_res = engine.evaluate("shell", {"command": "pip install requests"})
        self.assertEqual(eval_res.decision, Decision.DENY)
        self.assertIn("显式拒绝规则", eval_res.reason)

    def test_mode_switch_and_fallback(self) -> None:
        """测试动态切换模式与未知字符串回退兜底。"""
        engine = PermissionEngine(self.workspace, mode="invalid_str")
        self.assertEqual(engine.mode, PermissionMode.DEFAULT)

        engine.set_mode("creative")
        self.assertEqual(engine.mode, PermissionMode.CREATIVE)

        engine.set_mode(PermissionMode.DEFAULT)
        self.assertEqual(engine.mode, PermissionMode.DEFAULT)


class StatePersistenceModeTests(unittest.TestCase):
    """state.toml 持久化契约测试（D30 / D130）。"""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.state_file = Path(self.tmpdir.name) / "state.toml"
        self.store = StateStore(self.state_file)

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_persist_and_read_permission_mode(self) -> None:
        """测试持久化写入与重新读取 mode。"""
        state = self.store.read()
        self.assertEqual(state.permissions.mode, "default")

        self.store.set_permission_mode("creative")
        reloaded = self.store.read()
        self.assertEqual(reloaded.permissions.mode, "creative")

        self.store.set_permission_mode("default")
        self.assertEqual(self.store.read().permissions.mode, "default")

    def test_invalid_permission_mode_raises(self) -> None:
        """设置未知权限模式必须抛出 ValueError。"""
        with self.assertRaises(ValueError):
            self.store.set_permission_mode("god_mode")


class FakeModeHost:
    def __init__(self, runtime: Any, answers: list[Any] | None = None) -> None:
        self.runtime = runtime
        self.theme = load_theme("logox-dark")
        self.effort = "auto"
        self.content_width = 80
        self.content_rows = 24
        self.answers = list(answers or [])
        self.notices: list[str] = []
        self.refreshed = 0

    async def push_overlay(self, component: Any, *, max_rows: int | None = None) -> Any:
        del max_rows
        if not self.answers:
            return None
        return self.answers.pop(0)

    def notice(self, message: str, *, token: str = "text_muted") -> None:
        del token
        self.notices.append(message)

    def refresh_status(self) -> None:
        self.refreshed += 1


class FakeModeRuntime:
    def __init__(self, mode: str = "default") -> None:
        self._mode = mode
        self.state_store = None
        self.reducer = MetricsReducer()
        self.reducer.metrics.permission_mode = mode

    @property
    def permission_mode(self) -> str:
        return self._mode

    def set_permission_mode(self, mode: str) -> None:
        self._mode = mode
        self.reducer.metrics.permission_mode = mode


class ModeCommandTests(unittest.IsolatedAsyncioTestCase):
    """Slash 命令 /mode 的行为测试。"""

    async def test_cmd_mode_with_argument_direct_switch(self) -> None:
        runtime = FakeModeRuntime("default")
        host = FakeModeHost(runtime)
        runner = CommandRunner(host)  # type: ignore[arg-type]

        # 1. 切换到 creative
        await runner.run(ResolvedCommand(raw="/mode creative", name="mode", argument="creative", state="ready"))
        self.assertEqual(runtime.permission_mode, "creative")
        self.assertEqual(runtime.reducer.metrics.permission_mode, "creative")
        self.assertGreater(host.refreshed, 0)
        self.assertTrue(any("已切换为 creative" in n for n in host.notices))

        # 2. 再次切回 default
        await runner.run(ResolvedCommand(raw="/mode default", name="mode", argument="default", state="ready"))
        self.assertEqual(runtime.permission_mode, "default")
        self.assertEqual(runtime.reducer.metrics.permission_mode, "default")
        self.assertTrue(any("已切换为 default" in n for n in host.notices))

    async def test_cmd_mode_invalid_argument(self) -> None:
        runtime = FakeModeRuntime("default")
        host = FakeModeHost(runtime)
        runner = CommandRunner(host)  # type: ignore[arg-type]

        await runner.run(ResolvedCommand(raw="/mode invalid", name="mode", argument="invalid", state="ready"))
        self.assertEqual(runtime.permission_mode, "default")
        self.assertTrue(any("未知权限模式" in n for n in host.notices))

    async def test_cmd_mode_picker_selection(self) -> None:
        runtime = FakeModeRuntime("default")
        host = FakeModeHost(runtime, answers=[Choice(value="creative", label="creative")])
        runner = CommandRunner(host)  # type: ignore[arg-type]

        await runner.run(ResolvedCommand(raw="/mode", name="mode", argument="", state="ready"))
        self.assertEqual(runtime.permission_mode, "creative")
        self.assertTrue(any("已切换为 creative" in n for n in host.notices))

    async def test_cmd_mode_picker_cancel(self) -> None:
        runtime = FakeModeRuntime("default")
        host = FakeModeHost(runtime, answers=[None])  # 用户按 Esc 取消
        runner = CommandRunner(host)  # type: ignore[arg-type]

        await runner.run(ResolvedCommand(raw="/mode", name="mode", argument="", state="ready"))
        self.assertEqual(runtime.permission_mode, "default")
        self.assertTrue(any("已取消" in n for n in host.notices))


class StatusBarPermissionRenderingTests(unittest.TestCase):
    """状态栏中权限模式呈现测试。"""

    def test_status_bar_render_permission_default_and_creative(self) -> None:
        palette = load_theme("logox-dark").palette
        context = StatusContext(
            palette=palette,
            items=None,  # type: ignore[arg-type]
            timing_fields=None,  # type: ignore[arg-type]
            width=80,
        )

        # default 模式
        m_default = SessionMetrics(permission_mode="default")
        res_default = _render_item("permission", m_default, context)
        self.assertIsNotNone(res_default)
        text, needs_attention = res_default
        self.assertEqual(text.plain, "default")
        self.assertFalse(needs_attention)

        # creative 模式（高亮警示色呈现）
        m_creative = SessionMetrics(permission_mode="creative")
        res_creative = _render_item("permission", m_creative, context)
        self.assertIsNotNone(res_creative)
        text, needs_attention = res_creative
        self.assertEqual(text.plain, "creative")
        self.assertTrue(needs_attention)
