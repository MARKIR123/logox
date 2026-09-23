"""人在回路 (HITL) 决策器与异步挂起通道（第 5 层：HierarchicalPermissionDecider）。

负责将 ``PermissionEngine`` 的评估结论接入实际执行环境：
1. 若评估结果为 ``ALLOW`` 或 ``DENY``，直接返回结果；
2. 若评估结果为 ``ASK``：
   - **交互终端模式 (prompter is not None)**：构建带风险等级（``RiskLevel``）的 ``PermissionAsk``，
     呼起终端浮层等待用户敲键盘裁决；
   - **无人值守模式 (Headless / prompter is None)**：
     **绝不自杀式拒绝**（用户裁定），而是进入挂起状态（``WAITING_FOR_PERMISSION``），
     等待外部人工审批信号注入或协作式取消（``asyncio.CancelledError``）；
3. 学习与规则沉淀：
   - 选 ``[2] 本会话允许`` $\rightarrow$ 动态记录至引擎的会话内存规则库；
   - 选 ``[3] 持久允许`` $\rightarrow$ 动态记录至项目规则库并原子写盘至 ``state.toml``。
"""

from __future__ import annotations

import asyncio
import difflib
import logging
from typing import Any, Dict, List, Optional, Tuple

from logox.permissions.engine import PermissionEngine, _normalize_tool_name
from logox.permissions.models import (
    Decision,
    PermissionEvaluation,
    PermissionRule,
    RiskLevel,
    RuleScope,
)
from logox.permission_types import PermissionAsk, PermissionChoice

logger = logging.getLogger(__name__)

__all__ = ["HierarchicalPermissionDecider", "format_permission_detail", "_format_args"]


class HierarchicalPermissionDecider:
    """多层级权限裁决器。实现内核 ``PermissionDecider`` 协议契约。"""

    def __init__(
        self,
        engine: Optional[PermissionEngine] = None,
        *,
        cwd: Any = None,
        state_store: Any = None,
        wait_headless: bool = True,
    ) -> None:
        self.engine = engine or PermissionEngine(cwd, state_store=state_store)
        #: 注册给 TUI 前端挂载的提问通道（如 InlineApp.ask_permission）
        self.prompter: Any = None
        #: 无人值守环境下的审批信号队列（协程挂起与外部信号注入）
        self._headless_queue: asyncio.Queue[PermissionChoice] = asyncio.Queue()
        #: 是否在 headless 模式下挂起等待人工审批
        self.wait_headless = wait_headless
        #: 历史审计追踪：记录每次裁决的 (tool, choice)
        self.history: List[Tuple[str, str]] = []

    # ------------------------------------------------------------------ #
    # 外部注入接口 (Headless Control)
    # ------------------------------------------------------------------ #

    def inject_approval(self, choice: PermissionChoice) -> None:
        """从外部通道向挂起的裁决器注入审批结果（用于 API 远程控制或单元测试）。"""
        self._headless_queue.put_nowait(choice)

    submit_headless_approval = inject_approval

    # ------------------------------------------------------------------ #
    # 状态注入与兼容性
    # ------------------------------------------------------------------ #

    @property
    def cwd(self) -> str:
        return str(self.engine.workspace_root)

    def seed_persisted(self, rules: Any, mode: Any = None) -> None:
        """从外部（如 state.toml）载入持久化规则与运行模式。"""
        if isinstance(rules, list):
            self.engine.load_persisted(
                allow_rules=[str(r) for r in rules],
                mode=str(mode) if mode else None,
            )
        elif mode:
            self.engine.set_mode(str(mode))

    @property
    def mode(self) -> str:
        return self.engine.mode.value

    def set_mode(self, mode: str) -> None:
        self.engine.set_mode(mode)

    @property
    def allowed_tools(self) -> set[str]:
        """向后兼容属性：返回所有当前匹配通配允许的工具名。"""
        res = set()
        for r in self.engine.session_rules + self.engine.project_rules:
            if r.decision == Decision.ALLOW:
                res.add(r.tool_name)
        return res

    # ------------------------------------------------------------------ #
    # 核心决策逻辑 (PermissionDecider Protocol)
    # ------------------------------------------------------------------ #

    async def decide(self, call: Any, tool: Any, turn: Any) -> Decision:
        """裁决一次工具调用。返回值必须为 Decision.ALLOW 或 Decision.DENY。"""
        tool_name = str(getattr(tool, "spec", None) and tool.spec.name or call.name)
        args: Dict[str, Any] = getattr(call, "arguments", None) or getattr(call, "args", None) or {}

        # 1. 运行权限引擎五层裁决流水线
        evaluation: PermissionEvaluation = self.engine.evaluate(tool_name, args)

        if evaluation.decision == Decision.ALLOW:
            return Decision.ALLOW
        if evaluation.decision == Decision.DENY:
            return Decision.DENY

        # 2. 需要人在回路 (HITL) 裁决
        ask = self._build_ask(call, tool_name, args, evaluation)

        prompter = self.prompter
        if prompter is not None:
            # 交互模式：弹窗让用户在终端选择
            try:
                choice = await prompter.ask_permission(ask)
            except Exception as exc:
                logger.warning("权限弹窗询问异常，按拒绝处理：%s", exc)
                choice = PermissionChoice.DENY
            if not isinstance(choice, PermissionChoice):
                choice = PermissionChoice.DENY
        else:
            # 无人模式：用户裁定“不要直接拒绝，而是一直等待人工处理”
            if not self.wait_headless:
                # 显式关闭挂起时的降级安全出口（让调度器按没有界面解释并说明原因）
                return Decision.ASK

            logger.info("当前处于无人值守模式，工具 %s 挂起等待人工审批信号...", tool_name)
            try:
                choice = await self._headless_queue.get()
            except asyncio.CancelledError:
                logger.info("人工审批等待被协作式取消")
                raise

        # 3. 沉淀与学习用户选择的规则
        self._record_choice(ask, choice, evaluation)
        return Decision.ALLOW if choice.allowed else Decision.DENY

    # ------------------------------------------------------------------ #
    # 辅助方法
    # ------------------------------------------------------------------ #

    def _build_ask(
        self,
        call: Any,
        tool_name: str,
        args: Dict[str, Any],
        evaluation: PermissionEvaluation,
    ) -> PermissionAsk:
        """组装供 TUI 渲染的纯数据对象 PermissionAsk。"""
        rule_str = ""
        rule_scope = ""
        if evaluation.suggested_rule:
            rule_str = evaluation.suggested_rule.pattern
            rule_scope = "建议规则模式"

        # 风险等级映射
        risk = "high" if evaluation.risk_level != RiskLevel.NORMAL else "normal"
        risk_note = evaluation.reason
        # 高危越界或敏感文件严禁持久化
        allow_project = evaluation.risk_level == RiskLevel.NORMAL

        return PermissionAsk(
            tool=tool_name,
            detail=format_permission_detail(tool_name, args, cwd=str(self.engine.workspace_root)),
            rule=rule_str,
            rule_scope=rule_scope,
            cwd=str(self.engine.workspace_root),
            risk=risk,
            risk_note=risk_note,
            allow_session=True,
            allow_project=allow_project,
            call_id=str(getattr(call, "call_id", "")),
        )

    def _record_choice(
        self,
        ask: PermissionAsk,
        choice: PermissionChoice,
        evaluation: PermissionEvaluation,
    ) -> None:
        """根据用户的选择，更新审计历史与规则库。"""
        self.history.append((ask.tool, choice.value))

        if choice is PermissionChoice.SESSION:
            # 记录会话级规则
            pattern = (
                evaluation.suggested_rule.pattern
                if evaluation.suggested_rule
                else "*"
            )
            rule = PermissionRule(
                tool_name=ask.tool,
                pattern=pattern,
                decision=Decision.ALLOW,
                scope=RuleScope.SESSION,
            )
            self.engine.learn_rule(rule)

        elif choice is PermissionChoice.PROJECT:
            # 记录项目级持久化规则
            pattern = (
                evaluation.suggested_rule.pattern
                if evaluation.suggested_rule
                else "*"
            )
            rule = PermissionRule(
                tool_name=ask.tool,
                pattern=pattern,
                decision=Decision.ALLOW,
                scope=RuleScope.PROJECT,
            )
            self.engine.learn_rule(rule)

    async def ask_continuation(self, turn: Any, iteration: int) -> bool:
        """询问用户是否允许续期继续执行更多工具轮次。"""
        prompter = self.prompter
        if prompter is not None and hasattr(prompter, "ask_continuation"):
            try:
                return bool(await prompter.ask_continuation(turn, iteration))
            except Exception as exc:
                logger.warning("询问轮次续期失败：%s", exc)
                return False
        if prompter is None and self.wait_headless:
            try:
                choice = self._headless_queue.get_nowait()
                return choice in (
                    PermissionChoice.ONCE,
                    PermissionChoice.SESSION,
                    PermissionChoice.PROJECT,
                )
            except asyncio.QueueEmpty:
                return False
        return False


def format_permission_detail(tool_name: str, args: Any, cwd: str = "") -> str:
    """把工具参数排成易于审阅的多行文本，支持 edit 原生 Diff 与 write/shell 结构化展示。"""
    if not isinstance(args, dict) or not args:
        return ""

    tool = _normalize_tool_name(tool_name)

    # 1. edit 工具：生成标准 Unified Diff
    if tool == "edit" and ("old_string" in args or "new_string" in args or "path" in args):
        target = str(args.get("path") or args.get("file_path") or "").strip()
        old_str = str(args.get("old_string") or "")
        new_str = str(args.get("new_string") or "")
        lines: list[str] = [f"path: {target}"]
        if old_str or new_str:
            old_lines = old_str.splitlines()
            new_lines = new_str.splitlines()
            diff_gen = list(
                difflib.unified_diff(
                    old_lines,
                    new_lines,
                    fromfile="原内容",
                    tofile="修改后",
                    lineterm="",
                    n=3,
                )
            )
            if diff_gen:
                lines.append("diff:")
                max_diff_lines = 80
                for d in diff_gen[:max_diff_lines]:
                    lines.append(f"  {d}")
                if len(diff_gen) > max_diff_lines:
                    lines.append(f"  ... (省略后续 {len(diff_gen) - max_diff_lines} 行变更)")
            else:
                lines.append("diff: （原内容与修改后内容无文本差异）")
        return "\n".join(lines)

    # 2. write 工具：展示目标路径与内容预览
    if tool == "write" and ("content" in args or "path" in args):
        target = str(args.get("path") or args.get("file_path") or "").strip()
        content = str(args.get("content") or "")
        lines = [f"path: {target}"]
        if content:
            lines.append("content:")
            for l in content.splitlines():
                lines.append(f"  {l}")
        return "\n".join(lines)

    # 3. shell 工具：展示待执行命令与工作目录
    if tool == "shell" and "command" in args:
        cmd = str(args.get("command") or "").strip()
        lines = [f"command: {cmd}"]
        if cwd:
            lines.append(f"cwd: {cwd}")
        return "\n".join(lines)

    # 4. 其他常规工具：key: value 展开
    lines = []
    for key, value in args.items():
        val_str = str(value)
        if "\n" in val_str:
            lines.append(f"{key}:")
            for sub in val_str.splitlines():
                lines.append(f"  {sub}")
        else:
            lines.append(f"{key}: {val_str}")
    return "\n".join(lines)


def _format_args(args: Any, tool_name: str = "") -> str:
    """向后兼容辅助函数。"""
    if tool_name:
        return format_permission_detail(tool_name, args)
    if not isinstance(args, dict) or not args:
        return ""
    # 自动探测工具名特征
    if "old_string" in args and "new_string" in args:
        return format_permission_detail("edit", args)
    if "content" in args and ("path" in args or "file_path" in args):
        return format_permission_detail("write", args)
    if "command" in args:
        return format_permission_detail("shell", args)
    lines = []
    for key, value in args.items():
        val_str = str(value)
        if "\n" in val_str:
            lines.append(f"{key}:")
            for sub in val_str.splitlines():
                lines.append(f"  {sub}")
        else:
            lines.append(f"{key}: {val_str}")
    return "\n".join(lines)
