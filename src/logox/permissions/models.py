"""权限与规则引擎的核心数据模型（M6）。

本模块定义了裁决结果（``Decision``）、规则作用域（``RuleScope``）、
风险级别（``RiskLevel``）、规则对象（``PermissionRule``）与综合评定（``PermissionEvaluation``）。
遵循纯数据模型设计，与具体的界面框架完全解耦。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

__all__ = [
    "Decision",
    "PermissionEvaluation",
    "PermissionMode",
    "PermissionRule",
    "RiskLevel",
    "RuleScope",
]


class PermissionMode(str, Enum):
    """权限运行模式。"""

    DEFAULT = "default"  # 默认防护模式：未白名单的写操作一律需 HITL 审批
    CREATIVE = "creative"  # 创造免打扰模式：除高危黑名单与越界敏感文件外，常规操作直接放行


class Decision(str, Enum):
    """权限裁决结论。与 ``logox.kernel.scheduler.Decision`` 词汇表完全对齐。"""

    ALLOW = "allow"  # 允许执行
    DENY = "deny"  # 显式拒绝
    ASK = "ask"  # 需人在回路 (HITL) 交互审批


class RuleScope(str, Enum):
    """规则所处的生命周期与生效范围。"""

    BUILTIN = "builtin"  # 内置只读安全基线（常驻不可变）
    PROJECT = "project"  # 持久化于项目 .logox/state.toml
    SESSION = "session"  # 仅当前进程内存有效，重启销毁


class RiskLevel(str, Enum):
    """操作风险评级。用于在第 3 层沙箱检测后，向 HITL 界面打上醒目标签。"""

    NORMAL = "normal"  # 常规操作（如普通源码编辑、安全前缀命令）
    HIGH_CROSS_BOUNDARY = "high_cross_boundary"  # 高危：操作路径超出工作区根目录
    HIGH_SENSITIVE = "high_sensitive"  # 高危：触碰 .git/、.env 或核心配置文件


@dataclass(frozen=True)
class PermissionRule:
    """权限裁决规则定义。

    支持对特定工具（``tool_name``）及参数模式（``pattern``）进行权限声明。
    """

    tool_name: str
    pattern: str = "*"
    decision: Decision = Decision.ALLOW
    scope: RuleScope = RuleScope.SESSION
    description: str = ""

    def to_str(self) -> str:
        """序列化为可持久化存储的字符串形式。"""
        if not self.pattern or self.pattern == "*":
            return self.tool_name
        return f"{self.tool_name}:{self.pattern}"

    @classmethod
    def from_str(
        cls,
        text: str,
        *,
        scope: RuleScope = RuleScope.PROJECT,
        decision: Decision = Decision.ALLOW,
    ) -> PermissionRule:
        """从字符串反序列化规则。

        向后兼容：
        - ``"shell:pytest"`` -> tool_name="shell", pattern="pytest"
        - ``"shell"`` -> tool_name="shell", pattern="*"
        - ``"pytest"`` -> 默认当作 shell 子命令前缀
        """
        raw = text.strip()
        if not raw:
            return cls(tool_name="*", pattern="*", decision=decision, scope=scope)

        if ":" in raw:
            tool, pat = raw.split(":", 1)
            return cls(
                tool_name=tool.strip(),
                pattern=pat.strip() or "*",
                decision=decision,
                scope=scope,
            )

        known_tools = {
            "shell",
            "write",
            "edit",
            "glob",
            "grep",
            "read",
            "fs_write",
            "fs_edit",
            "fs_glob",
            "fs_grep",
            "fs_read",
        }
        if raw in known_tools:
            return cls(tool_name=raw, pattern="*", decision=decision, scope=scope)

        # 默认假设是 shell 命令前缀模式
        return cls(tool_name="shell", pattern=raw, decision=decision, scope=scope)


@dataclass
class PermissionEvaluation:
    """权限引擎裁决评估报告。包含最终决断、原因、风险评级与推荐规则。"""

    decision: Decision
    reason: str
    risk_level: RiskLevel = RiskLevel.NORMAL
    matched_rule: PermissionRule | None = None
    suggested_rule: PermissionRule | None = None
