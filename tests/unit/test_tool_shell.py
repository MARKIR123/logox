"""ShellTool 单元测试（M5）。"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path

from logox.errors import ErrorCategory
from logox.tools.base import ToolContext
from logox.tools.shell import (
    ShellArgs,
    ShellBackend,
    ShellTool,
    detect_shell,
    truncate_output,
)


class TestShellTool(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.cwd = Path(self.tmp_dir.name).resolve()
        self.tool = ShellTool()
        self.ctx = ToolContext(cwd=self.cwd)

    def tearDown(self) -> None:
        try:
            self.tmp_dir.cleanup()
        except OSError:
            pass

    async def test_shell_basic_execution(self) -> None:
        # Cross-platform command using python executable
        py = sys.executable
        cmd = f'"{py}" -c "print(\'hello from shell\')"'
        res = await self.tool.run(ShellArgs(command=cmd), self.ctx)

        self.assertTrue(res.ok)
        self.assertIn("hello from shell", res.content)
        self.assertIsNotNone(res.display)
        self.assertEqual(res.display.payload["exit_code"], 0)

    async def test_shell_exit_code_failure(self) -> None:
        py = sys.executable
        cmd = f'"{py}" -c "import sys; sys.stderr.write(\'fatal failure\\n\'); sys.exit(3)"'
        res = await self.tool.run(ShellArgs(command=cmd), self.ctx)

        self.assertFalse(res.ok)
        self.assertIsNotNone(res.error)
        self.assertEqual(res.error.category, ErrorCategory.TOOL_FAILURE)
        self.assertIn("fatal failure", res.content)
        self.assertIn("退出码: 3", res.error.message)

    async def test_shell_stdout_and_stderr_combined(self) -> None:
        py = sys.executable
        cmd = f'"{py}" -c "import sys; print(\'out line\'); sys.stderr.write(\'err line\\n\')"'
        res = await self.tool.run(ShellArgs(command=cmd), self.ctx)

        self.assertTrue(res.ok)
        self.assertIn("out line", res.content)
        self.assertIn("[stderr]:", res.content)
        self.assertIn("err line", res.content)

    async def test_shell_truncation_logic(self) -> None:
        short_text = "abc" * 100
        res, truncated = truncate_output(short_text)
        self.assertFalse(truncated)
        self.assertEqual(res, short_text)

        long_text = "start_marker" + ("X" * 10000) + "end_marker"
        res, truncated = truncate_output(long_text)
        self.assertTrue(truncated)
        self.assertIn("start_marker", res)
        self.assertIn("end_marker", res)
        self.assertIn("已截断", res)
        # Verify length is well within bounds
        self.assertLess(len(res), 5000)

    async def test_shell_large_output_truncation_in_execution(self) -> None:
        py = sys.executable
        # Generate 15000 characters of output
        cmd = f'"{py}" -c "print(\'A\' * 15000)"'
        res = await self.tool.run(ShellArgs(command=cmd), self.ctx)

        self.assertTrue(res.ok)
        self.assertIn("已截断", res.content)
        self.assertIsNotNone(res.display)
        self.assertTrue(res.display.payload["truncated"])

    async def test_shell_timeout_and_process_cleanup(self) -> None:
        py = sys.executable
        # Sleep for 5 seconds with timeout of 1 second
        cmd = f'"{py}" -c "import time; time.sleep(5)"'
        res = await self.tool.run(ShellArgs(command=cmd, timeout_seconds=1), self.ctx)

        self.assertFalse(res.ok)
        self.assertIsNotNone(res.error)
        self.assertEqual(res.error.category, ErrorCategory.TOOL_FAILURE)
        self.assertIn("超时", res.error.message)

    async def test_shell_respects_cwd(self) -> None:
        test_file = self.cwd / "sample.txt"
        test_file.write_text("content inside cwd", encoding="utf-8")

        py = sys.executable
        cmd = f'"{py}" -c "import pathlib; print(pathlib.Path(\'sample.txt\').read_text())"'
        res = await self.tool.run(ShellArgs(command=cmd), self.ctx)

        self.assertTrue(res.ok)
        self.assertIn("content inside cwd", res.content)

    async def test_shell_env_injection(self) -> None:
        py = sys.executable
        cmd = f'"{py}" -c "import os; print(\'UTF8=\' + os.environ.get(\'PYTHONUTF8\', \'\'))"'
        res = await self.tool.run(ShellArgs(command=cmd), self.ctx)

        self.assertTrue(res.ok)
        self.assertIn("UTF8=1", res.content)

    async def test_powershell_exit_code_trap_detection(self) -> None:
        # Create a mock tool with powershell backend
        tool = ShellTool(backend="powershell")
        if tool.backend.name != "powershell":
            self.skipTest("powershell not available on this platform")

        # Simulate a powershell cmdlet failure that might produce CategoryInfo in stderr
        cmd = "Get-Item non_existent_mock_file_xyz"
        res = await tool.run(ShellArgs(command=cmd), self.ctx)

        self.assertFalse(res.ok)
        self.assertIsNotNone(res.error)
        self.assertEqual(res.error.category, ErrorCategory.TOOL_FAILURE)

    def test_detect_shell_backend(self) -> None:
        backend = detect_shell()
        self.assertIsInstance(backend, ShellBackend)
        self.assertIn(backend.name, ("gitbash", "powershell", "cmd", "wsl", "posix"))
        self.assertTrue(len(backend.args_prefix) >= 1)
