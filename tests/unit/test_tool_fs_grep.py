"""GrepTool 单元测试（M5）。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from logox.errors import ErrorCategory
from logox.tools.base import ToolContext
from logox.tools.fs_grep import GrepArgs, GrepTool


class TestGrepTool(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.cwd = Path(self.tmp_dir.name).resolve()
        self.tool = GrepTool()
        self.ctx = ToolContext(cwd=self.cwd)

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    async def test_grep_basic_keyword(self) -> None:
        file1 = self.cwd / "a.py"
        file1.write_text("line 1\nhello world\nline 3\n", encoding="utf-8")
        file2 = self.cwd / "b.txt"
        file2.write_text("another line\nsay hello\n", encoding="utf-8")

        res = await self.tool.run(GrepArgs(pattern="hello"), self.ctx)

        self.assertTrue(res.ok)
        self.assertIn("a.py:2: hello world", res.content)
        self.assertIn("b.txt:2: say hello", res.content
)
        self.assertIsNotNone(res.display)
        self.assertEqual(res.display.payload["count"], 2)

    async def test_grep_single_file(self) -> None:
        file1 = self.cwd / "single.txt"
        file1.write_text("apple\nbanana\napple pie\n", encoding="utf-8")

        res = await self.tool.run(GrepArgs(pattern="apple", path="single.txt"), self.ctx)

        self.assertTrue(res.ok)
        self.assertIn("single.txt:1: apple", res.content)
        self.assertIn("single.txt:3: apple pie", res.content)
        self.assertNotIn("banana", res.content)

    async def test_grep_case_sensitivity(self) -> None:
        file1 = self.cwd / "case.txt"
        file1.write_text("Target\ntarget\nTARGET\n", encoding="utf-8")

        # Default case sensitive
        res1 = await self.tool.run(GrepArgs(pattern="Target"), self.ctx)
        self.assertTrue(res1.ok)
        self.assertIn("case.txt:1: Target", res1.content)
        self.assertNotIn("target", res1.content)

        # Case insensitive
        res2 = await self.tool.run(GrepArgs(pattern="Target", case_sensitive=False), self.ctx)
        self.assertTrue(res2.ok)
        self.assertEqual(res2.display.payload["count"], 3)

    async def test_grep_regex_pattern(self) -> None:
        file1 = self.cwd / "code.py"
        file1.write_text("def test_one():\n    pass\ndef helper():\n    pass\ndef test_two():\n    pass\n", encoding="utf-8")

        res = await self.tool.run(GrepArgs(pattern=r"def test_\w+\(\):"), self.ctx)

        self.assertTrue(res.ok)
        self.assertIn("code.py:1: def test_one():", res.content)
        self.assertIn("code.py:5: def test_two():", res.content)
        self.assertNotIn("helper", res.content)

    async def test_grep_invalid_regex(self) -> None:
        res = await self.tool.run(GrepArgs(pattern="[unclosed"), self.ctx)

        self.assertFalse(res.ok)
        self.assertIsNotNone(res.error)
        self.assertEqual(res.error.category, ErrorCategory.BAD_REQUEST)
        self.assertIn("正则表达式不合法", res.content)

    async def test_grep_nonexistent_path(self) -> None:
        res = await self.tool.run(GrepArgs(pattern="test", path="no_such_dir"), self.ctx)

        self.assertFalse(res.ok)
        self.assertIsNotNone(res.error)
        self.assertEqual(res.error.category, ErrorCategory.BAD_REQUEST)

    async def test_grep_skips_binary_files(self) -> None:
        bin_file = self.cwd / "binary.dat"
        bin_file.write_bytes(b"hello world\x00some binary junk")

        txt_file = self.cwd / "valid.txt"
        txt_file.write_text("hello world\n", encoding="utf-8")

        res = await self.tool.run(GrepArgs(pattern="hello"), self.ctx)

        self.assertTrue(res.ok)
        self.assertIn("valid.txt:1: hello world", res.content)
        self.assertNotIn("binary.dat", res.content)

    async def test_grep_skips_ignored_directories(self) -> None:
        git_dir = self.cwd / ".git"
        git_dir.mkdir()
        (git_dir / "config.txt").write_text("secret_keyword", encoding="utf-8")

        venv_dir = self.cwd / ".venv"
        venv_dir.mkdir()
        (venv_dir / "lib.py").write_text("secret_keyword", encoding="utf-8")

        src_dir = self.cwd / "src"
        src_dir.mkdir()
        (src_dir / "main.py").write_text("secret_keyword found here", encoding="utf-8")

        res = await self.tool.run(GrepArgs(pattern="secret_keyword"), self.ctx)

        self.assertTrue(res.ok)
        self.assertIn("src/main.py:1: secret_keyword found here", res.content)
        self.assertNotIn(".git", res.content)
        self.assertNotIn(".venv", res.content)

    async def test_grep_max_matches_truncation(self) -> None:
        file1 = self.cwd / "many.txt"
        lines = [f"item {i}" for i in range(50)]
        file1.write_text("\n".join(lines), encoding="utf-8")

        res = await self.tool.run(GrepArgs(pattern="item", max_matches=5), self.ctx)

        self.assertTrue(res.ok)
        self.assertIn("匹配项已达到上限 5 条", res.content)
        self.assertIsNotNone(res.display)
        self.assertEqual(res.display.payload["count"], 5)
        self.assertTrue(res.display.payload["truncated"])

    async def test_grep_long_line_truncation(self) -> None:
        file1 = self.cwd / "long.txt"
        long_line = "prefix " + "a" * 300 + " suffix"
        file1.write_text(long_line, encoding="utf-8")

        res = await self.tool.run(GrepArgs(pattern="prefix"), self.ctx)

        self.assertTrue(res.ok)
        self.assertIn("long.txt:1: prefix ", res.content)
        self.assertIn("…", res.content)
        first_line = res.content.splitlines()[0]
        self.assertLessEqual(len(first_line), 250)

    async def test_grep_no_matches(self) -> None:
        file1 = self.cwd / "a.txt"
        file1.write_text("nothing here\n", encoding="utf-8")

        res = await self.tool.run(GrepArgs(pattern="absent_needle"), self.ctx)

        self.assertTrue(res.ok)
        self.assertIn("未找到匹配模式", res.content)
        self.assertEqual(res.display.payload["count"], 0)
