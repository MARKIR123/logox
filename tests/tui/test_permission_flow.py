"""工具权限确认的**整条链路**（内核 → 装配根 → 新界面）。

与另外两个文件的分工
==================

* `test_render_overlay.py`   —— 弹窗**长什么样**、按键怎么映射到四个选项
* `test_permission_decider.py` —— 决策器**翻译得对不对**（含每一种失败都拒绝）
* **本文件** —— 三者接起来**真的跑一遍**：真内核、真总线、真脚本 provider、
  真 `InlineApp`、一个真的需要授权的工具，然后**用按键回答弹窗**。

为什么非要这一条：前面两个文件各自全绿，也完全可能"没人把决策器装进内核"——
那种情况下权限确认**一次都不会触发**，而每一个单测都通过。整条链路才是证据。
"""

from __future__ import annotations

import asyncio
import unittest
from typing import Any

from logox.app import UiPermissionDecider
from logox.kernel.bus import EventBus
from logox.kernel.loop import KernelLoop
from logox.tui.render.app import InlineApp
from logox.tui.render.keys import Key
from tests.tui.render_support import build_inline
from tests.unit.kernel_support import StubTool, text_chunks, tool_chunks


def build_harness(
    *,
    tool: StubTool,
    script: list[list[Any]] | None = None,
    rows: int = 30,
    decider: UiPermissionDecider | None = None,
) -> tuple[InlineApp, KernelLoop, EventBus, StubTool]:
    """真内核 + 真总线 + 真界面 + 一个需要授权的工具。"""
    # `tool_chunks` 的元组顺序是 ``(call_id, tool_name, arguments)``——
    # 写反了不会报错，只会得到一张 `unknown_tool` 的卡片（实测踩到）。
    calls = [("c1", tool.spec.name, {"path": "hello.py"})]
    harness = build_inline(
        script or [tool_chunks(calls), text_chunks("好的")],
        tools=[tool],
        decider=decider if decider is not None else UiPermissionDecider(cwd="."),
        rows=rows,
    )
    return harness.app, harness.kernel, harness.bus, tool


class PermissionFlowTests(unittest.IsolatedAsyncioTestCase):
    """四种回答各自的后果（走完整条链路）。"""

    async def _run_turn(self, tool: StubTool, *keys: Key) -> InlineApp:
        app, kernel, _bus, _tool = build_harness(tool=tool)
        app._loop = asyncio.get_running_loop()  # noqa: SLF001
        task = asyncio.create_task(kernel.start("帮我写文件"))
        await _wait_for_overlay(app)
        self.assertTrue(app.screen.has_overlay(), "权限弹窗没有出现")
        app.press(*keys)
        await asyncio.wait_for(task, timeout=5)
        # 让浮层收尾的那一帧画完
        await asyncio.sleep(0)
        return app

    async def test_allow_once_runs_the_tool(self) -> None:
        """★ 按 ``1``（仅本次允许）→ 工具**真的执行了**。"""
        tool = StubTool("write", readonly=False, requires_permission=True)
        app = await self._run_turn(tool, Key("1", char="1"))
        self.assertEqual(tool.calls, ["hello.py"], "允许了却没执行")
        self.assertFalse(app.screen.has_overlay(), "弹窗没有关掉")

    async def test_deny_skips_the_tool_and_tells_the_model(self) -> None:
        """★ 按 ``3``（拒绝）→ 工具**没执行**，而模型收到"被拒绝"以便自愈。

        注意这条路径**不发**权限事件（调度器只在"被问过"时才发）：用户拒绝之后
        模型拿到的是一个普通的失败结果，它会换个办法继续——这正是我们要的。
        卡片上的 ``denied`` 就是那个失败被归到的类别。
        """
        tool = StubTool("write", readonly=False, requires_permission=True)
        app = await self._run_turn(tool, Key("3", char="3"))
        self.assertEqual(tool.calls, [], "拒绝了却还是执行了")
        self.assertIn("denied", app.frame_text(), "拒绝没有被归成 denied 类别")

    async def test_custom_feedback_input_denies_and_supplies_reason_to_model(self) -> None:
        """★ 用户在输入框键入补充要求并回车 → 工具未执行，模型收到补充要求以便自愈调整。"""
        tool = StubTool("write", readonly=False, requires_permission=True)
        app, kernel, _bus, _tool = build_harness(tool=tool)
        app._loop = asyncio.get_running_loop()  # noqa: SLF001
        task = asyncio.create_task(kernel.start("帮我写文件"))
        await _wait_for_overlay(app)
        self.assertTrue(app.screen.has_overlay(), "权限弹窗没有出现")

        # 键入补充意见（非数字字符自动进入输入框），然后按 Enter
        app.press(
            Key("a", char="改"),
            Key("b", char="用"),
            Key("c", char="只"),
            Key("d", char="读"),
            Key("enter"),
        )
        await asyncio.wait_for(task, timeout=5)
        await asyncio.sleep(0)

        self.assertEqual(tool.calls, [], "拒绝后工具不应执行")
        self.assertEqual(app.last_feedback, "改用只读")
        # 验证模型收到的 ToolResultBlock 包含补充意见
        tool_results = [m for m in kernel.history if m.role == "tool"]
        self.assertTrue(len(tool_results) > 0)
        self.assertIn("用户补充要求：改用只读", tool_results[0].blocks[0].content)
        # 验证时间线/卡片提示已拒绝并带有补充要求
        self.assertIn("已拒绝调用，补充要求：改用只读", app.frame_text())

    async def test_escape_denies(self) -> None:
        tool = StubTool("write", readonly=False, requires_permission=True)
        await self._run_turn(tool, Key("escape"))
        self.assertEqual(tool.calls, [])

    async def test_session_allow_does_not_ask_twice(self) -> None:
        """★ 按 ``2``（本会话总是允许）→ 同一会话内**第二次不再问**。"""
        tool = StubTool("write", readonly=False, requires_permission=True)
        calls = [("c1", "write", {"path": "hello.py"})]
        # 两轮都要有工具调用：`scripted_provider` 超过脚本长度后会**重复最后一项**，
        # 而最后一项如果只是文本，第二轮就根本不会调工具（那样这条用例会假通过）。
        script = [
            tool_chunks(calls),
            text_chunks("好的"),
            tool_chunks(calls),
            text_chunks("好的"),
        ]
        app, kernel, _bus, _tool = build_harness(tool=tool, script=script)
        app._loop = asyncio.get_running_loop()  # noqa: SLF001

        first = asyncio.create_task(kernel.start("第一次"))
        await _wait_for_overlay(app)
        app.press(Key("2", char="2"))
        await asyncio.wait_for(first, timeout=5)

        second = asyncio.create_task(kernel.start("第二次"))
        # 不该再有弹窗：等若干轮事件循环，期间确认浮层始终没出现
        for _ in range(20):
            await asyncio.sleep(0.005)
            self.assertFalse(app.screen.has_overlay(), "会话内允许之后又问了一次")
            if second.done():
                break
        await asyncio.wait_for(second, timeout=5)
        self.assertEqual(len(tool.calls), 2, "两次调用都该执行")

    async def test_persisted_allow_skips_the_dialog_entirely(self) -> None:
        """★ 持久允许过之后，**连弹窗都不出现**。"""
        tool = StubTool("write", readonly=False, requires_permission=True)
        app, kernel, _bus, _tool = build_harness(tool=tool)
        app._loop = asyncio.get_running_loop()  # noqa: SLF001
        app.runtime.permission_decider.seed_persisted(["write"])

        task = asyncio.create_task(kernel.start("帮我写文件"))
        await asyncio.wait_for(task, timeout=5)
        self.assertEqual(tool.calls, ["hello.py"])
        self.assertFalse(app.screen.has_overlay())

    async def test_tools_that_do_not_require_permission_never_ask(self) -> None:
        """只读且不需要授权的工具（今天的 `read`）**一次都不该问**。"""
        tool = StubTool("read", readonly=True, requires_permission=False)
        app, kernel, _bus, _tool = build_harness(tool=tool)
        app._loop = asyncio.get_running_loop()  # noqa: SLF001
        task = asyncio.create_task(kernel.start("读文件"))
        await asyncio.wait_for(task, timeout=5)
        self.assertEqual(tool.calls, ["hello.py"])
        self.assertFalse(app.screen.has_overlay())


class TinyTerminalTests(unittest.IsolatedAsyncioTestCase):
    """终端太矮时**不能**画一张按钮看不见的弹窗。"""

    async def test_too_small_terminal_denies_with_a_reason(self) -> None:
        """★ 弹窗放不下 → 拒绝 + 说明原因。

        画出来的后果是"这个回合永远卡在等待授权上"：用户不知道按什么键，
        也没有任何提示告诉他为什么没反应——那比"拒绝并解释"糟糕得多。
        """
        tool = StubTool("write", readonly=False, requires_permission=True)
        app, kernel, _bus, _tool = build_harness(tool=tool, rows=8)
        app._loop = asyncio.get_running_loop()  # noqa: SLF001
        task = asyncio.create_task(kernel.start("帮我写文件"))
        await asyncio.wait_for(task, timeout=5)
        self.assertEqual(tool.calls, [], "终端放不下弹窗时不该放行")
        self.assertIn("终端太小", app.frame_text())


class DeciderRegistrationTests(unittest.TestCase):
    def test_app_registers_itself_as_the_prompter(self) -> None:
        """★ 界面构造时就把自己注册成提问者。

        漏注册的症状是**工具静默被拒绝**（决策器回落到"问不了"那条路径），
        而用户只会觉得"这个工具坏了"。
        """
        tool = StubTool("write", readonly=False, requires_permission=True)
        app, _kernel, _bus, _tool = build_harness(tool=tool)
        self.assertIs(app.runtime.permission_decider.prompter, app)

    def test_stop_unregisters_the_prompter(self) -> None:
        """退出后没有人能回答弹窗 → 必须回到"拒绝"这条安全路径。"""
        tool = StubTool("write", readonly=False, requires_permission=True)
        app, _kernel, _bus, _tool = build_harness(tool=tool)
        app.stop()
        self.assertIsNone(app.runtime.permission_decider.prompter)

    def test_ask_without_a_loop_raises_so_the_caller_can_deny(self) -> None:
        """没有事件循环时**抛异常**，由决策器兜成拒绝。

        这条钉住的是"失败方向"：`ask_permission` 遇到不可能回答的情况时
        必须**明确失败**，而不是返回一个看起来像"允许"的值。决策器那边
        有一条 `except → DENY`（见 `test_permission_decider.py`）。
        """
        from logox.permission_types import PermissionAsk

        tool = StubTool("write", readonly=False, requires_permission=True)
        app, _kernel, _bus, _tool = build_harness(tool=tool)
        with self.assertRaises(RuntimeError):
            asyncio.run(app.ask_permission(PermissionAsk(tool="write")))


async def _wait_for_overlay(app: InlineApp, *, ticks: int = 200) -> None:
    """等到浮层出现（或超时）。

    为什么不能"睡固定时长"：`kernel.start` 要先建上下文、发请求、解析工具调用，
    耗时与机器负载有关。轮询"弹窗出现了没有"既确定又快。
    """
    for _ in range(ticks):
        await asyncio.sleep(0)
        if app.screen.has_overlay():
            return


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
