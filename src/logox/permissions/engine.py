"""权限裁决与规则引擎实现（第 2 层与第 4 层：Blacklist & Multi-Scope Rule Engine）。

本模块实现了 5 层防护链条中的核心调度与规则匹配逻辑：
1. 第 2 层：系统绝对自毁黑名单（一票否决，直接 DENY，绝不弹窗）；
2. 第 3 层：沙箱审计结果接驳（越界或敏感文件标记 HIGH 风险并直接路由至 HITL）；
3. 第 4 层：多级规则链条判定：
   - 显式拒绝规则（Deny Rules）优先
   - 会话级放行规则（Session Rules）
   - 项目级持久化放行规则（Project Rules，读取 .logox/state.toml）
   - 内置只读安全基线（Built-in Safe Whitelist）
   - 未命中规则时，生成规范化的建议规则并返回 ASK。
"""

from __future__ import annotations

import fnmatch
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from logox.permissions.models import (
    Decision,
    PermissionEvaluation,
    PermissionMode,
    PermissionRule,
    RiskLevel,
    RuleScope,
)
from logox.permissions.normalizer import normalize_shell_command
from logox.permissions.sandbox import PathSandbox

logger = logging.getLogger(__name__)

__all__ = ["PermissionEngine"]

#: 第 2 层：绝对高危自毁命令黑名单（正则表达式）
_HARD_DENY_PATTERNS = [
    # Windows 致命破坏
    re.compile(r"(?i)\bformat\s+[a-zA-Z]:"),
    re.compile(r"(?i)\bRemove-Item\s+.*[a-zA-Z]:\\"),
    re.compile(r"(?i)\bbcdedit\b"),
    re.compile(r"(?i)\bshutdown(\.exe)?\s+/[srf]"),
    # POSIX 致命破坏
    re.compile(r"\brm\s+-(?:r[fF]|f[rR]|rf)\s+(?:/|\*)"),
    re.compile(r":\(\)\{\s*:\|:&\s*\};:"),
    re.compile(r"\bmkfs(\.[a-zA-Z0-9]+)?\s+"),
    re.compile(r"\bdd\s+if=.*of=/dev/"),
]

#: 内置默认只读命令白名单（无需弹窗打扰）
_BUILTIN_SAFE_SHELL_PREFIXES = {
    "git status",
    "git diff",
    "git log",
    "git branch",
    "git show",
    "pytest",
    "python -m pytest",
    "cargo check",
    "cargo test",
    "npm test",
    "pnpm test",
    "dir",
    "ls",
    "pwd",
    "echo",
    "cat",
    "head",
    "tail",
}


def _normalize_tool_name(name: str) -> str:
    aliases = {
        "fs_edit": "edit",
        "fs_write": "write",
        "fs_glob": "glob",
        "fs_grep": "grep",
        "fs_read": "read",
    }
    return aliases.get(name, name)


class PermissionEngine:
    """权限裁决与规则引擎。"""

    def __init__(
        self,
        workspace_root: Path | str | None = None,
        *,
        state_store: Any = None,
        mode: PermissionMode | str = PermissionMode.DEFAULT,
    ) -> None:
        self.workspace_root = Path(workspace_root or ".").resolve()
        self.sandbox = PathSandbox(self.workspace_root)
        self.state_store = state_store
        try:
            self.mode = PermissionMode(mode)
        except Exception:
            self.mode = PermissionMode.DEFAULT

        #: 内存中的会话级规则
        self.session_rules: List[PermissionRule] = []
        #: 项目级持久化规则（启动时从 state.toml 加载）
        self.project_rules: List[PermissionRule] = []

    def set_mode(self, mode: PermissionMode | str) -> None:
        """动态调整权限运行模式并同步持久化（D130）。"""
        try:
            self.mode = PermissionMode(mode)
        except Exception:
            self.mode = PermissionMode.DEFAULT

        if self.state_store is not None and hasattr(self.state_store, "set_permission_mode"):
            try:
                self.state_store.set_permission_mode(self.mode.value)
            except Exception as exc:
                logger.warning("权限模式写入 state.toml 失败：%s", exc)

    # ------------------------------------------------------------------ #
    # 规则导入与学习 (Persistence & Learning)
    # ------------------------------------------------------------------ #

    def load_persisted(
        self,
        allow_rules: Optional[List[str]] = None,
        deny_rules: Optional[List[str]] = None,
        mode: Optional[str] = None,
    ) -> None:
        """从配置中加载持久化规则列表与运行模式。"""
        if mode:
            try:
                self.mode = PermissionMode(mode)
            except Exception:
                self.mode = PermissionMode.DEFAULT
        loaded: List[PermissionRule] = []
        for raw in allow_rules or []:
            loaded.append(
                PermissionRule.from_str(
                    raw, scope=RuleScope.PROJECT, decision=Decision.ALLOW
                )
            )
        for raw in deny_rules or []:
            loaded.append(
                PermissionRule.from_str(
                    raw, scope=RuleScope.PROJECT, decision=Decision.DENY
                )
            )
        self.project_rules = loaded

    def learn_rule(self, rule: PermissionRule) -> None:
        """记录一条新规则。支持 Session 内存与 Project 持久化。"""
        if rule.scope == RuleScope.SESSION:
            # 去重添加
            if rule not in self.session_rules:
                self.session_rules.append(rule)
        elif rule.scope == RuleScope.PROJECT:
            if rule not in self.project_rules:
                self.project_rules.append(rule)
            if self.state_store is not None:
                kind = "allow" if rule.decision == Decision.ALLOW else "deny"
                try:
                    self.state_store.learn_permission(kind, rule.to_str())
                except Exception as exc:
                    logger.warning("规则写入 state.toml 失败：%s", exc)

    def revoke_rule(self, rule: PermissionRule) -> bool:
        """撤销/移除一条既有规则（D132）。支持 Session 与 Project。"""
        removed = False
        if rule.scope == RuleScope.SESSION:
            to_remove = [
                r
                for r in self.session_rules
                if r.tool_name == rule.tool_name
                and r.pattern == rule.pattern
                and r.decision == rule.decision
            ]
            for r in to_remove:
                self.session_rules.remove(r)
                removed = True
        elif rule.scope == RuleScope.PROJECT:
            to_remove = [
                r
                for r in self.project_rules
                if r.tool_name == rule.tool_name
                and r.pattern == rule.pattern
                and r.decision == rule.decision
            ]
            for r in to_remove:
                self.project_rules.remove(r)
                removed = True
            if self.state_store is not None and hasattr(self.state_store, "revoke_permission"):
                kind = "allow" if rule.decision == Decision.ALLOW else "deny"
                try:
                    self.state_store.revoke_permission(kind, rule.to_str())
                except Exception as exc:
                    logger.warning("从 state.toml 撤销规则失败：%s", exc)

        return removed

    def revoke_by_str(
        self,
        rule_str: str,
        *,
        scope: RuleScope = RuleScope.PROJECT,
        decision: Decision = Decision.ALLOW,
    ) -> bool:
        """根据序列化字符串与作用域撤销规则。"""
        rule = PermissionRule.from_str(rule_str, scope=scope, decision=decision)
        return self.revoke_rule(rule)

    def get_rules_snapshot(self) -> Dict[str, Any]:
        """获取当前权限与物理沙箱配置快照（D132）。"""
        return {
            "workspace_root": self.workspace_root,
            "mode": self.mode,
            "project_rules": list(self.project_rules),
            "session_rules": list(self.session_rules),
            "sensitive_items": [".git/", ".env", ".logox/config.toml", ".logox/permissions.toml"],
        }

    # ------------------------------------------------------------------ #
    # 匹配算法 (Rule Matching)
    # ------------------------------------------------------------------ #

    def _matches_rule(
        self,
        rule: PermissionRule,
        tool_name: str,
        args: Dict[str, Any],
        normalized_prefix: str,
        command_raw: str,
    ) -> bool:
        """检查特定规则是否匹配当前调用。"""
        rule_tool = _normalize_tool_name(rule.tool_name)
        call_tool = _normalize_tool_name(tool_name)
        if rule_tool != "*" and rule_tool != call_tool:
            return False

        if rule.pattern == "*" or not rule.pattern:
            return True

        if tool_name == "shell":
            pat = rule.pattern.lower()
            if pat.endswith("*"):
                prefix = pat[:-1].strip()
                return (
                    normalized_prefix.startswith(prefix)
                    or command_raw.lower().startswith(prefix)
                )
            return normalized_prefix == pat or command_raw.lower().startswith(pat)

        # 文件操作：按相对路径或通配符匹配
        target_path = str(args.get("path") or args.get("file_path") or "").strip()
        if target_path:
            try:
                rel = str(
                    Path(target_path).resolve().relative_to(self.workspace_root)
                ).replace("\\", "/")
            except Exception:
                rel = target_path.replace("\\", "/")

            clean_pat = rule.pattern.replace("\\", "/")
            return fnmatch.fnmatch(rel, clean_pat) or fnmatch.fnmatch(
                target_path, clean_pat
            )

        return False

    # ------------------------------------------------------------------ #
    # 核心裁决管线 (5-Layer Evaluation Pipeline)
    # ------------------------------------------------------------------ #

    def evaluate(
        self, tool_name: str, args: Optional[Dict[str, Any]] = None
    ) -> PermissionEvaluation:
        """执行五层裁决评估。"""
        args_dict = args or {}

        # -------------------------------------------------------------- #
        # 第 1 层：规范化与清洗 (Normalization)
        # -------------------------------------------------------------- #
        command_raw = str(args_dict.get("command", "") or "")
        normalized_prefix = ""
        is_compound = False
        if tool_name == "shell":
            normalized_prefix, _tokens, is_compound = normalize_shell_command(
                command_raw
            )

        # -------------------------------------------------------------- #
        # 第 2 层：系统绝对自毁黑名单 (Hardcoded Blacklist)
        # -------------------------------------------------------------- #
        if tool_name == "shell":
            for pat in _HARD_DENY_PATTERNS:
                if pat.search(command_raw):
                    return PermissionEvaluation(
                        decision=Decision.DENY,
                        reason="命中系统高危自毁绝对黑名单，一票否决禁止执行",
                        risk_level=RiskLevel.HIGH_SENSITIVE,
                    )

        # -------------------------------------------------------------- #
        # 第 3 层：路径沙箱与敏感防护 (Path Sandboxing)
        # -------------------------------------------------------------- #
        risk_level, risk_note = self.sandbox.audit_tool_args(tool_name, args_dict)
        if risk_level != RiskLevel.NORMAL:
            # 用户裁定：越界与敏感操作不直接拒绝，提权直通 HITL 审批！
            suggested = self._build_suggested_rule(
                tool_name, args_dict, normalized_prefix
            )
            if suggested:
                suggested = PermissionRule(
                    tool_name=suggested.tool_name,
                    pattern=suggested.pattern,
                    decision=Decision.ALLOW,
                    scope=RuleScope.SESSION,
                )
            return PermissionEvaluation(
                decision=Decision.ASK,
                reason=risk_note or "高风险操作（工作区越界或涉及敏感文件）",
                risk_level=risk_level,
                suggested_rule=suggested,
            )

        # 如果 shell 命令包含复合操作符（管道/分号/串联），仅在 default 模式下降级为 HITL 审批
        if tool_name == "shell" and is_compound and self.mode != PermissionMode.CREATIVE:
            suggested = self._build_suggested_rule(
                tool_name, args_dict, normalized_prefix
            )
            return PermissionEvaluation(
                decision=Decision.ASK,
                reason="检测到 Shell 复合操作符（管道、分号或逻辑串联），为确保安全需人工确认",
                risk_level=RiskLevel.NORMAL,
                suggested_rule=suggested,
            )

        # -------------------------------------------------------------- #
        # 第 4 层：三层规则引擎匹配 (Rule Engine: Deny > Session > Project > Built-in)
        # -------------------------------------------------------------- #
        all_rules = self.session_rules + self.project_rules

        # 4.1 显式拒绝规则优先一票否决
        for rule in all_rules:
            if rule.decision == Decision.DENY:
                if self._matches_rule(
                    rule, tool_name, args_dict, normalized_prefix, command_raw
                ):
                    return PermissionEvaluation(
                        decision=Decision.DENY,
                        reason=f"命中显式拒绝规则：{rule.to_str()}",
                        matched_rule=rule,
                    )

        # 4.2 会话级放行规则
        for rule in self.session_rules:
            if rule.decision == Decision.ALLOW:
                if self._matches_rule(
                    rule, tool_name, args_dict, normalized_prefix, command_raw
                ):
                    return PermissionEvaluation(
                        decision=Decision.ALLOW,
                        reason=f"命中本次会话允许规则：{rule.to_str()}",
                        matched_rule=rule,
                    )

        # 4.3 项目级持久化放行规则
        for rule in self.project_rules:
            if rule.decision == Decision.ALLOW:
                if self._matches_rule(
                    rule, tool_name, args_dict, normalized_prefix, command_raw
                ):
                    return PermissionEvaluation(
                        decision=Decision.ALLOW,
                        reason=f"命中项目持久允许规则：{rule.to_str()}",
                        matched_rule=rule,
                    )

        # 4.4 内置只读安全基线 (Built-in Safe Whitelist)
        if tool_name in {"fs_glob", "fs_grep", "glob", "grep"}:
            return PermissionEvaluation(
                decision=Decision.ALLOW,
                reason="天然只读安全工具",
                matched_rule=PermissionRule(
                    tool_name=tool_name,
                    pattern="*",
                    decision=Decision.ALLOW,
                    scope=RuleScope.BUILTIN,
                ),
            )

        if tool_name == "shell" and normalized_prefix in _BUILTIN_SAFE_SHELL_PREFIXES:
            return PermissionEvaluation(
                decision=Decision.ALLOW,
                reason=f"命中内置常用只读命令白名单：{normalized_prefix}",
                matched_rule=PermissionRule(
                    tool_name="shell",
                    pattern=normalized_prefix,
                    decision=Decision.ALLOW,
                    scope=RuleScope.BUILTIN,
                ),
            )

        # -------------------------------------------------------------- #
        # 4.5 未命中放行规则
        # -------------------------------------------------------------- #
        if self.mode == PermissionMode.CREATIVE:
            return PermissionEvaluation(
                decision=Decision.ALLOW,
                reason="处于创造模式 (creative)，自动放行常规操作",
                risk_level=RiskLevel.NORMAL,
            )

        suggested = self._build_suggested_rule(tool_name, args_dict, normalized_prefix)
        return PermissionEvaluation(
            decision=Decision.ASK,
            reason="非白名单操作，需人工授权",
            risk_level=RiskLevel.NORMAL,
            suggested_rule=suggested,
        )

    def _build_suggested_rule(
        self, tool_name: str, args: Dict[str, Any], normalized_prefix: str
    ) -> PermissionRule:
        """为当前操作推导推荐记录的细粒度规则。"""
        if tool_name == "shell":
            pattern = normalized_prefix or "*"
            return PermissionRule(
                tool_name="shell",
                pattern=pattern,
                decision=Decision.ALLOW,
                scope=RuleScope.PROJECT,
            )

        # 文件操作：若在子目录内，推导所属目录模式（如 docs/*）；若在根目录，仅推导当前文件名（杜绝全量通配符 *）
        target_path = str(args.get("path") or args.get("file_path") or "").strip()
        if target_path:
            try:
                rel = Path(target_path).resolve().relative_to(self.workspace_root)
                parent = str(rel.parent).replace("\\", "/")
                if parent and parent != ".":
                    pattern = f"{parent}/*"
                else:
                    pattern = rel.name
            except Exception:
                pattern = target_path.replace("\\", "/")
            return PermissionRule(
                tool_name=tool_name,
                pattern=pattern,
                decision=Decision.ALLOW,
                scope=RuleScope.PROJECT,
            )

        return PermissionRule(
            tool_name=tool_name,
            pattern="*",
            decision=Decision.ALLOW,
            scope=RuleScope.PROJECT,
        )
