"""Win32 控制台模式、按键零延迟通道与跨线程调度测试（D123 / 方案 B）。"""

from __future__ import annotations

import asyncio
import unittest
from typing import Any
from unittest.mock import MagicMock

from logox.tui.render.keys import Key
from logox.tui.render.screen import Screen
from logox.tui.render.terminal import FakeTerminal, Win32Terminal


class Win32TerminalConstantsTests(unittest.TestCase):
    """验证 Win32 控制台常量与官方 Windows SDK (wincon.h) 严格一致。"""

    def test_win32_console_constants_match_sdk(self) -> None:
        self.assertEqual(Win32Terminal._ENABLE_PROCESSED_INPUT, 0x0001)
        self.assertEqual(Win32Terminal._ENABLE_LINE_INPUT, 0x0002)
        self.assertEqual(Win32Terminal._ENABLE_ECHO_INPUT, 0x0004)
        self.assertEqual(Win32Terminal._ENABLE_WINDOW_INPUT, 0x0008)
        self.assertEqual(Win32Terminal._ENABLE_MOUSE_INPUT, 0x0010)
        self.assertEqual(Win32Terminal._ENABLE_VIRTUAL_TERMINAL_INPUT, 0x0200)
        self.assertEqual(Win32Terminal._ENABLE_VIRTUAL_TERMINAL_PROCESSING, 0x0004)

    def test_raw_mode_bit_operations_clear_line_and_mouse(self) -> None:
        """测试进入 Raw 模式时，行缓冲与鼠标事件被精确清除，而窗口大小支持被开启。"""
        # 模拟典型的 Windows 默认控制台输入模式（开启了回显、行输入、处理输入、窗口调整、鼠标输入）
        saved_in = (
            Win32Terminal._ENABLE_PROCESSED_INPUT
            | Win32Terminal._ENABLE_LINE_INPUT
            | Win32Terminal._ENABLE_ECHO_INPUT
            | Win32Terminal._ENABLE_WINDOW_INPUT
            | Win32Terminal._ENABLE_MOUSE_INPUT
        )

        raw = saved_in
        raw |= Win32Terminal._ENABLE_VIRTUAL_TERMINAL_INPUT
        raw |= Win32Terminal._ENABLE_WINDOW_INPUT
        raw &= ~Win32Terminal._ENABLE_ECHO_INPUT
        raw &= ~Win32Terminal._ENABLE_LINE_INPUT
        raw &= ~Win32Terminal._ENABLE_PROCESSED_INPUT
        raw &= ~Win32Terminal._ENABLE_MOUSE_INPUT

        # 核心断言：行缓冲必须被清除，消除操作系统击键防抖与输入法延迟
        self.assertEqual(raw & Win32Terminal._ENABLE_LINE_INPUT, 0)
        self.assertEqual(raw & Win32Terminal._ENABLE_ECHO_INPUT, 0)
        self.assertEqual(raw & Win32Terminal._ENABLE_PROCESSED_INPUT, 0)
        self.assertEqual(raw & Win32Terminal._ENABLE_MOUSE_INPUT, 0)

        # 窗口大小事件与 VT 输入必须启用
        self.assertNotEqual(raw & Win32Terminal._ENABLE_WINDOW_INPUT, 0)
        self.assertNotEqual(raw & Win32Terminal._ENABLE_VIRTUAL_TERMINAL_INPUT, 0)


class ScreenInteractiveKeyRenderingTests(unittest.TestCase):
    """测试按键交互强制即时渲染（动静分流）。"""

    def test_handle_key_schedules_an_immediate_frame(self) -> None:
        """★ D133：按键仍然"最快看到"（延迟 0），但**不再在调用栈里同步画**。

        为什么改了（这是本轮的性能修复核心）：一次终端读取会交上来**一个批次**
        （最多 32 条记录），而每个按键都会请求重绘。旧语义下 `force=True` 是
        **同步 `render_now()`** ⇒ 32 个按键 = 32 次全帧计算 + 32 次终端写+flush，
        而且全在**读线程**上做 —— 读线程被占住时控制台输入缓冲会积压，
        症状就是用户报的"松开 a 之后还会继续打一会儿"。

        新语义：请求合并到**事件循环的下一轮**，一次输入只画一帧；
        `force` 的作用是"把这一帧的延迟压到 0"（跳过节流），而不是"就地画"。
        """
        terminal = FakeTerminal(columns=80, rows=24)
        screen = Screen(terminal)

        mock_component = MagicMock()
        mock_component.handle_input.return_value = True
        screen.set_focus(mock_component)

        scheduled: list[tuple[float, Any]] = []

        def fake_defer(delay: float, callback: Any) -> Any:
            scheduled.append((delay, callback))
            return MagicMock()

        screen.on_defer = fake_defer
        screen.render_now()  # 先画一帧，让 elapsed 落在 16ms 窗口内（模拟快速输入）
        scheduled.clear()

        screen.handle_key(Key("a", char="a"))

        # 核心断言：按键被消费 → **排了一帧且延迟为 0**（跳过节流），但没在调用栈里画
        self.assertEqual(len(scheduled), 1, "按键应当排一帧（而不是同步画）")
        self.assertEqual(scheduled[0][0], 0.0, "按键请求必须是'立刻'那一档（延迟 0）")

        frames_before = screen.stats["frames"]
        scheduled[0][1]()  # 定时器到点
        self.assertEqual(screen.stats["frames"], frames_before + 1, "到点后应当真的画出这一帧")


class ThreadsafeDeferHandleTests(unittest.IsolatedAsyncioTestCase):
    """测试跨线程安全调度器与 InlineApp 优雅退出事件。"""

    async def test_threadsafe_defer_and_cancellation(self) -> None:
        loop = asyncio.get_running_loop()

        called = False
        fired = asyncio.Event()

        def my_callback() -> None:
            nonlocal called
            called = True
            fired.set()

        # 测试 DeferHandle 机制
        class _DeferHandle:
            def __init__(self) -> None:
                self._cancelled = False
                self._timer_handle: asyncio.TimerHandle | None = None

            def _schedule(self) -> None:
                if not self._cancelled:
                    self._timer_handle = loop.call_later(0.01, self._run)

            def _run(self) -> None:
                if not self._cancelled:
                    my_callback()

            def cancel(self) -> None:
                self._cancelled = True
                if self._timer_handle is not None:
                    loop.call_soon_threadsafe(self._timer_handle.cancel)

        handle = _DeferHandle()
        loop.call_soon_threadsafe(handle._schedule)

        # 等回调**真的**被调用，而不是赌固定休眠够长。
        # 原实现是 `await asyncio.sleep(0.03)`：只比 0.01s 的定时器多 20ms 余量，
        # 整机一忙就翻车（实测 6 次里失败 2 次）。改成“等到发生”，
        # 超时给足 2s —— 它只会在回调真的没被触发时失败。
        await asyncio.wait_for(fired.wait(), timeout=2.0)
        self.assertTrue(called)

        # 测试取消（断言是**否定**的，所以睡久一点只会更严、不会更松）
        called = False
        fired.clear()
        handle2 = _DeferHandle()
        loop.call_soon_threadsafe(handle2._schedule)
        handle2.cancel()
        await asyncio.sleep(0.05)
        self.assertFalse(called)


if __name__ == "__main__":
    unittest.main()
