"""`/` 补全的**渲染组件**：把 `CompletionState` 交给 `content.completion.render_completion`。

⚠️ **它不是浮层**（D169 修正）：由 `AppLayout` 插在「时间线」与「输入框」之间绘制 ——
输入框永远在它下方、永远不被遮住。早先做成浮层的后果是：`AppLayout` 一见浮层就让出输入区，
列表于是正好画在输入框原来的位置上（用户报障："不能遮挡输入框中的内容"）。
"""

from __future__ import annotations

from typing import Any

from logox.tui.content.completion import render_completion
from logox.tui.content.overlay import PickerState

__all__ = ["CompletionComponent"]


class CompletionComponent:
    """薄适配层：布局只需要一个 `render(width) -> list[Text]` 的东西。"""

    def __init__(self, state: PickerState, palette: Any) -> None:
        self.state = state
        self.palette = palette

    def render(self, width: int) -> list[Any]:
        # ★ D165：**整宽**（用户要求「所有提示框一律整宽展示」）——
        #   与 picker / confirm 同口径（`components/overlay.py::_box_width` 也是整宽）。
        #   `frame_box` 内部仍会按 cell 精算，窄终端不会溢出。
        return render_completion(self.state, self.palette, width=width)

    def handle_input(self, key: Any) -> bool:
        """不消费按键（键位决策在 app）。保留此方法是防呆：万一被塞进焦点链也别崩。"""
        del key
        return False
