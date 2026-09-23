"""内容 → 文本（**纯函数，不依赖任何界面框架**）。

这一层解决什么问题
==================

一个 agent 界面要画的东西分两类：

* **内容**：工具卡片、助手正文（Markdown）、推理块、diff、选择器、帮助文本、
  状态行……它们都是"给定数据 + 宽度 + 调色板 → 若干行文本"；
* **怎么把它放到终端上**：光标移动、差分重画、滚动、按键解析。

本包只管前一半，:mod:`logox.tui.render` 管后一半。分界线划在这里的好处很直接：

1. **视觉规则可以脱离终端单测**——一条断言就是"给定宽度，第 3 行长什么样"，
   不需要真终端、不需要事件循环、不需要界面框架；
2. **换渲染引擎不用重写视觉**。这不是假想：本项目就换过一次
   （Textual 备用屏 → 自研主屏行式渲染器），当时这些文件**一行没改**，
   只换了调用它们的那一层。

为什么不 import Textual
======================

这些模块**一个字符都不该依赖 Textual**：它们是"内容"，与"谁来画"无关。
历史上它们住在 `tui/widgets/` 里（那个包名本身就意味着"界面组件"），
于是即使某个文件是纯函数，也被 `from textual...` 拖着——想在不装 Textual
的环境里跑新界面就不可能。

边界（`tests/unit/test_imports.py` 与 `test_kernel_port.py` 会验）：
**本包只允许 import 标准库、rich、pydantic 与 ``logox.config`` / ``logox.kernel`` /
``logox.tui``，以及根下的中立叶子模块**（``logox.errors`` / ``logox.paths`` / ``logox.difftext`` /
``logox.permission_types``）。不要在这里 import `textual`、`logox.providers`、`logox.tools`。

⚠️ **反过来也成立**（D140 新增 `test_kernel_port.py::T44`）：**下面各层不得 import 本包**。
纯数据类型（diff 的 hunks、权限问答的数据形状）必须放到根下的中立叶子里 ——
否则「工具/权限可以在没有界面的进程里独立运行」这条能力就被静默绑死了。
"""

from __future__ import annotations

__all__: list[str] = []
