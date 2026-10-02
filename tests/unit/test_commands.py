"""tests/unit/test_commands.py - 提示词模板自定义命令单元测试。

测试覆盖：
1. parse_frontmatter 轻量解析器（带前置元数据、无前置元数据、非闭合防御）；
2. CommandManager 发现机制（项目级与用户全局级、优先级覆盖）；
3. 提示词模板参数插值（{{args}} 与 $ARGUMENTS 替换、空参数容错）；
4. 命令查询与规范化（/cmd 与 cmd 兼容）。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from logox.commands.manager import CommandManager, parse_frontmatter


class CommandManagerTests(unittest.TestCase):
    """自定义命令管理器功能测试。"""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.cwd = Path(self.tmp_dir.name).resolve()
        self.user_dir = self.cwd / "user_home"
        self.user_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_parse_frontmatter_with_valid_header(self) -> None:
        """解析带有标准 --- 头部的 Markdown 内容。"""
        content = (
            "---\n"
            "description: Run code review\n"
            "argument-hint: [file_or_diff]\n"
            "---\n"
            "Please review the following code changes:\n"
            "{{args}}\n"
        )
        meta, body = parse_frontmatter(content)
        self.assertEqual(meta["description"], "Run code review")
        self.assertEqual(meta["argument-hint"], "[file_or_diff]")
        self.assertEqual(body, "Please review the following code changes:\n{{args}}")

    def test_parse_frontmatter_without_header(self) -> None:
        """解析无 frontmatter 的纯 Markdown。"""
        content = "Just do a git status and explain it.\n"
        meta, body = parse_frontmatter(content)
        self.assertEqual(meta, {})
        self.assertEqual(body, content)

    def test_parse_frontmatter_unclosed(self) -> None:
        """未闭合的 --- 头应当作普通文本，返回空元数据。"""
        content = "---\ndescription: test\nbody without closing"
        meta, body = parse_frontmatter(content)
        self.assertEqual(meta, {})
        self.assertEqual(body, content)

    def test_discover_commands_project_overrides_user(self) -> None:
        """项目级命令优先覆盖用户全局级同名命令。"""
        proj_cmd_dir = self.cwd / ".logox" / "commands"
        proj_cmd_dir.mkdir(parents=True, exist_ok=True)
        user_cmd_dir = self.user_dir / "commands"
        user_cmd_dir.mkdir(parents=True, exist_ok=True)

        # 用户全局级定义 review.md
        (user_cmd_dir / "review.md").write_text(
            "---\ndescription: User review\n---\nUser review template",
            encoding="utf-8",
        )
        # 项目级也定义 review.md
        (proj_cmd_dir / "review.md").write_text(
            "---\ndescription: Project review\n---\nProject review template",
            encoding="utf-8",
        )
        # 用户全局级定义 commit.md
        (user_cmd_dir / "commit.md").write_text(
            "---\ndescription: Git commit\n---\nGenerate commit msg",
            encoding="utf-8",
        )

        mgr = CommandManager(cwd=self.cwd, user_dir=self.user_dir)
        cmd_list = mgr.list_commands()
        cmd_dict = {c.name: c for c in cmd_list}

        self.assertEqual(len(cmd_list), 2)
        self.assertIn("review", cmd_dict)
        self.assertIn("commit", cmd_dict)

        # 验证 review 被项目级覆盖
        review_cmd = cmd_dict["review"]
        self.assertEqual(review_cmd.description, "Project review")
        self.assertEqual(review_cmd.template, "Project review template")
        self.assertEqual(review_cmd.scope, "project")

        # 验证 commit 为全局级
        commit_cmd = cmd_dict["commit"]
        self.assertEqual(commit_cmd.scope, "global")

    def test_render_template_with_args(self) -> None:
        """模板插值：成功替换 {{args}} 与 $ARGUMENTS。"""
        cmd_dir = self.cwd / ".logox" / "commands"
        cmd_dir.mkdir(parents=True, exist_ok=True)
        (cmd_dir / "testargs.md").write_text(
            "Task: {{args}} (also check $ARGUMENTS)",
            encoding="utf-8",
        )

        mgr = CommandManager(cwd=self.cwd, user_dir=self.user_dir)
        rendered = mgr.render("testargs", "src/main.py")

        self.assertEqual(rendered, "Task: src/main.py (also check src/main.py)")

    def test_render_template_with_empty_args(self) -> None:
        """模板插值：无参数时默认置空。"""
        cmd_dir = self.cwd / ".logox" / "commands"
        cmd_dir.mkdir(parents=True, exist_ok=True)
        (cmd_dir / "testempty.md").write_text(
            "Check status for: '{{args}}'",
            encoding="utf-8",
        )

        mgr = CommandManager(cwd=self.cwd, user_dir=self.user_dir)
        rendered = mgr.render("testempty", "")

        self.assertEqual(rendered, "Check status for: ''")

    def test_get_command_normalizes_slash(self) -> None:
        """查询命令时兼容带斜杠和不带斜杠的名称。"""
        cmd_dir = self.cwd / ".logox" / "commands"
        cmd_dir.mkdir(parents=True, exist_ok=True)
        (cmd_dir / "explain.md").write_text(
            "---\ndescription: Explain this code\n---\nExplain {{args}}",
            encoding="utf-8",
        )

        mgr = CommandManager(cwd=self.cwd, user_dir=self.user_dir)
        cmd1 = mgr.get_command("/explain")
        cmd2 = mgr.get_command("explain")
        cmd3 = mgr.get_command("nonexistent")

        self.assertIsNotNone(cmd1)
        self.assertIsNotNone(cmd2)
        self.assertIsNone(cmd3)
        self.assertEqual(cmd1.name, "explain")
        self.assertEqual(cmd2.name, "explain")


if __name__ == "__main__":
    unittest.main()
