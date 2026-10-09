"""真实适配器回放：结束原因、截断续写和工具批次安全。"""

import asyncio
import json
import unittest
from types import SimpleNamespace

from logox.config.schema import KernelConfig
from logox.context.storage import SessionTranscriptWriter
from logox.errors import ErrorCategory
from logox.kernel.scheduler import AllowAllDecider
from logox.kernel.turn import TurnStatus
from logox.providers.anthropic import AnthropicProvider
from logox.providers.base import ChatRequest, DeltaEvent, ProviderErrorEvent, StopEvent
from logox.store.persistence import SessionPersistenceSubscriber
from logox.store.replay import reconstruct_messages
from tests.contract.support import stream_of
from tests.tui.render_support import build_inline, make_config
from tests.unit.kernel_support import Pause, StubTool, install, text_chunks, tool_chunks, usage_chunk
from tests.unit.support import make_temp_dir, remove_temp_dir


class ContinuationDecider(AllowAllDecider):
    def __init__(self, answers):
        self.answers = iter(answers)
        self.asked = []

    async def ask_continuation(self, turn, iteration):
        self.asked.append(iteration)
        return next(self.answers, False)


class RecordingProvider:
    name = "recording"

    def __init__(self, wrapped):
        self.wrapped = wrapped
        self.requests = []
        self.second_started = asyncio.Event()

    async def stream(self, request):
        self.requests.append(request)
        if len(self.requests) == 2:
            self.second_started.set()
        async for event in self.wrapped.stream(request):
            yield event


def response(*, text=None, reasoning=None, stop="stop"):
    delta = {}
    if text is not None:
        delta["content"] = text
    if reasoning is not None:
        delta["reasoning_content"] = reasoning
    return [{"choices": [{"index": 0, "delta": delta, "finish_reason": stop}]}]


class ResponseTerminationTests(unittest.IsolatedAsyncioTestCase):
    async def test_natural_text_finishes_without_continuation(self):
        env = install([response(text="完成", reasoning="检查")])
        turn = await env.kernel.submit("任务")
        self.assertIs(turn.status, TurnStatus.DONE)
        self.assertEqual(len(env.recorder.of("model_request_started")), 1)

    async def test_stop_sequence_text_is_a_natural_final(self):
        env = install([response(text="完成", stop="stop_sequence")])
        turn = await env.kernel.submit("任务")
        self.assertIs(turn.status, TurnStatus.DONE)
        self.assertEqual(len(env.recorder.of("model_request_started")), 1)

    async def test_only_explicit_truncated_parameter_error_continues(self):
        for category, flagged, expected in (
            (ErrorCategory.BAD_REQUEST, True, TurnStatus.DONE),
            (ErrorCategory.BAD_REQUEST, False, TurnStatus.FAILED),
            (ErrorCategory.NETWORK, True, TurnStatus.FAILED),
        ):
            with self.subTest(category=category, flagged=flagged):
                env = install([text_chunks("完成")])
                wrapped = env.kernel._provider

                class FirstError:
                    name = "error"

                    def __init__(self, wrapped_provider, error):
                        self.first = True
                        self.wrapped = wrapped_provider
                        self.error = error

                    async def stream(self, request):
                        if self.first:
                            self.first = False
                            yield DeltaEvent(kind="text", text="半句")
                            yield self.error
                        else:
                            async for event in self.wrapped.stream(request):
                                yield event

                error = ProviderErrorEvent(category=category, message="Token 上限", is_truncated=flagged)
                env.kernel.set_provider(FirstError(wrapped, error))
                turn = await env.kernel.submit("任务")
                self.assertIs(turn.status, expected)
                self.assertEqual(len(env.recorder.of("model_request_started")), 2 if expected is TurnStatus.DONE else 1)

    async def test_truncated_batch_never_requests_permission(self):
        class NoAuthorization(AllowAllDecider):
            async def decide(self, call, tool, turn):
                raise AssertionError("截断批次不应进入权限决策")

        first = tool_chunks([("c1", "read", {})])
        first[-1]["choices"][0]["finish_reason"] = "length"
        tool = StubTool(requires_permission=True)
        env = install([first, text_chunks("完成")], tools=[tool], decider=NoAuthorization())
        turn = await env.kernel.submit("任务")
        self.assertIs(turn.status, TurnStatus.DONE)
        self.assertEqual(tool.calls, [])
        self.assertEqual(env.recorder.of("permission_requested"), [])

    async def test_budget_picker_uses_config_and_truthful_stop_label(self):
        config = make_config().model_copy(update={"kernel": KernelConfig(max_iterations=7)})
        harness = build_inline([text_chunks("完成")], config=config)
        states = []

        async def overlay(component):
            states.append(component.state)
            return SimpleNamespace(value="continue")

        harness.app.push_overlay = overlay
        self.assertTrue(await harness.app.ask_continuation(None, 7))
        self.assertIn("7 步模型请求", states[0].choices[0].label)
        self.assertIn("尚未完成", states[0].choices[1].label)

    async def test_natural_empty_or_reasoning_fails_even_after_tools(self):
        for delta in ({}, {"reasoning": "还在规划"}, {"text": "  "}):
            for after_tool in (False, True):
                with self.subTest(delta=delta, after_tool=after_tool):
                    script = [tool_chunks([("c1", "read", {})])] if after_tool else []
                    env = install([*script, response(**delta)], tools=[StubTool()])
                    turn = await env.kernel.submit("任务")
                    self.assertIs(turn.status, TurnStatus.FAILED)
                    self.assertEqual(len(env.recorder.of("model_request_started")), len(script) + 1)
                    self.assertIn("没有给出正文", env.recorder.find("error_occurred").message)

    async def test_length_continues_empty_reasoning_and_partial_text(self):
        for delta in ({}, {"reasoning": "规划"}, {"text": "半句话"}):
            with self.subTest(delta=delta):
                env = install([response(**delta, stop="length"), text_chunks("完成")])
                turn = await env.kernel.submit("原始任务")
                self.assertIs(turn.status, TurnStatus.DONE)
                self.assertEqual(len(env.recorder.of("model_request_started")), 2)
                self.assertEqual(sum(m.role == "user" for m in env.kernel.history), 1)
                next_turn = await env.kernel.submit("第二个任务")
                self.assertEqual(next_turn.turn_index, 2)

    async def test_original_long_task_shape_continues_after_tool_progress(self):
        tool = StubTool()
        env = install([
            response(reasoning="规划一", stop="length"),
            tool_chunks([("c1", "read", {})]),
            response(reasoning="规划二", stop="length"),
            text_chunks("完成"),
        ], tools=[tool])
        turn = await env.kernel.submit("原始任务")
        self.assertIs(turn.status, TurnStatus.DONE)
        self.assertEqual(len(env.recorder.of("model_request_started")), 4)
        self.assertEqual(tool.calls, ["a.txt"])
        self.assertEqual(len(env.recorder.of("context_built")), 4)
        self.assertEqual(len(env.recorder.of("turn_finished")), 1)
        self.assertEqual(env.kernel._turn_messages(turn)[0].text, "原始任务")

    async def test_truncation_uses_existing_iteration_limit(self):
        env = install([response(text="半句话", stop="length")], max_iterations=3)
        turn = await env.kernel.submit("任务")
        self.assertIs(turn.status, TurnStatus.FAILED)
        self.assertEqual(len(env.recorder.of("model_request_started")), 3)
        self.assertEqual(env.recorder.find("turn_finished").reason, "error")

    async def test_abnormal_or_missing_stop_does_not_execute_tools(self):
        for stop in (None, "alien", "refusal", "content_filter", "pause_turn"):
            with self.subTest(stop=stop):
                tool = StubTool()
                first = tool_chunks([("c1", "read", {})])
                first[-1]["choices"][0]["finish_reason"] = stop
                env = install([first, text_chunks("不应请求")], tools=[tool])
                turn = await env.kernel.submit("任务")
                self.assertIs(turn.status, TurnStatus.FAILED)
                self.assertEqual(tool.calls, [])
                self.assertEqual(len(env.recorder.of("model_request_started")), 1)

    async def test_truncated_batch_is_rejected_and_reissued_once(self):
        for malformed in (False, True):
            with self.subTest(malformed=malformed):
                tool = StubTool()
                first = tool_chunks([("c1", "read", {})])
                if malformed:
                    broken = tool_chunks([("c2", "read", {})])[0]
                    call = broken["choices"][0]["delta"]["tool_calls"][0]
                    call["index"] = 1
                    call["function"]["arguments"] = '{"path":'
                    first.insert(1, broken)
                first[-1]["choices"][0]["finish_reason"] = "length"
                first.append(usage_chunk(output_tokens=99))
                env = install([first, tool_chunks([("c3", "read", {})]), text_chunks("完成")], tools=[tool])
                turn = await env.kernel.submit("任务")
                self.assertIs(turn.status, TurnStatus.DONE)
                self.assertEqual(tool.calls, ["a.txt"])
                finished = env.recorder.of("tool_call_finished")
                self.assertEqual([e.ok for e in finished], [False, True])
                self.assertEqual(len(env.recorder.of("tool_call_started")), len(finished))
                requests = env.recorder.of("model_request_finished")
                self.assertEqual(requests[0].usage.output_tokens, 99)
                self.assertEqual(requests[0].stop_reason, "max_tokens")
                self.assertEqual(requests[0].raw_stop_reason, "length")

    async def test_iteration_renewal_allows_or_stops_truncation(self):
        for allow in (True, False):
            with self.subTest(allow=allow):
                decider = ContinuationDecider([allow])
                env = install([
                    response(reasoning="一", stop="length"),
                    response(text="二", stop="length"),
                    text_chunks("完成"),
                ], decider=decider, max_iterations=2)
                turn = await env.kernel.submit("任务")
                self.assertIs(turn.status, TurnStatus.DONE if allow else TurnStatus.FAILED)
                self.assertEqual(decider.asked, [2])
                self.assertEqual(len(env.recorder.of("model_request_started")), 3 if allow else 2)

    async def test_final_at_iteration_limit_does_not_prompt(self):
        decider = ContinuationDecider([True])
        env = install([response(stop="length"), text_chunks("完成")], decider=decider, max_iterations=2)
        turn = await env.kernel.submit("任务")
        self.assertIs(turn.status, TurnStatus.DONE)
        self.assertEqual(decider.asked, [])

    async def test_continuation_prompt_is_only_in_the_next_request(self):
        env = install([
            response(text="半句", stop="length"),
            tool_chunks([("c1", "read", {})]),
            text_chunks("完成"),
        ], tools=[StubTool()])
        provider = RecordingProvider(env.kernel._provider)
        env.kernel.set_provider(provider)
        turn = await env.kernel.submit("真正的任务")
        self.assertIs(turn.status, TurnStatus.DONE)
        self.assertIn("系统提示", provider.requests[1].messages[-1].text)
        self.assertFalse(any("系统提示" in m.text for m in provider.requests[2].messages))
        self.assertFalse(any("系统提示" in m.text for m in env.kernel.history))
        self.assertEqual(len(env.recorder.of("user_prompt_submit")), 1)

    async def test_malformed_parameters_are_regenerated_without_entering_history(self):
        broken = tool_chunks([("c1", "read", {})])
        broken[0]["choices"][0]["delta"]["tool_calls"][0]["function"]["arguments"] = '{"path":'
        broken[-1]["choices"][0]["finish_reason"] = "length"
        env = install([broken, tool_chunks([("c2", "read", {})]), text_chunks("完成")], tools=[StubTool()])
        provider = RecordingProvider(env.kernel._provider)
        env.kernel.set_provider(provider)
        turn = await env.kernel.submit("任务")
        self.assertIs(turn.status, TurnStatus.DONE)
        self.assertIn("不要拼接残缺 JSON", provider.requests[1].messages[-1].text)
        self.assertNotIn("c1", str([m.model_dump() for m in env.kernel.history]))

    async def test_context_limit_continues_and_unknown_text_fails(self):
        for stop, expected in (("model_context_window_exceeded", TurnStatus.DONE), ("alien", TurnStatus.FAILED)):
            with self.subTest(stop=stop):
                env = install([response(text="部分答复", stop=stop), text_chunks("完成")])
                turn = await env.kernel.submit("任务")
                self.assertIs(turn.status, expected)
                self.assertEqual(env.kernel.history[1].text, "部分答复")
                self.assertEqual(env.recorder.of("model_request_finished")[0].raw_stop_reason, stop)

    async def test_cancel_during_continuation_preserves_output_and_finishes_once(self):
        env = install([response(text="第一段", stop="length"), [*response(text="第二段", stop=None), Pause(10)]])
        provider = RecordingProvider(env.kernel._provider)
        env.kernel.set_provider(provider)
        turn = await env.kernel.start("原任务")
        await asyncio.wait_for(provider.second_started.wait(), 2)
        turn.cancel()
        await env.kernel.wait(turn)
        self.assertIs(turn.status, TurnStatus.CANCELLED)
        self.assertEqual(env.kernel.history[1].text, "第一段")
        self.assertEqual(len(env.recorder.of("turn_finished")), 1)
        self.assertEqual(env.recorder.find("turn_finished").reason, "cancelled")

    async def test_cancel_during_budget_confirmation(self):
        entered = asyncio.Event()

        class WaitingDecider(AllowAllDecider):
            async def ask_continuation(self, turn, iteration):
                entered.set()
                await asyncio.Event().wait()

        env = install([response(stop="length")], max_iterations=1, decider=WaitingDecider())
        turn = await env.kernel.start("任务")
        await asyncio.wait_for(entered.wait(), 2)
        turn.cancel()
        await env.kernel.wait(turn)
        self.assertIs(turn.status, TurnStatus.CANCELLED)
        self.assertEqual(len(env.recorder.of("turn_finished")), 1)

    async def test_persistence_keeps_stop_reasons_and_old_records_replay(self):
        root = make_temp_dir("termination-")
        try:
            writer = SessionTranscriptWriter(base_dir=root, session_id="termination")
            env = install([response(text="半句", stop="length"), text_chunks("完成")])
            subscriber = SessionPersistenceSubscriber(writer)
            env.bus.subscribe("*", subscriber.handle, name="persistence")
            await env.kernel.submit("原任务")
            records = [json.loads(line) for line in writer.log_file.read_text(encoding="utf-8").splitlines()]
            outputs = [r for r in records if r.get("type") == "model_output"]
            self.assertEqual([r["meta"]["stop_reason"] for r in outputs], ["max_tokens", "end_turn"])
            self.assertEqual([r["meta"]["raw_stop_reason"] for r in outputs], ["length", "stop"])
            current = reconstruct_messages(records)
            for record in outputs:
                record["meta"].pop("stop_reason")
                record["meta"].pop("raw_stop_reason")
            legacy = reconstruct_messages(records)
            self.assertEqual([m.model_dump() for m in current], [m.model_dump() for m in legacy])
            self.assertEqual(sum(m.role == "user" for m in legacy), 1)
        finally:
            remove_temp_dir(root)

    async def test_anthropic_distinct_stop_reasons_and_parameter_truncation(self):
        for raw, normalized in (
            ("end_turn", "end_turn"), ("stop_sequence", "stop_sequence"),
            ("max_tokens", "max_tokens"), ("model_context_window_exceeded", "context_limit"),
            ("pause_turn", "pause_turn"), ("refusal", "refusal"), ("alien", "unknown"),
        ):
            with self.subTest(raw=raw):
                provider = AnthropicProvider(raw_stream=stream_of([
                    {"type": "message_delta", "delta": {"stop_reason": raw}},
                ]))
                events = [e async for e in provider.stream(ChatRequest(model="mock"))]
                stop = next(e for e in events if isinstance(e, StopEvent))
                self.assertEqual((stop.stop_reason, stop.raw_stop_reason), (normalized, raw))
        provider = AnthropicProvider(raw_stream=stream_of([
            {"type": "message_start", "message": {"usage": {"input_tokens": 10, "output_tokens": 0}}},
            {"type": "content_block_start", "index": 0,
             "content_block": {"type": "tool_use", "id": "c1", "name": "read", "input": {}}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "input_json_delta", "partial_json": '{"path":'}},
            {"type": "message_delta", "delta": {"stop_reason": "max_tokens"}, "usage": {"output_tokens": 99}},
        ]))
        env = install([text_chunks("完成")], tools=[StubTool()])

        class FirstAnthropic(RecordingProvider):
            async def stream(self, request):
                if not self.requests:
                    self.requests.append(request)
                    async for event in provider.stream(request):
                        yield event
                else:
                    async for event in self.wrapped.stream(request):
                        yield event

        env.kernel.set_provider(FirstAnthropic(env.kernel._provider))
        turn = await env.kernel.submit("任务")
        self.assertIs(turn.status, TurnStatus.DONE)
        finished = env.recorder.of("model_request_finished")
        self.assertEqual(finished[0].usage.output_tokens, 99)
        self.assertEqual(finished[0].raw_stop_reason, "max_tokens")
