"""工具中断后，下一轮请求的每批调用必须紧跟完整结果。"""

from __future__ import annotations

import asyncio
import unittest
from copy import deepcopy

from logox.kernel import events as ev
from logox.kernel.messages import Message, MessageMeta, ToolResultBlock, ToolUseBlock, complete_tool_results
from logox.kernel.turn import TurnStatus
from logox.providers.base import ChatRequest
from logox.providers.openai_compat import OpenAICompatProvider
from logox.store.replay import reconstruct_messages
from tests.unit.kernel_support import StubTool, install, text_chunks, tool_chunks


def assert_paired_payload(test: unittest.TestCase, messages: list[dict]) -> None:
    pending: set[str] = set()
    for message in messages:
        if message["role"] == "tool":
            call_id = message["tool_call_id"]
            test.assertIn(call_id, pending, f"结果 {call_id} 未对应当前批次")
            pending.remove(call_id)
        else:
            test.assertFalse(
                pending,
                "insufficient tool messages following tool_calls message: " + str(sorted(pending)),
            )
            pending = {call["id"] for call in message.get("tool_calls", [])}
    test.assertFalse(pending, "请求末尾仍缺工具结果: " + str(sorted(pending)))


class ToolPairingRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_second_batch_keeps_next_request_paired(self) -> None:
        env = install(
            [
                tool_chunks([("first", "read", {})]),
                tool_chunks([("second", "shell", {}), ("never_started", "shell", {})]),
                text_chunks("继续回答"),
            ],
            tools=[StubTool("read"), StubTool("shell", readonly=False, delay_s=60)],
        )
        started = asyncio.Event()

        async def observe(event: ev.ToolCallStarted) -> None:
            if event.call_id == "second":
                started.set()

        env.bus.subscribe(ev.ToolCallStarted, observe, name="second-batch-started")
        turn = await env.kernel.start("先读取再运行命令")
        await asyncio.wait_for(started.wait(), timeout=2)
        turn.cancel()
        await env.kernel.wait(turn)
        self.assertIs(turn.status, TurnStatus.CANCELLED)
        self.assertEqual(env.registry.get("shell").calls, ["a.txt"])

        payloads: list[dict] = []
        original_stream = env.kernel._provider.stream

        async def capture(request: ChatRequest):
            payloads.append(env.kernel._provider.build_payload(request))
            async for event in original_stream(request):
                yield event

        env.kernel._provider.stream = capture
        await env.kernel.submit("现在呢？")
        self.assertEqual(len(payloads), 1)
        assert_paired_payload(self, payloads[0]["messages"])
        await env.bus.aclose()

    async def test_resume_partial_batch_completes_missing_results(self) -> None:
        messages = reconstruct_messages(
            [
                {"type": "user_prompt", "role": "user", "content": "运行四个命令"},
                {
                    "type": "model_output",
                    "role": "assistant",
                    "tool_calls": [
                        {"id": "done", "name": "shell", "arguments": {}},
                        {"id": "never_started", "name": "shell", "arguments": {}},
                    ],
                },
                {"type": "tool_result", "role": "tool", "call_id": "done", "content": "已完成"},
                {"type": "turn_finished", "role": "system", "reason": "cancelled"},
                {"type": "user_prompt", "role": "user", "content": "现在呢？"},
            ]
        )
        payload = OpenAICompatProvider().build_payload(ChatRequest(model="mock-model", messages=messages))
        assert_paired_payload(self, payload["messages"])
        results = [block for message in messages for block in message.blocks_of(ToolResultBlock)]
        self.assertEqual([block.id for block in results], ["done", "never_started"])
        self.assertFalse(results[-1].ok)

    async def test_cancel_next_turn_with_reused_call_id(self) -> None:
        env = install(
            [
                tool_chunks([("same_id", "read", {})]),
                text_chunks("第一轮完成"),
                tool_chunks([("same_id", "shell", {})]),
                text_chunks("继续回答"),
            ],
            tools=[StubTool("read"), StubTool("shell", readonly=False, delay_s=60)],
        )
        await env.kernel.submit("读取")
        started = asyncio.Event()

        async def observe(event: ev.ToolCallStarted) -> None:
            started.set()

        env.bus.subscribe(ev.ToolCallStarted, observe, name="next-turn-started")
        turn = await env.kernel.start("执行命令")
        await asyncio.wait_for(started.wait(), timeout=2)
        turn.cancel()
        await env.kernel.wait(turn)
        await env.kernel.submit("继续")
        payload = OpenAICompatProvider().build_payload(
            ChatRequest(model="mock-model", messages=env.kernel.history)
        )
        assert_paired_payload(self, payload["messages"])
        results = [block for message in env.kernel.history for block in message.blocks_of(ToolResultBlock)]
        self.assertEqual([block.ok for block in results], [True, False])
        self.assertEqual(results[0].content, "文件内容")
        await env.bus.aclose()

    async def test_resume_multiple_batches_and_missing_tail(self) -> None:
        records = [
            {"type": "user_prompt", "role": "user", "content": "运行命令", "line": 1},
            {
                "type": "model_output",
                "role": "assistant",
                "line": 2,
                "tool_calls": [
                    {"id": "same", "name": "shell", "arguments": {}},
                ],
            },
            {"type": "tool_result", "role": "tool", "call_id": "same", "content": "真实结果", "line": 3},
            {
                "type": "model_output",
                "role": "assistant",
                "line": 4,
                "tool_calls": [
                    {"id": "same", "name": "shell", "arguments": {}},
                ],
            },
        ]
        original_records = deepcopy(records)
        messages = reconstruct_messages(records)
        self.assertEqual(records, original_records)
        payload = OpenAICompatProvider().build_payload(ChatRequest(model="mock-model", messages=messages))
        assert_paired_payload(self, payload["messages"])
        self.assertEqual(messages[2].blocks[0].content, "真实结果")
        self.assertEqual(messages[2].meta.transcript_line, 3)
        self.assertEqual(messages[-2].meta.transcript_line, 4)
        self.assertIsNone(messages[-1].meta.transcript_line)
        self.assertFalse(messages[-1].blocks[0].ok)
        self.assertEqual(complete_tool_results(messages), messages)

    async def test_repair_preserves_existing_results_and_metadata(self) -> None:
        existing = ToolResultBlock(id="done", content="已完成", ok=True, archived=True)
        tool_meta = MessageMeta(transcript_line=5)
        history = [
            Message(
                role="assistant",
                blocks=[
                    ToolUseBlock(id="done", name="shell"),
                    ToolUseBlock(id="missing", name="shell"),
                ],
            ),
            Message(role="tool", blocks=[existing], meta=tool_meta),
            Message(role="user"),
        ]
        original_history = deepcopy(history)
        repaired = complete_tool_results(history)
        self.assertEqual(history, original_history)
        self.assertEqual(repaired[1].meta, tool_meta)
        self.assertEqual(repaired[1].blocks[0], existing)
        self.assertEqual(repaired[1].blocks[1].id, "missing")
        self.assertFalse(repaired[1].blocks[1].ok)
        self.assertEqual(complete_tool_results(repaired), repaired)
        payload = OpenAICompatProvider().build_payload(ChatRequest(model="mock-model", messages=repaired))
        assert_paired_payload(self, payload["messages"])

    async def test_repair_preserves_complete_history(self) -> None:
        history = [
            Message(role="assistant", blocks=[ToolUseBlock(id="done", name="shell")]),
            Message(role="tool", blocks=[ToolResultBlock(id="done", content="实际结果")]),
            Message(role="user"),
        ]
        repaired = complete_tool_results(history)
        self.assertEqual(repaired, history)
        self.assertTrue(all(before is after for before, after in zip(history, repaired, strict=True)))
