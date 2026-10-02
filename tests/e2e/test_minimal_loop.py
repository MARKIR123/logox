"""M3 的验收核心：**端到端最小闭环**（MODULE_kernel_loop §7.4）。

"能读文件并回答"这句话里，三个词都必须在场才算数：

* **文件**——真的磁盘文件，不是 mock 掉的工具（T-63）
* **读**——真的走 ``read`` 工具，真的把内容回灌给模型
* **回答**——真的第二遍调模型，真的把答案落进历史

所以这里只有 provider 是回放的，其余全部是真的：真内核、真事件总线、真工具、
真文件系统、真编码处理。这是"能读文件并回答"唯一可信的证明——
前面的单元用例都可以用假工具糊过去，只有这一组不能。
"""

from __future__ import annotations

import asyncio
import unittest

from logox.kernel.messages import ToolResultBlock, ToolUseBlock
from logox.kernel.turn import TurnStatus
from logox.tools.fs_read import build as build_read_tool
from tests.unit.kernel_support import (
    install,
    text_chunks,
    tool_chunks,
    usage_chunk,
)
from tests.unit.support import make_temp_dir, remove_temp_dir

ANSWER = "a.py 里定义了 retry()，它在第 3 行把超时改成了 30 秒。"
READ_A_PY = tool_chunks([("call_1", "read", {"path": "a.py"})])


def read_script(answer: str = ANSWER) -> list[list[object]]:
    """两轮脚本：先读文件，再依据读到的内容回答。"""
    return [
        [*READ_A_PY[:-1], usage_chunk(120, 30, cached=96)],
        [*text_chunks(answer)[:-1], usage_chunk(400, 60, cached=350)],
    ]


class MinimalLoopTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.root = make_temp_dir("e2e-")
        (self.root / "a.py").write_text(
            "def retry():\n    # 把超时改成 30 秒\n    return 30\n", encoding="utf-8"
        )

    def tearDown(self) -> None:
        remove_temp_dir(self.root)

    def _env(self, script, **kwargs):  # type: ignore[no-untyped-def]
        return install(script, tools=[build_read_tool()], cwd=self.root, **kwargs)

    async def test_t60_full_event_sequence_is_exactly_as_designed(self) -> None:
        """完整事件序列逐个断言——顺序错了，界面就会讲一个错误的故事。

        ★ CHANGE-005 裁定 8：第二个 `context_built` 是**回合内复检**。
        工具结果回来后上下文变了，所以要**重新组装一次视图**并重新判水位线 ——
        否则本回合的工具输出会一路涨到超窗口（见 `KernelLoop._body`）。
        """
        env = self._env(read_script())
        turn = await env.kernel.submit("a.py 里写了什么？")
        self.assertIs(turn.status, TurnStatus.DONE)
        self.assertEqual(
            env.recorder.types(),
            [
                "user_prompt_submit",
                "context_built",
                "model_request_started",
                "model_delta",  # tool_args
                "model_request_finished",
                "tool_call_requested",
                "tool_call_started",
                "tool_call_finished",
                "context_built",  # ★ 回合内复检（裁定 8）
                "model_request_started",
                "model_delta",  # 答案（一个分片）
                "model_request_finished",
                "turn_finished",
            ],
        )

    async def test_t61_the_real_file_content_reached_the_model(self) -> None:
        """★ 这一条是"能读文件并回答"的核心：工具真的读到了磁盘上的字节。"""
        env = self._env(read_script())
        await env.kernel.submit("a.py 里写了什么？")

        tool_message = next(m for m in env.kernel.history if m.role == "tool")
        block = tool_message.blocks[0]
        self.assertIsInstance(block, ToolResultBlock)
        self.assertTrue(block.ok)
        self.assertIn("def retry():", block.content)  # 真的读到了磁盘内容
        self.assertIn("把超时改成 30 秒", block.content)  # 中文没有乱码
        self.assertIn("1\t", block.content)  # 带行号

    async def test_t62_the_model_saw_the_file_path_it_asked_for(self) -> None:
        """请求消息里必须能看到模型**自己发出的**工具调用，否则它无从回顾。"""
        env = self._env(read_script())
        await env.kernel.submit("a.py 里写了什么？")
        assistant = env.kernel.history[1]
        self.assertIsInstance(assistant.blocks[0], ToolUseBlock)
        self.assertEqual(assistant.blocks[0].name, "read")
        self.assertEqual(assistant.blocks[0].input, {"path": "a.py"})

    async def test_t63_final_answer_is_the_last_assistant_message(self) -> None:
        env = self._env(read_script())
        await env.kernel.submit("a.py 里写了什么？")
        self.assertEqual(env.kernel.history[-1].text, ANSWER)
        self.assertEqual([m.role for m in env.kernel.history], ["user", "assistant", "tool", "assistant"])

    async def test_t64_metrics_are_end_to_end_consistent(self) -> None:
        """D39 的三个度量字段在这条路径上必须真的成立。"""
        env = self._env(read_script())
        turn = await env.kernel.submit("a.py 里写了什么？")

        finished = env.recorder.of("model_request_finished")
        self.assertEqual(len(finished), 2)
        self.assertTrue(all(item.usage_reported for item in finished))
        self.assertEqual(finished[0].usage.cached_input_tokens, 96)
        self.assertEqual(finished[1].usage.cached_input_tokens, 350)

        turn_finished = env.recorder.find("turn_finished")
        self.assertEqual(turn_finished.usage.input_tokens, 520)  # 120 + 400
        self.assertEqual(turn_finished.usage.output_tokens, 90)  # 30 + 60
        self.assertEqual(turn.tool_call_count, 1)

    async def test_t65_cost_is_none_for_an_unpriced_model(self) -> None:
        """未知模型**不猜价格**——状态栏显示 `—` 比一个假数字好。"""
        env = self._env(read_script(), model="mock-model")
        await env.kernel.submit("a.py 里写了什么？")
        self.assertIsNone(env.recorder.of("model_request_finished")[0].cost_usd)

    async def test_t65b_cost_is_computed_when_the_assembly_root_injects_an_estimator(self) -> None:
        """费用估算由**装配根注入**——内核不认识 L5 的价格表（R1）。

        这条同时验证了接缝两个方向：不注入 → 不猜；注入 → 算得出来。
        """
        from logox.providers.pricing import estimate_cost_usd

        env = self._env(
            read_script(),
            model="deepseek-flash",
            cost_estimator=lambda usage, model: estimate_cost_usd(usage, model),
        )
        await env.kernel.submit("a.py 里写了什么？")
        costs = [item.cost_usd for item in env.recorder.of("model_request_finished")]
        self.assertTrue(all(cost is not None for cost in costs))
        self.assertGreater(sum(costs), 0.0)  # type: ignore[arg-type]

    async def test_t66_retry_in_the_middle_still_produces_the_same_answer(self) -> None:
        """首轮 429（尚无输出）→ 退避重试 → 后续与 T-60 完全一致。"""
        from tests.unit.kernel_support import chunks, rate_limit_error

        delays: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            delays.append(seconds)

        env = self._env([chunks(rate_limit_error()), *read_script()], sleep_fn=fake_sleep)
        turn = await env.kernel.submit("a.py 里写了什么？")

        self.assertIs(turn.status, TurnStatus.DONE)
        self.assertEqual(len(env.recorder.of("retry_scheduled")), 1)
        self.assertTrue(delays)
        self.assertEqual(env.kernel.history[-1].text, ANSWER)

    async def test_t67_duplicate_reads_in_one_batch_run_concurrently(self) -> None:
        """批内全只读 → 并发（D27），且结果顺序仍与请求顺序一致。"""
        (self.root / "b.py").write_text("b = 2\n", encoding="utf-8")
        script = [
            tool_chunks([("c1", "read", {"path": "a.py"}), ("c2", "read", {"path": "b.py"})]),
            text_chunks("两个文件都读到了。"),
        ]
        env = self._env(script)
        turn = await env.kernel.submit("两个文件都读一下")
        self.assertEqual(turn.tool_call_count, 2)
        started = env.recorder.of("tool_call_started")
        self.assertTrue(all(item.concurrent_group == 0 for item in started))
        results = [b for m in env.kernel.history if m.role == "tool" for b in m.blocks]
        self.assertIn("def retry():", results[0].content)
        self.assertIn("b = 2", results[1].content)

    async def test_t68_missing_file_is_reported_to_the_model_not_crashed(self) -> None:
        """读一个不存在的文件：回合照常结束，模型收到可读的错误说明。"""
        script = [
            tool_chunks([("c1", "read", {"path": "nope.py"})]),
            text_chunks("那个文件不存在。"),
        ]
        env = self._env(script)
        turn = await env.kernel.submit("读一下 nope.py")
        self.assertIs(turn.status, TurnStatus.DONE)
        block = next(m for m in env.kernel.history if m.role == "tool").blocks[0]
        self.assertFalse(block.ok)
        self.assertIn("文件不存在", block.content)
        self.assertFalse(env.recorder.of("error_occurred"))

    async def test_t69_escape_during_the_answer_keeps_everything_consistent(self) -> None:
        """按 Esc 打断回答：历史里留下半句话，且**下一轮仍然能正常发出**。"""
        from tests.unit.kernel_support import Pause

        script = [
            READ_A_PY,
            [*text_chunks("a.py 里定义了 retry()")[:-1], Pause(0.3)],
            text_chunks("继续说的话。"),
        ]
        env = self._env(script)
        turn = await env.kernel.start("a.py 里写了什么？")
        await asyncio.sleep(0.1)
        turn.cancel()
        await env.kernel.wait(turn)

        self.assertIs(turn.status, TurnStatus.CANCELLED)
        self.assertEqual(env.recorder.find("turn_finished").reason, "cancelled")
        # 半句话留在历史里（D51）
        self.assertEqual(env.kernel.history[-1].text, "a.py 里定义了 retry()")
        # 历史仍然**结构完整**：工具调用与结果一一对应（E-3）
        uses = [b for m in env.kernel.history for b in m.blocks if isinstance(b, ToolUseBlock)]
        results = [b for m in env.kernel.history for b in m.blocks if isinstance(b, ToolResultBlock)]
        self.assertEqual({b.id for b in uses}, {b.id for b in results})
        # 中断之后还能开新的一轮
        second = await env.kernel.submit("接着说")
        self.assertIs(second.status, TurnStatus.DONE)


class HistoryValidityTests(unittest.IsolatedAsyncioTestCase):
    """历史能不能**直接发出去**——这是 E-3 的真正后果。"""

    def setUp(self) -> None:
        self.root = make_temp_dir("e2e-hist-")

    def tearDown(self) -> None:
        remove_temp_dir(self.root)

    async def _run_and_interrupt(self):  # type: ignore[no-untyped-def]
        from tests.unit.kernel_support import Pause

        (self.root / "a.py").write_text("x = 1\n", encoding="utf-8")
        script = [
            tool_chunks([("c1", "read", {"path": "a.py"}), ("c2", "read", {"path": "a.py"})]),
            [*text_chunks("读到了")[:-1], Pause(0.3)],
        ]
        env = install(script, tools=[build_read_tool()], cwd=self.root)
        turn = await env.kernel.start("读两遍")
        await asyncio.sleep(0.1)
        turn.cancel()
        await env.kernel.wait(turn)
        return env

    async def test_t70_history_after_a_tool_phase_interrupt_is_sendable(self) -> None:
        """★ E-3 的端到端证明：把中断后的历史真的喂给适配器，看它能不能构造出请求。"""
        env = await self._run_and_interrupt()
        provider = env.kernel._provider  # noqa: SLF001 - 刻意用真实适配器转换一遍

        from logox.providers.base import ChatRequest

        request = ChatRequest(model="mock-model", messages=list(env.kernel.history), tools=[])
        payload = provider.build_payload(request)

        tool_messages = [m for m in payload["messages"] if m["role"] == "tool"]
        assistant_tool_calls = [
            call for m in payload["messages"] if m.get("tool_calls") for call in m["tool_calls"]
        ]
        self.assertEqual(len(tool_messages), 2)
        self.assertEqual(len(assistant_tool_calls), 2)
        self.assertEqual(
            {m["tool_call_id"] for m in tool_messages},
            {call["id"] for call in assistant_tool_calls},
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
