"""tests/unit/test_hooks.py - 观察型生命周期钩子单元测试。

测试覆盖：
1. 钩子发现机制（配置声明与目录扫描）；
2. 钩子命令执行（stdin JSON 载荷、环境变量注入、stdout/stderr 捕获）；
3. 超时强杀与进程树清理（短超时防御）；
4. 异常隔离与失效保护（Fail-Safe：脚本失败不阻塞、不抛出）；
5. 事件总线挂接（attach_to_bus 事件分发）。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from logox.config.schema import HookEntry, HooksConfig
from logox.hooks import HookRunner
from logox.kernel.bus import EventBus
from logox.kernel.events import SessionStart


class HookRunnerTests(unittest.IsolatedAsyncioTestCase):
    """生命周期钩子核心功能测试。"""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.cwd = Path(self.tmp_dir.name).resolve()
        self.hooks_dir = self.cwd / ".logox" / "hooks"
        self.hooks_dir.mkdir(parents=True, exist_ok=True)


    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_discover_from_config(self) -> None:
        """从配置 entries 中读取声明的钩子命令。"""
        cfg = HooksConfig(
            enabled=True,
            entries=[
                HookEntry(event="session_start", command="echo start1"),
                HookEntry(event="session_start", command="echo start2"),
                HookEntry(event="post_tool_use", command="echo tool_used"),
            ],
        )
        runner = HookRunner(config=cfg, cwd=self.cwd)

        start_hooks = runner.discover_hooks("session_start")
        self.assertEqual(start_hooks, ["echo start1", "echo start2"])

        tool_hooks = runner.discover_hooks("post_tool_use")
        self.assertEqual(tool_hooks, ["echo tool_used"])

        empty_hooks = runner.discover_hooks("pre_compact")
        self.assertEqual(empty_hooks, [])

    def test_discover_from_directory_scripts(self) -> None:
        """从 .logox/hooks/ 目录扫描特定事件命名的脚本文件。"""
        script1 = self.hooks_dir / "session_start.py"
        script1.write_text("print('hello')", encoding="utf-8")
        script2 = self.hooks_dir / "post_tool_use.py"
        script2.write_text("print('tool')", encoding="utf-8")

        runner = HookRunner(config=HooksConfig(enabled=True), cwd=self.cwd)
        hooks_start = runner.discover_hooks("session_start")
        hooks_tool = runner.discover_hooks("post_tool_use")

        self.assertEqual(len(hooks_start), 1)
        self.assertEqual(Path(hooks_start[0]).resolve(), script1.resolve())
        self.assertEqual(len(hooks_tool), 1)
        self.assertEqual(Path(hooks_tool[0]).resolve(), script2.resolve())


    def test_hooks_disabled_returns_empty(self) -> None:
        """当 hooks.enabled 为 False 时，不发现任何钩子。"""
        cfg = HooksConfig(
            enabled=False,
            entries=[HookEntry(event="session_start", command="echo start")],
        )
        runner = HookRunner(config=cfg, cwd=self.cwd)
        self.assertEqual(runner.discover_hooks("session_start"), [])

    async def test_execute_command_success_with_payload_and_env(self) -> None:
        """成功执行命令：验证 stdin JSON 传入与 LOGOX_* 环境变量注入。"""
        test_script = self.cwd / "test_hook.py"
        test_script.write_text(
            "import sys, json, os\n"
            "data = json.load(sys.stdin)\n"
            "evt = os.environ.get('LOGOX_EVENT', '')\n"
            "custom = os.environ.get('CUSTOM_VAR', '')\n"
            "print(f'EVT:{evt}|ARG:{data.get(\"foo\")}|CUSTOM:{custom}')\n",
            encoding="utf-8",
        )

        runner = HookRunner(config=HooksConfig(enabled=True), cwd=self.cwd)
        # 直接传入脚本文件路径，HookRunner 内部会自动用 sys.executable 执行
        res = await runner.execute_command(
            command=str(test_script),
            event_name="session_start",
            payload={"foo": "bar"},
            env_vars={"CUSTOM_VAR": "val123"},
        )

        self.assertTrue(res.ok)
        self.assertEqual(res.exit_code, 0)
        self.assertIn("EVT:session_start|ARG:bar|CUSTOM:val123", res.stdout)
        self.assertFalse(res.timed_out)
        self.assertGreaterEqual(res.duration_ms, 0.0)

    async def test_execute_command_failure_fail_safe(self) -> None:
        """命令执行返回非 0 状态码时，Fail-Safe 保障不抛异常且记录失败结果。"""
        fail_script = self.cwd / "fail_hook.py"
        fail_script.write_text("import sys\nsys.exit(42)\n", encoding="utf-8")

        runner = HookRunner(config=HooksConfig(enabled=True), cwd=self.cwd)
        res = await runner.execute_command(
            command=str(fail_script),
            event_name="post_tool_use",
        )

        self.assertFalse(res.ok)
        self.assertEqual(res.exit_code, 42)
        self.assertFalse(res.timed_out)

    async def test_execute_command_timeout_kills_process(self) -> None:
        """钩子执行超时测试：短超时后强制终止进程树，返回 timed_out=True。"""
        sleep_script = self.cwd / "sleep_hook.py"
        sleep_script.write_text("import time\ntime.sleep(10)\n", encoding="utf-8")

        # 配置短超时 0.3s
        cfg = HooksConfig(enabled=True, timeout_s=0.3)
        runner = HookRunner(config=cfg, cwd=self.cwd)

        res = await runner.execute_command(
            command=str(sleep_script),
            event_name="post_tool_use",
        )

        self.assertFalse(res.ok)
        self.assertTrue(res.timed_out)
        self.assertIn("timed out after 0.3s", res.stderr)

    async def test_dispatch_records_history(self) -> None:
        """dispatch 依次分发所有发现的钩子并写入 runner.history。"""
        s1 = self.cwd / "s1.py"
        s1.write_text("print('hook1')", encoding="utf-8")
        s2 = self.cwd / "s2.py"
        s2.write_text("print('hook2')", encoding="utf-8")

        cfg = HooksConfig(
            enabled=True,
            entries=[
                HookEntry(event="stop", command=str(s1)),
                HookEntry(event="stop", command=str(s2)),
            ],
        )
        runner = HookRunner(config=cfg, cwd=self.cwd)
        results = await runner.dispatch("stop", payload={"reason": "done"})

        self.assertEqual(len(results), 2)
        self.assertEqual(len(runner.history), 2)
        self.assertIn("hook1", results[0].stdout)
        self.assertIn("hook2", results[1].stdout)

    async def test_attach_to_bus(self) -> None:
        """挂接到 EventBus 后，内核事件自动驱动钩子分发。"""
        flag_file = self.cwd / "hook_triggered.txt"
        hook_script = self.cwd / "sess_hook.py"
        hook_script.write_text(
            f"import sys, json\n"
            f"data = json.load(sys.stdin)\n"
            f"open(r'{flag_file}', 'w', encoding='utf-8').write(data.get('session_id', ''))\n",
            encoding="utf-8",
        )

        cfg = HooksConfig(
            enabled=True,
            entries=[
                HookEntry(event="session_start", command=str(hook_script)),
            ],
        )
        runner = HookRunner(config=cfg, cwd=self.cwd)
        bus = EventBus(session_id="sess-m10")
        runner.attach_to_bus(bus)

        # 发布 SessionStart 事件
        await bus.publish(
            SessionStart(
                session_id="sess-m10",
                cwd=str(self.cwd),
                model="mock-model",
                provider="mock-provider",
            )
        )

        # 检查标记文件是否被钩子成功写入
        self.assertTrue(flag_file.exists())
        self.assertEqual(flag_file.read_text(encoding="utf-8"), "sess-m10")
        self.assertEqual(len(runner.history), 1)
        self.assertTrue(runner.history[0].ok)


if __name__ == "__main__":
    unittest.main()
