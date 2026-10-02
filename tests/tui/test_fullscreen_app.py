"""全屏备用屏应用生命周期与交互测试（D179 / MODULE_tui_fullscreen）。

测试目标
--------
1. FullscreenApp 初始化与 FullscreenLayout 挂载；
2. 备用屏与鼠标捕获（\x1b[?1049h / \x1b[?1000h\x1b[?1006h）；
3. 退出时原子清理与幂等恢复；
4. SGR 1006 鼠标字节流直通测试（\x1b[<64;...M 向上滚轮）；
5. 键盘翻页、Escape 回底、打字即时归位；
6. run_fullscreen 同步入口与 fail-safe 保护。
"""

from __future__ import annotations

import asyncio
import unittest
from dataclasses import dataclass, field
from typing import Any
from unittest import mock

from logox.kernel.bus import EventBus
from logox.tui.render.fullscreen import FullscreenApp, FullscreenLayout, run_fullscreen
from logox.tui.render.keys import Key
from logox.tui.render.terminal import FakeTerminal


@dataclass
class FakeKernel:
    started: list[str] = field(default_factory=list)
    cancels: int = 0

    async def start(self, text: str) -> Any:
        self.started.append(text)
        return None

    def cancel(self) -> bool:
        self.cancels += 1
        return True

    @property
    def current_turn(self) -> Any:
        return None


class _Config:
    class provider:  # noqa: N801
        thinking_effort = "auto"


class _Runtime:
    def __init__(self) -> None:
        self.kernel = FakeKernel()
        self.bus = EventBus(session_id="test-fullscreen")
        self.model = "test-model"
        self.config = _Config()
        self.closed = False

    def close(self) -> None:
        self.closed = True


def make_app(
    columns: int = 80, rows: int = 24
) -> tuple[FullscreenApp, FakeTerminal, _Runtime]:
    runtime = _Runtime()
    terminal = FakeTerminal(columns=columns, rows=rows)
    app = FullscreenApp(runtime=runtime, terminal=terminal)
    return app, terminal, runtime


class FullscreenAppTests(unittest.TestCase):
    def test_fullscreen_app_initialization(self) -> None:
        """FullscreenApp 挂载 FullscreenLayout 作为 root。"""
        app, terminal, runtime = make_app()
        self.assertIsInstance(app.root, FullscreenLayout)
        self.assertIn(app.root, app.screen.root.children)

    def test_clear_screen_enters_alt_screen_and_enables_mouse(self) -> None:
        """_clear_screen 写入备用屏转义序列与 SGR 1006 鼠标捕获。"""
        app, terminal, _runtime = make_app()
        app._clear_screen()
        written = "".join(w[0] for w in terminal.writes)
        self.assertIn("\x1b[?1049h", written, "必须进入备用屏")
        self.assertIn("\x1b[?1000h", written, "必须开启鼠标追踪")
        self.assertIn("\x1b[?1006h", written, "必须开启 SGR 1006 扩展格式")

    def test_restore_screen_exits_alt_screen_and_disables_mouse(self) -> None:
        """_restore_screen 退出备用屏并关闭鼠标跟踪，具备幂等性。"""
        app, terminal, _runtime = make_app()
        app._restore_screen()
        written = "".join(w[0] for w in terminal.writes)
        self.assertIn("\x1b[?1000l\x1b[?1006l", written, "必须关闭鼠标追踪")
        self.assertIn("\x1b[?1049l", written, "必须退出备用屏")
        self.assertIn("\x1b[?25h", written, "必须确保光标恢复可见")

        writes_count_before = len(terminal.writes)
        # 再次调用 restore_terminal 不重复写序列
        app.restore_terminal()
        self.assertEqual(len(terminal.writes), writes_count_before)

    def test_mouse_wheel_sgr_raw_input(self) -> None:
        """向终端投喂原始 SGR 滚轮转义序列，驱动视口滚动。"""
        app, terminal, _runtime = make_app(columns=80, rows=24)
        for i in range(40):
            app.timeline.buffer.add_notice(f"Line {i}")

        # 向上滚轮: \x1b[<64;10;20M
        app.send("\x1b[<64;10;20M")
        self.assertEqual(app.root.scroll_offset, 3)

        app.send("\x1b[<64;10;20M")
        self.assertEqual(app.root.scroll_offset, 6)

        # 向下滚轮: \x1b[<65;10;20M
        app.send("\x1b[<65;10;20M")
        self.assertEqual(app.root.scroll_offset, 3)

    def test_pageup_pagedown_raw_input(self) -> None:
        """通过 CSI 5~ / 6~ 驱动翻页。"""
        app, terminal, _runtime = make_app(columns=80, rows=24)
        for i in range(50):
            app.timeline.buffer.add_notice(f"Message {i}")
        # viewport = 20, half = 10

        app.send("\x1b[5~")
        self.assertEqual(app.root.scroll_offset, 10)

        app.send("\x1b[6~")
        self.assertEqual(app.root.scroll_offset, 0)

    def test_escape_resets_scroll_offset_without_cancelling_kernel(self) -> None:
        """向上翻看历史时按 Esc 只返回底部，不打断内核。"""
        app, terminal, runtime = make_app(columns=80, rows=24)
        for i in range(40):
            app.timeline.buffer.add_notice(f"Message {i}")

        app.root.scroll_offset = 8
        app._busy = True  # 假定正在生成中

        app.press("escape")
        self.assertEqual(app.root.scroll_offset, 0, "按 Esc 必须返回底部")
        self.assertEqual(runtime.kernel.cancels, 0, "第一次 Esc 不应打断正在生成的内核")

        # 当已经在底部（scroll_offset=0）且正在生成时，再次按 Esc 触发内核中断
        app.press("escape")
        self.assertEqual(runtime.kernel.cancels, 1)

    def test_typing_printable_preserves_scroll_offset(self) -> None:
        """用户在向上翻看历史时直接打字，视口保持原滚动位置，文字正常输入。"""
        app, terminal, _runtime = make_app(columns=80, rows=24)
        for i in range(40):
            app.timeline.buffer.add_notice(f"Message {i}")

        app.root.scroll_offset = 10
        app.send("hi")
        self.assertEqual(app.root.scroll_offset, 10, "打字不应强制使视口归零")
        self.assertEqual(app.editor.text, "hi")

    def test_submit_resets_scroll_offset(self) -> None:
        """提交消息时自动回底。"""
        app, terminal, runtime = make_app(columns=80, rows=24)
        app.root.scroll_offset = 12
        app.editor.set_text("hello")
        app.press("enter")
        self.assertEqual(app.root.scroll_offset, 0)
        self.assertEqual(runtime.kernel.started, ["hello"])

    def test_run_fullscreen_sync_entrypoint(self) -> None:
        """run_fullscreen 完整启动并正常退出。"""
        runtime = _Runtime()
        terminal = FakeTerminal(columns=80, rows=24)

        async def _stop_soon():
            await asyncio.sleep(0.01)
            # 模拟用户按 Ctrl+D 退出
            # 找到当前 running 的 app
            pass

        async def fake_run(self):
            # 模拟执行
            self.stop()
            return

        with mock.patch.object(FullscreenApp, "run", fake_run):
            exit_code = run_fullscreen(runtime, terminal=terminal)
            self.assertEqual(exit_code, 0)
            self.assertTrue(runtime.closed)
            self.assertTrue(terminal.stopped)


if __name__ == "__main__":
    unittest.main()
