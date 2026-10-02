"""EditTool 单元测试（M5）。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from logox.errors import ErrorCategory
from logox.tools.base import ToolContext
from logox.tools.fs_edit import EditArgs, EditTool


class TestEditTool(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.cwd = Path(self.tmp_dir.name).resolve()
        self.tool = EditTool()
        self.ctx = ToolContext(cwd=self.cwd)

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    async def test_exact_unique_replacement(self) -> None:
        target = self.cwd / "sample.py"
        target.write_text("def hello():\n    return 'world'\n", encoding="utf-8")

        args = EditArgs(
            path="sample.py",
            old_string="    return 'world'",
            new_string="    return 'logox'",
        )
        result = await self.tool.run(args, self.ctx)

        self.assertTrue(result.ok)
        self.assertIn("已成功编辑文件", result.content)
        self.assertIsNotNone(result.change_stat)
        assert result.change_stat is not None
        self.assertEqual(result.change_stat.kind, "modify")
        self.assertEqual(result.change_stat.added, 1)
        self.assertEqual(result.change_stat.removed, 1)

        # 检查最终文件内容
        self.assertEqual(target.read_text(encoding="utf-8"), "def hello():\n    return 'logox'\n")

        # 检查 display hint 结构
        self.assertIsNotNone(result.display)
        assert result.display is not None
        self.assertEqual(result.display.kind, "diff")
        self.assertIn("hunks", result.display.payload)
        self.assertGreaterEqual(len(result.display.payload["hunks"]), 1)

    async def test_multiple_matches_fails_without_replace_all(self) -> None:
        target = self.cwd / "multi.py"
        target.write_text("a = 1\nb = 2\na = 1\n", encoding="utf-8")

        args = EditArgs(path="multi.py", old_string="a = 1", new_string="a = 99")
        result = await self.tool.run(args, self.ctx)

        self.assertFalse(result.ok)
        self.assertIsNotNone(result.error)
        assert result.error is not None
        self.assertEqual(result.error.category, ErrorCategory.BAD_REQUEST)
        self.assertIn("命中了 2 处", result.content)

        # 文件内容保持不变
        self.assertEqual(target.read_text(encoding="utf-8"), "a = 1\nb = 2\na = 1\n")

    async def test_multiple_matches_succeeds_with_replace_all(self) -> None:
        target = self.cwd / "multi_all.py"
        target.write_text("a = 1\nb = 2\na = 1\n", encoding="utf-8")

        args = EditArgs(path="multi_all.py", old_string="a = 1", new_string="a = 99", replace_all=True)
        result = await self.tool.run(args, self.ctx)

        self.assertTrue(result.ok)
        self.assertIn("全量替换了 2 处", result.content)
        self.assertEqual(target.read_text(encoding="utf-8"), "a = 99\nb = 2\na = 99\n")

    async def test_normalized_whitespace_matching(self) -> None:
        target = self.cwd / "trailing.py"
        # 原文件使用 4 空格缩进
        target.write_text("def calc():\n    x = 10\n    y = 20\n    return x + y\n", encoding="utf-8")

        # 模型传入了 2 空格缩进，属于缩进/空白偏差，Level 1 无法作为子串精确匹配
        args = EditArgs(
            path="trailing.py",
            old_string="  x = 10\n  y = 20",
            new_string="  x = 100\n  y = 200",
        )
        result = await self.tool.run(args, self.ctx)

        self.assertTrue(result.ok)
        self.assertIn("空白归一化自动对齐", result.content)
        self.assertEqual(target.read_text(encoding="utf-8"), "def calc():\n  x = 100\n  y = 200\n    return x + y\n")

    async def test_nearest_block_suggestion_on_failure(self) -> None:
        target = self.cwd / "code.py"
        target.write_text("def calculate_total(items):\n    subtotal = sum(items)\n    return subtotal\n", encoding="utf-8")

        # 模型打错了函数名
        args = EditArgs(
            path="code.py",
            old_string="def calculate_totallll(items):\n    subtotal = sum(items)",
            new_string="def calculate_total(items, discount=0):",
        )
        result = await self.tool.run(args, self.ctx)

        self.assertFalse(result.ok)
        self.assertIsNotNone(result.error)
        assert result.error is not None
        self.assertEqual(result.error.category, ErrorCategory.BAD_REQUEST)
        # 验证提示中包含最近的代码行和相近内容
        self.assertIn("最相近的代码片段", result.error.detail or "")
        self.assertIn("calculate_total", result.error.detail or "")

    async def test_crlf_preservation(self) -> None:
        target = self.cwd / "windows_file.py"
        # 使用 CRLF 写入
        target.write_bytes(b"line 1\r\nline 2\r\nline 3\r\n")

        args = EditArgs(path="windows_file.py", old_string="line 2", new_string="line 2 modified")
        result = await self.tool.run(args, self.ctx)

        self.assertTrue(result.ok)
        raw = target.read_bytes()
        self.assertIn(b"\r\n", raw)
        self.assertEqual(raw.count(b"\r\n"), 3)
        self.assertEqual(raw, b"line 1\r\nline 2 modified\r\nline 3\r\n")

    async def test_bom_preservation(self) -> None:
        target = self.cwd / "bom_file.txt"
        bom_content = b"\xef\xbb\xbfHello\nWorld\n"
        target.write_bytes(bom_content)

        args = EditArgs(path="bom_file.txt", old_string="World", new_string="Logox")
        result = await self.tool.run(args, self.ctx)

        self.assertTrue(result.ok)
        raw = target.read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(raw, b"\xef\xbb\xbfHello\nLogox\n")

    async def test_binary_file_rejected(self) -> None:
        target = self.cwd / "app.bin"
        target.write_bytes(b"\x00\x01\x02binary data")

        args = EditArgs(path="app.bin", old_string="binary", new_string="text")
        result = await self.tool.run(args, self.ctx)

        self.assertFalse(result.ok)
        self.assertIsNotNone(result.error)
        assert result.error is not None
        self.assertEqual(result.error.category, ErrorCategory.BAD_REQUEST)
        self.assertIn("二进制", result.content)

    def test_spec_properties(self) -> None:
        self.assertEqual(self.tool.spec.name, "edit")
        self.assertFalse(self.tool.spec.readonly)
        self.assertTrue(self.tool.spec.requires_permission)
        self.assertEqual(self.tool.spec.summary({"path": "file.py"}), "编辑 file.py")


if __name__ == "__main__":
    unittest.main()
