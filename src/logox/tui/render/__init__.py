"""自研渲染层（D80 / MODULE_tui_render）。

这一层的定位：**把"要显示什么"变成"往终端写什么字节"**，且不依赖 Textual。
内核、事件总线、Provider 与它无关——它们之间的契约是 `KernelPort` 与事件模型。
"""

from __future__ import annotations
