"""Logox 的全部异常类型与错误分类（叶子模块，无任何依赖）。

放在顶层而不是 ``config/`` 或 ``kernel/`` 下，是为了让两个模块都能引用而
**不产生跨层 import**（ARCHITECTURE.md 规则 R2）。

错误信息的一贯要求（D44 / MODULE_config §9）：可定位报错必须同时包含
**文件路径 + 字段路径 + 原因 +（可用值）**四要素。
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any

__all__ = [
    "BusClosedError",
    "ConfigError",
    "ConfigSyntaxError",
    "ConfigValidationError",
    "ConfigWriteError",
    "ErrorCategory",
    "LogoxError",
    "MissingApiKeyError",
    "SessionMismatchError",
    "SubscriberError",
    "ThemeError",
    "TomlWriteError",
    "UnknownEventError",
    "UnknownProviderError",
]


class LogoxError(Exception):
    """Logox 全部异常的基类。"""


class ErrorCategory(str, Enum):
    """错误分类（D22：决定是否可自动重试 / 是否可回灌模型自愈）。

    ``RATE_LIMIT`` / ``NETWORK``          → 可重试（指数退避，限次）
    ``CONTEXT_OVERFLOW``                  → 可重试，但需先压缩上下文
    ``AUTH`` / ``BAD_REQUEST`` / ``UNKNOWN`` → 不可重试
    ``TOOL_FAILURE``                      → 可回灌给模型自愈（限次）
    """

    RATE_LIMIT = "rate_limit"
    NETWORK = "network"
    CONTEXT_OVERFLOW = "context_overflow"
    AUTH = "auth"
    BAD_REQUEST = "bad_request"
    TOOL_FAILURE = "tool_failure"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"

    @property
    def retryable(self) -> bool:
        return self in (ErrorCategory.RATE_LIMIT, ErrorCategory.NETWORK, ErrorCategory.CONTEXT_OVERFLOW)

    @property
    def feedable_to_model(self) -> bool:
        return self in (ErrorCategory.TOOL_FAILURE, ErrorCategory.BAD_REQUEST)


# --------------------------------------------------------------------------- #
# 事件总线（kernel/bus.py）
# --------------------------------------------------------------------------- #


class BusClosedError(LogoxError):
    """向已关闭的事件总线发布事件。

    **绝不静默丢弃**（E-11）：静默丢弃会让「会话其实已经结束」这件事变得不可见。
    """

    def __init__(self, session_id: str) -> None:
        super().__init__(
            f"事件总线已关闭（session_id={session_id}）；"
            "如需继续，请新建 EventBus 或调用 reset()。"
        )
        self.session_id = session_id


class SessionMismatchError(LogoxError):
    """事件携带的 ``session_id`` 与总线不符（E-10）。

    专门拦截这一情形，是因为 ``/resume`` 恢复会话时极易把两个会话的事件串在一起，
    而串会话的症状（上下文错乱、检查点回滚到别人的文件）非常难排查。
    """

    def __init__(self, expected: str, actual: str) -> None:
        super().__init__(
            f"事件的 session_id 与总线不符：总线为 {expected!r}，事件为 {actual!r}。"
        )
        self.expected = expected
        self.actual = actual


class UnknownEventError(LogoxError):
    """反序列化遇到未知事件类型（E-13）。

    **不静默跳过**：静默跳过会让「日志少了几条」永远查不出来。
    """

    def __init__(self, type_name: str, known: tuple[str, ...] = ()) -> None:
        known_hint = f"；已知类型：{', '.join(sorted(known))}" if known else ""
        super().__init__(f"未知的事件类型 {type_name!r}{known_hint}")
        self.type_name = type_name


class SubscriberError(LogoxError):
    """订阅者自身抛出的错误（保留原始异常链）。"""


# --------------------------------------------------------------------------- #
# 配置与主题（config/*）
# --------------------------------------------------------------------------- #


class ConfigError(LogoxError):
    """配置相关错误基类。"""

    def __init__(self, path: Path | str, message: str) -> None:
        self.path = Path(path)
        super().__init__(f"{self.path}: {message}")


class ConfigSyntaxError(ConfigError):
    """TOML 语法错误（E-2）。行列信息来自 ``tomllib`` 的异常消息（尽力提取）。"""

    def __init__(self, path: Path | str, message: str, line: int | None = None, column: int | None = None) -> None:
        location = ""
        if line is not None:
            location = f"（第 {line} 行" + (f"，第 {column} 列）" if column is not None else "）")
        super().__init__(path, f"TOML 语法错误{location}：{message}")
        self.line = line
        self.column = column


class ConfigValidationError(ConfigError):
    """字段校验/语义约束失败（E-4：**一次列出全部**问题，不是只报第一个）。"""

    def __init__(self, path: Path | str, issues: list[Any]) -> None:
        self.issues = list(issues)
        count = len(self.issues)
        super().__init__(path, f"配置校验失败（{count} 处问题）")


class ConfigWriteError(ConfigError):
    """状态写回失败（E-9 / E-10 / E-21）。**调用方必须降级为「记日志 + 继续」。**"""

    def __init__(self, path: Path | str, message: str, cause: BaseException | None = None) -> None:
        super().__init__(path, message)
        self.cause = cause


class TomlWriteError(LogoxError):
    """极简 TOML writer 遇到不支持的类型（E-18 / E-19）。

    **绝不静默降级**：写出格式错误的 TOML 会污染用户的配置文件。
    """

    def __init__(self, message: str, key_path: str = "") -> None:
        where = f"（位置：{key_path}）" if key_path else ""
        super().__init__(f"无法序列化为 TOML{where}：{message}")
        self.key_path = key_path


class ThemeError(ConfigError):
    """主题文件加载失败（E-12 / E-13）。调用方应回退到 ``logox-dark`` 并提示。"""


# --------------------------------------------------------------------------- #
# Provider 装配（providers/registry.py）
# --------------------------------------------------------------------------- #


class UnknownProviderError(LogoxError):
    """配置引用了不存在的 Provider 实例名。

    按 D44 四要素组织消息：**字段路径 + 原因 + 可用值 + 下一步**。
    只说"找不到"会让用户无从下手——可用值列表才是能立刻行动的信息。
    """

    def __init__(self, name: str, known: tuple[str, ...] | list[str]) -> None:
        self.name = name
        self.known = tuple(sorted(known))
        available = "、".join(self.known) if self.known else "（无）"
        super().__init__(
            f"provider.name = {name!r} 没有对应的 Provider 实例；可用值：{available}。"
            f"请在 config.toml 里补上 [providers.{name}]，或改用上面列出的名字。"
        )


class MissingApiKeyError(LogoxError):
    """所需的环境变量未设置或为空（E-16）。

    **绝不回显任何密钥内容**——连用户写的值的一部分都不回显，因为错误消息会进日志、
    进终端、进截图。消息里只出现**变量名**。
    """

    def __init__(self, provider: str, env_var: str) -> None:
        self.provider = provider
        self.env_var = env_var
        super().__init__(
            f"Provider {provider!r} 需要环境变量 {env_var}，但它未设置或为空。"
            f"请在启动 Logox 前导出它，或在 config.toml 里把 "
            f'[providers.{provider}] 的 api_key_env 改成实际使用的变量名。'
        )
