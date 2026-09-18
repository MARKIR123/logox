"""极简 TOML writer（D30，约 60 行有效代码）。

为什么自写
----------
Python 标准库的 ``tomllib`` **只读不写**，而 Logox 需要把运行时状态写回磁盘
（``/model``、``/theme``、``/effort``、Shell 探测缓存、学习到的权限规则）。
为把依赖控制在预算内（D28），这里只实现本项目用到的语法子集。

支持的类型子集
--------------
``str`` / ``bool`` / ``int`` / ``float`` / ``list[标量]`` / ``dict[str, T]``（表）
/ ``list[dict]``（数组表）。``None`` 表示**跳过该键**（TOML 无 null）。

其余一律抛 :class:`~logox.errors.TomlWriteError`——**绝不静默降级**，因为写出
格式错误的 TOML 会污染用户的配置文件。

两条必须守住的规则
------------------
1. **同一张表内，标量/数组必须先输出，子表与数组表必须后输出。**
   否则后续标量会被解析器归入前面刚打开的子表，造成**静默的语义漂移**。
2. **子表与数组表的路径前缀必须层层传递**：数组表项内部的子表要写成
   ``[parent.child]``，否则生成的是非法 TOML（数组表项会被"拔"到根层级）。
3. **``bool`` 必须早于 ``int`` 判定**——Python 的 ``bool`` 是 ``int`` 的子类，
   顺序写反会把 ``True`` 写成 ``1``。
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from logox.errors import TomlWriteError

__all__ = ["dumps"]

_BARE_KEY_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")

_ESCAPES = {
    '"': '\\"',
    "\\": "\\\\",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
    "\b": "\\b",
    "\f": "\\f",
}


def dumps(data: Mapping[str, Any]) -> str:
    """把嵌套 dict 序列化为 TOML 文本（输出顺序 = 输入 dict 的插入顺序）。

    输出**逐字节稳定**：同一个对象重复序列化结果完全一致，便于用户 diff，
    也便于测试断言。
    """
    lines: list[str] = []
    _emit_body(lines, data, prefix=(), key_path="")
    if not lines:
        return ""
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# 内部实现
# --------------------------------------------------------------------------- #


def _emit_body(
    lines: list[str],
    table: Mapping[str, Any],
    *,
    prefix: tuple[str, ...],
    key_path: str,
) -> None:
    """输出一张表的内容（**不含**本表的头部，头部由调用方负责）。

    ``prefix`` 是本表在文档中的完整路径，用于给子表/数组表生成正确的头部。
    """
    scalars: list[tuple[str, Any]] = []
    sub_tables: list[tuple[str, Mapping[str, Any]]] = []
    array_tables: list[tuple[str, list[Mapping[str, Any]]]] = []

    for key, value in table.items():
        if value is None:
            continue  # TOML 无 null：跳过该键
        if isinstance(value, Mapping):
            sub_tables.append((key, value))
        elif _is_array_of_tables(value):
            array_tables.append((key, value))
        else:
            scalars.append((key, value))

    for key, value in scalars:
        lines.append(f"{_key(key)} = {_value(value, f'{key_path}{key}')}")

    for key, child in sub_tables:
        _blank(lines)
        lines.append(f"[{_dotted((*prefix, key))}]")
        _emit_body(lines, child, prefix=(*prefix, key), key_path=f"{key_path}{key}.")

    for key, items in array_tables:
        for index, item in enumerate(items):
            _blank(lines)
            lines.append(f"[[{_dotted((*prefix, key))}]]")
            _emit_body(lines, item, prefix=(*prefix, key), key_path=f"{key_path}{key}[{index}].")


def _blank(lines: list[str]) -> None:
    """在段落之间插入一个空行（文档开头不加）。"""
    if lines and lines[-1] != "":
        lines.append("")


def _is_array_of_tables(value: Any) -> bool:
    return isinstance(value, (list, tuple)) and len(value) > 0 and all(isinstance(i, Mapping) for i in value)


def _dotted(parts: tuple[str, ...]) -> str:
    return ".".join(_key(part) for part in parts)


def _key(key: str) -> str:
    if key and all(char in _BARE_KEY_CHARS for char in key):
        return key
    return _quote(key)


def _value(value: Any, key_path: str) -> str:
    # 顺序至关重要：bool 是 int 的子类，必须先判 bool。
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise TomlWriteError("TOML 不支持 nan / inf", key_path)
        return repr(value)
    if isinstance(value, str):
        return _quote(value, key_path)
    if isinstance(value, (list, tuple)):
        return _array(value, key_path)
    raise TomlWriteError(f"不支持的类型 {type(value).__name__}", key_path)


def _array(items: Any, key_path: str) -> str:
    rendered: list[str] = []
    for index, item in enumerate(items):
        if isinstance(item, (Mapping, list, tuple)):
            raise TomlWriteError(
                "数组内不支持嵌套的表或数组（请改用数组表 list[dict]）",
                f"{key_path}[{index}]",
            )
        rendered.append(_value(item, f"{key_path}[{index}]"))
    return "[" + ", ".join(rendered) + "]"


def _quote(text: str, key_path: str = "") -> str:
    """基本字符串：转义必需字符，**CJK 原样输出 UTF-8**（保证文件可读）。"""
    out: list[str] = ['"']
    for char in text:
        escaped = _ESCAPES.get(char)
        if escaped is not None:
            out.append(escaped)
            continue
        if ord(char) < 0x20:
            # 静默丢弃控制字符会让内容悄无声息地损坏，必须报错。
            raise TomlWriteError(f"字符串包含无法转义的控制字符 U+{ord(char):04X}", key_path)
        out.append(char)
    out.append('"')
    return "".join(out)
