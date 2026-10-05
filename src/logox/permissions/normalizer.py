"""命令清洗与规范化器（第 1 层：Normalization & Sanitization）。

负责将用户或模型生成的任意原始 Shell 命令清洗为统一格式：
1. 检测复合命令符号（管道符 ``|``、逻辑与/或 ``&&`` / ``||``、分号 ``;``、输出重定向 ``>`` / ``>>``），
   将复合命令标记为 ``is_compound=True``，剥夺静默放行资格，强制降级至 HITL；
2. 提取核心指令前缀（如 ``git status``、``pytest``、``python -m pytest``），
   实现跨平台的稳定前缀规则匹配。
"""

from __future__ import annotations

import shlex

__all__ = ["is_compound_command", "normalize_shell_command"]

#: 触发复合命令降级的操作符列表（仅在引号外部有效）
_COMPOUND_OPERATORS = {"|", "&&", "||", ";", ">", ">>", "&"}

#: 常见的前缀穿透解释器/执行器
_WRAPPER_EXECUTABLES = {
    "python",
    "python.exe",
    "python3",
    "py",
    "uv",
    "node",
    "node.exe",
    "npx",
}


def is_compound_command(tokens: list[str]) -> bool:
    """检测 Token 序列中是否包含复合操作符。

    因为 tokens 已经通过 shlex 解析，引号包裹的字面量内部的符号
    （如 ``git commit -m "fix | bug"`` 或 ``echo "1; 2"``）不会被误判。
    """
    for token in tokens:
        raw = token.strip()
        if not raw:
            continue
        # 如果是被成对引号完整包裹的字面量，跳过检测
        if (raw.startswith('"') and raw.endswith('"') and len(raw) >= 2) or (
            raw.startswith("'") and raw.endswith("'") and len(raw) >= 2
        ):
            continue
        # 只要未加引号的词法单元中出现了分号、管道、逻辑运算符或重定向
        if any(op in raw for op in (";", "|", "&", ">")):
            return True
    return False


def normalize_shell_command(command: str) -> tuple[str, list[str], bool]:
    """清洗并规范化 Shell 命令。

    :param command: 模型生成的原始命令行字符串
    :return: ``(normalized_prefix, tokens, is_compound)``
        - ``normalized_prefix``: 规范化提取出的主命令及关键子命令前缀（小写标准形态）
        - ``tokens``: 解析后的词法单元列表
        - ``is_compound``: 是否为复合命令
    """
    raw = (command or "").strip()
    if not raw:
        return "", [], False

    try:
        # 使用 posix=False 以在 Windows 环境下正确识别反斜杠与 Windows 样式双引号
        tokens = shlex.split(raw, posix=False)
    except Exception:
        # 语法解析异常（例如未闭合引号），直接退化为简单空白分割，并标记为复合/不安全
        tokens = raw.split()
        return tokens[0].lower() if tokens else "", tokens, True

    if not tokens:
        return "", [], False

    # 剥离 token 外层可能保留的双引号
    clean_tokens = [t.strip('"\'') for t in tokens]
    clean_tokens = [t for t in clean_tokens if t]

    if not clean_tokens:
        return "", [], False

    is_compound = is_compound_command(tokens)

    # 提取关键命令前缀
    first = clean_tokens[0].lower()
    prefix_parts = [first]

    # 特殊穿透逻辑 1: python -m <module>
    if first in _WRAPPER_EXECUTABLES:
        if len(clean_tokens) >= 3 and clean_tokens[1] == "-m":
            prefix_parts = [first, "-m", clean_tokens[2].lower()]
        elif len(clean_tokens) >= 2 and not clean_tokens[1].startswith("-"):
            prefix_parts = [first, clean_tokens[1].lower()]
    # 特殊穿透逻辑 2: git / npm / cargo 等具备核心二级子命令的工具
    elif first in {"git", "npm", "cargo", "docker", "pnpm", "yarn"} and len(clean_tokens) >= 2 and not clean_tokens[1].startswith("-"):
        prefix_parts = [first, clean_tokens[1].lower()]

    normalized_prefix = " ".join(prefix_parts)
    return normalized_prefix, clean_tokens, is_compound
