"""键位表、帮助内容与斜杠命令的测试。

最重要的一条是**漂移守卫**：:data:`logox.tui.keymap.KEYMAP` 是权威，
帮助浮层与文档都从它渲染，测试断言"帮助里列出的每个键**真的有人处理**"。

⚠️ 这一条在 D85 精简时才真正有了牙齿：旧界面删掉之后，``KEYMAP`` 里还留着
一整套侧栏与折叠快捷键（``Ctrl+2…6``、``Ctrl+H``、``Ctrl+F``、``滚轮``），
而**没有任何代码处理它们**。帮助里写着一个按了没反应的键，比少写一个糟糕得多
——用户会以为是自己按错了。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from rich.cells import cell_len

from logox.tui import commands as command_module
from logox.tui.content.help import layout_for, render_help
from logox.tui.keymap import KEYMAP, KEYMAP_SECTIONS, format_keymap, section_of
from logox.tui.theme import load_theme

REPO_ROOT = Path(__file__).resolve().parents[2]
PALETTE = load_theme("logox-dark").palette

#: `keymap.py` 里列出的按键 → 新界面**真的处理**它的地方。
#:
#: 这张表是人工维护的，但它比"什么都不查"强得多：新增键位时若不在这里登记，
#: 用例会失败并逼作者回答"谁处理它"。它挡住的正是"D85 之后帮助里还剩一堆
#: 没人处理的键"那种情况。
HANDLED_BY: dict[str, str] = {
    "Enter": "Editor: enter → 提交（CR）；候选列表开着时 InlineApp._handle_completion_key 先补全再提交",
    "Tab": "InlineApp._handle_completion_key → 接受 `/` 补全（D162）",
    "Shift+Enter": "Editor: shift+enter → 换行（D130 恢复；需终端能区分修饰位，WT 上区分不了）",
    "Ctrl+Enter / Ctrl+J": "keys.parse_key：单独的 LF ⇒ Key(enter, ctrl) ⇒ 换行（D128；与 Pi 逐字节一致）",
    "\\ + Enter": "Editor._submit：行尾反斜杠 ⇒ 吃掉它并换行（D131；照 Pi 的变通搬来）",
    "← → ↑ ↓ Home End": "Editor: 方向键 / home / end（空输入时 ↑↓ 翻历史）",
    "Ctrl+W / Alt+退格": "Editor: ctrl+w / alt+backspace → 删一个词",
    "Ctrl+U / Ctrl+K": "Editor: ctrl+u / ctrl+k → 删到行首 / 行尾",
    "Ctrl+A / Ctrl+E": "Editor: ctrl+a / ctrl+e → 行首 / 行尾",
    "粘贴": "keys.KeyParser 的括号粘贴状态机 → Editor.insert_text",
    "Ctrl+O": "TimelineComponent.handle_input / InlineApp._dispatch → toggle_expand_tools",
    "Ctrl+T": "TimelineComponent.handle_input / InlineApp._dispatch → toggle_expand_reasoning",
    "Esc": "InlineApp._dispatch → 关浮层，其次中断生成",
    "Ctrl+C": "InlineApp._interrupt（空闲时按两下才退出）",
    "Ctrl+D": "InlineApp._dispatch → 退出",
    "滚轮 / Shift+PgUp": "**终端自己**（Logox 不接管鼠标，见 D78）",
    "拖选 / Ctrl+Shift+C": "**终端自己**（同上）",
    "↑ ↓": "PickerComponent / PermissionComponent 移动焦点",
    "1 … 9": "PickerComponent / PermissionComponent 数字直达",
    "直接打字 / Backspace": "PickerComponent 筛选 · PromptComponent 退格",
    "Enter / Esc": "各浮层的确认与取消",
    "PgUp / PgDn": "PanelComponent / PermissionComponent 滚动长内容",
}


class KeymapDriftGuardTests(unittest.TestCase):
    def test_every_key_in_the_keymap_is_actually_handled(self) -> None:
        """★★ 帮助里列出的每个键，都必须**真的有人处理**。

        这是 D85 那轮精简留下的最直接教训：旧的 ``KEYMAP`` 里有一整套侧栏与
        折叠快捷键，而处理它们的代码随旧界面一起删了——帮助于是变成了一份
        "按了没反应"的清单。**用户会以为是自己按错了。**
        """
        for item in KEYMAP:
            with self.subTest(keys=item.keys):
                self.assertIn(
                    item.keys,
                    HANDLED_BY,
                    f"键位 {item.keys!r} 没有登记处理者。要么去实现它，"
                    "要么把它从 KEYMAP 里删掉——不要留一个按了没反应的键。",
                )

    def test_handled_table_has_no_stale_entries(self) -> None:
        """反向也一样：登记表里不该有 KEYMAP 已经没有的键（否则它迟早误导人）。"""
        listed = {item.keys for item in KEYMAP}
        for keys in HANDLED_BY:
            with self.subTest(keys=keys):
                self.assertIn(keys, listed, f"{keys!r} 已不在 KEYMAP 里，登记表该同步删掉")

    def test_no_removed_interface_keys_are_advertised(self) -> None:
        """删掉的界面留下的键**不能**出现在帮助里（这是本次精简的具体目标之一）。"""
        advertised = "\n".join(f"{item.keys} {item.action}" for item in KEYMAP)
        for gone in ("Ctrl+2", "Ctrl+3", "Ctrl+H", "Ctrl+F", "Ctrl+B", "侧栏"):
            with self.subTest(gone=gone):
                self.assertNotIn(gone, advertised, f"帮助里还在提已经不存在的 {gone}")

    def test_sections_are_complete_and_ordered(self) -> None:
        self.assertEqual(KEYMAP_SECTIONS, ("输入", "会话", "浏览", "浮层"))
        for name in KEYMAP_SECTIONS:
            self.assertTrue(section_of(name), f"分组 {name} 是空的")

    def test_format_keymap_flattens_with_group_labels(self) -> None:
        rows = format_keymap()
        self.assertEqual(len(rows), len(KEYMAP))
        labelled = [row[0] for row in rows if row[0]]
        self.assertEqual(labelled, list(KEYMAP_SECTIONS), "每个分组的第一行才带标签")

    def test_keys_do_not_exceed_the_render_column(self) -> None:
        """按键列必须放得进帮助的双栏布局——放不下就会被裁掉（等于没写）。"""
        from logox.tui.content.help import KEY_COLUMN

        for item in KEYMAP:
            with self.subTest(keys=item.keys):
                self.assertLessEqual(cell_len(item.keys), KEY_COLUMN)


class HelpContentTests(unittest.TestCase):
    def test_renders_both_columns_on_a_wide_terminal(self) -> None:
        text = render_help(PALETTE, width=120).plain
        self.assertIn("键盘", text)
        self.assertIn("命令", text)
        self.assertIn("Enter", text)

    def test_commands_come_from_the_single_source(self) -> None:
        """★ 帮助里的命令清单必须来自 `tui/commands.py`（**只有一份**）。

        历史上帮助内容与命令实现各维护一份清单，于是"命令加了但帮助没更新"
        完全可能发生——而帮助里写着一个按了没反应的命令，比少写一个糟糕得多。
        """
        text = render_help(PALETTE, width=120).plain
        for name, _desc, ready in command_module.command_catalog():
            if ready:
                self.assertIn(name, text, f"可用命令 {name} 没有出现在帮助里")

    def test_never_exceeds_the_requested_width(self) -> None:
        """帮助在**任何**宽度下都不能超宽（超宽会让终端折行、整个浮层错位）。"""
        from logox.tui.render.ansi import visible_width

        for width in (30, 46, 78, 100, 200):
            with self.subTest(width=width):
                for row in render_help(PALETTE, width=width).plain.split("\n"):
                    self.assertLessEqual(visible_width(row), max(width, 30))

    def test_narrow_terminal_uses_the_compact_layout(self) -> None:
        """窄终端要退到紧凑版，**而不是**把双栏裁掉一半。"""
        content, _left, _action = layout_for(50)
        self.assertLess(content, 76, "50 格终端应当走紧凑布局")


class CommandResolutionTests(unittest.TestCase):
    def test_aliases_resolve_to_canonical_names(self) -> None:
        """``/q`` 与 ``/quit`` 都归一成 ``exit``——命令实现因此只需要写一份。"""
        for typed in ("/q", "/quit", "/exit"):
            with self.subTest(typed=typed):
                resolved = command_module.resolve(typed)
                self.assertIsNotNone(resolved)
                assert resolved is not None
                self.assertEqual(resolved.name, "exit")
                self.assertTrue(resolved.is_ready)

    def test_available_commands_all_have_handlers(self) -> None:
        """★ 声明"可用"的命令必须**真的有实现**。

        否则 `/help` 会列出一个打进去没反应的命令（D47 的第三条要求：
        宁可少列，也不假装成功）。
        """
        from logox.tui.render.commands import CommandRunner

        for name in command_module.AVAILABLE_COMMANDS:
            with self.subTest(name=name):
                self.assertTrue(
                    hasattr(CommandRunner, f"_cmd_{name}"),
                    f"/{name} 声明可用，但 CommandRunner 没有 _cmd_{name}",
                )

    def test_planned_commands_have_no_handlers(self) -> None:
        """反向：还没实现的命令**不该**有实现（否则状态标错了）。"""
        from logox.tui.render.commands import CommandRunner

        for name in command_module.PLANNED_COMMANDS:
            with self.subTest(name=name):
                self.assertFalse(
                    hasattr(CommandRunner, f"_cmd_{name}"),
                    f"/{name} 标为未实现，却已经有 _cmd_{name}——该挪进 AVAILABLE 了",
                )

    def test_no_command_is_both_available_and_planned(self) -> None:
        overlap = set(command_module.AVAILABLE_COMMANDS) & set(command_module.PLANNED_COMMANDS)
        self.assertEqual(overlap, set())

    def test_unknown_command_lists_what_is_available(self) -> None:
        resolved = command_module.resolve("/nosuchthing")
        assert resolved is not None
        notice = command_module.format_unknown_notice(resolved)
        self.assertIn("未知命令", notice)
        self.assertIn("/help", notice)

    def test_plain_text_is_not_a_command(self) -> None:
        for text in ("hello", "a/b", ""):
            with self.subTest(text=text):
                self.assertIsNone(command_module.resolve(text))

    def test_ui_spec_mentions_no_removed_keys(self) -> None:
        """文档也要跟着删：``UI-SPEC.md`` 里不该再教用户按侧栏快捷键。"""
        spec = (REPO_ROOT / "docs" / "UI-SPEC.md").read_text(encoding="utf-8")
        for gone in ("Ctrl+2", "Ctrl+6"):
            with self.subTest(gone=gone):
                self.assertNotIn(gone, spec, f"UI-SPEC 里还在提已经不存在的 {gone}")


def _strip_commands(text: str) -> str:
    return re.sub(r"/[a-z-]+", "", text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
