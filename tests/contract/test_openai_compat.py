"""OpenAI 兼容适配器契约测试（MODULE_providers §8 的 T-20 – T-45）。

覆盖范围刻意压在**最容易出错的三处**：
1. 请求体构造（系统提示位置、工具定义形状、不支持的字段必须剔除）
2. 流式分片装配（工具参数是增量 JSON 片段，必须拼完再给上层）
3. 用量归一化（``None`` 与 ``0`` 语义不同，D39）

全部离线：分片来自 ``tests/fixtures/sse/``，**不发起任何网络请求**。
"""

from __future__ import annotations

import asyncio
import unittest

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
from logox.providers.base import (
    ChatRequest,
    DeltaEvent,
    ProviderErrorEvent,
    StopEvent,
    ThinkingConfig,
    ToolCallEvent,
    UsageEvent,
)
from logox.providers.openai_compat import OpenAICompatProvider, usage_from_openai
from tests.contract.support import load_chunks, make_request, raising_stream, stream_of, translate_events

TOOL = ToolSchema(
    name="read",
    description="读取文件",
    parameters={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
)


def provider(**kwargs: object) -> OpenAICompatProvider:
    return OpenAICompatProvider(**kwargs)  # type: ignore[arg-type]


class PayloadTests(unittest.TestCase):
    """中立请求 → OpenAI 请求体。"""

    def test_t20_system_prompt_goes_to_first_message(self) -> None:
        payload = provider().build_payload(make_request(messages=[user_message("你好")]))
        self.assertEqual(payload["messages"][0], {"role": "system", "content": "你是 Logox"})
        self.assertEqual(payload["messages"][1], {"role": "user", "content": "你好"})

    def test_t21_no_system_message_when_system_is_empty(self) -> None:
        payload = provider().build_payload(make_request(system="", messages=[user_message("你好")]))
        self.assertEqual([m["role"] for m in payload["messages"]], ["user"])

    def test_t22_streaming_always_requests_usage(self) -> None:
        """不加 ``stream_options.include_usage``，流式响应里**根本拿不到 usage**，
        而 D39 的 cache/tok-s 度量全部依赖它。"""
        payload = provider().build_payload(make_request())
        self.assertIs(payload["stream"], True)
        self.assertEqual(payload["stream_options"], {"include_usage": True})

    def test_t23_temperature_and_max_tokens_are_optional(self) -> None:
        bare = provider().build_payload(make_request())
        self.assertNotIn("temperature", bare)
        self.assertNotIn("max_tokens", bare)

        explicit = provider().build_payload(make_request(temperature=0.2, max_tokens=512))
        self.assertEqual(explicit["temperature"], 0.2)
        self.assertEqual(explicit["max_tokens"], 512)

    def test_t24_tool_schema_uses_function_parameters(self) -> None:
        payload = provider().build_payload(make_request(tools=[TOOL]))
        self.assertEqual(
            payload["tools"],
            [
                {
                    "type": "function",
                    "function": {
                        "name": "read",
                        "description": "读取文件",
                        "parameters": TOOL.parameters,
                    },
                }
            ],
        )

    def test_t25_no_tools_key_when_tool_list_is_empty(self) -> None:
        self.assertNotIn("tools", provider().build_payload(make_request()))

    def test_t26_assistant_tool_use_encodes_arguments_as_json_string(self) -> None:
        """OpenAI 要求 ``arguments`` 是 JSON **字符串**，不是对象。"""
        message = Message(role="assistant", blocks=[ToolUseBlock(id="call_1", name="read", input={"path": "a.py"})])
        payload = provider().build_payload(make_request(system="", messages=[message]))
        entry = payload["messages"][0]
        self.assertEqual(entry["role"], "assistant")
        self.assertIsNone(entry["content"])
        self.assertEqual(entry["tool_calls"][0]["id"], "call_1")
        self.assertEqual(entry["tool_calls"][0]["type"], "function")
        self.assertEqual(entry["tool_calls"][0]["function"]["arguments"], '{"path": "a.py"}')

    def test_t27_text_and_tool_use_share_one_message(self) -> None:
        message = Message(
            role="assistant",
            blocks=[TextBlock(text="我先看一下"), ToolUseBlock(id="c", name="read", input={})],
        )
        payload = provider().build_payload(make_request(system="", messages=[message]))
        self.assertEqual(len(payload["messages"]), 1)
        self.assertEqual(payload["messages"][0]["content"], "我先看一下")

    def test_t28_tool_result_becomes_its_own_role_tool_message(self) -> None:
        message = Message(role="tool", blocks=[ToolResultBlock(id="call_1", content="文件内容", ok=True)])
        payload = provider().build_payload(make_request(system="", messages=[message]))
        self.assertEqual(
            payload["messages"],
            [{"role": "tool", "tool_call_id": "call_1", "content": "文件内容"}],
        )

    def test_t29_reasoning_is_deliberately_dropped(self) -> None:
        """D31：推理内容不回传——兼容端点普遍不接受 reasoning 入参。

        这是**决策**，不是遗漏，因此用测试把它钉住。
        """
        message = Message(
            role="assistant",
            blocks=[ReasoningBlock(text="先看 kernel", signature="SIG-1"), TextBlock(text="我来改。")],
        )
        payload = provider().build_payload(make_request(system="", messages=[message]))
        self.assertEqual(payload["messages"], [{"role": "assistant", "content": "我来改。"}])
        self.assertNotIn("SIG-1", str(payload))

    def test_t30_reasoning_only_message_does_not_crash(self) -> None:
        message = Message(role="assistant", blocks=[ReasoningBlock(text="只有思考")])
        payload = provider().build_payload(make_request(system="", messages=[message]))
        self.assertEqual(payload["messages"], [{"role": "assistant", "content": ""}])

    def test_t30b_deepseek_preserves_reasoning_content_for_kv_cache(self) -> None:
        """DeepSeek 官方规范：多轮 assistant 消息必须带回 reasoning_content 保持 KV 缓存命中。"""
        message = Message(
            role="assistant",
            blocks=[ReasoningBlock(text="思考过程"), TextBlock(text="回答正文")],
        )
        payload = provider().build_payload(make_request(model="deepseek-flash", system="", messages=[message]))
        self.assertEqual(
            payload["messages"],
            [{"role": "assistant", "content": "回答正文", "reasoning_content": "思考过程"}],
        )


class ThinkingPayloadTests(unittest.TestCase):
    """D42 思考档位 → ``reasoning_effort``；E-13 不支持的模型必须**静默忽略**。"""

    def test_t31_known_reasoning_model_gets_effort(self) -> None:
        for model in ("o3-mini", "deepseek-reasoner", "gpt-5-codex", "qwen3-32b"):
            with self.subTest(model=model):
                payload = provider().build_payload(make_request(model=model, thinking=ThinkingConfig(effort="high")))
                self.assertEqual(payload["reasoning_effort"], "high")

    def test_t32_off_maps_to_minimal(self) -> None:
        payload = provider().build_payload(make_request(model="o3", thinking=ThinkingConfig(effort="off")))
        self.assertEqual(payload["reasoning_effort"], "minimal")

    def test_t33_unknown_model_ignores_the_level_instead_of_erroring(self) -> None:
        """E-13：对 Ollama 这类严格端点发 ``reasoning_effort`` 会**每个请求都 400**，
        比静默忽略一个可选参数糟糕得多。"""
        payload = provider().build_payload(make_request(model="llama3.2", thinking=ThinkingConfig(effort="high")))
        self.assertNotIn("reasoning_effort", payload)

    def test_t34_auto_sends_nothing(self) -> None:
        payload = provider().build_payload(make_request(model="o3", thinking=ThinkingConfig(effort="auto")))
        self.assertNotIn("reasoning_effort", payload)

    def test_t35_absent_config_sends_nothing(self) -> None:
        payload = provider().build_payload(make_request(model="o3"))
        self.assertNotIn("reasoning_effort", payload)

    def test_t36_vendor_specific_keys_are_never_leaked(self) -> None:
        """``reasoning`` / ``thinking`` 是别家的字段名；显式剔除，不靠记忆。"""
        payload = provider().build_payload(make_request(model="o3", thinking=ThinkingConfig(effort="high")))
        self.assertNotIn("reasoning", payload)
        self.assertNotIn("thinking", payload)

    def test_t37_supports_thinking_is_conservative(self) -> None:
        inst = provider()
        self.assertTrue(inst.supports_thinking("o3-mini"))
        self.assertTrue(inst.supports_thinking("DEEPSEEK-REASONER"))
        self.assertFalse(inst.supports_thinking("gpt-4o"))
        self.assertFalse(inst.supports_thinking("llama3.2"))

    def test_t38_list_models_reports_price_and_thinking(self) -> None:
        infos = provider(models=["deepseek-reasoner", "mystery-model"], context_window=65536).list_models()
        by_id = {info.id: info for info in infos}
        self.assertTrue(by_id["deepseek-reasoner"].supports_thinking)
        self.assertEqual(by_id["deepseek-reasoner"].input_price_per_mtok, None)
        self.assertEqual(by_id["deepseek-reasoner"].context_window, 65536)
        # 未知模型：不给假价格（UI 显示 ``—``）
        self.assertIsNone(by_id["mystery-model"].input_price_per_mtok)
        self.assertIsNone(by_id["mystery-model"].output_price_per_mtok)


class UsageNormalizationTests(unittest.TestCase):
    """D39 的核心：``None``（未上报）与 ``0``（真的零命中）语义完全不同。"""

    def test_t39_full_usage_is_normalized(self) -> None:
        usage = usage_from_openai(
            {
                "prompt_tokens": 120,
                "completion_tokens": 18,
                "prompt_tokens_details": {"cached_tokens": 96},
                "completion_tokens_details": {"reasoning_tokens": 5},
            }
        )
        assert usage is not None
        self.assertEqual((usage.input_tokens, usage.output_tokens), (120, 18))
        self.assertEqual(usage.cached_input_tokens, 96)
        self.assertEqual(usage.reasoning_tokens, 5)
        self.assertAlmostEqual(usage.cache_hit_ratio or 0.0, 0.8)

    def test_t39b_context_tokens_is_prompt_tokens_itself(self) -> None:
        """★ CHANGE-005：OpenAI 兼容的 ``prompt_tokens`` **本身就是上下文总量**。

        与 Anthropic **相反**（那里要三个字段相加）—— 这就是为什么 ``context_tokens``
        必须由**适配器**填，而不是让下游去加减。
        """
        usage = usage_from_openai(
            {
                "prompt_tokens": 120,
                "completion_tokens": 18,
                "prompt_tokens_details": {"cached_tokens": 96},
            }
        )
        assert usage is not None
        self.assertEqual(
            usage.context_tokens,
            120,
            "不能写成 120+96：缓存命中的 96 本来就含在这 120 里",
        )

    def test_t40_missing_cache_is_none_not_zero(self) -> None:
        usage = usage_from_openai({"prompt_tokens": 500, "completion_tokens": 40})
        assert usage is not None
        self.assertIsNone(usage.cached_input_tokens)
        self.assertIsNone(usage.cache_hit_ratio)  # 状态栏据此**隐藏** cache 项

    def test_t41_explicit_zero_cache_is_zero(self) -> None:
        usage = usage_from_openai({"prompt_tokens": 800, "completion_tokens": 12, "prompt_tokens_details": {"cached_tokens": 0}})
        assert usage is not None
        self.assertEqual(usage.cached_input_tokens, 0)
        self.assertEqual(usage.cache_hit_ratio, 0.0)  # 状态栏据此**显示** ``cache 0%``

    def test_t42_deepseek_cache_field_is_a_fallback(self) -> None:
        usage = usage_from_openai({"prompt_tokens": 4000, "completion_tokens": 260, "prompt_cache_hit_tokens": 3500})
        assert usage is not None
        self.assertEqual(usage.cached_input_tokens, 3500)

    def test_t43_missing_required_tokens_yields_none(self) -> None:
        """E-6：少任何一项都不构成有意义的用量——**不伪造 0**。"""
        self.assertIsNone(usage_from_openai({"prompt_tokens": 10}))
        self.assertIsNone(usage_from_openai({"completion_tokens": 10}))
        self.assertIsNone(usage_from_openai({}))
        self.assertIsNone(usage_from_openai(None))

    def test_t44_non_numeric_tokens_yield_none(self) -> None:
        self.assertIsNone(usage_from_openai({"prompt_tokens": "many", "completion_tokens": 1}))


class TextStreamTests(unittest.TestCase):
    def test_t45_text_deltas_are_emitted_in_order(self) -> None:
        events = translate_events(provider(), load_chunks("openai_text"))
        deltas = [e for e in events if isinstance(e, DeltaEvent)]
        self.assertEqual([d.kind for d in deltas], ["text", "text"])
        self.assertEqual("".join(d.text for d in deltas), "把超时改成 30 秒")

    def test_t46_stop_event_carries_model_and_reason(self) -> None:
        events = translate_events(provider(), load_chunks("openai_text"))
        stops = [e for e in events if isinstance(e, StopEvent)]
        self.assertEqual(len(stops), 1)
        self.assertEqual(stops[0].stop_reason, "end_turn")
        self.assertEqual(stops[0].model, "gpt-4o-mini")

    def test_t47_usage_event_is_last_before_stop(self) -> None:
        """用量分片在末尾（``choices`` 为空）；它必须仍然被读到。"""
        events = translate_events(provider(), load_chunks("openai_text"))
        kinds = [type(e).__name__ for e in events]
        self.assertEqual(kinds, ["DeltaEvent", "DeltaEvent", "UsageEvent", "StopEvent"])

    def test_t48_deepseek_reasoning_comes_before_text(self) -> None:
        events = translate_events(provider(), load_chunks("deepseek_reasoning"))
        deltas = [e for e in events if isinstance(e, DeltaEvent)]
        self.assertEqual([d.kind for d in deltas], ["reasoning", "reasoning", "text"])
        self.assertEqual("".join(d.text for d in deltas if d.kind == "reasoning"), "先看 kernel 里的重试实现，改成指数退避。")

    def test_t49_reasoning_usage_is_carried_through(self) -> None:
        events = translate_events(provider(), load_chunks("deepseek_reasoning"))
        usage = next(e.usage for e in events if isinstance(e, UsageEvent))
        self.assertEqual(usage.cached_input_tokens, 3500)
        self.assertEqual(usage.reasoning_tokens, 180)

    def test_t50_empty_content_fragments_emit_nothing(self) -> None:
        events = translate_events(provider(), load_chunks("openai_text"))
        self.assertTrue(all(d.text for d in events if isinstance(d, DeltaEvent)))


    def test_ollama_reasoning_is_streamed_before_text_and_stop(self) -> None:
        chunks = [
            {"choices": [{"delta": {"reasoning": "先核对"}}]},
            {"choices": [{"delta": {"reasoning": "原始依据", "content": "结论"}}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        ]
        events = translate_events(provider(), chunks)
        deltas = [e for e in events if isinstance(e, DeltaEvent)]
        self.assertEqual([(e.kind, e.text) for e in deltas],
                         [("reasoning", "先核对"), ("reasoning", "原始依据"), ("text", "结论")])
        self.assertIsInstance(events[-1], StopEvent)

    def test_reasoning_aliases_do_not_duplicate_or_hide_valid_fallback(self) -> None:
        for primary in (None, "", {}, [], 42):
            with self.subTest(primary=primary):
                events = translate_events(provider(), [{"choices": [{"delta": {
                    "reasoning_content": primary, "reasoning": "Ollama 思考"}}]}])
                self.assertEqual([e.text for e in events if isinstance(e, DeltaEvent)], ["Ollama 思考"])
        events = translate_events(provider(), [{"choices": [{"delta": {
            "reasoning_content": "只展示一次", "reasoning": "只展示一次"}}]}])
        self.assertEqual([e.text for e in events if isinstance(e, DeltaEvent)], ["只展示一次"])

    def test_invalid_reasoning_fields_emit_no_fake_thoughts(self) -> None:
        for value in (None, "", {}, [], 42):
            with self.subTest(value=value):
                events = translate_events(provider(), [{"choices": [{"delta": {"reasoning": value}}]}])
                self.assertFalse(any(isinstance(e, DeltaEvent) for e in events))


class ToolCallStreamTests(unittest.TestCase):
    """工具参数是增量 JSON 片段——**拼完再给上层**是这一层的全部意义。"""

    def test_t51_single_tool_call_is_fully_assembled(self) -> None:
        events = translate_events(provider(), load_chunks("openai_tool_call"))
        calls = [e for e in events if isinstance(e, ToolCallEvent)]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].call_id, "call_abc")
        self.assertEqual(calls[0].name, "read")
        self.assertEqual(calls[0].arguments, {"path": "src/a.py"})

    def test_t52_stop_reason_is_tool_use(self) -> None:
        events = translate_events(provider(), load_chunks("openai_tool_call"))
        stop = next(e for e in events if isinstance(e, StopEvent))
        self.assertEqual(stop.stop_reason, "tool_use")

    def test_t53_tool_call_is_emitted_after_all_text(self) -> None:
        """装配完成的调用在流末尾一次性产出，避免上层看到半截参数。"""
        events = translate_events(provider(), load_chunks("openai_tool_call"))
        self.assertIsInstance(events[0], ToolCallEvent)

    def test_t54_parallel_tool_calls_are_kept_apart_by_index(self) -> None:
        events = translate_events(provider(), load_chunks("openai_multi_tool"))
        calls = [e for e in events if isinstance(e, ToolCallEvent)]
        self.assertEqual([c.index for c in calls], [0, 1])
        self.assertEqual(calls[0].arguments, {"path": "a.py"})
        self.assertEqual(calls[1].name, "grep")
        self.assertEqual(calls[1].arguments, {"pattern": "retry"})

    def test_t55_no_usage_event_when_vendor_reports_none(self) -> None:
        events = translate_events(provider(), load_chunks("openai_multi_tool"))
        self.assertFalse(any(isinstance(e, UsageEvent) for e in events))

    def test_t56_truncated_arguments_never_leak_a_half_call(self) -> None:
        """E-2：``max_tokens`` 截断导致 JSON 不完整时，**报错而不是给半个调用**。"""
        events = translate_events(provider(), load_chunks("openai_truncated_tool"))
        self.assertFalse(any(isinstance(e, ToolCallEvent) for e in events))
        errors = [e for e in events if isinstance(e, ProviderErrorEvent)]
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].category, ErrorCategory.BAD_REQUEST)
        self.assertIn("不是合法 JSON", errors[0].message)
        self.assertIn('"old', errors[0].detail or "")

    def test_t56b_truncated_arguments_are_flagged_for_self_healing(self) -> None:
        """★ **D160 的守卫（openai 侧）**：必须打 `is_truncated` 并给出"请分批"的提示。

        为什么值得单独一条：`kernel/loop.py:607` 的 D121 自愈闭环**只认这个标记**，
        而它此前**从未被任何用例断言过** —— 于是 Anthropic 侧一直漏打（见
        `test_anthropic.py::test_t99b`），半年内没人发现。
        **一个没人断言过的标记，等于没有标记。**
        """
        events = translate_events(provider(), load_chunks("openai_truncated_tool"))
        error = next(e for e in events if isinstance(e, ProviderErrorEvent))
        self.assertTrue(error.is_truncated, "截断必须打标记，否则自愈闭环不会触发")
        self.assertIn("Token 上限", error.message, "还要告诉模型‘请分批’，光有标记不够")

    def test_t57_stop_event_still_arrives_after_a_bad_tool_call(self) -> None:
        events = translate_events(provider(), load_chunks("openai_truncated_tool"))
        stop = next(e for e in events if isinstance(e, StopEvent))
        self.assertEqual(stop.stop_reason, "max_tokens")


class ErrorHandlingTests(unittest.TestCase):
    def test_t58_error_embedded_in_a_chunk_is_classified(self) -> None:
        events = translate_events(provider(), load_chunks("openai_error_chunk"))
        errors = [e for e in events if isinstance(e, ProviderErrorEvent)]
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].category, ErrorCategory.RATE_LIMIT)
        self.assertEqual(errors[0].retry_after_s, 7.0)
        self.assertIn("Rate limit", errors[0].message)

    def test_t59_stream_stops_after_an_embedded_error(self) -> None:
        """错误分片之后不该再产出用量/结束事件——否则上层会以为这一轮正常收尾了。"""
        events = translate_events(provider(), load_chunks("openai_error_chunk"))
        self.assertFalse(any(isinstance(e, StopEvent) for e in events))
        self.assertIsInstance(events[-1], ProviderErrorEvent)

    def test_t60_network_exception_becomes_a_classified_event(self) -> None:
        events = translate_events(provider(), load_chunks("openai_text"))  # 仅用于确认基线
        self.assertTrue(events)

        inst = provider(raw_stream=raising_stream(ConnectionError("connection reset")))
        result = asyncio.run(_drain(inst.stream(make_request())))
        self.assertIsInstance(result[0], DeltaEvent)  # 异常之前的分片已经发出
        error = result[-1]
        self.assertIsInstance(error, ProviderErrorEvent)
        assert isinstance(error, ProviderErrorEvent)
        self.assertEqual(error.category, ErrorCategory.NETWORK)

    def test_t61_cancelled_error_is_reraised_not_swallowed(self) -> None:
        """E-5：**吞掉它会让 Esc 中断静默失效**，这是最不可接受的失败方式。"""
        inst = provider(raw_stream=raising_stream(asyncio.CancelledError()))
        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(_drain(inst.stream(make_request())))

    def test_t62_missing_api_key_is_reported_as_auth_error(self) -> None:
        """未配置 Key 时不许崩，也不许静默——给出可定位的分类错误。"""
        result = asyncio.run(_drain(provider().stream(make_request())))
        self.assertEqual(len(result), 1)
        error = result[0]
        assert isinstance(error, ProviderErrorEvent)
        self.assertEqual(error.category, ErrorCategory.AUTH)
        self.assertIn("API Key", error.message)

    def test_t63_http_429_is_rate_limited(self) -> None:
        class FakeStatusError(Exception):
            status_code = 429

        inst = provider(raw_stream=raising_stream(FakeStatusError("slow down")))
        result = asyncio.run(_drain(inst.stream(make_request())))
        error = result[-1]
        assert isinstance(error, ProviderErrorEvent)
        self.assertEqual(error.category, ErrorCategory.RATE_LIMIT)
        self.assertEqual(error.detail, "HTTP 429")

    def test_t64_retry_after_header_is_picked_up(self) -> None:
        class FakeResponse:
            headers = {"retry-after": "12.5"}

        class FakeStatusError(Exception):
            status_code = 503
            response = FakeResponse()

        inst = provider(raw_stream=raising_stream(FakeStatusError("unavailable")))
        result = asyncio.run(_drain(inst.stream(make_request())))
        error = result[-1]
        assert isinstance(error, ProviderErrorEvent)
        self.assertEqual(error.retry_after_s, 12.5)
        self.assertEqual(error.category, ErrorCategory.NETWORK)


class RawStreamSeamTests(unittest.TestCase):
    """接缝本身的行为：注入的源必须被真的用上，且能读到请求。"""

    def test_t65_injected_stream_receives_the_request(self) -> None:
        seen: list[ChatRequest] = []

        def factory(request: ChatRequest):  # type: ignore[no-untyped-def]
            seen.append(request)

            async def generator():  # type: ignore[no-untyped-def]
                yield {"choices": [{"index": 0, "delta": {"content": "ok"}, "finish_reason": "stop"}]}

            return generator()

        inst = provider(raw_stream=factory)
        request = make_request(model="deepseek-chat", temperature=0.3)
        events = asyncio.run(_drain(inst.stream(request)))
        self.assertEqual(seen, [request])
        self.assertEqual([e.text for e in events if isinstance(e, DeltaEvent)], ["ok"])

    def test_t66_text_fixture_can_also_be_replayed_through_stream(self) -> None:
        """同一个夹具既能喂 ``translate()`` 也能喂 ``stream()``，两条路结果一致。"""
        chunks = load_chunks("openai_text")
        inst = provider(raw_stream=stream_of(chunks))
        via_stream = asyncio.run(_drain(inst.stream(make_request())))
        via_translate = translate_events(inst, chunks)
        self.assertEqual([type(e).__name__ for e in via_stream], [type(e).__name__ for e in via_translate])


async def _drain(agen):  # type: ignore[no-untyped-def]
    return [item async for item in agen]


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
