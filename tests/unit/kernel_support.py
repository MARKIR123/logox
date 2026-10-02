"""内核测试脚手架：**脚本化的 mock provider** + 事件录制器。

为什么用「脚本」而不是 mock 对象
--------------------------------
内核循环最容易错的不是"某个函数算错了"，而是**时序**：重试发生在第几个分片之后、
中断时哪些事件已经发出、工具结果按什么顺序进历史。这些只有用"第 1 轮吐这些分片、
第 2 轮抛这个异常"这种**可回放的脚本**才能精确断言。

脚本直接喂给 M2 的 ``OpenAICompatProvider(raw_stream=...)`` 接缝，因此
**走的是真实的适配器翻译路径**——不是绕过适配层假装的。这样内核测试同时
也在回归适配层，而且全程不发网络请求。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from typing import Any

from logox.kernel.bus import EventBus
from logox.kernel.loop import KernelLoop, SimpleContextBuilder
from logox.kernel.registry import ToolRegistry
from logox.providers.openai_compat import OpenAICompatProvider
from logox.tools.base import ToolArgs, ToolContext, ToolResult, ToolSpec

__all__ = [
    "Pause",
    "Recorder",
    "RecordingBundle",
    "StubArgs",
    "StubTool",
    "auth_error",
    "bad_request_error",
    "chunks",
    "install",
    "network_error",
    "rate_limit_error",
    "scripted_provider",
    "text_chunks",
    "tool_chunks",
    "usage_chunk",
]


# --------------------------------------------------------------------------- #
# 错误构造
# --------------------------------------------------------------------------- #


def _status_error(code: int, message: str) -> Exception:
    """带 ``status_code`` 的假异常——``classify_exception`` 正是靠这个属性分类。"""

    class _StatusError(Exception):
        status_code = code

    return _StatusError(message)


def rate_limit_error(message: str = "Rate limit reached") -> Exception:
    return _status_error(429, message)


def auth_error(message: str = "Invalid API key") -> Exception:
    return _status_error(401, message)


def bad_request_error(message: str = "invalid request") -> Exception:
    return _status_error(400, message)


def network_error(message: str = "connection reset") -> Exception:
    return ConnectionError(message)


# --------------------------------------------------------------------------- #
# 分片构造（与真实 OpenAI 流式响应同形）
# --------------------------------------------------------------------------- #


def chunks(*items: Any) -> list[Any]:
    """一串分片。元素里可以是 dict（分片）或 Exception（在此处抛出）。"""
    return list(items)


def text_chunks(*parts: str, model: str = "mock-model") -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for part in parts:
        out.append(
            {
                "choices": [{"index": 0, "delta": {"content": part}, "finish_reason": None}],
                "model": model,
            }
        )
    out.append({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "model": model})
    return out


def tool_chunks(calls: Sequence[tuple[str, str, dict[str, Any]]], *, model: str = "mock-model") -> list[dict[str, Any]]:
    """``[(call_id, tool_name, arguments)]`` → 一批工具调用分片。

    **每个调用必须有不同的 ``index``**：适配层正是靠它区分并行调用的。
    全部用 ``index=0`` 会让两个调用的参数被拼成 ``{}{}``——适配层会（正确地）
    拒绝这个不合法的 JSON，于是整轮以"工具参数不是合法 JSON"失败。
    """
    out: list[dict[str, Any]] = []
    for index, (call_id, name, arguments) in enumerate(calls):
        out.append(
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": index,
                                    "id": call_id,
                                    "type": "function",
                                    "function": {
                                        "name": name,
                                        "arguments": json.dumps(arguments, ensure_ascii=False),
                                    },
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ],
                "model": model,
            }
        )
    out.append({"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}], "model": model})
    return out


def usage_chunk(
    input_tokens: int = 100,
    output_tokens: int = 20,
    *,
    cached: int | None = None,
    model: str = "mock-model",
) -> dict[str, Any]:
    """末尾的用量分片（``choices`` 为空，这正是 OpenAI 流式的真实形状）。"""
    usage: dict[str, Any] = {"prompt_tokens": input_tokens, "completion_tokens": output_tokens}
    if cached is not None:
        usage["prompt_tokens_details"] = {"cached_tokens": cached}
    return {"choices": [], "usage": usage, "model": model}


# --------------------------------------------------------------------------- #
# 一个最小的工具（循环测试用；调度器的并发行为另有专门工具）
# --------------------------------------------------------------------------- #


class StubArgs(ToolArgs):
    path: str = "a.txt"


class StubTool:
    """固定返回一小段文本的工具。可选延迟与失败，用于制造可控的时序。"""

    def __init__(
        self,
        name: str = "read",
        *,
        content: str = "文件内容",
        readonly: bool = True,
        requires_permission: bool = False,
        delay_s: float = 0.0,
        raises: BaseException | None = None,
    ) -> None:
        self.spec = ToolSpec(
            name=name,
            params=StubArgs,
            readonly=readonly,
            requires_permission=requires_permission,
            summary_template=f"{name} {{path}}",
        )
        self.content = content
        self.delay_s = delay_s
        self.raises = raises
        self.calls: list[str] = []

    async def run(self, args: Any, ctx: ToolContext) -> ToolResult:
        self.calls.append(getattr(args, "path", ""))
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if self.raises is not None:
            raise self.raises
        return ToolResult(ok=True, content=self.content)


# --------------------------------------------------------------------------- #
# 脚本化 provider
# --------------------------------------------------------------------------- #


class Pause:
    """脚本里的一个"停一下"标记——用来制造**可在中途取消**的流。

    没有它就无法确定性地测"模型吐了一半时按 Esc"：流必须在某个已知位置挂起，
    测试才有机会在那一刻发出取消。
    """

    __slots__ = ("seconds",)

    def __init__(self, seconds: float = 0.05) -> None:
        self.seconds = seconds


def scripted_provider(script: Sequence[Sequence[Any]], **kwargs: Any) -> OpenAICompatProvider:
    """按脚本逐轮回放的 provider。

    ``script[i]`` 是第 ``i`` 次模型请求要吐出的分片序列；超过脚本长度后
    重复最后一个（让"重试 N 次"这类用例不必写 N 份相同的脚本）。
    序列元素可以是 dict（分片）、:class:`Pause`（挂起）或 Exception（在此处抛出）。
    """
    state = {"index": 0}

    def raw_stream(_request: Any):  # type: ignore[no-untyped-def]
        step = list(script[min(state["index"], len(script) - 1)])
        state["index"] += 1

        async def generator():  # type: ignore[no-untyped-def]
            for item in step:
                if isinstance(item, Pause):
                    await asyncio.sleep(item.seconds)
                    continue
                if isinstance(item, BaseException):
                    raise item
                yield item

        return generator()

    kwargs.setdefault("models", ["mock-model"])
    return OpenAICompatProvider(raw_stream=raw_stream, **kwargs)


# --------------------------------------------------------------------------- #
# 事件录制
# --------------------------------------------------------------------------- #


class Recorder:
    """订阅全部事件并保序记录。用来对**事件顺序**做断言。"""

    def __init__(self) -> None:
        self.events: list[Any] = []

    async def __call__(self, event: Any) -> None:
        self.events.append(event)

    # -- 便捷读取 -------------------------------------------------------- #

    def types(self) -> list[str]:
        return [event.type for event in self.events]

    def of(self, event_type: Any) -> list[Any]:
        from logox.kernel.events import Event

        if isinstance(event_type, type) and issubclass(event_type, Event):
            return [event for event in self.events if isinstance(event, event_type)]
        return [event for event in self.events if event.type == event_type]

    def deltas(self, kind: str | None = None) -> list[str]:
        return [
            event.delta
            for event in self.of("model_delta")
            if kind is None or event.kind == kind
        ]

    def index_of(self, event_type: str) -> int:
        return next(index for index, event in enumerate(self.events) if event.type == event_type)

    def find(self, event_type: str) -> Any:
        return next(event for event in self.events if event.type == event_type)


class RecordingBundle:
    """一套装配好的内核 + 总线 + 录制器。"""

    def __init__(
        self,
        kernel: KernelLoop,
        bus: EventBus,
        recorder: Recorder,
        registry: ToolRegistry,
    ) -> None:
        self.kernel = kernel
        self.bus = bus
        self.recorder = recorder
        self.registry = registry

    def of(self, event_type: Any) -> list[Any]:
        return self.recorder.of(event_type)

    @property
    def events(self) -> list[Any]:
        return self.recorder.events


def install(
    script: Sequence[Sequence[Any]],
    *,
    tools: Sequence[Any] = (),
    decider: Any = None,
    system: str = "sys",
    model: str = "mock-model",
    **loop_kwargs: Any,
) -> RecordingBundle:
    """装配一个"内核 + 脚本化 provider + 录制器"的完整环境。"""
    bus = EventBus(session_id="test-session")
    recorder = Recorder()
    bus.subscribe("*", recorder, name="recorder")

    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)

    # 测试默认**关掉**「再调一次模型补写摘要」（D135 第 2 层）：脚本化 provider 的短回答
    # 大多不合规，开着会让每个用工具的用例都多发一次请求，把「请求次数 / 用量」断言搅乱。
    # 要测第 2 层的用例显式把它打开（见 `tests/unit/test_turn_summary_position.py`）。
    loop_kwargs.setdefault("model_summary_fallback", False)

    kernel = KernelLoop(
        bus,
        scripted_provider(script),
        registry,
        SimpleContextBuilder(system),
        decider,
        model=model,
        **loop_kwargs,
    )
    return RecordingBundle(kernel, bus, recorder, registry)
