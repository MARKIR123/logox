"""自研渲染层的组件（D80）。

每个组件的契约都一样：``render(width) -> list[Text]``，且**每行不超宽**。
这份简单是刻意的——Pi 的整套界面就建立在三个方法的协议上。
"""

from __future__ import annotations

from logox.tui.render.components.text import CURSOR_MARKER, Box, TextComponent, TruncatedText

__all__ = ["CURSOR_MARKER", "Box", "TextComponent", "TruncatedText"]
