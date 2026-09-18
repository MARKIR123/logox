"""Logox 内核层（D7：事件总线内核）。

本包是**唯一的主干**：Agent 循环只发布事件，权限 / 日志 / 渲染 / 压缩 /
持久化 / 钩子全部是订阅者。

硬约束（ARCHITECTURE.md 规则 R2）：本包**禁止** import
``textual`` / ``rich`` / ``providers`` / ``store`` / ``tui`` / ``permissions``
等具体实现——它必须能在一个没有 TUI、没有 Provider 的进程里被 import 与测试。
"""

from __future__ import annotations

__all__: list[str] = []
