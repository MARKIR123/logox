"""Logox 权限裁决与规则引擎（M6）。

提供五层纵深防御体系：
1. 命令清洗与 Token 前缀规范化（``normalizer``）
2. 致命高危自毁黑名单（``engine._HARD_DENY_PATTERNS``）
3. 物理路径沙箱与工作区越界提权（``sandbox``）
4. 多作用域规则引擎（``engine``）
5. 人在回路 (HITL) 与无人值守异步挂起通道（``decider``）
"""

from __future__ import annotations

from logox.permissions.decider import HierarchicalPermissionDecider
from logox.permissions.engine import PermissionEngine
from logox.permissions.models import (
    Decision,
    PermissionEvaluation,
    PermissionMode,
    PermissionRule,
    RiskLevel,
    RuleScope,
)
from logox.permissions.normalizer import (
    is_compound_command,
    normalize_shell_command,
)
from logox.permissions.sandbox import PathSandbox

__all__ = [
    "Decision",
    "HierarchicalPermissionDecider",
    "PathSandbox",
    "PermissionEngine",
    "PermissionEvaluation",
    "PermissionMode",
    "PermissionRule",
    "RiskLevel",
    "RuleScope",
    "is_compound_command",
    "normalize_shell_command",
]
