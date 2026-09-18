"""组件协议与容器（D80 / MODULE_tui_render §3.2）。

与 Pi 的关系
------------
Pi 的组件协议只有三个方法：

.. code-block:: typescript

   interface Component {
     render(width: number): string[];
     handleInput?(data: string): void;
     invalidate?(): void;
   }

本模块保持同样的形状（Python 版），只有一处**刻意的差异**：
``handle_input`` 收的是**已解析的 :class:`~logox.tui.render.keys.Key`**，
而不是原始字节。理由见 `MODULE_tui_render.md` §6.3：解析集中到一处才可测，
且 Windows 与 POSIX 的字节形态并不完全一致。

那条唯一的硬约束
----------------
``render(width)`` 返回的**每一行的可见宽度必须 <= width**。

**为什么这条约束这么重要**：终端遇到超宽行的处理是**折行**（不是裁掉）。
一旦某一行折了，它下面**所有行的位置全部错位**——用户看到的是"整个界面花了"，
而不是"某一行有问题"。这类故障极难从症状反推原因。

因此本模块提供 :func:`assert_lines_fit`，让所有组件的测试都能复用同一套断言。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from rich.text import Text

from logox.tui.render.ansi import visible_width
from logox.tui.render.keys import Key

__all__ = [
    "Component",
    "Container",
    "Spacer",
    "assert_lines_fit",
    "fit_lines",
]


@runtime_checkable
class Component(Protocol):
    """一个可以渲染成"若干行文本"的东西。"""

    def render(self, width: int) -> list[Text]:
        """产出这一帧的全部行。**每行宽度必须 <= width**（见模块 docstring）。"""
        ...

    def handle_input(self, key: Key) -> bool:
        """收到按键。返回 ``True`` 表示已消费（不再往上冒泡）。"""
        ...

    def invalidate(self) -> None:
        """作废缓存。下一次 ``render`` 必须从头算。

        **谁改数据谁调用它**——这与 `TimelineBuffer.invalidate` 是同一条纪律：
        漏调的后果是"显示的是旧内容，而且没有任何提示"。
        """
        ...


class Container:
    """按顺序垂直堆叠子组件（对应 Pi 的 ``Container``）。

    没有布局引擎：主屏模式下的界面就是"一条竖着的流"，
    每个子组件拿到**同样的宽度**、产出自己的行。这比 CSS 布局简单得多，
    也正是 Pi 那 1077 行能覆盖全部渲染的原因。
    """

    def __init__(self) -> None:
        self.children: list[Component] = []

    def add(self, component: Component) -> None:
        self.children.append(component)

    def remove(self, component: Component) -> None:
        if component in self.children:
            self.children.remove(component)

    def clear(self) -> None:
        self.children.clear()

    def render(self, width: int) -> list[Text]:
        lines: list[Text] = []
        for child in list(self.children):  # 复制：渲染期间子组件可能增删自己
            lines.extend(child.render(width))
        return lines

    def handle_input(self, key: Key) -> bool:
        """交给**最后一个**愿意处理它的子组件。

        为什么从后往前：后面的组件在视觉上"在下面"、通常是输入区，
        而输入区应当优先拿到按键。这与 Pi 的焦点机制结果一致，
        但省掉了完整的焦点栈（我们只有一个可聚焦组件）。
        """
        return any(child.handle_input(key) for child in reversed(self.children))

    def invalidate(self) -> None:
        for child in self.children:
            child.invalidate()


class Spacer:
    """固定高度的空白（对应 Pi 的 ``Spacer``）。

    Pi 在消息之间插入 ``Spacer(1)`` 来分隔；我们也一样——
    **间距是布局的一部分**，不该散落成各组件里的 ``"\\n"``。
    """

    def __init__(self, height: int = 1) -> None:
        self.height = max(0, height)

    def render(self, width: int) -> list[Text]:
        return [Text() for _ in range(self.height)]

    def handle_input(self, key: Key) -> bool:
        return False

    def invalidate(self) -> None:
        return None


# --------------------------------------------------------------------------- #
# 行宽不变量（本模块最重要的工具）
# --------------------------------------------------------------------------- #


def fit_lines(lines: list[Text], width: int) -> list[Text]:
    """把超宽的行**硬切**到 ``width`` 以内（渲染器的兜底，不丢内容）。

    渲染器在把行交给终端**之前**会调用它。为什么要兜底而不是"相信组件"：
    组件里一个算错的宽度就会让整个界面错位（见模块 docstring），
    而**当场切掉一行**的代价远小于"界面花了"。
    """
    if width <= 0:
        return [Text() for _ in lines]
    out: list[Text] = []
    for line in lines:
        out.extend(_split_hard(line, width))
    return out


def _split_hard(line: Text, width: int) -> list[Text]:
    """按 **cell 宽度**把一行切成若干行，样式跟着字符走。

    ⚠️ 两处都必须处理，否则会"看着能用但掉细节"：

    1. **基样式**（``line.style``）要跟着每一段走。它不在 ``spans`` 里，
       只复制 spans 会让 ``Text("x", style="on #282832")`` 切分后**底色全丢**。
    2. **单个字符就比 width 还宽**（中文占 2 格、width=1）时，**宽度不变量优先**：
       宁可丢一个字符（并说明），也不能返回超宽行——超宽会让整个界面错位，
       而丢一个字符在 width=1 这种病态宽度下是可以接受的。
    """
    if visible_width(line.plain) <= width:
        return [line]

    chunks: list[Text] = []
    current = Text(style=line.style)
    used = 0
    for index, char in enumerate(line.plain):
        char_width = visible_width(char)
        if char_width > width:
            continue  # 这个字符在任何一行里都放不下 → 跳过（保住宽度不变量）
        if used + char_width > width:
            chunks.append(current)
            current = Text(style=line.style)
            used = 0
        current.append(char)
        for span in line.spans:
            if span.start <= index < span.end:
                current.stylize(span.style, len(current.plain) - 1, len(current.plain))
        used += char_width
    chunks.append(current)
    return chunks


def assert_lines_fit(lines: list[Text], width: int, *, where: str = "") -> None:
    """断言每一行都不超宽，**并且没有丢内容**。

    「不丢内容」这一半经常被忽略：为了让行变窄，最容易犯的错是**截断**——
    而截断在用户那里的表现是"话说到一半没了"，比超宽更难查。
    因此这里同时比对"非空白字符序列"是否守恒。
    """
    for index, line in enumerate(lines):
        actual = visible_width(line.plain)
        if actual > width:
            raise AssertionError(f"{where}第 {index} 行超宽：{actual} > {width}；{line.plain!r}")
