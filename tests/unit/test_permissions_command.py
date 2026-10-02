"""/permissions 权限规则与物理沙箱管理指令单元测试（M6 扩展 / D132）。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List
import pytest

from logox.config.state import StateFile, StateStore
from logox.permissions.engine import PermissionEngine
from logox.permissions.models import Decision, PermissionMode, PermissionRule, RuleScope
from logox.tui.commands import resolve
from logox.tui.content.overlay import Choice, PickerState
from logox.tui.render.commands import CommandHost, CommandRunner


class DummyHost:
    """测试用 CommandHost。"""

    def __init__(self, runtime: Any = None) -> None:
        self.runtime = runtime
        self.notices: list[tuple[str, str]] = []
        self.pushed_overlay: Any = None
        self.pick_result: Any = None
        from logox.tui.theme import load_theme

        self.theme = load_theme()

    def notice(self, message: str, *, token: str = "text") -> None:
        self.notices.append((message, token))

    def refresh_status(self) -> None:
        pass

    async def push_overlay(self, component: Any) -> Any:
        self.pushed_overlay = component
        return self.pick_result


class DummyRuntime:
    """测试用 Runtime。"""

    def __init__(self, workspace_root: Path, state_store: Any = None) -> None:
        self.cwd = str(workspace_root)
        self.state_store = state_store
        self.engine = PermissionEngine(workspace_root, state_store=state_store)
        self.revoked_calls: list[tuple[str, str, str]] = []

    def get_permission_snapshot(self) -> dict[str, Any]:
        return self.engine.get_rules_snapshot()

    def revoke_permission_rule(self, scope: str, kind: str, rule_str: str) -> bool:
        self.revoked_calls.append((scope, kind, rule_str))
        target_scope = RuleScope.SESSION if scope == "session" else RuleScope.PROJECT
        target_dec = Decision.ALLOW if kind == "allow" else Decision.DENY
        return self.engine.revoke_by_str(rule_str, scope=target_scope, decision=target_dec)


def test_permissions_command_resolution() -> None:
    """测试 /permissions、/permission、/perm 命令路由解析与归一。"""
    for cmd in ("/permissions", "/permission", "/perm", "/PERMISSIONS", "/Permission"):
        res = resolve(cmd)
        assert res.state == "ready"
        assert res.name == "permissions"


def test_state_store_revoke_permission(tmp_path: Path) -> None:
    """测试 StateStore.revoke_permission 的原子移除与去重保护。"""
    store_file = tmp_path / "state.toml"
    store = StateStore(store_file)

    # 预置允许和拒绝规则
    store.learn_permission("allow", "shell:pytest")
    store.learn_permission("allow", "edit:src/**")
    store.learn_permission("deny", "shell:rm*")

    assert "shell:pytest" in store.read().permissions.allow
    assert "edit:src/**" in store.read().permissions.allow
    assert "shell:rm*" in store.read().permissions.deny

    # 撤销一条存在的规则
    res = store.revoke_permission("allow", "shell:pytest")
    assert res is True
    assert "shell:pytest" not in store.read().permissions.allow
    assert "edit:src/**" in store.read().permissions.allow

    # 撤销一条不存在的规则（返回 False，不写盘）
    res_non_exist = store.revoke_permission("allow", "shell:pytest")
    assert res_non_exist is False

    # 非法 kind 报错
    with pytest.raises(ValueError, match="kind 只能是 'allow' 或 'deny'"):
        store.revoke_permission("invalid", "shell:pytest")


def test_permission_engine_revoke_rule(tmp_path: Path) -> None:
    """测试 PermissionEngine 的会话与项目规则撤销。"""
    store = StateStore(tmp_path / "state.toml")
    engine = PermissionEngine(tmp_path, state_store=store)

    # 记录项目规则和会话规则
    proj_rule = PermissionRule(tool_name="shell", pattern="pytest", decision=Decision.ALLOW, scope=RuleScope.PROJECT)
    sess_rule = PermissionRule(tool_name="shell", pattern="git status", decision=Decision.ALLOW, scope=RuleScope.SESSION)

    engine.learn_rule(proj_rule)
    engine.learn_rule(sess_rule)

    assert len(engine.project_rules) == 1
    assert len(engine.session_rules) == 1
    assert "shell:pytest" in store.read().permissions.allow

    # 撤销项目规则
    revoked_proj = engine.revoke_rule(proj_rule)
    assert revoked_proj is True
    assert len(engine.project_rules) == 0
    assert "shell:pytest" not in store.read().permissions.allow

    # 撤销会话规则
    revoked_sess = engine.revoke_rule(sess_rule)
    assert revoked_sess is True
    assert len(engine.session_rules) == 0


@pytest.mark.anyio
async def test_cmd_permissions_choices_and_display(tmp_path: Path) -> None:
    """测试 /permissions 弹窗选项的构造与呈现（沙箱物理路径、敏感文件、运行模式）。"""
    runtime = DummyRuntime(tmp_path)
    host = DummyHost(runtime=runtime)
    runner = CommandRunner(host)  # type: ignore

    # 添加自定义规则
    runtime.engine.learn_rule(
        PermissionRule(tool_name="shell", pattern="pytest", decision=Decision.ALLOW, scope=RuleScope.PROJECT)
    )
    runtime.engine.learn_rule(
        PermissionRule(tool_name="shell", pattern="git status", decision=Decision.ALLOW, scope=RuleScope.SESSION)
    )

    # 触发命令
    res = resolve("/permissions")
    await runner.run(res)

    assert host.pushed_overlay is not None
    picker = host.pushed_overlay
    state: PickerState = picker.state

    assert "权限规则与沙箱防护" in state.title
    assert state.allow_delete is True
    assert "确定撤销权限规则" in state.delete_prompt

    # 检查顶部只读展示行（header_lines）
    assert any("[工作区沙箱]" in h for h in state.header_lines)
    assert any("[敏感文件保护]" in h for h in state.header_lines)
    assert any("[当前运行模式]" in h for h in state.header_lines)
    assert any(str(tmp_path.resolve()) in h for h in state.header_lines)

    # 检查规则选项（choices 纯净，仅包含规则）
    values = [c.value for c in state.choices]
    assert "project:allow:shell:pytest" in values
    assert "session:allow:shell:git status" in values

    # 测试上下交互逻辑：初始焦点在规则 0，按向下移动到规则 1，按向上返回规则 0
    assert state.index == 0
    state.move(1)
    assert state.index == 1
    state.move(-1)
    assert state.index == 0


@pytest.mark.anyio
async def test_cmd_permissions_handle_delete_project_and_session(tmp_path: Path) -> None:
    """测试通过 on_delete 回调删除项目与会话规则。"""
    store = StateStore(tmp_path / "state.toml")
    runtime = DummyRuntime(tmp_path, state_store=store)
    host = DummyHost(runtime=runtime)
    runner = CommandRunner(host)  # type: ignore

    proj_rule = PermissionRule(tool_name="shell", pattern="pytest", decision=Decision.ALLOW, scope=RuleScope.PROJECT)
    sess_rule = PermissionRule(tool_name="shell", pattern="git status", decision=Decision.ALLOW, scope=RuleScope.SESSION)
    runtime.engine.learn_rule(proj_rule)
    runtime.engine.learn_rule(sess_rule)

    res = resolve("/permissions")
    await runner.run(res)

    picker = host.pushed_overlay
    on_delete = picker.on_delete
    assert callable(on_delete)

    # 1. 尝试删除非规则项 -> 应拦截并弹出提示
    info_choice = Choice(value="info:sandbox", label="[工作区沙箱]", disabled=True)
    keep = on_delete(info_choice)
    assert keep is True
    assert any("不可删除" in msg for msg, tok in host.notices)

    # 2. 删除项目规则
    proj_choice = Choice(value="project:allow:shell:pytest", label="[项目允许] shell:pytest")
    keep = on_delete(proj_choice)
    assert keep is True
    assert ("project", "allow", "shell:pytest") in runtime.revoked_calls
    assert len(runtime.engine.project_rules) == 0
    assert "shell:pytest" not in store.read().permissions.allow
    assert any("已撤销项目持久权限规则：shell:pytest" in msg for msg, tok in host.notices)

    # 3. 删除会话规则
    sess_choice = Choice(value="session:allow:shell:git status", label="[会话允许] shell:git status")
    keep = on_delete(sess_choice)
    assert keep is True
    assert ("session", "allow", "shell:git status") in runtime.revoked_calls
    assert len(runtime.engine.session_rules) == 0
    assert any("已撤销会话临时权限规则：shell:git status" in msg for msg, tok in host.notices)


@pytest.mark.anyio
async def test_cmd_permissions_empty_rules(tmp_path: Path) -> None:
    """测试无自定义规则时的优雅展示与上下键盘导航能力。"""
    runtime = DummyRuntime(tmp_path)
    host = DummyHost(runtime=runtime)
    runner = CommandRunner(host)  # type: ignore

    res = resolve("/permissions")
    await runner.run(res)

    picker = host.pushed_overlay
    state: PickerState = picker.state

    # 检查顶部环境元数据依然完整
    assert any("[工作区沙箱]" in h for h in state.header_lines)

    # 检查无规则时的占位项正常且非禁用
    values = [c.value for c in state.choices]
    assert "none" in values
    empty_choice = next(c for c in state.choices if c.value == "none")
    assert "暂无自定义规则" in empty_choice.label
    assert empty_choice.disabled is False

    # 确保上下移动不会死循环或卡死
    state.move(1)
    assert state.index == 0
    state.move(-1)
    assert state.index == 0


@pytest.mark.anyio
async def test_ui_permission_decider_session_vs_project_persistence(tmp_path: Path) -> None:
    """测试 HITL 决策器中 '本次会话允许'（纯内存）与 '持久允许'（写 state.toml）的严格分工。"""
    from logox.app import UiPermissionDecider
    from logox.permission_types import PermissionChoice

    @dataclass
    class _ToolSpec:
        name: str = "write"
        readonly: bool = False

    @dataclass
    class _Tool:
        spec: _ToolSpec = field(default_factory=_ToolSpec)

    @dataclass
    class _Call:
        name: str = "write"
        args: dict[str, Any] = field(default_factory=lambda: {"path": "src/hello.py", "content": "print(1)"})
        call_id: str = "c1"

    class _MockPrompter:
        def __init__(self, answer: PermissionChoice) -> None:
            self.answer = answer
            self.asks = []

        async def ask_permission(self, ask: Any) -> PermissionChoice:
            self.asks.append(ask)
            return self.answer

    store = StateStore(tmp_path / "state.toml")

    # 1. 验证 SESSION 模式：内存记录，绝不写入 state.toml
    decider_sess = UiPermissionDecider(cwd=str(tmp_path))
    decider_sess.state_store = store
    decider_sess.prompter = _MockPrompter(PermissionChoice.SESSION)

    dec = await decider_sess.decide(_Call(), _Tool(), turn=None)
    assert dec == Decision.ALLOW
    # 验证内存规则已沉淀
    assert len(decider_sess.engine.session_rules) == 1
    # 验证 state.toml 绝未写入 session 规则
    assert len(store.read().permissions.allow) == 0

    # 2. 验证 PROJECT 模式：内存记录 + 立即持久化写入 state.toml
    decider_proj = UiPermissionDecider(cwd=str(tmp_path))
    decider_proj.state_store = store
    decider_proj.prompter = _MockPrompter(PermissionChoice.PROJECT)

    dec2 = await decider_proj.decide(_Call(), _Tool(), turn=None)
    assert dec2 == Decision.ALLOW
    # 验证内存与 state.toml 均已沉淀
    assert len(decider_proj.engine.project_rules) == 1
    persisted = store.read().permissions.allow
    assert len(persisted) == 1
    assert any("write" in rule for rule in persisted)
