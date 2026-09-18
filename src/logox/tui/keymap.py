"""快捷键表——**界面与文档的唯一事实来源**。

为什么单独成模块
----------------
键位提示曾经是硬编码在提示行里的一句字符串，与 ``UI-SPEC.md`` 是两份独立数据，
必然漂移。现在 **代码里的 :data:`KEYMAP` 是权威**，``/help`` 从它渲染，
测试再断言它与这份表一致。于是"改了键位但忘了改文档"会立刻被测试抓住。

⚠️ **这张表只描述真实存在的键**。历史上它列过一整套侧栏与折叠快捷键
（``Ctrl+2…6``、``Ctrl+H``、``Ctrl+F``、``Ctrl+B``、``滚轮``）——那些是 Textual
全屏界面的东西，随 D85 一起删掉了。**帮助里写着一个按了没反应的键，
比少写一个键糟糕得多**：用户会以为是自己按错了。
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["KEYMAP", "KEYMAP_SECTIONS", "KeyBinding", "format_keymap", "section_of"]


@dataclass(frozen=True)
class KeyBinding:
    """一条键位。``keys`` 是显示出来的按键，``action`` 是行为描述。"""

    keys: str
    action: str
    section: str


KEYMAP: tuple[KeyBinding, ...] = (
    # -- 输入 ----------------------------------------------------------- #
    KeyBinding("Enter", "发送", "输入"),
    KeyBinding("Alt+Enter / Ctrl+J", "换行（Ctrl+J 是通用备选键）", "输入"),
    KeyBinding("← → ↑ ↓ Home End", "移动光标（空输入时 ↑↓ 翻历史）", "输入"),
    KeyBinding("Ctrl+W / Alt+退格", "删掉前一个词", "输入"),
    KeyBinding("Ctrl+U / Ctrl+K", "删到行首 / 行尾", "输入"),
    KeyBinding("Ctrl+A / Ctrl+E", "跳到行首 / 行尾", "输入"),
    KeyBinding("粘贴", "一次粘贴 = 一次插入（不会逐条发出去）", "输入"),
    # -- 会话 ----------------------------------------------------------- #
    KeyBinding("Esc", "关掉浮层（有筛选时先清筛选）", "会话"),
    KeyBinding("Ctrl+C", "生成中中断；空闲时**按两下**退出", "会话"),
    KeyBinding("Ctrl+D", "退出", "会话"),
    # 复制**不在本表里**：Logox 不启用鼠标追踪（D78），选择与复制由终端自己做
    # —— 和 bash 里一样（拖选、Ctrl+Shift+C / Ctrl+Insert 都是终端的键）。
    # -- 浏览 ----------------------------------------------------------- #
    KeyBinding("滚轮 / Shift+PgUp", "往上翻（用**终端自己**的回滚缓冲）", "浏览"),
    KeyBinding("拖选 / Ctrl+Shift+C", "选中与复制（终端原生，Logox 不接管鼠标）", "浏览"),
    # -- 浮层 ----------------------------------------------------------- #
    KeyBinding("↑ ↓", "在选项之间移动", "浮层"),
    KeyBinding("1 … 9", "数字直达（选第几项）", "浮层"),
    KeyBinding("直接打字 / Backspace", "筛选（选择器）· 删一个字符", "浮层"),
    KeyBinding("Enter / Esc", "确认 / 取消（权限弹窗里 **Esc = 拒绝**）", "浮层"),
    KeyBinding("PgUp / PgDn", "滚动长内容（帮助、参数、事件流）", "浮层"),
)

KEYMAP_SECTIONS: tuple[str, ...] = ("输入", "会话", "浏览", "浮层")
"""帮助里的分组顺序。"""


def section_of(name: str) -> tuple[KeyBinding, ...]:
    return tuple(item for item in KEYMAP if item.section == name)


def format_keymap(*, key_width: int = 22) -> list[tuple[str, str, str]]:
    """把键位表摊平成 ``(分组, 按键, 行为)`` 三元组，供渲染层使用。

    放在这里而不是渲染层，是为了让"帮助文本内容"可以在无界面的环境里断言。
    """
    rows: list[tuple[str, str, str]] = []
    for name in KEYMAP_SECTIONS:
        for index, item in enumerate(section_of(name)):
            rows.append((name if index == 0 else "", item.keys, item.action))
    del key_width  # 列宽由渲染层按可用宽度决定（帮助支持双栏 / 紧凑两种布局）
    return rows
