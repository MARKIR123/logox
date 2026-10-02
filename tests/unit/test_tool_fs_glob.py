"""GlobTool 单元测试（M5）。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from logox.errors import ErrorCategory
from logox.tools.base import ToolContext
from logox.tools.fs_glob import GlobArgs, GlobTool


class TestGlobTool(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.cwd = Path(self.tmp_dir.name).resolve()
        self.tool = GlobTool()
        self.ctx = ToolContext(cwd=self.cwd)

        # 构建测试目录树
        (self.cwd / "src").mkdir()
        (self.cwd / "src" / "a").mkdir()
        (self.cwd / "src" / "main.py").write_text("print('main')", encoding="utf-8")
        (self.cwd / "src" / "a" / "helper.py").write_text("print('helper')", encoding="utf-8")
        (self.cwd / "README.md").write_text("# Readme", encoding="utf-8")
        (self.cwd / "setup.cfg").write_text("[metadata]", encoding="utf-8")

        # 忽略目录
        (self.cwd / ".git").mkdir()
        (self.cwd / ".git" / "config").write_text("git config", encoding="utf-8")
        (self.cwd / "node_modules").mkdir()
        (self.cwd / "node_modules" / "pkg.py").write_text("module", encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    async def test_glob_simple_match(self) -> None:
        args = GlobArgs(pattern="*.md")
        result = await self.tool.run(args, self.ctx)

        self.assertTrue(result.ok)
        self.assertIn("README.md", result.content)
        self.assertNotIn("main.py", result.content)

    async def test_glob_recursive_double_star(self) -> None:
        args = GlobArgs(pattern="**/*.py")
        result = await self.tool.run(args, self.ctx)

        self.assertTrue(result.ok)
        self.assertIn("src/main.py", result.content)
        self.assertIn("src/a/helper.py", result.content)
        # 验证自动剪枝忽略了 node_modules
        self.assertNotIn("node_modules", result.content)
        self.assertNotIn("pkg.py", result.content)

    async def test_glob_ignores_default_blacklisted_dirs(self) -> None:
        args = GlobArgs(pattern="**/*config*")
        result = await self.tool.run(args, self.ctx)

        self.assertTrue(result.ok)
        # .git/config 必须被跳过
        self.assertNotIn(".git", result.content)

    async def test_glob_no_match_returns_clean_message(self) -> None:
        args = GlobArgs(pattern="*.nonexistent")
        result = await self.tool.run(args, self.ctx)

        self.assertTrue(result.ok)
        self.assertIn("未找到与模式 '*.nonexistent' 匹配", result.content)

    async def test_glob_truncation(self) -> None:
        # 创建 10 个测试文件
        bulk_dir = self.cwd / "bulk"
        bulk_dir.mkdir()
        for i in range(10):
            (bulk_dir / f"file_{i:02d}.txt").write_text("text", encoding="utf-8")

        args = GlobArgs(pattern="bulk/*.txt", max_results=4)
        result = await self.tool.run(args, self.ctx)

        self.assertTrue(result.ok)
        lines = [line for line in result.content.splitlines() if line.startswith("bulk/")]
        self.assertEqual(len(lines), 4)
        self.assertIn("已截断省略", result.content)

    async def test_glob_nonexistent_root_fails(self) -> None:
        args = GlobArgs(pattern="*.py", path="does_not_exist")
        result = await self.tool.run(args, self.ctx)

        self.assertFalse(result.ok)
        self.assertIsNotNone(result.error)
        assert result.error is not None
        self.assertEqual(result.error.category, ErrorCategory.BAD_REQUEST)

    def test_spec_properties(self) -> None:
        self.assertEqual(self.tool.spec.name, "glob")
        self.assertTrue(self.tool.spec.readonly)
        self.assertFalse(self.tool.spec.requires_permission)
        self.assertEqual(self.tool.spec.summary({"pattern": "**/*.py"}), "查找 **/*.py")


if __name__ == "__main__":
    unittest.main()
