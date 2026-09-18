"""**无 API Key 的体验模式**——装配根的一个受限开关（M4）。

它解决什么问题
==============

M4 之后，`logox` 启动的就是真实内核：真的组装上下文、真的调工具、真的走事件总线。
但要真正用起来，你先得**有一个能上网的模型端点**（厂商 API Key，或本机跑着的
Ollama / LM Studio）。这两样都没有时，最该被看到的东西反而看不到：

* 界面真的在驱动内核吗？
* 工具卡片会随着真实执行而改变状态吗？
* 状态栏的 tok/s、cache、ctx 是真的算出来的吗？

本模块提供一个**只替换模型响应**的开关：其它一切都是真的——真 `KernelLoop`、
真 `EventBus`、真 `read` 工具（**真的去读磁盘上的文件**）、真 `LogoxApp`。
因此你能看到完整的"提问 → 模型要工具 → 工具执行 → 回灌 → 回答"链路。

**它不能做什么（必须说清楚）**：这些回答是**预先写好的脚本**，不是模型生成的。
它证明的是"界面与内核的接线正确"，**不是**"模型能力好不好"。

它是怎么接进真实路径的
======================

`LOGOX_SCRIPTED_PROVIDER=1` → `logox.app.build_runtime()` 在这一步换成脚本 provider。
因此**走的是与生产完全相同的那条 `prepare_runtime()`**，包括四类启动检查与主题回退。
启动时会往 stderr 打醒目横幅——生产误开的可能性被压到最低，而且一眼可见。

为什么放在 `src/` 而不是 `tools/`
================================

因为它必须被 `logox.app`（生产装配根）import。放在 `tools/` 会让生产代码依赖一个
脚本目录。代价是这个模块会随发行版一起走——所以它**不做任何危险的事**：
不写文件、不读配置、不发网络请求，只有一段写死的分片序列。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any

from logox.providers.base import (
    ChatRequest,
    ModelInfo,
    ProviderEvent,
)
from logox.providers.openai_compat import OpenAICompatProvider

__all__ = ["ENV_FLAG", "MODEL_NAME", "LogoxDevProvider", "default_script"]

#: 开启体验模式的环境变量。值是 ``"1"`` 即可。
ENV_FLAG = "LOGOX_SCRIPTED_PROVIDER"

MODEL_NAME = "logox-dev-demo"

#: 启动横幅（写到 stderr，不与界面争屏幕）
BANNER = (
    "⚠ 体验模式（LOGOX_SCRIPTED_PROVIDER=1）：模型响应来自**预置脚本**，"
    "不会联网、也不消耗额度；其余（内核、事件总线、工具、界面）全是真的。\n"
)


def _delta(text: str) -> dict[str, Any]:
    return {"choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}], "model": MODEL_NAME}


def _tool_call(call_id: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "choices": [
            {
                "index": 0,
                "delta": {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": call_id,
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
                        }
                    ]
                },
                "finish_reason": None,
            }
        ],
        "model": MODEL_NAME,
    }


def _stop(reason: str) -> dict[str, Any]:
    return {"choices": [{"index": 0, "delta": {}, "finish_reason": reason}], "model": MODEL_NAME}


def _usage(prompt: int, completion: int, cached: int | None = None) -> dict[str, Any]:
    usage: dict[str, Any] = {"prompt_tokens": prompt, "completion_tokens": completion}
    if cached is not None:
        usage["prompt_tokens_details"] = {"cached_tokens": cached}
    return {"choices": [], "usage": usage, "model": MODEL_NAME}


def default_script() -> list[list[dict[str, Any]]]:
    """三个回合的脚本。

    **每一轮都是独立的**（不依赖上一轮读了哪个文件），因此不管你问什么，
    都能完整看到"模型要工具 → 工具真跑 → 回灌 → 回答"这条链路，而不会因为
    脚本与你的问题不匹配而卡住。
    """
    return [
        # 第 1 轮：先解释自己是谁，再要一个工具
        [
            _delta("我是 Logox 的**体验模式**（响应来自预置脚本，不联网）。\n"),
            _delta("为了证明工具链是真的，我去读一个真实文件给你看。\n\n"),
            _usage(120, 40, cached=96),
            _tool_call("demo-1", "read", {"path": "hello.py"}),
            _stop("tool_calls"),
        ],
        # 第 2 轮：基于刚读到的内容给结论
        [
            _delta("读到了。上面那张卡片里的路径、耗时来自**真实的工具执行**，"),
            _delta("文件内容也是从磁盘上读出来的。\n\n"),
            _delta("顺带一提，我现在这台机器上没有配置任何模型端点，"),
            _delta("所以回答本身是写好的——但你能看到的所有**状态**都是真的算出来的。\n"),
            _usage(320, 90, cached=300),
            _stop("stop"),
        ],
    ]


class LogoxDevProvider:
    """按脚本逐轮回放的 Provider（**只替换模型响应**）。

    它是 :class:`~logox.providers.base.Provider` 协议的一个实现，内部复用
    ``OpenAICompatProvider`` 的 ``raw_stream`` 接缝——这与 M3 的测试脚手架
    ``tests/unit/kernel_support.scripted_provider`` 用的是**同一个接缝**，
    因此"体验模式"与"测试"验证的是同一条翻译路径（SSE 装配、工具参数拼接、
    用量归一化），只是脚本不同。

    :param script: 每一轮的原始分片列表；用完后**重复最后一轮**
        （这样你连问几次都不会突然没反应）。
    :param delay_s: 每个分片之间的延迟。**必须非零**：否则一整轮会在同一帧内
        瞬间吐完，你就看不到"流式"这件事了。
    """

    def __init__(self, script: Sequence[Sequence[dict[str, Any]]] | None = None, *, delay_s: float = 0.05) -> None:
        self._script = [list(turn) for turn in (script or default_script())]
        self._index = 0
        self._delay_s = max(0.0, delay_s)
        self.name = "logox-dev"
        self._inner = OpenAICompatProvider(raw_stream=self._raw_stream, models=[MODEL_NAME])

    # -- Provider 协议 --------------------------------------------------- #

    def list_models(self) -> list[ModelInfo]:
        return [
            ModelInfo(
                id=MODEL_NAME,
                provider=self.name,
                supports_thinking=False,
                context_window=32_000,
            )
        ]

    def stream(self, request: ChatRequest) -> AsyncIterator[ProviderEvent]:
        return self._inner.stream(request)

    # -- 脚本回放 -------------------------------------------------------- #

    def _raw_stream(self, request: ChatRequest) -> AsyncIterator[Mapping[str, Any]]:  # noqa: ARG002 - 与接缝签名一致
        chunks = self._script[min(self._index, len(self._script) - 1)]
        self._index += 1

        async def generator() -> AsyncIterator[Mapping[str, Any]]:
            import asyncio

            for chunk in chunks:
                if self._delay_s:
                    await asyncio.sleep(self._delay_s)
                yield chunk

        return generator()


def scripted_provider() -> LogoxDevProvider:
    """构造体验模式用的 provider（装配根调用）。"""
    return LogoxDevProvider()
