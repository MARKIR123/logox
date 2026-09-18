"""错误分类（D22）。

把任意异常映射成 :class:`~logox.errors.ErrorCategory`，供内核循环决定
「可否自动重试」与「可否回灌给模型自愈」。

**不使用**任何厂商 SDK 的类型：全部通过鸭子类型（``status_code`` 等属性）
与类名字符串判断，因此本模块在未安装 openai/anthropic 的环境下也能工作。
"""

from __future__ import annotations

import asyncio
import socket
import ssl

from logox.errors import ErrorCategory

__all__ = ["MAX_FEEDBACK_ATTEMPTS", "NETWORK_ERROR_NAMES", "classify_exception", "is_retryable"]

MAX_FEEDBACK_ATTEMPTS = 3
"""工具错误回灌给模型自愈的最大次数（D22：限次，避免死循环）。"""

NETWORK_ERROR_NAMES = (
    "APIConnectionError",
    "APITimeoutError",
    "ConnectError",
    "ConnectTimeout",
    "ReadTimeout",
    "WriteTimeout",
    "PoolTimeout",
    "RemoteProtocolError",
)
"""厂商 / httpx 的**连接类**异常名。列出来是为了让"哪些算网络错误"可被测试断言，
而不是散落在代码里的一串字符串字面量。"""

_RATE_LIMIT_HINTS = ("rate limit", "rate_limit", "ratelimit", "too many requests", "overloaded")
_CONTEXT_HINTS = ("context length", "context_length", "too long", "maximum context", "token limit")

#: **按类名**识别的网络错误。
#:
#: 为什么必须补这一条：厂商 SDK 把自己的连接失败包成了 ``APIConnectionError`` /
#: ``APITimeoutError``，而它们**不是** :class:`ConnectionError` 的子类（openai 的
#: ``APIError`` 直接继承 ``Exception``），所以 isinstance 那几条全部命中不了，
#: 最后落到 ``UNKNOWN``——而 ``UNKNOWN`` **不可重试**。
#: 后果是本项目最该自动重试的一类故障（网络抖动）恰好不重试，
#: 实测就是"连不上本地 Ollama，直接以 unknown 失败"。
#:
#: 用类名而不是 import SDK 类型，是为了让本模块在未安装 openai/anthropic 的
#: 环境里也能工作（本模块的既定原则）。
_NETWORK_NAME_HINTS = (
    "apiconnectionerror",
    "apitimeouterror",
    "apistatuserror",  # 5xx 分支已在上面按状态码处理，这里兜底其余情况
    "connecterror",
    "connecttimeout",
    "readtimeout",
    "writetimeout",
    "pooltimeout",
    "remoteprotocolerror",
)


def _status_code(exc: BaseException) -> int | None:
    for attr in ("status_code", "status", "http_status", "code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    response = getattr(exc, "response", None)
    code = getattr(response, "status_code", None)
    return code if isinstance(code, int) else None


def classify_exception(exc: BaseException) -> ErrorCategory:
    """把异常归类。

    :class:`asyncio.CancelledError` 单独归类为 ``CANCELLED``——调用方**必须**
    把它原样重新抛出，绝不可当作普通失败吞掉（E-5/E-6：否则 Esc 会静默失效）。
    """
    if isinstance(exc, asyncio.CancelledError):
        return ErrorCategory.CANCELLED

    if isinstance(exc, (TimeoutError, socket.timeout)):
        return ErrorCategory.NETWORK

    if isinstance(exc, (ssl.SSLError, ConnectionError, socket.gaierror)):
        return ErrorCategory.NETWORK

    code = _status_code(exc)
    if code is not None:
        if code == 429:
            return ErrorCategory.RATE_LIMIT
        if code in (401, 403):
            return ErrorCategory.AUTH
        if code in (400, 404, 405, 422):
            return ErrorCategory.BAD_REQUEST
        if 500 <= code < 600:
            return ErrorCategory.NETWORK
        if code == 408:
            return ErrorCategory.NETWORK

    name = type(exc).__name__.casefold()
    message = str(exc).casefold()
    haystack = f"{name} {message}"

    if any(hint in haystack for hint in _RATE_LIMIT_HINTS):
        return ErrorCategory.RATE_LIMIT
    if any(hint in haystack for hint in _CONTEXT_HINTS):
        return ErrorCategory.CONTEXT_OVERFLOW
    if any(hint in haystack for hint in ("unauthorized", "authentication", "invalid api key", "api key")):
        return ErrorCategory.AUTH
    # 厂商 SDK 的连接/超时异常（见 _NETWORK_NAME_HINTS 的说明）
    if any(hint == name or hint in name for hint in _NETWORK_NAME_HINTS):
        return ErrorCategory.NETWORK
    if isinstance(exc, (OSError,)):
        return ErrorCategory.NETWORK
    if isinstance(exc, (ValueError, TypeError, KeyError)):
        return ErrorCategory.BAD_REQUEST

    return ErrorCategory.UNKNOWN


def is_retryable(exc: BaseException) -> bool:
    """该异常是否值得自动重试（D22：仅限流 / 网络 / 上下文超限）。"""
    return classify_exception(exc).retryable
