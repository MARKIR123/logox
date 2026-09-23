"""物理路径沙箱与敏感资源审计（第 3 层：Path Sandboxing）。

负责对文件系统操作（``fs_write``, ``fs_edit``, ``fs_glob`` 等）及命令参数中涉及的物理路径进行安全性审计：
1. 真实物理路径消解（``resolve()``），有效防御通过 ``../../`` 进行路径穿越或软链接逃逸；
2. 工作区越界检测：如果目标路径超出 ``workspace_root``，打上 ``RiskLevel.HIGH_CROSS_BOUNDARY`` 标签；
3. 敏感文件与配置保护：如果目标路径涉及 ``.git/``、``.env`` 或核心只读配置文件，
   打上 ``RiskLevel.HIGH_SENSITIVE`` 标签。

设计原则（用户裁定）：
越界或敏感操作**绝不直接报错终止**，而是打上高危标签直通 HITL 弹窗，将最终裁决权留给人类。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Tuple

from logox.permissions.models import RiskLevel

__all__ = ["PathSandbox"]

#: 默认敏感文件名或目录标记（任何路径片段命中即视作敏感）
_SENSITIVE_NAMES = {".git", ".env"}
_SENSITIVE_CONFIGS = {"config.toml", "permissions.toml"}


class PathSandbox:
    """项目工作区物理沙箱。"""

    def __init__(self, workspace_root: Path | str | None = None) -> None:
        self.workspace_root = Path(workspace_root or ".").resolve()

    def audit_path(self, path: Path | str) -> Tuple[RiskLevel, str]:
        """审计给定的目标路径。

        :param path: 待检查的相对或绝对路径
        :return: ``(RiskLevel, warning_message)``
        """
        raw_str = str(path or "").strip()
        if not raw_str:
            return RiskLevel.NORMAL, ""

        try:
            # 完整物理路径展开（相对路径相对工作区根目录展开，展开 .. 与软链接）
            p = Path(raw_str)
            resolved = (self.workspace_root / p).resolve() if not p.is_absolute() else p.resolve()
        except Exception as exc:
            return (
                RiskLevel.HIGH_CROSS_BOUNDARY,
                f"无法解析的非法目标路径 ({raw_str}): {exc}",
            )

        # 1. 边界检测：是否越出工作区根目录
        try:
            rel = resolved.relative_to(self.workspace_root)
        except (ValueError, RuntimeError):
            return (
                RiskLevel.HIGH_CROSS_BOUNDARY,
                f"目标路径越出项目工作区根目录：{resolved}",
            )

        # 2. 敏感资源检测：是否触碰 .git / .env / 核心配置
        parts = resolved.parts
        for part in parts:
            if part == ".git":
                return (
                    RiskLevel.HIGH_SENSITIVE,
                    f"目标路径触碰版本控制系统元数据：{resolved}",
                )
            if part.startswith(".env"):
                return (
                    RiskLevel.HIGH_SENSITIVE,
                    f"目标路径触碰密钥凭据敏感文件：{resolved.name}",
                )

        # 检查是否试图改写 .logox 核心只读配置
        if ".logox" in parts and resolved.name in _SENSITIVE_CONFIGS:
            return (
                RiskLevel.HIGH_SENSITIVE,
                f"目标路径触碰只读核心配置文件：{resolved.name}",
            )

        return RiskLevel.NORMAL, ""

    def audit_tool_args(
        self, tool_name: str, args: Dict[str, Any]
    ) -> Tuple[RiskLevel, str]:
        """从工具参数中提取关键路径并执行沙箱审计。"""
        if not isinstance(args, dict):
            return RiskLevel.NORMAL, ""

        # 常见路径参数名
        target_path = None
        for key in ("path", "file_path", "target_file", "file", "dir", "cwd"):
            if key in args and args[key]:
                target_path = args[key]
                break

        if target_path:
            return self.audit_path(target_path)

        # 特殊：对 shell 命令做基本敏感路径探测（如包含 .git/ 或 .env）
        if tool_name == "shell":
            cmd = str(args.get("command", "") or "")
            for sensitive in (".git", ".env"):
                if sensitive in cmd:
                    return (
                        RiskLevel.HIGH_SENSITIVE,
                        f"终端命令文本中包含敏感资源标记 ({sensitive})",
                    )

        return RiskLevel.NORMAL, ""
