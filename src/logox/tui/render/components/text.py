"""基础组件：``Box`` 与 ``Text`` / ``TruncatedText``（D80，对齐 Pi 的同名组件）。

这两个组件是"内容分层"的载体：Pi 的界面之所以一眼能分清"用户说的 / 工具 / 回答"，
靠的是 ``Box`` 给整块刷底色。因此 ``Box`` 的职责很明确——**内边距 + 底色**，
不做布局、不做换行（那是子组件的事）。
"""

from __future__ import annotations

from rich.text import Text

from logox.tui.render.ansi import visible_width
from logox.tui.render.keys import Key

__all__ = ["Box", "TextComponent", "TruncatedText"]

#: 零宽 APC 序列，告诉终端"硬件光标应该停在这里"。
#:
#: 为什么需要它：**中文输入法的候选窗位置取决于硬件光标**。
#: 终端里的输入框是"自己画的假光标"，硬件光标被隐藏了；不告诉终端真实位置的话，
#: 候选窗会弹在默认位置（通常是屏幕左上或右下），中文输入体验会明显变差。
#: 本序列零宽，不影响任何宽度计算——解析时才用得上（`render/screen.py` 负责抽掉它）。
CURSOR_MARKER = "\x1b_Ga=p\x1b\\"


class Box:
    """给子组件套一层**内边距 + 底色**（对应 Pi 的 ``Box``）。

    Pi 的构造是 ``new Box(paddingX, paddingY, bgFn)``；这里用同样的三个概念，
    但底色给的是**颜色字符串**而不是函数——我们的语义 token 已经是颜色值了。
    """

    def __init__(
        self,
        *,
        padding_x: int = 1,
        padding_y: int = 0,
        background: str | None = None,
    ) -> None:
        self.padding_x = max(0, padding_x)
        self.padding_y = max(0, padding_y)
        self.background = background
        self.children: list[object] = []

    def add(self, component: object) -> None:
        self.children.append(component)

    def clear(self) -> None:
        self.children.clear()

    def render(self, width: int) -> list[Text]:
        inner_width = max(1, width - self.padding_x * 2)
        rows: list[Text] = [Text() for _ in range(self.padding_y)]

        for child in self.children:
            # 只用 duck typing：`Box` 不关心子组件具体是什么，只要有 render(width)
            child_lines: list[Text] = child.render(inner_width)  # type: ignore[attr-defined]
            for line in child_lines:
                rows.append(self._decorate(line, width))

        rows.extend(Text() for _ in range(self.padding_y))
        return rows

    def _decorate(self, line: Text, width: int) -> Text:
        """给一行加内边距与底色，并**补到整宽**。

        必须补满：底色块随字数参差不齐的话，看起来像一排长短不一的色条
        （这个观感用户在"选中高亮"那次截图里见过）。
        """
        piece = Text(" " * self.padding_x)
        piece.append_text(line)
        piece.style = f"on {self.background}" if self.background else ""
        missing = width - visible_width(piece.plain)
        if missing > 0:
            piece.append(" " * missing)
        return piece

    def handle_input(self, key: Key) -> bool:
        for child in self.children:
            handler = getattr(child, "handle_input", None)
            if handler is not None and handler(key):
                return True
        return False

    def invalidate(self) -> None:
        for child in self.children:
            getattr(child, "invalidate", lambda: None)()


class TextComponent:
    """多行文本（对应 Pi 的 ``Text``）：**按宽度折行**，可选内边距。

    折行直接复用 `logox.tui.format.wrap_cells`——它已经过了 D68/D74 两轮打磨
    （不丢字、不重复、不超宽、中文断句优先标点）。**这是前一版留下的最有价值的资产。**
    """

    def __init__(
        self,
        text: str = "",
        *,
        padding_x: int = 0,
        padding_y: int = 0,
        style: str | None = None,
    ) -> None:
        self.text = text
        self.padding_x = max(0, padding_x)
        self.padding_y = max(0, padding_y)
        self.style = style

    def set_text(self, text: str) -> None:
        self.text = text
        self.invalidate()

    def render(self, width: int) -> list[Text]:
        from logox.tui import format as fmt

        inner = max(1, width - self.padding_x * 2)
        rows: list[Text] = [Text() for _ in range(self.padding_y)]

        if self.text:
            wrapped = fmt.wrap_cells(self.text, inner)
            for line in wrapped.split("\n"):
                piece = Text(" " * self.padding_x)
                piece.append(line, style=self.style or "")
                rows.append(piece)

        rows.extend(Text() for _ in range(self.padding_y))
        return rows

    def handle_input(self, key: Key) -> bool:
        return False

    def invalidate(self) -> None:
        return None


class TruncatedText(TextComponent):
    """单行、超宽就**截断**（对应 Pi 的 ``TruncatedText``）。

    用于状态行这种"必须只占一行"的地方。⚠️ 这里**可以**截断，
    因为它承载的是摘要信息（路径、数字），而不是需要完整读到的正文。
    """

    def render(self, width: int) -> list[Text]:
        from logox.tui import format as fmt

        inner = max(1, width - self.padding_x * 2)
        piece = Text(" " * self.padding_x)
        piece.append(fmt.clip(self.text, inner), style=self.style or "")
        missing = width - visible_width(piece.plain)
        if missing > 0:
            piece.append(" " * missing)
        return [piece]
