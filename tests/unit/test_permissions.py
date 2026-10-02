"""M6 权限裁决与规则引擎单元测试（tests/unit/test_permissions.py）。

覆盖五层纵深防御体系的各项核心能力：
1. 第 1 层：命令清洗、Token 前缀提取与复合操作符降级；
2. 第 2 层：高危自毁绝对黑名单物理拦截；
3. 第 3 层：物理路径沙箱、跨界穿透与敏感文件提权；
4. 第 4 层：三层规则引擎优先级（Deny > Session > Project > Built-in）；
5. 第 5 层：人在回路、state.toml 持久化与无人值守异步挂起等待通道。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import tempfile
import unittest
from typing import Any, Dict

from logox.permissions.decider import HierarchicalPermissionDecider
from logox.permissions.engine import PermissionEngine
from logox.permissions.models import (
    Decision,
    PermissionRule,
    RiskLevel,
    RuleScope,
)
from logox.permissions.normalizer import (
    is_compound_command,
    normalize_shell_command,
)
from logox.permissions.sandbox import PathSandbox
from logox.permission_types import PermissionAsk, PermissionChoice


class FakeSpec:
    def __init__(self, name: str = "shell", readonly: bool = False) -> None:
        self.name = name
        self.readonly = readonly
        self.requires_permission = True


class FakeTool:
    def __init__(self, name: str = "shell", readonly: bool = False) -> None:
        self.spec = FakeSpec(name=name, readonly=readonly)


class FakeCall:
    def __init__(self, name: str = "shell", args: Dict[str, Any] | None = None) -> None:
        self.name = name
        self.args = args or {}
        self.call_id = "call_test_1"


class FakePrompter:
    def __init__(self, *choices: PermissionChoice) -> None:
        self.choices = list(choices)
        self.asks: list[PermissionAsk] = []

    async def ask_permission(self, ask: PermissionAsk) -> PermissionChoice:
        self.asks.append(ask)
        if not self.choices:
            return PermissionChoice.DENY
        return self.choices.pop(0)


class FakeStateStore:
    def __init__(self) -> None:
        self.learned: list[tuple[str, str]] = []

    def learn_permission(self, kind: str, rule: str) -> bool:
        self.learned.append((kind, rule))
        return True


# =========================================================================== #
# 1. 命令清洗与复合检测测试 (Layer 1: Normalizer)
# =========================================================================== #


class TestCommandNormalizer(unittest.TestCase):
    def test_normalize_empty_and_spaces(self) -> None:
        prefix, tokens, is_compound = normalize_shell_command("   ")
        self.assertEqual(prefix, "")
        self.assertEqual(tokens, [])
        self.assertFalse(is_compound)

    def test_normalize_standard_commands(self) -> None:
        prefix, tokens, is_compound = normalize_shell_command("git status --short")
        self.assertEqual(prefix, "git status")
        self.assertFalse(is_compound)

        prefix, tokens, is_compound = normalize_shell_command("pytest -v tests/")
        self.assertEqual(prefix, "pytest")
        self.assertFalse(is_compound)

    def test_wrapper_penetration(self) -> None:
        prefix, tokens, is_compound = normalize_shell_command("python -m pytest -v")
        self.assertEqual(prefix, "python -m pytest")
        self.assertFalse(is_compound)

        prefix, tokens, is_compound = normalize_shell_command("python main.py --foo")
        self.assertEqual(prefix, "python main.py")
        self.assertFalse(is_compound)

    def test_compound_detection(self) -> None:
        # 管道符
        prefix, tokens, is_comp = normalize_shell_command("git log | grep fix")
        self.assertTrue(is_comp)

        # 逻辑与
        prefix, tokens, is_comp = normalize_shell_command("git status && dir")
        self.assertTrue(is_comp)

        # 分号
        prefix, tokens, is_comp = normalize_shell_command("echo 1; echo 2")
        self.assertTrue(is_comp)

        # 重定向
        prefix, tokens, is_comp = normalize_shell_command("pytest > out.txt")
        self.assertTrue(is_comp)

    def test_quotes_preserve_inner_operators(self) -> None:
        # 引号内部包含管道符，不被误报为复合命令
        prefix, tokens, is_comp = normalize_shell_command('git commit -m "feat | test"')
        self.assertFalse(is_comp)


# =========================================================================== #
# 2. 系统高危自毁绝对黑名单测试 (Layer 2: Hardcoded Blacklist)
# =========================================================================== #


class TestHardcodedBlacklist(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = PermissionEngine(workspace_root=".")

    def test_format_disk_hard_deny(self) -> None:
        eval_res = self.engine.evaluate("shell", {"command": "format c: /fs:NTFS"})
        self.assertEqual(eval_res.decision, Decision.DENY)
        self.assertIn("黑名单", eval_res.reason)

        eval_res_d = self.engine.evaluate("shell", {"command": "format D:"})
        self.assertEqual(eval_res_d.decision, Decision.DENY)

    def test_rm_rf_root_hard_deny(self) -> None:
        eval_res = self.engine.evaluate("shell", {"command": "rm -rf /"})
        self.assertEqual(eval_res.decision, Decision.DENY)
        self.assertIn("黑名单", eval_res.reason)

    def test_fork_bomb_hard_deny(self) -> None:
        eval_res = self.engine.evaluate("shell", {"command": ":(){ :|:& };:"})
        self.assertEqual(eval_res.decision, Decision.DENY)

    def test_powershell_remove_system_hard_deny(self) -> None:
        eval_res = self.engine.evaluate("shell", {"command": "Remove-Item -Recurse -Force C:\\"})
        self.assertEqual(eval_res.decision, Decision.DENY)


# =========================================================================== #
# 3. 物理路径沙箱与敏感审计测试 (Layer 3: Path Sandbox)
# =========================================================================== #


class TestPathSandbox(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name).resolve()
        self.sandbox = PathSandbox(self.workspace)
        self.engine = PermissionEngine(self.workspace)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_inside_workspace_path_is_normal(self) -> None:
        sub = self.workspace / "src" / "main.py"
        risk, _msg = self.sandbox.audit_path(sub)
        self.assertEqual(risk, RiskLevel.NORMAL)

    def test_relative_path_traversal_detection(self) -> None:
        traversal = self.workspace / ".." / "outside.txt"
        risk, msg = self.sandbox.audit_path(traversal)
        self.assertEqual(risk, RiskLevel.HIGH_CROSS_BOUNDARY)
        self.assertIn("越出项目工作区", msg)

    def test_sensitive_git_detection(self) -> None:
        git_cfg = self.workspace / ".git" / "config"
        risk, msg = self.sandbox.audit_path(git_cfg)
        self.assertEqual(risk, RiskLevel.HIGH_SENSITIVE)
        self.assertIn("版本控制系统元数据", msg)

    def test_sensitive_env_detection(self) -> None:
        env_file = self.workspace / ".env.local"
        risk, msg = self.sandbox.audit_path(env_file)
        self.assertEqual(risk, RiskLevel.HIGH_SENSITIVE)
        self.assertIn("密钥凭据", msg)

    def test_boundary_elevation_to_hitl_in_engine(self) -> None:
        # 用户裁定：越界不直接报错终止，而是打上高危标签直通 HITL 审批！
        eval_res = self.engine.evaluate("fs_write", {"path": "../../system.ini", "content": "bad"})
        self.assertEqual(eval_res.decision, Decision.ASK)
        self.assertEqual(eval_res.risk_level, RiskLevel.HIGH_CROSS_BOUNDARY)
        self.assertIn("越出项目工作区", eval_res.reason)

    def test_shell_cwd_cross_boundary_detection(self) -> None:
        """测试 shell 工具指定越界 cwd 时被路径沙箱拦截提权至 ASK。"""
        eval_res = self.engine.evaluate("shell", {"command": "ls", "cwd": "../../outside"})
        self.assertEqual(eval_res.decision, Decision.ASK)
        self.assertEqual(eval_res.risk_level, RiskLevel.HIGH_CROSS_BOUNDARY)
        self.assertIn("越出项目工作区", eval_res.reason)

    def test_shell_command_sensitive_env_detection(self) -> None:
        """测试 shell 命令行中包含 .env 敏感标记时被沙箱探测为高危。"""
        eval_res = self.engine.evaluate("shell", {"command": "cat .env"})
        self.assertEqual(eval_res.decision, Decision.ASK)
        self.assertEqual(eval_res.risk_level, RiskLevel.HIGH_SENSITIVE)
        self.assertIn("敏感资源标记", eval_res.reason)

    def test_shell_command_sensitive_git_detection(self) -> None:
        """测试 shell 命令行中触碰 .git 敏感标记时被沙箱探测为高危。"""
        eval_res = self.engine.evaluate("shell", {"command": "cat .git/config"})
        self.assertEqual(eval_res.decision, Decision.ASK)
        self.assertEqual(eval_res.risk_level, RiskLevel.HIGH_SENSITIVE)
        self.assertIn("敏感资源标记", eval_res.reason)


# =========================================================================== #
# 4. 多作用域规则引擎与优先级测试 (Layer 4: Rule Engine)
# =========================================================================== #


class TestMultiScopeRuleEngine(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name).resolve()
        self.engine = PermissionEngine(self.workspace)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_builtin_safe_read_only_tools(self) -> None:
        self.assertEqual(self.engine.evaluate("fs_glob", {"pattern": "*.py"}).decision, Decision.ALLOW)
        self.assertEqual(self.engine.evaluate("fs_grep", {"pattern": "def "}).decision, Decision.ALLOW)

    def test_builtin_safe_shell_commands(self) -> None:
        self.assertEqual(self.engine.evaluate("shell", {"command": "git status"}).decision, Decision.ALLOW)
        self.assertEqual(self.engine.evaluate("shell", {"command": "git diff --stat"}).decision, Decision.ALLOW)
        self.assertEqual(self.engine.evaluate("shell", {"command": "pytest tests/ -v"}).decision, Decision.ALLOW)

    def test_compound_safe_command_degrades_to_ask(self) -> None:
        # 单独 git status 是安全的，但与复合操作符结合必须降级到 ASK 人工审批
        eval_res = self.engine.evaluate("shell", {"command": "git status && rm -rf dist"})
        self.assertEqual(eval_res.decision, Decision.ASK)
        self.assertIn("复合操作符", eval_res.reason)

    def test_session_rule_allow(self) -> None:
        # 初始未知命令需 ASK
        self.assertEqual(self.engine.evaluate("shell", {"command": "custom_build --all"}).decision, Decision.ASK)

        # 注入会话规则后放行
        rule = PermissionRule("shell", pattern="custom_build", decision=Decision.ALLOW, scope=RuleScope.SESSION)
        self.engine.learn_rule(rule)
        self.assertEqual(self.engine.evaluate("shell", {"command": "custom_build --all"}).decision, Decision.ALLOW)

    def test_deny_rule_overrides_session_and_builtin(self) -> None:
        # 显式加入针对 pytest 的 Deny 规则
        deny_rule = PermissionRule("shell", pattern="pytest", decision=Decision.DENY, scope=RuleScope.SESSION)
        self.engine.learn_rule(deny_rule)

        # 尽管 pytest 是内置白名单，但 Deny 拥有更高一票否决权
        eval_res = self.engine.evaluate("shell", {"command": "pytest tests/"})
        self.assertEqual(eval_res.decision, Decision.DENY)
        self.assertIn("显式拒绝", eval_res.reason)

    def test_fs_write_workspace_rule_matching(self) -> None:
        target = str(self.workspace / "src" / "test.py")
        # 默认工作区写入需 ASK (方案 A 默认)
        self.assertEqual(self.engine.evaluate("fs_write", {"path": target}).decision, Decision.ASK)

        # 加入针对 src/* 的放行规则
        rule = PermissionRule("fs_write", pattern="src/*", decision=Decision.ALLOW, scope=RuleScope.PROJECT)
        self.engine.learn_rule(rule)
        self.assertEqual(self.engine.evaluate("fs_write", {"path": target}).decision, Decision.ALLOW)


# =========================================================================== #
# 5. 人在回路 (HITL) 与无人值守异步挂起通道 (Layer 5: Decider)
# =========================================================================== #


class TestHierarchicalDecider(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name).resolve()
        self.state_store = FakeStateStore()
        self.decider = HierarchicalPermissionDecider(
            cwd=self.workspace,
            state_store=self.state_store,
            wait_headless=True,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_interactive_prompter_flow(self) -> None:
        prompter = FakePrompter(PermissionChoice.PROJECT)
        self.decider.prompter = prompter

        call = FakeCall("fs_write", {"path": str(self.workspace / "docs" / "api.md")})
        tool = FakeTool("fs_write", readonly=False)

        decision = await self.decider.decide(call, tool, None)
        self.assertEqual(decision, Decision.ALLOW)
        self.assertEqual(len(prompter.asks), 1)

        # 验证项目持久化被记录
        self.assertEqual(len(self.state_store.learned), 1)
        self.assertEqual(self.state_store.learned[0][0], "allow")

        # 再次执行同目录下写操作，已被持久化规则覆盖，免打扰直接放行
        prompter_2 = FakePrompter()
        self.decider.prompter = prompter_2
        call_2 = FakeCall("fs_write", {"path": str(self.workspace / "docs" / "readme.md")})
        decision_2 = await self.decider.decide(call_2, tool, None)
        self.assertEqual(decision_2, Decision.ALLOW)
        self.assertEqual(len(prompter_2.asks), 0)

    async def test_headless_suspended_wait_and_resume(self) -> None:
        # 无 prompter 且 wait_headless=True：不直接拒绝，进入协程挂起等待人工信号
        self.decider.prompter = None
        call = FakeCall("shell", {"command": "npm install lodash"})
        tool = FakeTool("shell", readonly=False)

        task = asyncio.create_task(self.decider.decide(call, tool, None))
        await asyncio.sleep(0.05)
        self.assertFalse(task.done(), "在未收到人工审批信号前，任务必须保持挂起！")

        # 模拟外部审批管道注入信号
        self.decider.submit_headless_approval(PermissionChoice.ONCE)
        res = await task
        self.assertEqual(res, Decision.ALLOW)

    async def test_headless_suspended_wait_cancellation(self) -> None:
        # 挂起等待必须支持协作式取消（如用户按 Ctrl+C）
        self.decider.prompter = None
        call = FakeCall("shell", {"command": "dangerous_action"})
        tool = FakeTool("shell", readonly=False)

        task = asyncio.create_task(self.decider.decide(call, tool, None))
        await asyncio.sleep(0.05)
        self.assertFalse(task.done())

        # 发送取消信号
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task


class TestClaudeCodeHITLAlignment(unittest.IsolatedAsyncioTestCase):
    """验证 D122：对齐 Claude Code 工业级标准的 HITL 人在回路与参数 Diff 防御体系。"""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name).resolve()
        self.state_store = FakeStateStore()
        self.decider = HierarchicalPermissionDecider(
            cwd=self.workspace,
            state_store=self.state_store,
            wait_headless=False,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_real_tool_call_event_arguments_extraction_and_diff(self) -> None:
        """验证真实 Provider 的 ToolCallEvent（带 arguments 字段）能被正确解析出 Unified Diff。"""
        from logox.providers.base import ToolCallEvent

        event = ToolCallEvent(
            call_id="call_edit_1",
            name="edit",
            arguments={
                "path": "pelican-bicycle.html",
                "old_string": "const speed = 10;",
                "new_string": "const speed = 25;",
            },
        )
        tool = FakeTool("edit", readonly=False)
        prompter = FakePrompter(PermissionChoice.ONCE)
        self.decider.prompter = prompter

        decision = await self.decider.decide(event, tool, None)
        self.assertEqual(decision, Decision.ALLOW)
        self.assertEqual(len(prompter.asks), 1)

        ask = prompter.asks[0]
        self.assertIn("path: pelican-bicycle.html", ask.detail)
        self.assertIn("diff:", ask.detail)
        self.assertIn("-const speed = 10;", ask.detail)
        self.assertIn("+const speed = 25;", ask.detail)
        self.assertNotIn("（无参数）", ask.detail)

    async def test_root_file_exact_rule_does_not_leak_to_other_files(self) -> None:
        """核心防裸奔验证：放行根目录 pelican-bicycle.html 绝不自动放行 index.html 或 src/*。"""
        tool = FakeTool("edit", readonly=False)

        # 1. 第一次调用：编辑根目录 pelican-bicycle.html，选择持久化项目规则
        prompter = FakePrompter(PermissionChoice.PROJECT)
        self.decider.prompter = prompter
        call1 = FakeCall(
            "edit",
            {
                "path": str(self.workspace / "pelican-bicycle.html"),
                "old_string": "v1",
                "new_string": "v2",
            },
        )
        dec1 = await self.decider.decide(call1, tool, None)
        self.assertEqual(dec1, Decision.ALLOW)
        self.assertEqual(len(prompter.asks), 1)

        # 验证记录的细粒度规则仅针对当前文件，绝非裸工具名或全量通配符 *
        self.assertEqual(len(self.state_store.learned), 1)
        self.assertEqual(self.state_store.learned[0], ("allow", "edit:pelican-bicycle.html"))

        # 2. 第二次调用：再次编辑 pelican-bicycle.html -> 自动放行免弹窗
        prompter2 = FakePrompter()
        self.decider.prompter = prompter2
        dec2 = await self.decider.decide(call1, tool, None)
        self.assertEqual(dec2, Decision.ALLOW)
        self.assertEqual(len(prompter2.asks), 0)

        # 3. 第三次调用：试图编辑未授权文件 index.html -> 必须被拦截弹窗！
        call3 = FakeCall(
            "edit",
            {
                "path": str(self.workspace / "index.html"),
                "old_string": "a",
                "new_string": "b",
            },
        )
        prompter3 = FakePrompter(PermissionChoice.DENY)
        self.decider.prompter = prompter3
        dec3 = await self.decider.decide(call3, tool, None)
        self.assertEqual(dec3, Decision.DENY)
        self.assertEqual(len(prompter3.asks), 1, "未授权的 index.html 必须触发 HITL 弹窗审批！")

    async def test_command_prefix_whitelist_does_not_leak_to_dangerous_commands(self) -> None:
        """核心防裸奔验证：放行 cargo test 前缀绝不自动放行 cargo publish 或 rm -rf。"""
        tool = FakeTool("shell", readonly=False)

        # 1. 授权 cargo test
        prompter = FakePrompter(PermissionChoice.SESSION)
        self.decider.prompter = prompter
        call1 = FakeCall("shell", {"command": "cargo test --workspace"})
        dec1 = await self.decider.decide(call1, tool, None)
        self.assertEqual(dec1, Decision.ALLOW)

        # 2. 执行 cargo test --lib -> 命中前缀，自动放行
        prompter2 = FakePrompter()
        self.decider.prompter = prompter2
        call2 = FakeCall("shell", {"command": "cargo test --lib"})
        dec2 = await self.decider.decide(call2, tool, None)
        self.assertEqual(dec2, Decision.ALLOW)
        self.assertEqual(len(prompter2.asks), 0)

        # 3. 试图执行 cargo publish -> 不匹配前缀，必须弹窗拦截！
        prompter3 = FakePrompter(PermissionChoice.DENY)
        self.decider.prompter = prompter3
        call3 = FakeCall("shell", {"command": "cargo publish"})
        dec3 = await self.decider.decide(call3, tool, None)
        self.assertEqual(dec3, Decision.DENY)
        self.assertEqual(len(prompter3.asks), 1)

    async def test_sensitive_and_cross_boundary_files_disallow_persistence(self) -> None:
        """高危铁律：对 .env 或工作区外文件的修改，必须关闭持久化选项（allow_project=False）。"""
        tool = FakeTool("write", readonly=False)

        # 1. 尝试修改 .env
        prompter = FakePrompter(PermissionChoice.DENY)
        self.decider.prompter = prompter
        call_env = FakeCall("write", {"path": str(self.workspace / ".env"), "content": "SECRET=1"})
        await self.decider.decide(call_env, tool, None)
        self.assertEqual(len(prompter.asks), 1)
        self.assertFalse(prompter.asks[0].allow_project, ".env 属于高危敏感文件，严禁持久化！")

        # 2. 尝试修改工作区外部文件
        call_out = FakeCall("write", {"path": "C:/Windows/system32/calc.exe", "content": "bad"})
        await self.decider.decide(call_out, tool, None)
        self.assertEqual(len(prompter.asks), 2)
        self.assertFalse(prompter.asks[1].allow_project, "越界文件严禁持久化！")


if __name__ == "__main__":
    unittest.main()
