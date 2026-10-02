"""``providers/base.py`` 的基础构件契约（MODULE_providers §8 的 T-01 – T-12）。

这些构件是适配器的"公共零件"：结束原因归一化、用量整数读取、工具调用装配。
它们错了，两个适配器会一起错，所以单独测。
"""

from __future__ import annotations

import time
import unittest

from pydantic import ValidationError

from logox.providers.base import (
    ChatRequest,
    Stopwatch,
    ThinkingConfig,
    ToolCallBuffer,
    ToolCallEvent,
    int_or_none,
    normalize_stop_reason,
)


class IntOrNoneTests(unittest.TestCase):
    """D39 / E-7：**取不到就是 None，绝不用 0 冒充**。"""

    def test_t01_present_int_is_returned(self) -> None:
        self.assertEqual(int_or_none(42), 42)

    def test_t02_missing_is_none_not_zero(self) -> None:
        for value in (None, "abc", {}, [], object()):
            with self.subTest(value=value):
                self.assertIsNone(int_or_none(value))

    def test_t03_bool_is_rejected(self) -> None:
        """``True`` 是 ``int`` 的子类；把它当 1 会让用量凭空多一个 token。"""
        self.assertIsNone(int_or_none(True))
        self.assertIsNone(int_or_none(False))

    def test_t04_float_and_numeric_string_coerce(self) -> None:
        self.assertEqual(int_or_none(3.0), 3)
        self.assertEqual(int_or_none("17"), 17)

    def test_t05_negative_is_preserved(self) -> None:
        """不在这里做合法性裁剪——pydantic 的 ``ge=0`` 才是唯一裁判。"""
        self.assertEqual(int_or_none(-1), -1)


class NormalizeStopReasonTests(unittest.TestCase):
    """E-4：厂商用词各异，未知值必须归一化为 ``unknown`` 而不是抛异常。"""

    def test_t06_openai_vocabulary(self) -> None:
        self.assertEqual(normalize_stop_reason("stop"), "end_turn")
        self.assertEqual(normalize_stop_reason("length"), "max_tokens")
        self.assertEqual(normalize_stop_reason("tool_calls"), "tool_use")
        self.assertEqual(normalize_stop_reason("function_call"), "tool_use")

    def test_t07_anthropic_vocabulary(self) -> None:
        self.assertEqual(normalize_stop_reason("end_turn"), "end_turn")
        self.assertEqual(normalize_stop_reason("tool_use"), "tool_use")
        self.assertEqual(normalize_stop_reason("stop_sequence"), "stop_sequence")

    def test_t08_unknown_and_missing(self) -> None:
        self.assertEqual(normalize_stop_reason("something_new_from_vendor"), "unknown")
        self.assertEqual(normalize_stop_reason(None), "unknown")
        self.assertEqual(normalize_stop_reason(""), "unknown")


class ToolCallBufferTests(unittest.TestCase):
    """E-2 / E-10：装配完成才外泄；拼不完整就报错，绝不给半成品。"""

    def test_t09_fragments_are_concatenated_and_parsed(self) -> None:
        buffer = ToolCallBuffer(0, call_id="call_1", name="read")
        for fragment in ('{"pa', 'th": "src/a.py"', "}"):
            buffer.merge(fragment=fragment)
        event = buffer.finalize()
        self.assertIsInstance(event, ToolCallEvent)
        assert isinstance(event, ToolCallEvent)
        self.assertEqual(event.arguments, {"path": "src/a.py"})
        self.assertEqual(event.call_id, "call_1")
        self.assertEqual(event.name, "read")

    def test_t10_name_is_replaced_not_appended(self) -> None:
        """厂商重发同名时拼接会得到 ``readread``。"""
        buffer = ToolCallBuffer(0, name="read")
        buffer.merge(name="read")
        buffer.merge(name="read")
        event = buffer.finalize()
        assert isinstance(event, ToolCallEvent)
        self.assertEqual(event.name, "read")

    def test_t11_truncated_json_never_leaks_a_half_call(self) -> None:
        buffer = ToolCallBuffer(0, call_id="call_bad", name="edit")
        buffer.merge(fragment='{"path": "a.py", "old')
        outcome = buffer.finalize()
        self.assertIsInstance(outcome, dict)
        assert isinstance(outcome, dict)
        self.assertIn("不是合法 JSON", outcome["error"])
        self.assertIn('"old', outcome["detail"])

    def test_t12_missing_name_is_an_error(self) -> None:
        outcome = ToolCallBuffer(2).finalize()
        assert isinstance(outcome, dict)
        self.assertIn("缺少函数名", outcome["error"])

    def test_t13_no_arguments_is_legal(self) -> None:
        """E-10：无参工具调用是合法的，不是错误。"""
        event = ToolCallBuffer(1, call_id="c", name="list_dir").finalize()
        assert isinstance(event, ToolCallEvent)
        self.assertEqual(event.arguments, {})

    def test_t14_non_object_arguments_rejected(self) -> None:
        buffer = ToolCallBuffer(0, name="read")
        buffer.merge(fragment="[1, 2]")
        outcome = buffer.finalize()
        assert isinstance(outcome, dict)
        self.assertIn("JSON 对象", outcome["error"])

    def test_t15_generated_call_id_when_vendor_omits_it(self) -> None:
        buffer = ToolCallBuffer(3, name="read")
        buffer.merge(fragment="{}")
        event = buffer.finalize()
        assert isinstance(event, ToolCallEvent)
        self.assertEqual(event.call_id, "call_3")


class ThinkingConfigTests(unittest.TestCase):
    """D42：``auto`` 表示「不干预」，因此不该向厂商传任何参数。"""

    def test_t16_default_is_auto_and_inactive(self) -> None:
        self.assertFalse(ThinkingConfig().active)
        self.assertFalse(ThinkingConfig(effort="auto").active)

    def test_t17_explicit_efforts_are_active(self) -> None:
        for effort in ("off", "low", "medium", "high"):
            with self.subTest(effort=effort):
                self.assertTrue(ThinkingConfig(effort=effort).active)  # type: ignore[arg-type]

    def test_t18_invalid_effort_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            ThinkingConfig(effort="turbo")  # type: ignore[arg-type]

    def test_t19_unknown_field_is_rejected(self) -> None:
        """``extra="forbid"``：拼错的配置项必须立刻报错，而不是被静默忽略。"""
        with self.assertRaises(ValidationError):
            ThinkingConfig(level="high")  # type: ignore[call-arg]


class StopwatchTests(unittest.TestCase):
    """D39：``first_token_ms`` 只算首字等待，``tok/s`` 只算生成阶段。"""

    def test_t20_first_token_unset_until_marked(self) -> None:
        watch = Stopwatch()
        self.assertIsNone(watch.first_token_ms)

    def test_t21_mark_records_only_the_first_time(self) -> None:
        watch = Stopwatch()
        time.sleep(0.01)
        watch.mark()
        first = watch.first_token_ms
        time.sleep(0.02)
        watch.mark()
        self.assertEqual(watch.first_token_ms, first)
        self.assertIsNotNone(first)
        assert first is not None
        self.assertGreaterEqual(first, 5)

    def test_t22_elapsed_is_non_negative(self) -> None:
        self.assertGreaterEqual(Stopwatch().elapsed_ms, 0)


class ChatRequestTests(unittest.TestCase):
    def test_t23_defaults_are_empty(self) -> None:
        request = ChatRequest(model="m")
        self.assertEqual(request.system, "")
        self.assertEqual(request.messages, [])
        self.assertEqual(request.tools, [])
        self.assertIsNone(request.temperature)
        self.assertIsNone(request.max_tokens)
        self.assertIsNone(request.thinking)

    def test_t24_unknown_field_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            ChatRequest(model="m", stream=True)  # type: ignore[call-arg]
