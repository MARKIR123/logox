"""WriteTool 单元测试（M5）。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from logox.errors import ErrorCategory
from logox.tools.base import ToolContext
from logox.tools.fs_write import WriteArgs, WriteTool


class TestWriteTool(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.cwd = Path(self.tmp_dir.name).resolve()
        self.tool = WriteTool()
        self.ctx = ToolContext(cwd=self.cwd)

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    async def test_write_new_file(self) -> None:
        args = WriteArgs(path="hello.txt", content="Hello, World!\nLine 2")
        result = await self.tool.run(args, self.ctx)

        self.assertTrue(result.ok)
        self.assertIn("已成功写入文件 hello.txt", result.content)
        self.assertIsNotNone(result.change_stat)
        assert result.change_stat is not None
        self.assertEqual(result.change_stat.kind, "new")
        self.assertEqual(result.change_stat.added, 2)
        self.assertEqual(result.change_stat.removed, 0)

        target = self.cwd / "hello.txt"
        self.assertTrue(target.exists())
        self.assertEqual(target.read_text(encoding="utf-8"), "Hello, World!\nLine 2")

    async def test_write_auto_creates_parent_directory(self) -> None:
        nested_path = "nested/deep/sub/dir/output.txt"
        args = WriteArgs(path=nested_path, content="Deep content")
        result = await self.tool.run(args, self.ctx)

        self.assertTrue(result.ok)
        target = self.cwd / nested_path
        self.assertTrue(target.exists())
        self.assertEqual(target.read_text(encoding="utf-8"), "Deep content")

    async def test_write_overwrite_existing_file(self) -> None:
        target = self.cwd / "existing.py"
        target.write_text("line 1\nline 2\nline 3\n", encoding="utf-8")

        args = WriteArgs(path="existing.py", content="new 1\nnew 2\n")
        result = await self.tool.run(args, self.ctx)

        self.assertTrue(result.ok)
        self.assertIsNotNone(result.change_stat)
        assert result.change_stat is not None
        self.assertEqual(result.change_stat.kind, "rewrite")
        self.assertEqual(result.change_stat.removed, 3)
        self.assertEqual(result.change_stat.added, 2)
        self.assertEqual(target.read_text(encoding="utf-8"), "new 1\nnew 2\n")

    async def test_write_to_directory_fails_safely(self) -> None:
        dir_path = self.cwd / "some_dir"
        dir_path.mkdir()

        args = WriteArgs(path="some_dir", content="invalid")
        result = await self.tool.run(args, self.ctx)

        self.assertFalse(result.ok)
        self.assertIsNotNone(result.error)
        assert result.error is not None
        self.assertEqual(result.error.category, ErrorCategory.BAD_REQUEST)
        self.assertIn("目录", result.content)

    async def test_write_honors_cancellation(self) -> None:
        cancelled_ctx = ToolContext(cwd=self.cwd, is_cancelled=lambda: True)
        args = WriteArgs(path="cancel.txt", content="never written")
        result = await self.tool.run(args, cancelled_ctx)

        self.assertFalse(result.ok)
        self.assertIsNotNone(result.error)
        assert result.error is not None
        self.assertEqual(result.error.category, ErrorCategory.CANCELLED)
        self.assertFalse((self.cwd / "cancel.txt").exists())

    def test_spec_and_summary(self) -> None:
        self.assertEqual(self.tool.spec.name, "write")
        self.assertFalse(self.tool.spec.readonly)
        self.assertTrue(self.tool.spec.requires_permission)
        self.assertEqual(self.tool.spec.summary({"path": "src/main.py"}), "写入 src/main.py")


if __name__ == "__main__":
    unittest.main()
