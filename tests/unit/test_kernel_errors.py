"""错误分类测试（D22 / `kernel/errors.py`）。

这个函数决定**要不要自动重试**，也就决定了"网络抖一下会不会让用户手动重发一次"。
它此前只有间接覆盖，而 M3 实测暴露了一个真实误判：

    连不上本地 Ollama → 厂商 SDK 抛 ``APIConnectionError`` → 被归为 ``UNKNOWN``
    → **不可重试** → 本项目最该自动重试的那类故障恰好不重试

所以这里逐类钉死，并特别覆盖"厂商 SDK 把自己的连接错误包了一层"这种情况。
"""

from __future__ import annotations

import asyncio
import ssl
import unittest

from logox.errors import ErrorCategory
from logox.kernel.errors import NETWORK_ERROR_NAMES, classify_exception, is_retryable


def _status_error(code: int) -> Exception:
    class _Error(Exception):
        status_code = code

    return _Error(f"HTTP {code}")


class VendorNetworkErrorTests(unittest.TestCase):
    """厂商 SDK 的连接/超时异常必须归为 ``NETWORK``（可重试）。"""

    def test_t01_api_connection_error_is_network(self) -> None:
        """openai 的 ``APIConnectionError`` 继承自 ``Exception`` 而**不是** ``ConnectionError``，
        所以 isinstance 那几条一个都命中不了——必须按类名兜住。"""
        for name in NETWORK_ERROR_NAMES:
            with self.subTest(vendor_error=name):
                exc = type(name, (Exception,), {})("Connection error.")
                self.assertIs(classify_exception(exc), ErrorCategory.NETWORK)

    def test_t02_vendor_network_errors_are_retryable(self) -> None:
        exc = type("APIConnectionError", (Exception,), {})("Connection error.")
        self.assertTrue(is_retryable(exc))

    def test_t03_bare_connection_error_is_still_network(self) -> None:
        self.assertIs(classify_exception(ConnectionError("reset")), ErrorCategory.NETWORK)

    def test_t04_timeout_and_socket_errors_are_network(self) -> None:
        for exc in (TimeoutError("slow"), TimeoutError("slow"), ssl.SSLError("bad cert")):
            with self.subTest(exc=type(exc).__name__):
                self.assertIs(classify_exception(exc), ErrorCategory.NETWORK)

    def test_t05_generic_oserror_is_network(self) -> None:
        self.assertIs(classify_exception(OSError("disk?")), ErrorCategory.NETWORK)


class StatusCodeTests(unittest.TestCase):
    def test_t10_rate_limit_is_retryable(self) -> None:
        self.assertIs(classify_exception(_status_error(429)), ErrorCategory.RATE_LIMIT)
        self.assertTrue(is_retryable(_status_error(429)))

    def test_t11_auth_is_not_retryable(self) -> None:
        for code in (401, 403):
            with self.subTest(code=code):
                self.assertIs(classify_exception(_status_error(code)), ErrorCategory.AUTH)
                self.assertFalse(is_retryable(_status_error(code)))

    def test_t12_bad_request_is_not_retryable_but_feedable(self) -> None:
        for code in (400, 404, 405, 422):
            with self.subTest(code=code):
                category = classify_exception(_status_error(code))
                self.assertIs(category, ErrorCategory.BAD_REQUEST)
                self.assertFalse(category.retryable)
                self.assertTrue(category.feedable_to_model)

    def test_t13_server_errors_are_network_and_retryable(self) -> None:
        for code in (500, 502, 503, 504):
            with self.subTest(code=code):
                self.assertIs(classify_exception(_status_error(code)), ErrorCategory.NETWORK)

    def test_t14_request_timeout_status_is_network(self) -> None:
        self.assertIs(classify_exception(_status_error(408)), ErrorCategory.NETWORK)

    def test_t15_status_is_read_from_a_wrapped_response_too(self) -> None:
        """有些 SDK 把状态码挂在 ``exc.response.status_code`` 上。"""

        class _Response:
            status_code = 503

        class _Error(Exception):
            response = _Response()

        self.assertIs(classify_exception(_Error("boom")), ErrorCategory.NETWORK)


class CancellationTests(unittest.TestCase):
    def test_t20_cancelled_error_is_classified_separately(self) -> None:
        """单独一类而不是"普通失败"——调用方**必须**把它原样重抛（E-5）。"""
        self.assertIs(classify_exception(asyncio.CancelledError()), ErrorCategory.CANCELLED)

    def test_t21_cancellation_is_never_retryable(self) -> None:
        """把取消当成"可重试"会让 Esc 变成"再试一次"，这是最恶劣的体验缺陷。"""
        self.assertFalse(is_retryable(asyncio.CancelledError()))
        self.assertFalse(ErrorCategory.CANCELLED.feedable_to_model)


class TextHintTests(unittest.TestCase):
    def test_t30_rate_limit_wording_is_recognised(self) -> None:
        for message in ("Rate limit reached", "429 Too Many Requests", "Overloaded"):
            with self.subTest(message=message):
                self.assertIs(classify_exception(RuntimeError(message)), ErrorCategory.RATE_LIMIT)

    def test_t31_context_overflow_wording_is_recognised(self) -> None:
        exc = RuntimeError("This model's maximum context length is 8192 tokens")
        self.assertIs(classify_exception(exc), ErrorCategory.CONTEXT_OVERFLOW)
        self.assertTrue(is_retryable(exc))  # 可重试，但需要先压缩（压缩由内核负责）

    def test_t32_auth_wording_is_recognised(self) -> None:
        self.assertIs(classify_exception(RuntimeError("Invalid API key provided")), ErrorCategory.AUTH)

    def test_t33_plain_value_error_is_a_bad_request(self) -> None:
        self.assertIs(classify_exception(ValueError("bad field")), ErrorCategory.BAD_REQUEST)

    def test_t34_unknown_stays_unknown(self) -> None:
        """不认识就是 ``UNKNOWN``（**不可重试**）——**绝不猜**。"""
        class WeirdError(Exception):
            pass

        category = classify_exception(WeirdError("something odd"))
        self.assertIs(category, ErrorCategory.UNKNOWN)
        self.assertFalse(category.retryable)

    def test_t35_network_hint_does_not_swallow_a_status_code(self) -> None:
        """类名/文案的兜底**不得**覆盖已明确的状态码——400 不能被说成网络错误。"""

        class APIConnectionError(Exception):
            status_code = 400

        self.assertIs(classify_exception(APIConnectionError("connection error")), ErrorCategory.BAD_REQUEST)


class CategoryContractTests(unittest.TestCase):
    def test_t40_retryable_set_is_exactly_three(self) -> None:
        retryable = {c for c in ErrorCategory if c.retryable}
        self.assertEqual(
            retryable,
            {ErrorCategory.RATE_LIMIT, ErrorCategory.NETWORK, ErrorCategory.CONTEXT_OVERFLOW},
        )

    def test_t41_feedable_set_is_exactly_two(self) -> None:
        feedable = {c for c in ErrorCategory if c.feedable_to_model}
        self.assertEqual(feedable, {ErrorCategory.TOOL_FAILURE, ErrorCategory.BAD_REQUEST})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
