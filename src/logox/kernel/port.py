"""界面与内核之间的**窄契约**（M4 / D55 / B1 / B3）。

为什么需要这个文件（而不是让界面直接 `import KernelLoop`）
=======================================================

**它解决什么问题**：全屏界面需要"提交一句话、取消当前回合、看看到底忙不忙"这三件事。
如果它直接依赖 :class:`~logox.kernel.loop.KernelLoop` 这个具体类，会产生两个后果：

1. **界面测试必须装齐 Provider SDK**——因为构造 `KernelLoop` 要一个 provider 实例。
   于是"测一个按钮按下去有没有反应"变成了一件要装 openai + anthropic 的事。
2. **界面会自然地开始越界**——既然 `kernel` 对象在手边，`kernel.history`、`kernel._turns`
   看起来都很方便。而这些是内核的实现细节，读它们等于把界面与内核焊死
   （`ARCHITECTURE.md` 规则 R3：界面不得持有内核可变状态）。

**没有它会怎样**：`tui/` 会 import `kernel/loop.py`，`tests/unit/test_imports.py` 那套
"UI 不碰实现层"的断言就得整体作废——而那套断言是 D55 选择装配根方案的全部理由。

**它怎么做**：把界面**允许用的全部能力**收进一个三成员的协议（Protocol，即"我不管你
是哪个类，只要你有这几个成员就行"）。

刻意**不放**进来的东西（每一条都有理由）
----------------------------------------

============================ ==========================================================
成员                          为什么不给界面
============================ ==========================================================
``submit(text)``             它会**等到回合结束**。界面用了它，回合期间事件循环被这个
                              ``await`` 占住，**Esc 根本排不上队**——而这正是
                              ``start()`` 存在的理由（见 ``MODULE_kernel_loop.md`` §4.0）
``history``                   内核拥有、只读。要显示历史就走事件；要落盘是 M8 的事
``turn_index``                状态栏用的是 ``TurnFinished.turn_index``（**走事件**），
                              D39 的"内核不为状态栏新增代码"就体现在这里
``registry`` / ``provider``   那是装配根该拿的东西。界面一旦能拿到注册表，
                              "界面不执行工具"这条边界就只剩一句口头承诺了
============================ ==========================================================

**可选能力**（``set_thinking``，D58）不进协议：界面用
``getattr(kernel, "set_thinking", None)`` 探测。这样"只有三个成员的假内核"仍然算合格实现，
界面测试不必为一个可选功能付出代价。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

__all__ = ["KernelPort"]


@runtime_checkable
class KernelPort(Protocol):
    """界面能用的**全部**内核能力。

    实现者：:class:`logox.kernel.loop.KernelLoop`（生产）、``tests/tui/tui_support.FakeKernel``
    （界面测试）。两者都满足本协议，因此**同一批界面用例可以在两种内核上跑**
    ——这正是"界面不依赖内核实现"的可执行证明。
    """

    async def start(self, text: str) -> Any:
        """开一个回合并**在后台跑**，立刻返回 ``Turn``（真实类型，此处用 Any 避免反向依赖）。

        :raises TurnInProgressError: 已有回合在进行中。调用方**不应依赖这个异常**
            做流程控制——正常路径是"忙就入队"（D41），异常只在竞态时兜底。
        """
        ...

    def cancel(self) -> bool:
        """取消当前回合。**幂等**；返回 ``True`` 表示"这次真的取消了点什么"。

        返回值是给界面用的：它决定要不要给用户一句"已中断"的反馈
        （空操作时说"已中断"是在骗人）。
        """
        ...

    @property
    def current_turn(self) -> Any | None:
        """正在跑的回合，没有则 ``None``（空闲判定）。"""
        ...
