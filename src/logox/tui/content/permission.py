"""权限询问的**界面侧数据**（Textual-free / 内核-free）。

为什么单独一个模块
================

"要不要允许执行这个工具"这件事横跨三层：

============================ ==================================================
内核（L3）                   ``PermissionDecider.decide()`` 返回 allow / deny / ask
装配根（L2，``logox/app.py``）把内核的 ask 翻译成一次**界面提问**
界面（L4，``tui/render``）    把提问画成弹窗、把用户的四个选择之一交回去
============================ ==================================================

于是需要一个**三层都能看见、但不属于任何一层**的数据形状：本模块的
:class:`PermissionAsk`（问什么）与 :class:`PermissionChoice`（答什么）。

**为什么不能直接用内核的 ``ev.PermissionRequested``**：那会让界面 import
``logox.kernel.events`` 的具体字段并**依赖它的形状**——今天只是多读一个字段，
明天就会有人顺手 `import logox.kernel.scheduler` 去拿枚举（`tests/unit/test_kernel_port.py`
的 B1/B2 红线正是为此存在的）。事件是**广播**，这个是**一次问答**，两者生命周期也不同。

四个选项与 UI-SPEC §5.8 一一对应
==============================

===================== ============================== ================================
选项                   返回                           落点
===================== ============================== ================================
``[1] 仅本次允许``     :attr:`PermissionChoice.ONCE`   只放行这一次调用
``[2] 本会话总是允许`` :attr:`PermissionChoice.SESSION` 记在内存里，退出即失效
``[3] 拒绝``（默认焦点）:attr:`PermissionChoice.DENY`   **安全默认**：误按 Enter 是拒绝
``[4] 持久允许``       :attr:`PermissionChoice.PROJECT` 写进 ``state.toml``（D10 的 learn_permission）
===================== ============================== ================================
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

__all__ = ["PermissionAsk", "PermissionChoice"]


class PermissionChoice(str, Enum):
    """用户在权限弹窗里做的选择。

    取值刻意与 ``ev.PermissionResolved.remember`` 的词汇表对齐
    （``once`` / ``session`` / ``project``），这样装配根翻译时不需要一张映射表
    ——多一张映射表就多一处会写错的地方。
    """

    ONCE = "once"
    SESSION = "session"
    DENY = "deny"
    PROJECT = "project"

    @property
    def allowed(self) -> bool:
        return self is not PermissionChoice.DENY

    @property
    def remember(self) -> str | None:
        """对应 ``PermissionResolved.remember``：拒绝时没有"记住"这回事。"""
        return None if self is PermissionChoice.DENY else self.value


@dataclass(frozen=True)
class PermissionAsk:
    """一次权限提问的全部内容（**纯数据，可以直接打印出来看**）。

    字段与 UI-SPEC §5.8 的"必需信息"逐条对应：工具名、**完整参数**、
    命中规则及来源、工作目录。其中规则两项在 M5 的规则引擎落地前是空的——
    **空就不显示那一行**，而不是编一个看起来很像的规则名（编出来的东西
    会让用户以为自己看懂了）。

    ``allow_session`` / ``allow_project`` 让界面知道哪两个选项**真的能兑现**：
    没有 ``state.toml`` 时"持久允许"是句空话，那种选项不该出现在屏幕上。
    """

    tool: str
    #: 完整参数（多行，**绝不截断**；弹窗内部滚动）
    detail: str = ""
    #: 命中规则（M5 之前为空）
    rule: str = ""
    #: 规则来源，如"内置默认" / "项目 state.toml"
    rule_scope: str = ""
    cwd: str = ""
    #: ``"high"`` 时弹窗顶部加一条警示（UI-SPEC §5.8）
    risk: str = "normal"
    #: 警示条的一句话说清**为什么**算高危
    risk_note: str = ""
    allow_session: bool = True
    allow_project: bool = True
    #: 调用 id（事件配对用；界面不关心，装配根要用）
    call_id: str = field(default="")
