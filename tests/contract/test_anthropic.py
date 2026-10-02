"""Anthropic 适配器契约测试（MODULE_providers §8 的 T-60 – T-90）。

Anthropic 与 OpenAI 兼容端点的差异**没有一处是改字段名就能解决的**，所以这份测试
逐条盯住它们：系统提示在顶层、工具定义叫 ``input_schema``、工具结果包在 ``user``
消息里、``max_tokens`` 必填、``temperature`` 只有 0–1、用量分散在两处、思考签名必须回传。

全部离线：分片来自 ``tests/fixtures/sse/``，**不发起任何网络请求**。
"""

from __future__ import annotations

import asyncio
import json
import unittest
from typing import Any

from logox.errors import ErrorCategory
from logox.kernel.messages import (
    Message,
    ReasoningBlock,
    TextBlock,
    ToolResultBlock,
    ToolSchema,
    ToolUseBlock,
    user_message,
)
from logox.providers.anthropic import (
    DEFAULT_MAX_TOKENS,
    THINKING_BUDGETS,
    AnthropicProvider,
    usage_from_anthropic,
)
from logox.providers.base import (
    ChatRequest,
    DeltaEvent,
    ProviderErrorEvent,
    StopEvent,
    ThinkingConfig,
    ToolCallEvent,
    UsageEvent,
)
from tests.contract.support import load_chunks, make_request, raising_stream, stream_of, translate_events

TOOL = ToolSchema(
    name="read",
    description="读取文件",
    parameters={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
)

THINKING_MODEL = "claude-sonnet-4-5"


def provider(**kwargs: object) -> AnthropicProvider:
    return AnthropicProvider(**kwargs)  # type: ignore[arg-type]


async def _drain(agen):  # type: ignore[no-untyped-def]
    return [item async for item in agen]


class PayloadTests(unittest.TestCase):
    def test_t60_max_tokens_is_always_present(self) -> None:
        """Anthropic 的 ``max_tokens`` 是**必填**项，不像 OpenAI 那样可省。"""
        payload = provider().build_payload(make_request())
        self.assertEqual(payload["max_tokens"], DEFAULT_MAX_TOKENS)

    def test_t61_explicit_max_tokens_wins(self) -> None:
        payload = provider().build_payload(make_request(max_tokens=2048))
        self.assertEqual(payload["max_tokens"], 2048)

    def test_t62_system_prompt_is_a_top_level_parameter(self) -> None:
        """**不是** ``messages[0]``——这正是必须有独立适配器的原因之一。"""
        payload = provider().build_payload(make_request(messages=[user_message("你好")]))
        self.assertEqual(payload["system"], "你是 Logox")
        self.assertEqual(payload["messages"], [{"role": "user", "content": [{"type": "text", "text": "你好"}]}])

    def test_t63_no_system_key_when_empty(self) -> None:
        payload = provider().build_payload(make_request(system=""))
        self.assertNotIn("system", payload)

    def test_t64_streaming_is_always_on(self) -> None:
        self.assertIs(provider().build_payload(make_request())["stream"], True)

    def test_t65_tools_use_input_schema(self) -> None:
        payload = provider().build_payload(make_request(tools=[TOOL]))
        self.assertEqual(
            payload["tools"],
            [{"name": "read", "description": "读取文件", "input_schema": TOOL.parameters}],
        )
        self.assertNotIn("parameters", payload["tools"][0])

    def test_t66_temperature_inside_range_is_passed(self) -> None:
        for value in (0.0, 0.5, 1.0):
            with self.subTest(value=value):
                payload = provider().build_payload(make_request(temperature=value))
                self.assertEqual(payload["temperature"], value)

    def test_t67_temperature_outside_range_is_omitted_not_sent(self) -> None:
        """超范围**不传**（Anthropic 默认 1.0，等同取上界），而不是发出去换一个 400。"""
        for value in (1.5, 2.0, -0.1):
            with self.subTest(value=value):
                payload = provider().build_payload(make_request(temperature=value))
                self.assertNotIn("temperature", payload)

    def test_t68_temperature_absent_by_default(self) -> None:
        self.assertNotIn("temperature", provider().build_payload(make_request()))


class ThinkingPayloadTests(unittest.TestCase):
    """D42 / E-13 / E-14：档位 → ``budget_tokens``，且必须严格小于 ``max_tokens``。"""

    def test_t69_high_effort_maps_to_budget(self) -> None:
        payload = provider().build_payload(make_request(model=THINKING_MODEL, thinking=ThinkingConfig(effort="high")))
        self.assertEqual(payload["thinking"], {"type": "enabled", "budget_tokens": THINKING_BUDGETS["high"]})

    def test_t70_every_effort_keeps_budget_below_max_tokens(self) -> None:
        """E-14：``budget_tokens`` 必须**严格小于** ``max_tokens``，否则 Anthropic 直接 400。"""
        for effort in ("low", "medium", "high"):
            for max_tokens in (None, 512, 1024, 2048, 8192, 64000):
                with self.subTest(effort=effort, max_tokens=max_tokens):
                    payload = provider().build_payload(
                        make_request(model=THINKING_MODEL, thinking=ThinkingConfig(effort=effort), max_tokens=max_tokens)
                    )
                    thinking = payload["thinking"]
                    self.assertLess(thinking["budget_tokens"], payload["max_tokens"])
                    self.assertGreaterEqual(thinking["budget_tokens"], 1024)  # Anthropic 的下限

    def test_t71_off_sends_no_thinking_parameter(self) -> None:
        payload = provider().build_payload(make_request(model=THINKING_MODEL, thinking=ThinkingConfig(effort="off")))
        self.assertNotIn("thinking", payload)

    def test_t72_auto_sends_no_thinking_parameter(self) -> None:
        payload = provider().build_payload(make_request(model=THINKING_MODEL, thinking=ThinkingConfig(effort="auto")))
        self.assertNotIn("thinking", payload)

    def test_t73_unsupported_model_ignores_the_level(self) -> None:
        """E-13：给 Haiku 3.5 发 thinking 会被拒——**忽略档位而不是让请求失败**。"""
        payload = provider().build_payload(make_request(model="claude-3-5-haiku-latest", thinking=ThinkingConfig(effort="high")))
        self.assertNotIn("thinking", payload)

    def test_t74_supports_thinking_matches_model_families(self) -> None:
        inst = provider()
        for model in (THINKING_MODEL, "claude-opus-4-1", "claude-3-7-sonnet-latest", "claude-4-sonnet"):
            with self.subTest(model=model):
                self.assertTrue(inst.supports_thinking(model))
        for model in ("claude-3-5-haiku-latest", "claude-3-opus-20240229", "gpt-4o"):
            with self.subTest(model=model):
                self.assertFalse(inst.supports_thinking(model))

    def test_t75_list_models_reports_price_and_window(self) -> None:
        infos = provider(models=[THINKING_MODEL, "claude-3-5-haiku-latest"], context_window=200_000).list_models()
        by_id = {info.id: info for info in infos}
        self.assertEqual(by_id[THINKING_MODEL].input_price_per_mtok, 3.00)
        self.assertEqual(by_id[THINKING_MODEL].output_price_per_mtok, 15.00)
        self.assertEqual(by_id[THINKING_MODEL].context_window, 200_000)
        self.assertTrue(by_id[THINKING_MODEL].supports_thinking)
        self.assertFalse(by_id["claude-3-5-haiku-latest"].supports_thinking)
        self.assertEqual(by_id["claude-3-5-haiku-latest"].input_price_per_mtok, 0.80)


class MessageConversionTests(unittest.TestCase):
    def test_t76_tool_result_is_wrapped_in_a_user_message(self) -> None:
        message = Message(role="tool", blocks=[ToolResultBlock(id="toolu_1", content="文件内容", ok=True)])
        converted = AnthropicProvider._convert_message(message)
        self.assertEqual(converted["role"], "user")
        self.assertEqual(converted["content"], [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "文件内容"}])

    def test_t77_failed_tool_result_is_marked_as_error(self) -> None:
        """``is_error`` 让模型知道可以自愈（D22 的 ``feedable_to_model``）。"""
        message = Message(role="tool", blocks=[ToolResultBlock(id="toolu_1", content="文件不存在", ok=False)])
        block = AnthropicProvider._convert_message(message)["content"][0]
        self.assertIs(block["is_error"], True)

    def test_t78_text_next_to_tool_results_is_kept_and_ordered_after(self) -> None:
        """``tool_result`` 必须排在最前；同消息里的文本**保留**而不是静默丢弃。"""
        message = Message(
            role="user",
            blocks=[TextBlock(text="继续"), ToolResultBlock(id="toolu_1", content="ok")],
        )
        content = AnthropicProvider._convert_message(message)["content"]
        self.assertEqual([block["type"] for block in content], ["tool_result", "text"])
        self.assertEqual(content[1]["text"], "继续")

    def test_t79_assistant_tool_use_keeps_input_as_object(self) -> None:
        """与 OpenAI 相反：这里 ``input`` 是**对象**，不是 JSON 字符串。"""
        message = Message(role="assistant", blocks=[ToolUseBlock(id="toolu_1", name="read", input={"path": "a.py"})])
        content = AnthropicProvider._convert_message(message)["content"]
        self.assertEqual(content, [{"type": "tool_use", "id": "toolu_1", "name": "read", "input": {"path": "a.py"}}])

    def test_t80_thinking_block_is_round_tripped_with_its_signature(self) -> None:
        """E-12：多轮工具调用时，签名必须**原样回传**，否则下一次请求会被拒绝。"""
        message = Message(role="assistant", blocks=[ReasoningBlock(text="先看重试实现", signature="SIG-abc")])
        content = AnthropicProvider._convert_message(message)["content"]
        self.assertEqual(content, [{"type": "thinking", "thinking": "先看重试实现", "signature": "SIG-abc"}])

    def test_t81_thinking_block_without_signature_is_dropped(self) -> None:
        """缺签名的 thinking 块发出去必被拒——只好丢掉（调用方应记 warning）。"""
        message = Message(role="assistant", blocks=[ReasoningBlock(text="没有签名"), TextBlock(text="正文")])
        content = AnthropicProvider._convert_message(message)["content"]
        self.assertEqual(content, [{"type": "text", "text": "正文"}])

    def test_t82_empty_assistant_message_does_not_produce_empty_content(self) -> None:
        message = Message(role="assistant", blocks=[])
        content = AnthropicProvider._convert_message(message)["content"]
        self.assertEqual(content, [{"type": "text", "text": ""}])


class UsageNormalizationTests(unittest.TestCase):
    """Anthropic 在流式下把 input 放在 ``message_start``、output 放在 ``message_delta``。"""

    def test_t83_input_and_output_are_merged_from_two_places(self) -> None:
        usage = usage_from_anthropic({"input_tokens": 120, "output_tokens": 1}, {"output_tokens": 18})
        assert usage is not None
        self.assertEqual((usage.input_tokens, usage.output_tokens), (120, 18))

    def test_t84_cache_read_tokens_become_cached_input(self) -> None:
        usage = usage_from_anthropic({"input_tokens": 100, "cache_read_input_tokens": 80, "output_tokens": 5}, {"output_tokens": 9})
        assert usage is not None
        self.assertEqual(usage.cached_input_tokens, 80)
        self.assertAlmostEqual(usage.cache_hit_ratio or 0.0, 0.8)

    def test_t85_cache_creation_is_the_fallback(self) -> None:
        usage = usage_from_anthropic({"input_tokens": 100, "cache_creation_input_tokens": 40, "output_tokens": 5}, {"output_tokens": 9})
        assert usage is not None
        self.assertEqual(usage.cached_input_tokens, 40)

    def test_t86_missing_cache_is_none_not_zero(self) -> None:
        """D39：状态栏据此**隐藏** cache 项，而不是显示 ``cache 0%``。"""
        usage = usage_from_anthropic({"input_tokens": 200, "output_tokens": 1}, {"output_tokens": 30})
        assert usage is not None
        self.assertIsNone(usage.cached_input_tokens)
        self.assertIsNone(usage.cache_hit_ratio)

    def test_t87_output_missing_from_both_places_yields_none(self) -> None:
        self.assertIsNone(usage_from_anthropic({"input_tokens": 10}, None))
        self.assertIsNone(usage_from_anthropic(None, {"output_tokens": 10}))
        self.assertIsNone(usage_from_anthropic(None, None))

    def test_t88_stop_event_usage_never_fabricates_zero(self) -> None:
        events = translate_events(provider(), load_chunks("anthropic_error"))
        self.assertFalse(any(isinstance(e, UsageEvent) for e in events))


class TextStreamTests(unittest.TestCase):
    def test_t89_text_deltas_are_joined(self) -> None:
        events = translate_events(provider(), load_chunks("anthropic_text"))
        deltas = [e for e in events if isinstance(e, DeltaEvent) and e.kind == "text"]
        self.assertEqual("".join(d.text for d in deltas), "把超时改成 30 秒")

    def test_t90_an_empty_text_block_start_emits_nothing(self) -> None:
        events = translate_events(provider(), load_chunks("anthropic_text"))
        self.assertEqual([type(e).__name__ for e in events], ["DeltaEvent", "DeltaEvent", "UsageEvent", "StopEvent"])

    def test_t91_model_and_stop_reason_come_from_the_message_envelope(self) -> None:
        events = translate_events(provider(), load_chunks("anthropic_text"))
        stop = next(e for e in events if isinstance(e, StopEvent))
        self.assertEqual(stop.model, THINKING_MODEL)
        self.assertEqual(stop.stop_reason, "end_turn")

    def test_t92_usage_combines_message_start_and_message_delta(self) -> None:
        events = translate_events(provider(), load_chunks("anthropic_text"))
        usage = next(e.usage for e in events if isinstance(e, UsageEvent))
        self.assertEqual((usage.input_tokens, usage.output_tokens), (120, 18))
        self.assertEqual(usage.cached_input_tokens, 96)

    def test_t92b_context_tokens_sums_the_three_input_buckets(self) -> None:
        """★ CHANGE-005：Anthropic 的 ``input_tokens`` **不含**缓存部分，总量要相加。

        厂商原文的字段关系：``input_tokens``（未命中）+ ``cache_creation_input_tokens``
        （写缓存）+ ``cache_read_input_tokens``（读缓存）**三块并列**。
        夹具里是 120 + 96（读）= **216**。

        若这里写成 120，则下游会把"已用 216k"看成"已用 120k" ——
        压缩永远不会触发，而窗口会**直接超**（这是 CHANGE-005 之前的真实风险）。
        """
        events = translate_events(provider(), load_chunks("anthropic_text"))
        usage = next(e.usage for e in events if isinstance(e, UsageEvent))
        self.assertEqual(usage.context_tokens, 216)

    def test_t92c_context_tokens_includes_cache_writes(self) -> None:
        """写缓存那部分也要算进去（夹具里只有读缓存，单独造一个写的）。"""
        usage = usage_from_anthropic(
            {
                "input_tokens": 10,
                "cache_creation_input_tokens": 100,
                "cache_read_input_tokens": 200,
            },
            {"output_tokens": 5},
        )
        assert usage is not None
        self.assertEqual(usage.context_tokens, 310)
        self.assertEqual(usage.cached_input_tokens, 200, "去重的口径仍以“读”优先（状态栏看的是命中率）")


class ThinkingStreamTests(unittest.TestCase):
    def test_t93_thinking_deltas_are_emitted_as_reasoning(self) -> None:
        events = translate_events(provider(), load_chunks("anthropic_thinking"))
        reasoning = [e for e in events if isinstance(e, DeltaEvent) and e.kind == "reasoning" and e.text]
        self.assertEqual("".join(e.text for e in reasoning), "先看 kernel 里的重试实现，改成指数退避。")

    def test_t94_signature_arrives_as_a_textless_reasoning_delta(self) -> None:
        """签名在思考文本之后才下发，只能单独补一条增量（见 ``DeltaEvent`` 的约定）。"""
        events = translate_events(provider(), load_chunks("anthropic_thinking"))
        signatures = [e for e in events if isinstance(e, DeltaEvent) and e.vendor_data and "signature" in e.vendor_data]
        self.assertEqual(len(signatures), 1)
        self.assertEqual(signatures[0].kind, "reasoning")
        self.assertEqual(signatures[0].text, "")
        self.assertEqual(signatures[0].vendor_data, {"signature": "SIG-payload-abc123"})

    def test_t95_signature_delta_follows_the_thinking_it_belongs_to(self) -> None:
        events = translate_events(provider(), load_chunks("anthropic_thinking"))
        kinds = [
            "sig" if (isinstance(e, DeltaEvent) and e.vendor_data) else e.kind
            for e in events
            if isinstance(e, DeltaEvent)
        ]
        self.assertEqual(kinds, ["reasoning", "reasoning", "sig", "text"])

    def test_t96_thinking_text_never_carries_a_signature(self) -> None:
        events = translate_events(provider(), load_chunks("anthropic_thinking"))
        for event in events:
            if isinstance(event, DeltaEvent) and event.text:
                self.assertIsNone(event.vendor_data)

    def test_t97_signature_survives_a_full_round_trip(self) -> None:
        """E-12 端到端：流里的签名 → ``ReasoningBlock`` → 下一轮请求体里的 thinking 块。"""
        events = translate_events(provider(), load_chunks("anthropic_thinking"))
        text = "".join(e.text for e in events if isinstance(e, DeltaEvent) and e.kind == "reasoning")
        signature = next(e.vendor_data["signature"] for e in events if isinstance(e, DeltaEvent) and e.vendor_data)

        history = Message(role="assistant", blocks=[ReasoningBlock(text=text, signature=signature)])
        payload = provider().build_payload(make_request(model=THINKING_MODEL, messages=[history]))
        blocks = payload["messages"][0]["content"]
        self.assertEqual(blocks[0]["type"], "thinking")
        self.assertEqual(blocks[0]["signature"], signature)
        self.assertEqual(blocks[0]["thinking"], text)

    def test_t98_thinking_usage_is_reported(self) -> None:
        events = translate_events(provider(), load_chunks("anthropic_thinking"))
        usage = next(e.usage for e in events if isinstance(e, UsageEvent))
        self.assertEqual(usage.input_tokens, 4000)
        self.assertEqual(usage.output_tokens, 260)
        self.assertEqual(usage.cached_input_tokens, 3500)


class ToolCallStreamTests(unittest.TestCase):
    def test_t99_tool_call_is_assembled_from_input_json_fragments(self) -> None:
        events = translate_events(provider(), load_chunks("anthropic_tool_use"))
        calls = [e for e in events if isinstance(e, ToolCallEvent)]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].call_id, "toolu_1")
        self.assertEqual(calls[0].name, "read")
        self.assertEqual(calls[0].arguments, {"path": "src/a.py"})

    def test_t100_tool_use_stop_reason(self) -> None:
        events = translate_events(provider(), load_chunks("anthropic_tool_use"))
        stop = next(e for e in events if isinstance(e, StopEvent))
        self.assertEqual(stop.stop_reason, "tool_use")

    def test_t101_arguments_never_leak_as_a_json_fragment(self) -> None:
        """上层拿到的是 dict；厂商原始字段与半截 JSON **绝不能**出现在任何事件里。"""
        events = translate_events(provider(), load_chunks("anthropic_tool_use"))
        rendered = json.dumps([e.model_dump(mode="json") for e in events], ensure_ascii=False)
        self.assertNotIn("partial_json", rendered)
        self.assertNotIn("content_block", rendered)
        for event in events:
            if isinstance(event, ToolCallEvent):
                self.assertIsInstance(event.arguments, dict)

    def test_t99b_truncated_tool_args_are_flagged_as_truncated(self) -> None:
        """★ **D160 的守卫**：Anthropic 上参数被 `max_tokens` 截断时，必须打 `is_truncated`。

        为什么这条用例非有不可：**D121 的自愈闭环只认这一个标记** ——

            # kernel/loop.py:607
            if failure.is_truncated and truncation_healing_count < max_truncation_healings:

        而这之前**只有 OpenAI 兼容那条路打了标记**（`openai_compat.py:388`），
        Anthropic 那条路一直漏着。后果：用户在 Anthropic 上撞到"截断事故"时，
        看到的是原来那个**无可挽回的闪退**，而 D121 承诺的自愈一次都不会触发。

        ⚠️ 这属于本项目**第五次**同形态缺陷：机制实现好了、文档写着已修，
        但**只接了一半的路**（前四次：用户主题目录、`ensure_dirs`、24 个主题 token、`[glyphs]`）。
        这个标记此前**没有任何用例断言过** —— 所以它漏了半年也没人发现。
        """
        chunks = [
            {
                "type": "message_start",
                "message": {
                    "id": "msg_1",
                    "type": "message",
                    "role": "assistant",
                    "model": "m",
                    "content": [],
                    "stop_reason": None,
                    "usage": {"input_tokens": 100, "output_tokens": 1},
                },
            },
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "tool_use", "id": "toolu_1", "name": "edit", "input": {}},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                # 断在字符串中间 ⇒ 复现"参数腰斩"
                "delta": {"type": "input_json_delta", "partial_json": '{"path": "src/a.py", "old'},
            },
            {"type": "content_block_stop", "index": 0},
            # Anthropic 用 max_tokens 表示"被输出上限截断"
            {"type": "message_delta", "delta": {"stop_reason": "max_tokens"}, "usage": {"output_tokens": 50}},
            {"type": "message_stop"},
        ]
        events = translate_events(provider(), chunks)

        # ① 半成品绝不外泄（E-2）—— 这条本来就对，一起钉住
        self.assertFalse(any(isinstance(e, ToolCallEvent) for e in events))
        errors = [e for e in events if isinstance(e, ProviderErrorEvent)]
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].category, ErrorCategory.BAD_REQUEST)
        # ② ★ 关闭自愈的那个标记
        self.assertTrue(
            errors[0].is_truncated,
            "Anthropic 上被 max_tokens 截断的工具参数必须打 is_truncated，"
            "否则 kernel/loop.py 的 D121 自愈闭环永远不会触发",
        )


class ErrorHandlingTests(unittest.TestCase):
    def test_t102_error_event_in_the_stream_is_classified(self) -> None:
        events = translate_events(provider(), load_chunks("anthropic_error"))
        self.assertEqual(len(events), 1)
        error = events[0]
        assert isinstance(error, ProviderErrorEvent)
        self.assertEqual(error.category, ErrorCategory.RATE_LIMIT)  # overloaded → 可重试
        self.assertIn("Overloaded", error.message)

    def test_t103_stream_stops_after_an_error_event(self) -> None:
        events = translate_events(provider(), load_chunks("anthropic_error"))
        self.assertFalse(any(isinstance(e, StopEvent) for e in events))

    def test_t104_cancelled_error_is_reraised(self) -> None:
        inst = provider(raw_stream=raising_stream(asyncio.CancelledError()))
        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(_drain(inst.stream(make_request())))

    def test_t105_network_failure_becomes_a_classified_event(self) -> None:
        inst = provider(raw_stream=raising_stream(TimeoutError("read timeout")))
        result = asyncio.run(_drain(inst.stream(make_request())))
        error = result[-1]
        assert isinstance(error, ProviderErrorEvent)
        self.assertEqual(error.category, ErrorCategory.NETWORK)

    def test_t106_missing_api_key_is_reported_as_auth_error(self) -> None:
        result = asyncio.run(_drain(provider().stream(make_request())))
        self.assertEqual(len(result), 1)
        error = result[0]
        assert isinstance(error, ProviderErrorEvent)
        self.assertEqual(error.category, ErrorCategory.AUTH)
        self.assertIn("API Key", error.message)

    def test_t107_http_400_is_not_retryable(self) -> None:
        class FakeBadRequest(Exception):
            status_code = 400

        inst = provider(raw_stream=raising_stream(FakeBadRequest("max_tokens: must be greater than thinking.budget_tokens")))
        result = asyncio.run(_drain(inst.stream(make_request())))
        error = result[-1]
        assert isinstance(error, ProviderErrorEvent)
        self.assertEqual(error.category, ErrorCategory.BAD_REQUEST)
        self.assertFalse(error.category.retryable)
        self.assertEqual(error.detail, "HTTP 400")


class RawStreamSeamTests(unittest.TestCase):
    def test_t108_injected_stream_receives_the_request(self) -> None:
        seen: list[ChatRequest] = []

        def factory(request: ChatRequest):  # type: ignore[no-untyped-def]
            seen.append(request)

            async def generator():  # type: ignore[no-untyped-def]
                yield {"type": "message_start", "message": {"model": "claude-sonnet-4-5", "usage": {"input_tokens": 1, "output_tokens": 1}}}
                yield {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 3}}

            return generator()

        inst = provider(raw_stream=factory)
        request = make_request(model=THINKING_MODEL)
        asyncio.run(_drain(inst.stream(request)))
        self.assertEqual(seen, [request])

    def test_t109_translate_and_stream_agree(self) -> None:
        chunks = load_chunks("anthropic_tool_use")
        inst = provider(raw_stream=stream_of(chunks))
        via_stream = asyncio.run(_drain(inst.stream(make_request())))
        via_translate = translate_events(inst, chunks)
        self.assertEqual([type(e).__name__ for e in via_stream], [type(e).__name__ for e in via_translate])


class RoleNormalizationTests(unittest.TestCase):
    """角色归一化：把「中立模型允许、Anthropic 不允许」的形状在本层消化掉（D7 / F-29）。

    为什么必须做：``Message.role`` 的 Literal 里**声明了** ``"system"``，
    而 Anthropic 的 ``messages`` 只接受 ``user`` / ``assistant``。
    **契约说允许、实现不兜住，就是把一个非法载荷发给厂商换回 400。**

    实测过的两种输入（改之前都是红的）：
    1. 消息序列里出现 ``role="system"``（压缩归档索引就会产生它）；
    2. 相邻同角色消息（归档摘要 + 随后的提问、多条 ``tool_result``）。
    """

    @staticmethod
    def _payload(messages: list[Message], system: str = "") -> dict[str, Any]:
        request = ChatRequest(
            model="claude-sonnet-4-5",
            system=system,
            messages=messages,
            tools=[],
            temperature=None,
            max_tokens=1024,
        )
        return AnthropicProvider().build_payload(request)

    def test_t110_mid_conversation_system_is_hoisted_not_passed_through(self) -> None:
        """中间 system 消息必须被并入顶层 `system`，**不得**出现在 messages 里。"""
        payload = self._payload(
            [
                Message(role="user", blocks=[TextBlock(text="初始目标")]),
                Message(role="system", blocks=[TextBlock(text="[历史归档索引] Epoch 1")]),
                Message(role="user", blocks=[TextBlock(text="最近的问题")]),
            ],
            system="你是 Logox",
        )
        roles = [m["role"] for m in payload["messages"]]
        self.assertNotIn("system", roles, f"messages 里不得出现 system：{roles}")
        self.assertIn("[历史归档索引]", payload["system"])
        self.assertIn("你是 Logox", payload["system"])

    def test_t111_adjacent_same_role_messages_are_merged(self) -> None:
        """相邻同角色必须合并 —— Anthropic 要求 user/assistant 严格交替。"""
        payload = self._payload(
            [
                Message(role="user", blocks=[TextBlock(text="归档摘要")]),
                Message(role="user", blocks=[TextBlock(text="最近的问题")]),
            ]
        )
        self.assertEqual([m["role"] for m in payload["messages"]], ["user"])
        merged_text = "".join(
            block["text"] for block in payload["messages"][0]["content"] if block["type"] == "text"
        )
        self.assertIn("归档摘要", merged_text)
        self.assertIn("最近的问题", merged_text)

    def test_t112_merged_tool_results_keep_tool_result_blocks_first(self) -> None:
        """合并时 `tool_result` 块必须排在最前 —— 这是 Anthropic 对「响应 tool_use」的格式要求。"""
        payload = self._payload(
            [
                Message(role="user", blocks=[TextBlock(text="归档摘要")]),
                Message(role="tool", blocks=[ToolResultBlock(id="c1", ok=True, content="out1")]),
                Message(role="tool", blocks=[ToolResultBlock(id="c2", ok=True, content="out2")]),
            ]
        )
        self.assertEqual([m["role"] for m in payload["messages"]], ["user"])
        blocks = payload["messages"][0]["content"]
        self.assertEqual(blocks[0]["type"], "tool_result")
        self.assertEqual(
            [b["tool_use_id"] for b in blocks if b["type"] == "tool_result"], ["c1", "c2"]
        )

    def test_t113_payload_never_violates_anthropic_role_rules(self) -> None:
        """总断言：任何输入下，产物都满足「只有 user/assistant + 严格交替」。"""
        payload = self._payload(
            [
                Message(role="system", blocks=[TextBlock(text="索引")]),
                Message(role="user", blocks=[TextBlock(text="q1")]),
                Message(role="user", blocks=[TextBlock(text="q2")]),
                Message(role="assistant", blocks=[TextBlock(text="a1")]),
                Message(role="assistant", blocks=[TextBlock(text="a2")]),
                Message(role="tool", blocks=[ToolResultBlock(id="c1", ok=True, content="out")]),
            ]
        )
        roles = [m["role"] for m in payload["messages"]]
        self.assertTrue(set(roles) <= {"user", "assistant"}, f"非法角色：{roles}")
        self.assertTrue(all(a != b for a, b in zip(roles, roles[1:])), f"相邻同角色：{roles}")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
