"""构建标识（D137）：让"我现在跑的到底是哪一版代码"变成**一眼可见**的事。

为什么需要它
============
本项目已经**三次**出现同一类错觉：Agent 改完代码说"已修复"，用户在终端里却看到旧行为。
根因不是谁犯错 —— 而是 **Python 进程不会热加载改过的模块**：
只要 logox 还开着，它跑的就是**启动那一刻**载入的代码，哪怕源文件已经变了。

这类错觉的代价特别大：它会让用户去怀疑"是不是没修好"，于是又一轮排查——
而真相只是**没重启**。（实测两次：`summary_reason` 字段没落盘、工具卡展开内容为空，
都是"代码已改、进程还是旧的"。）

做法
====
不引入版本号管理（那需要发版流程），只报**源码最后改动时间**：
``src/logox`` 下所有 ``.py`` 的**最新 mtime**。它回答的正是用户的问题——
"我改完那一刻之后的代码，跑起来了吗？"

代价（明确登记）：目录扫描一次约几毫秒，且**只在启动时算一次**
（`code_stamp` 缓存结果）。如果将来改成"每次调用都扫"，那点开销会出现在状态行刷新里。
"""

from __future__ import annotations

import time
from pathlib import Path

__all__ = ["code_root", "code_stamp"]

#: 缓存：目录扫描过一次就不再算（启动时调用一次即可）
_CACHED: str | None = None


def code_root() -> Path:
    """被监视的源码树根：``src/logox``（本模块在 ``logox/tui/`` 下，跟着项目源码走）。"""
    return Path(__file__).resolve().parent.parent


def code_stamp(*, refresh: bool = False) -> str:
    """源码最后改动时间的短标识，例如 ``09-19 23:58``；取不到时返回 ``?``。

    为什么用 mtime 而不是 git 短哈希：**没有 git 也要能用**（用户可能拿到的是压缩包），
    而且 mtime 直接对应"你什么时候让我改的"这件事，比哈希好读。
    """
    global _CACHED
    if _CACHED is not None and not refresh:
        return _CACHED
    try:
        latest = max(
            (path.stat().st_mtime for path in code_root().rglob("*.py")),
            default=0.0,
        )
    except OSError:  # pragma: no cover - 权限/路径异常时不该让界面起不来
        latest = 0.0
    _CACHED = time.strftime("%m-%d %H:%M", time.localtime(latest)) if latest else "?"
    return _CACHED
