"""M4 的验收核心：**界面真的接上了真内核**（端到端）。

为什么这组用例不可替代
======================

前面的界面用例都可以用**假内核**糊过去——它们证明的是"界面自己的逻辑对"。
这一组同时要求在场地有**四样真东西**：

1. 真的 ``KernelLoop``（不是 `FakeKernel`）；
2. 真的 ``read`` 工具 + 真的磁盘文件（不是 mock 掉的工具）；
3. 真的 ``EventBus``（事件真的走完订阅者链路）；
4. 真的 ``InlineApp`` 渲染。

并且断言的是**渲染到屏幕上的文本**，而不是中间对象。
"组件的 render() 只是中间结果，屏幕上那一行才算数"——这条教训来自本项目
真实踩过的坑（写对了行、却画在了错的位置）。

⚠️ 历史上这一组跑在 Textual 的 ``app.run_test()`` 上（Pilot）。D85 删掉
Textual 之后，界面变成自研的行式渲染器，于是这组用例**不再需要任何界面框架**
就能跑——这正是那次重写要换来的东西之一。
"""

from __future__ import annotations

import asyncio
import unittest

from logox.tools.fs_read import build as build_read_tool
from tests.tui.render_support import build_inline, screen_text, start_runtime
from tests.unit.kernel_support import Pause, text_chunks, tool_chunks, usage_chunk
from tests.unit.support import make_temp_dir, remove_temp_dir

ANSWER = "a.py 里定义了 retry()，注释说把超时改成了 30 秒。"
SETTLE = 10.0


def _read_script(answer: str = ANSWER) -> list[list[object]]:
    """两轮脚本：先请求 read，再依据读到的内容回答。"""
    read = tool_chunks([("call_1", "read", {"path": "a.py"})])
    return [
        [*read, usage_chunk(120, 30, cached=96)],
        [*text_chunks(answer), usage_chunk(400, 60, cached=350)],
    ]


def _pause_script(first: str, second: str) -> list[list[object]]:
    """先流出一句话，停一下，再流下一句——用来制造"可以在中途取消"的流。"""
    return [
        [
            {"choices": [{"index": 0, "delta": {"content": first}, "finish_reason": None}], "model": "m"},
            Pause(0.4),
            {"choices": [{"index": 0, "delta": {"content": second}, "finish_reason": None}], "model": "m"},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "model": "m"},
        ]
    ]


class InlineEndToEndTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.root = make_temp_dir("e2e-inline-")
        (self.root / "a.py").write_text(
            "def retry():\n    # 把超时改成 30 秒\n    return 30\n", encoding="utf-8"
        )

    def tearDown(self) -> None:
        remove_temp_dir(self.root)

    async def _wait_for(self, predicate, *, ticks: int = 400) -> None:
        for _ in range(ticks):
            if predicate():
                return
            await asyncio.sleep(0.01)

    async def test_ui_renders_a_real_read_and_answer(self) -> None:
        """★ 唯一的可信证明：用户提问 → 真 `read` 读真文件 → 屏幕上出现答案。

        断言四件事，缺一不可：

        * 屏幕上出现**用户的问题**（由内核发的 `UserPromptSubmit` 产生）
        * 屏幕上出现**工具卡片且状态是成功**（真工具真的跑了）
        * 屏幕上出现**模型基于文件内容给出的答案**（真回灌真的发生了）
        * 工具的**执行结果真的进了模型上下文**（不只是改了界面）
        """
        harness = build_inline(_read_script(), tools=[build_read_tool()], cwd=self.root)
        start_runtime(harness)
        turn = await asyncio.wait_for(harness.kernel.start("a.py 里写了什么？"), timeout=SETTLE)
        await asyncio.wait_for(harness.kernel.wait(turn), timeout=SETTLE)
        await harness.bus.drain()

        text = screen_text(harness)

        # ① 用户的问题
        self.assertIn("a.py 里写了什么", text)

        # ② 工具卡片：真的调用了 read，而且**没有停在运行中**
        cards = [block for block in harness.app.timeline.buffer.blocks if block.kind == "tool"]
        self.assertTrue(cards, "真工具调用必须渲染成卡片")
        self.assertEqual([card.name for card in cards], ["read"])
        self.assertEqual(cards[0].state, "ok", "回合结束后卡片必须是终态")
        self.assertIn("a.py", text)

        # ③ 模型基于文件内容的回答
        self.assertIn("retry()", text)
        self.assertIn("30 秒", text, "答案里的关键事实必须来自文件内容")

        # ④ 工具的**执行结果真的进了模型上下文**
        tool_messages = [
            block
            for message in harness.kernel.history
            for block in message.blocks
            if type(block).__name__ == "ToolResultBlock"
        ]
        self.assertTrue(tool_messages, "工具结果必须回灌进历史")
        self.assertIn("把超时改成 30 秒", tool_messages[0].content, "回灌内容应来自磁盘文件")

    async def test_escape_mid_stream_keeps_text_and_no_running_card(self) -> None:
        """中途按 Esc：屏幕上保留半句话、卡片不留"运行中"、历史配对完整。

        最后一条是 E-3 在**界面链路**上的回归：中断后历史里 ``tool_use``
        必须仍有配对的 ``tool_result``，否则下一轮请求会被厂商 400
        ——而界面正是按下 Esc 的那一方。

        ⚠️ 注意 ``turn.task``：``kernel.start()`` **立刻返回**一个 `Turn` 对象，
        真正在跑的是 ``turn.task``。用 ``create_task(kernel.start(...))`` 包一层的话，
        被包住的是"起回合"这个动作——它几毫秒就完成了，于是
        ``assertFalse(task.done())`` 之类的断言**永远为假**：测试看着是绿的，
        却什么都没测到（本文件第一次改写时就是这样）。
        """
        harness = build_inline(
            _pause_script("我先读一下 a.py：", "读到了。"), tools=[build_read_tool()], cwd=self.root
        )
        start_runtime(harness)
        turn = await harness.kernel.start("读一下 a.py")

        await self._wait_for(
            lambda: any("我先读一下" in block.text for block in harness.app.timeline.buffer.blocks)
        )
        self.assertFalse(turn.task.done(), "这一步必须发生在回合**还在跑**的时候")

        harness.app.press("escape")
        await asyncio.wait_for(turn.task, timeout=SETTLE)

        text = screen_text(harness)
        self.assertIn("我先读一下", text, "D51：已流出的正文必须保留")

        card_states = [
            block.state for block in harness.app.timeline.buffer.blocks if block.kind == "tool"
        ]
        self.assertNotIn("running", card_states, "中断后不得有卡片停在运行中")

        # E-3 回归：历史里 tool_use / tool_result 必须配对（否则下一轮发不出去）
        uses = [
            block
            for message in harness.kernel.history
            for block in message.blocks
            if type(block).__name__ == "ToolUseBlock"
        ]
        results = [
            block
            for message in harness.kernel.history
            for block in message.blocks
            if type(block).__name__ == "ToolResultBlock"
        ]
        self.assertEqual(len(uses), len(results), "中断后 tool_use 与 tool_result 必须数量相等")

    async def test_streaming_text_appears_before_the_turn_ends(self) -> None:
        """★★ **流式**：正文必须在回合结束**之前**就出现在屏幕上。

        这一条来自 D85 精简时实测抓到的一个严重缺陷：`TimelineBuffer.add_delta()`
        只把增量放进缓冲，真正的"落块"要有人按 ``ui.stream_fps`` 去做。
        新界面最初**漏了那一步**，症状是"整段回答在回合结束时一次性蹦出来"
        ——而当时**所有单测都是绿的**，因为它们直接驱动组件，绕过了这条链路。

        断言方式刻意选"回合还在跑的时候文本已经在了"：只看最终屏幕的话，
        "一次性蹦出来"与"逐字流出"的结果**完全一样**。
        """
        harness = build_inline(_pause_script("前半句", "后半句"), cwd=self.root)
        start_runtime(harness)
        turn = await harness.kernel.start("说两句")

        await self._wait_for(lambda: _has_text(harness, "前半句"))
        self.assertIn("前半句", screen_text(harness), "流式内容没有及时上屏")
        self.assertFalse(turn.task.done(), "上屏必须发生在回合结束之前（否则就是一次性蹦出来）")
        self.assertNotIn("后半句", screen_text(harness), "后半句还没到，不该已经出现")

        await asyncio.wait_for(turn.task, timeout=SETTLE)
        self.assertIn("后半句", screen_text(harness))

    async def test_second_turn_can_still_be_sent_after_interrupt(self) -> None:
        """★ 中断之后**还能继续对话**——这是 E-3 真正的验收口径。

        历史配对不完整时，厂商会在**下一轮**直接 400。因此必须真的再发一轮，
        而不是只检查历史对象。
        """
        script = [
            *_pause_script("半句", "完整"),
            [*text_chunks("第二轮回答"), usage_chunk(50, 10)],
        ]
        harness = build_inline(script, tools=[build_read_tool()], cwd=self.root)
        start_runtime(harness)

        first = await harness.kernel.start("第一次")
        await self._wait_for(lambda: _has_text(harness, "半句"))
        harness.app.press("escape")
        await asyncio.wait_for(first.task, timeout=SETTLE)

        # 中断后的历史会随**下一次请求**一起发给适配层——那一轮本身就是
        # "配对是否完整"的证明（厂商会在那一刻校验 tool_use / tool_result）。
        second = await harness.kernel.start("第二次")
        await asyncio.wait_for(second.task, timeout=SETTLE)

        self.assertEqual(len(harness.requests), 2, "中断后必须还能发出第二次请求")
        self.assertIn("第二轮回答", screen_text(harness))

    async def test_esc_closes_an_overlay_before_interrupting(self) -> None:
        """★ Esc 的优先级：**先关浮层，再中断生成**。

        顺序反过来会很难受：浮层开着时按 Esc 应该是"关掉它"，
        而不是把后台正在跑的那一回合顺手停掉。
        """
        harness = build_inline(_pause_script("先说一句", "继续说"), cwd=self.root)
        start_runtime(harness)
        turn = await harness.kernel.start("问一句")
        await self._wait_for(lambda: _has_text(harness, "先说一句"))

        # 走**真实的浮层路径**（`push_overlay`），而不是手工 show_overlay：
        # 只有前者会在收到结果后把浮层摘掉——手工放上去的浮层没人负责收尾。
        from logox.tui.content.help import render_help

        panel = _StaticPanel(render_help(harness.app.theme.palette))
        opened = asyncio.create_task(harness.app.push_overlay(panel))
        await self._wait_for(lambda: harness.app.screen.has_overlay())

        harness.app.press("escape")
        await asyncio.wait_for(opened, timeout=SETTLE)

        self.assertFalse(harness.app.screen.has_overlay(), "Esc 应当先关掉浮层")
        self.assertFalse(turn.task.done(), "浮层开着时按 Esc 不该把回合停掉")

        await asyncio.wait_for(turn.task, timeout=SETTLE)


class _StaticPanel:
    """只显示一段文本的最小浮层（测试用）。"""

    def __init__(self, text) -> None:  # noqa: ANN001
        self.text = text
        self.on_done = None
        self.focused = False

    def render(self, width: int) -> list:
        return [self.text]

    def handle_input(self, key) -> bool:  # noqa: ANN001
        if key.name == "escape":
            if self.on_done is not None:
                self.on_done(None)
            return True
        return False

    def invalidate(self) -> None:
        return None


def _has_text(harness, needle: str) -> bool:  # noqa: ANN001
    return any(needle in block.text for block in harness.app.timeline.buffer.blocks)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
