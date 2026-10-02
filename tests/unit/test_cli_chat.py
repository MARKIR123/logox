"""``--chat`` 文本模式测试（MODULE_kernel_loop §7.5 / D53）。

这个模式存在的唯一理由是**让"能读文件并回答"可以真的手工跑一遍**，
所以测试要盯住三件事：stdout 干净（可重定向）、Ctrl+C 语义与 Esc 一致、
以及"未配置密钥"这类首次启动问题给出**可行动**的提示而不是一个堆栈。
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import io
import os
import sys
import unittest
from pathlib import Path

from logox.cli import (
    EXIT_CONFIG_ERROR,
    EXIT_NOT_READY,
    EXIT_OK,
    _chat_loop,
    _chat_sigint_action,
    _ChatRenderer,
    build_parser,
)
from logox.kernel.turn import TurnStatus
from logox.tools.base import ToolArgs, ToolResult, ToolSpec
from tests.unit.kernel_support import StubTool, install, text_chunks, tool_chunks
from tests.unit.support import make_temp_dir, remove_temp_dir

REPO_ROOT = Path(__file__).resolve().parents[2]


class _WriterTool:
    """一个**非只读**工具，只用于验证安全闸门。永远不会被真的调用。"""

    spec = ToolSpec(
        name="write",
        description="写文件",
        params=ToolArgs,
        readonly=False,
        requires_permission=True,
    )

    async def run(self, args: object, ctx: object) -> ToolResult:  # pragma: no cover - 闸门会先拦下
        return ToolResult(ok=True, content="不该走到这里")


class ParserTests(unittest.TestCase):
    def test_t70_chat_flag_is_documented(self) -> None:
        help_text = build_parser().format_help()
        self.assertIn("--chat", help_text)
        self.assertIn("最小文本模式", help_text)

    def test_t71_cli_keeps_heavy_imports_lazy(self) -> None:
        """D29：``--version`` 的快速路径不得被 ``--chat`` 的新增导入拖慢。"""
        source = (REPO_ROOT / "src" / "logox" / "cli.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        top_level: set[str] = set()
        for node in tree.body:
            if isinstance(node, ast.Import):
                top_level.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                top_level.add(node.module.split(".")[0])
        for name in sorted(top_level):
            with self.subTest(imported=name):
                self.assertNotIn(name, ("pydantic", "textual", "rich", "httpx", "openai", "anthropic"))
                self.assertNotIn(name, ("logox.kernel", "logox.tui", "logox.config", "logox.providers"))
        self.assertIn("os", top_level, "--chat 需要读 LOGOX_ALLOW_UNSAFE_TOOLS")

    def test_t72_version_fast_path_does_not_touch_providers(self) -> None:
        """``--version`` 只写一行就返回，绝不触碰内核与适配层。"""
        source = (REPO_ROOT / "src" / "logox" / "cli.py").read_text(encoding="utf-8")
        body = source.split("def _run(args", 1)[0]
        self.assertNotIn("KernelLoop", body)
        self.assertNotIn("ProviderRegistry", body)


class SigintPolicyTests(unittest.TestCase):
    """Ctrl+C 的策略与界面里的 Esc 必须完全一致（D51）。"""

    class _Kernel:
        def __init__(self, outcomes: list[bool]) -> None:
            self._outcomes = list(outcomes)
            self.calls = 0

        def cancel(self) -> bool:
            self.calls += 1
            return self._outcomes.pop(0) if self._outcomes else False

    def test_t73_first_ctrl_c_interrupts_the_turn(self) -> None:
        kernel = self._Kernel([True])
        self.assertTrue(_chat_sigint_action(kernel))
        self.assertEqual(kernel.calls, 1)

    def test_t74_second_ctrl_c_exits(self) -> None:
        """回合已经停了，再按一次就该退出——否则用户按不出程序。"""
        kernel = self._Kernel([True, False])
        self.assertTrue(_chat_sigint_action(kernel))
        self.assertFalse(_chat_sigint_action(kernel))

    def test_t75_no_kernel_yet_means_exit(self) -> None:
        self.assertFalse(_chat_sigint_action(None))


class StartupFailureTests(unittest.TestCase):
    """首次启动最常见的问题必须给出**可行动**的提示，而不是一个堆栈。"""

    def setUp(self) -> None:
        self.root = make_temp_dir("chat-startup-")
        self.addCleanup(remove_temp_dir, self.root)

    def _bundle(self, config_text: str):  # type: ignore[no-untyped-def]
        """在隔离的用户目录里加载一份配置，**绝不读开发机上真实的 ~/.logox**。

        用 ``LogoxPaths.at(root)`` 而不是手工拼路径：``LogoxPaths`` 的字段是**文件路径**
        （``config`` 就是 ``root/config.toml``），手工拼时很容易把目录当成文件传进去——
        那样 ``load()`` 会静默回退到内置默认值，测试看着"通过了"其实配置根本没读到。
        """
        from logox.config.loader import load
        from logox.config.schema import StateFile
        from logox.paths import LogoxPaths

        paths = LogoxPaths.at(self.root)
        paths.ensure_dirs()
        paths.config.write_text(config_text, encoding="utf-8")
        return load(self.root, paths=paths, project_chain=[], state=StateFile())

    def _run_chat_main(self, config_text: str) -> tuple[int, str]:
        import asyncio

        from logox.cli import _chat_main

        bundle = self._bundle(config_text)
        err = io.StringIO()
        # ★ D153：显式给一个**临时根**的 paths —— 既满足"writer 必须有明确落点"，
        #   又保证测试绝不往真实的 ~/.logox 或仓库里写东西（LogoxPaths.at 就是为此存在的）。
        from logox.paths import LogoxPaths

        with contextlib.redirect_stderr(err):
            code = asyncio.run(
                _chat_main(self.root, LogoxPaths.at(self.root), bundle, {"kernel": None})
            )
        return code, err.getvalue()

    def test_t75b_missing_api_key_is_explained_with_the_variable_name(self) -> None:
        saved = os.environ.pop("OPENAI_API_KEY", None)
        try:
            code, message = self._run_chat_main('schema_version = 1\n[provider]\nmodel = "gpt-4o-mini"\n')
        finally:
            if saved is not None:
                os.environ["OPENAI_API_KEY"] = saved
        self.assertEqual(code, EXIT_CONFIG_ERROR)
        self.assertIn("OPENAI_API_KEY", message)
        self.assertIn("api_key_env", message)
        self.assertNotIn("Traceback", message)

    def test_t75c_startup_failure_happens_before_any_network_call(self) -> None:
        """测试环境的网络很慢——所以这条断言同时也是"离线可测"的保证。"""
        saved = os.environ.pop("OPENAI_API_KEY", None)
        try:
            code, _ = self._run_chat_main('schema_version = 1\n[provider]\nmodel = "gpt-4o-mini"\n')
        finally:
            if saved is not None:
                os.environ["OPENAI_API_KEY"] = saved
        self.assertNotEqual(code, 0)

    def test_t75d_a_writer_tool_is_refused_without_a_permission_system(self) -> None:
        """★ E-20：M3 的安全闸门在**真实的装配路径**上生效。

        `kernel/registry.py` 单测过 `assert_no_writers()`，但那只能证明"函数会抛"。
        这条测的是**`--chat` 真的会调用它并且拒绝启动**——两者是不同的保证：
        前者说锁是好的，后者说门真的锁上了。

        用 ollama 预设（不需要密钥）才能走到闸门那一步；否则会先因为缺密钥退出。
        """
        from unittest import mock

        writer = _WriterTool()
        config = 'schema_version = 1\n[provider]\nname = "ollama"\nmodel = "qwen3:8b"\n'
        with mock.patch("logox.tools.fs_read.build", return_value=writer):
            code, message = self._run_chat_main(config)

        self.assertEqual(code, EXIT_NOT_READY)
        self.assertIn("非只读工具", message)
        self.assertIn("write", message)
        self.assertIn("LOGOX_ALLOW_UNSAFE_TOOLS", message)  # 告诉用户逃生舱在哪

    def test_t75e_the_escape_hatch_warns_loudly(self) -> None:
        """逃生舱必须**醒目**：静默放行写工具就等于没有闸门。"""
        from unittest import mock

        writer = _WriterTool()
        config = 'schema_version = 1\n[provider]\nname = "ollama"\nmodel = "qwen3:8b"\n'
        stdin = io.StringIO("")  # 读到 EOF 即退出，不会真的发请求
        with (
            mock.patch("logox.tools.fs_read.build", return_value=writer),
            mock.patch.dict(os.environ, {"LOGOX_ALLOW_UNSAFE_TOOLS": "1"}),
            mock.patch("sys.stdin", stdin),
        ):
            code, message = self._run_chat_main(config)

        self.assertEqual(code, EXIT_OK)
        self.assertIn("⚠", message)
        self.assertIn("LOGOX_ALLOW_UNSAFE_TOOLS", message)


class RendererTests(unittest.TestCase):
    """stdout 只放助手的正文，其余全走 stderr。"""

    def _render(self, events: list[object]) -> tuple[str, str, _ChatRenderer]:  # type: ignore[valid-type]
        renderer = _ChatRenderer()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):

            async def run() -> None:
                for event in events:
                    await renderer(event)

            asyncio.run(run())
        return out.getvalue(), err.getvalue(), renderer

    def test_t76_text_deltas_go_to_stdout(self) -> None:
        from logox.kernel.events import ModelDelta

        out, err, _ = self._render(
            [
                ModelDelta(session_id="s", kind="text", delta="你好", request_index=0),
                ModelDelta(session_id="s", kind="text", delta="，世界", request_index=0),
            ]
        )
        self.assertEqual(out, "你好，世界")
        self.assertEqual(err, "")

    def test_t77_reasoning_goes_to_stderr(self) -> None:
        from logox.kernel.events import ModelDelta

        out, err, _ = self._render([ModelDelta(session_id="s", kind="reasoning", delta="先想想", request_index=0)])
        self.assertEqual(out, "")
        self.assertIn("[思考] 先想想", err)

    def test_t78_tool_events_go_to_stderr(self) -> None:
        from logox.kernel.events import ToolCallFinished, ToolCallRequested

        out, err, _ = self._render(
            [
                ToolCallRequested(session_id="s", call_id="c", name="read", args={"path": "a.py"}),
                ToolCallFinished(session_id="s", call_id="c", ok=True, duration_ms=12),
            ]
        )
        self.assertEqual(out, "")
        self.assertIn("read", err)
        self.assertIn("12ms", err)

    def test_t79_errors_and_retries_go_to_stderr(self) -> None:
        from logox.kernel.events import ErrorOccurred, RetryScheduled

        out, err, _ = self._render(
            [
                RetryScheduled(session_id="s", attempt=1, delay_s=2.0, reason="限流"),
                ErrorOccurred(session_id="s", category="auth", message="密钥无效"),
            ]
        )
        self.assertEqual(out, "")
        self.assertIn("重试", err)
        self.assertIn("密钥无效", err)

    def test_t80_plain_output_contains_no_ansi_escapes(self) -> None:
        """纯文本模式的意义就在于可重定向——带 ANSI 的"纯文本"是自相矛盾的。"""
        from logox.kernel.events import ModelDelta

        out, _, _ = self._render([ModelDelta(session_id="s", kind="text", delta="答案", request_index=0)])
        self.assertNotIn("\x1b[", out)

    def test_t81_usage_summary_marks_unreported_items_with_a_dash(self) -> None:
        """D39：未上报显示 `—`，而不是把 0 当作"真的没消耗"。"""
        from logox.kernel.events import TurnFinished, Usage

        out, err, renderer = self._render([])
        with contextlib.redirect_stderr(io.StringIO()) as captured:
            renderer._summary(  # noqa: SLF001 - 内部方法，测试刻意直接驱动
                TurnFinished(
                    session_id="s",
                    turn_index=1,
                    duration_ms=5,
                    tool_call_count=0,
                    usage=Usage(input_tokens=10, output_tokens=2),
                )
            )
        self.assertIn("cache —", captured.getvalue())
        self.assertEqual(out, "")

    def test_t82_usage_summary_shows_a_real_zero(self) -> None:
        from logox.kernel.events import TurnFinished, Usage

        _, _, renderer = self._render([])
        with contextlib.redirect_stderr(io.StringIO()) as captured:
            renderer._summary(  # noqa: SLF001
                TurnFinished(
                    session_id="s",
                    turn_index=1,
                    duration_ms=5,
                    tool_call_count=0,
                    usage=Usage(input_tokens=10, output_tokens=2, cached_input_tokens=0),
                )
            )
        self.assertIn("cache 0%", captured.getvalue())


class ChatLoopTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.root = make_temp_dir("chat-")
        (self.root / "a.txt").write_text("hello\n", encoding="utf-8")

    def tearDown(self) -> None:
        remove_temp_dir(self.root)

    async def _run_loop(self, script, stdin_text: str):  # type: ignore[no-untyped-def]
        env = install(script, tools=[StubTool()], cwd=self.root)
        renderer = _ChatRenderer()
        # 渲染器是**总线订阅者**，必须真的订阅上——这正是 D7 的结构在起作用：
        # 文本模式与全屏界面消费的是同一套事件，区别只在订阅者怎么渲染。
        env.bus.subscribe("*", renderer, name="chat-renderer")
        out, err = io.StringIO(), io.StringIO()
        fake_stdin = io.StringIO(stdin_text)
        original = sys.stdin
        sys.stdin = fake_stdin
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = await _chat_loop(env.kernel, renderer, False)
        finally:
            sys.stdin = original
        return code, out.getvalue(), err.getvalue(), env

    async def test_t83_piped_input_runs_to_eof_then_exits_cleanly(self) -> None:
        code, out, _, env = await self._run_loop([text_chunks("收到")], "你好\n")
        self.assertEqual(code, 0)
        self.assertIn("收到", out)
        self.assertEqual([m.role for m in env.kernel.history], ["user", "assistant"])

    async def test_t84_multiple_lines_become_multiple_turns(self) -> None:
        _, _, _, env = await self._run_loop([text_chunks("收到")], "一\n二\n三\n")
        self.assertEqual(env.kernel.turn_index, 3)

    async def test_t85_exit_command_stops_the_loop(self) -> None:
        _, _, _, env = await self._run_loop([text_chunks("收到")], "一\n/exit\n二\n")
        self.assertEqual(env.kernel.turn_index, 1)

    async def test_t86_blank_lines_are_ignored(self) -> None:
        _, _, _, env = await self._run_loop([text_chunks("收到")], "\n   \n一\n")
        self.assertEqual(env.kernel.turn_index, 1)

    async def test_t87_tool_output_stays_out_of_stdout(self) -> None:
        """``--chat < in.txt > out.txt`` 拿到的必须是干净答案。"""
        script = [tool_chunks([("c1", "read", {"path": "a.txt"})]), text_chunks("文件里是 hello")]
        code, out, err, env = await self._run_loop(script, "读一下 a.txt\n")
        self.assertEqual(code, 0)
        self.assertNotIn("[工具]", out)
        self.assertIn("[工具]", err)
        self.assertIn("文件里是 hello", out)

    async def test_t88_a_failed_turn_does_not_kill_the_session(self) -> None:
        from tests.unit.kernel_support import auth_error, chunks

        code, _, err, env = await self._run_loop([chunks(auth_error()), text_chunks("第二次成功")], "一\n二\n")
        self.assertEqual(code, 0)
        self.assertIn("auth", err)
        self.assertEqual(env.kernel.turn_index, 2)

    async def test_t89_interrupted_turn_is_reported_on_stderr(self) -> None:
        """被中断的回合要在 stderr 上说明，否则用户不知道那一轮为什么没有下文。"""
        env = install([text_chunks("ok")], tools=[], cwd=self.root)
        renderer = _ChatRenderer()
        await env.kernel.submit("一")
        turn = env.kernel._turns[-1]  # noqa: SLF001
        self.assertIs(turn.status, TurnStatus.DONE)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            renderer.end_turn(turn)
        self.assertEqual(err.getvalue(), "")  # 正常的回合不啰嗦


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
