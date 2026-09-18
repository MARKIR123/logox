"""工作区路径 Slugging 与短哈希唯一映射（D98）。

对齐 Pi Agent 与 Claude Code 的路径转义规则：
1. 规范化路径为绝对字符串（去除末尾分隔符，Windows 保持大小写规范）；
2. 非英文字母、数字和短横线全部替换为 '-'；
3. 两端包裹 '--'（如 '--G-hz-codes-Logox--'）；
4. 计算规范路径字符串的 SHA256 哈希，截取前 8 位作为后缀（防碰撞守卫）；
5. 最终生成 '--G-hz-codes-Logox--_8f3a1c2d' 格式。
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

__all__ = ["slugify_cwd", "unslug_cwd_hint"]

_ILLEGAL_CHAR_RE = re.compile(r"[^A-Za-z0-9\-]+")
_CONSECUTIVE_DASHES_RE = re.compile(r"-+")


def slugify_cwd(cwd: Path | str) -> str:
    """把任意平台的工作区绝对路径转义为安全、唯一、人类可读的目录分桶名。"""
    if isinstance(cwd, str) and cwd.startswith("/"):
        path_str = cwd.rstrip("/")
    else:
        try:
            resolved = Path(cwd).resolve()
            path_str = str(resolved).rstrip("/\\")
        except OSError:
            path_str = str(cwd).rstrip("/\\")

    # 计算防碰撞短哈希（8 位）
    digest = hashlib.sha256(path_str.encode("utf-8", errors="replace")).hexdigest()[:8]

    # 将非法字符替换为短横线
    cleaned = _ILLEGAL_CHAR_RE.sub("-", path_str)
    cleaned = _CONSECUTIVE_DASHES_RE.sub("-", cleaned).strip("-")

    if not cleaned:
        cleaned = "root"

    return f"--{cleaned}--_{digest}"


def unslug_cwd_hint(slug: str) -> str:
    """从 slug 中提取人类可读的路径提示（去除两端 -- 与 _<hash>）。"""
    name = slug
    if "_" in name:
        name = name.rsplit("_", 1)[0]
    return name.strip("-")
