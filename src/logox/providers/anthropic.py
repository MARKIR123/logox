"""Anthropic 适配器（D9）。

与 OpenAI 兼容端点的实质差异（都不是"改个字段名"那么简单）
----------------------------------------------------------
| 维度 | OpenAI 兼容 | Anthropic |
|---|---|---|
| 系统提示 | ``messages[0]`` | **顶层 ``system`` 参数** |
| 工具定义 | ``function.parameters`` | **``input_schema``** |
| 工具结果 | ``role="tool"`` 独立消息 | **包在 ``user`` 消息的 ``tool_result`` 块里** |
| 推理内容 | 只出不进 | **``thinking`` 块带 ``signature``，必须原样回传** |
| ``max_tokens`` | 可选 | **必填** |
| ``temperature`` | 0–2 | **0–1** |
| 用量 | 一个 ``usage`` 对象 | **input 在 ``message_start``、output 在 ``message_delta``** |

因此必须有独立适配器，而不是给 OpenAI 那个加几个 if。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from typing import Any

from logox.errors import ErrorCategory
from logox.kernel.errors import classify_exception
from logox.kernel.events import Usage
from logox.kernel.messages import (
    Message,
    ReasoningBlock,
    TextBlock,
    ToolResultBlock,
    ToolSchema,
    ToolUseBlock,
)
from logox.providers.base import (
    ChatRequest,
    DeltaEvent,
    ModelInfo,
    ProviderErrorEvent,
    ProviderEvent,
    RawChunks,
    RawStream,
    StopEvent,
    ToolCallBuffer,
    ToolCallEvent,
    UsageEvent,
    int_or_none,
    normalize_stop_reason,
)
from logox.providers.pricing import PriceTable, price_for

__all__ = ["AnthropicProvider", "THINKING_BUDGETS", "usage_from_anthropic"]

DEFAULT_BASE_URL = "https://api.anthropic.com"

#: Anthropic 的 ``max_tokens`` 是必填项，缺省时用这个值
DEFAULT_MAX_TOKENS = 8192

#: 思考档位 → ``thinking.budget_tokens``（D42）。取值需有区分度且不宜过小。
THINKING_BUDGETS: dict[str, int] = {"low": 1_024, "medium": 4_096, "high": 16_384}

#: ``budget_tokens`` 必须严格小于 ``max_tokens``，留出的余量（思考之后还要产出正文）
BUDGET_HEADROOM = 1_024

#: Anthropic 的 temperature 取值范围（与 OpenAI 的 0–2 不同）
TEMPERATURE_RANGE = (0.0, 1.0)


def usage_from_anthropic(
    start_usage: Mapping[str, Any] | None, delta_usage: Mapping[str, Any] | None
) -> Usage | None:
    """把分散在两处的用量合并归一化。

    Anthropic 在流式下把 input 放在 ``message_start``、output 放在 ``message_delta``，
    因此必须两处都看。缺任一必需项 → ``None``（**不伪造 0**，E-6）。

    ⚠️ **三个 input 字段的关系**（这是本适配器独有的口径，下游不该去猜）：
    ``input_tokens`` 是**未命中缓存**的那部分，而 ``cache_creation_input_tokens``
    （写入缓存）与 ``cache_read_input_tokens``（读缓存）是**并列**的另外两块 ——
    **总量 = 三者之和**。OpenAI 兼容端点恰好相反（``prompt_tokens`` 本身就是总量）。
    所以 ``context_tokens`` 必须在这里算好：**只有适配器知道厂商的口径**（D9）。
    """
    plain_input = int_or_none((start_usage or {}).get("input_tokens"))
    cache_read = int_or_none((start_usage or {}).get("cache_read_input_tokens"))
    cache_write = int_or_none((start_usage or {}).get("cache_creation_input_tokens"))
    cached = cache_read if cache_read is not None else cache_write
    output_tokens = int_or_none((delta_usage or {}).get("output_tokens"))
    if output_tokens is None:
        output_tokens = int_or_none((start_usage or {}).get("output_tokens"))
    if plain_input is None or output_tokens is None:
        return None
    return Usage(
        input_tokens=plain_input,
        output_tokens=output_tokens,
        cached_input_tokens=cached,
        context_tokens=plain_input + (cache_read or 0) + (cache_write or 0),
    )


class AnthropicProvider:
    """Anthropic Messages API 的适配器。"""

    name = "anthropic"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        raw_stream: RawStream | None = None,
        price_table: PriceTable | None = None,
        context_window: int | None = None,
        model_windows: dict[str, int] | None = None,
        models: list[str] | None = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url or DEFAULT_BASE_URL
        self._raw_stream = raw_stream
        self.price_table = price_table
        self.context_window = context_window
        self.model_windows: dict[str, int] = dict(model_windows or {})
        self._models = list(models or [])
        self._client: Any = None

    # ------------------------------------------------------------------ #
    # 模型信息
    # ------------------------------------------------------------------ #

    def window_for(self, model_id: str) -> int | None:
        """查指定模型的上下文窗口。按精确名 -> :latest 规范化 -> context_window 兜底。"""
        if model_id in self.model_windows:
            return self.model_windows[model_id]
        clean_id = model_id.removesuffix(":latest")
        if clean_id in self.model_windows:
            return self.model_windows[clean_id]
        tagged_id = f"{clean_id}:latest"
        if tagged_id in self.model_windows:
            return self.model_windows[tagged_id]
        return self.context_window

    def list_models(self) -> list[ModelInfo]:
        return [
            ModelInfo(
                id=model_id,
                provider=self.name,
                # Claude 3.7 起支持扩展思考；保守起见按"支持"处理，由厂商决定是否拒绝
                supports_thinking=self.supports_thinking(model_id),
                context_window=self.window_for(model_id),
                input_price_per_mtok=(price.input_per_mtok if (price := price_for(model_id, self.price_table)) else None),
                output_price_per_mtok=(price.output_per_mtok if price else None),
            )
            for model_id in self._models
        ]

    def set_models(self, models: list[str]) -> None:
        """替换本地模型列表（`/login` 抓到真实列表后调用）。

        与 ``OpenAICompatProvider.set_models`` 同名同义：`list_models()` 的契约是
        **同步、只读本地、不发网络**，因此抓到的列表必须由外部送进来。
        """
        self._models = list(models)

    @staticmethod
    def supports_thinking(model: str) -> bool:
        """Claude 的扩展思考从 3.7 / 4 代开始支持。"""
        lowered = model.lower()
        return any(token in lowered for token in ("claude-3-7", "claude-3.7", "claude-4", "claude-opus", "claude-sonnet"))

    # ------------------------------------------------------------------ #
    # 请求构造（纯函数，可离线测试）
    # ------------------------------------------------------------------ #

    def build_payload(self, request: ChatRequest) -> dict[str, Any]:
        """中立请求 → Anthropic 请求体。"""
        # ★ 角色归一化（D7）：把「中立模型允许、Anthropic 不允许」的两种形状在本层消化。
        #   顺序有讲究：先搬走 system（它不该在 messages 里），
        #   再合并相邻同角色（搬走 system 后，它左右两边可能变成相邻同角色）。
        system_text, converted = self._hoist_system_messages(
            [self._convert_message(message) for message in request.messages],
            request.system,
        )
        payload: dict[str, Any] = {
            "model": request.model,
            "max_tokens": request.max_tokens or DEFAULT_MAX_TOKENS,  # Anthropic 必填
            "messages": self._merge_adjacent_same_role(converted),
            "stream": True,
        }
        if system_text:
            payload["system"] = system_text  # 顶层参数，不是消息

        if request.tools:
            payload["tools"] = [
                self._convert_tool(tool) for tool in request.tools if isinstance(tool, ToolSchema)
            ]

        if request.temperature is not None:
            low, high = TEMPERATURE_RANGE
            if low <= request.temperature <= high:
                payload["temperature"] = request.temperature
            # 超范围就**不传**（Anthropic 默认 1.0，等同于取上界），而不是发出去换一个 400

        thinking = self._thinking_payload(request)
        if thinking is not None:
            payload["thinking"] = thinking
            # E-14：budget 必须**严格小于** max_tokens（Anthropic 直接 400）。
            # 预算按档位如实下发，不够就上调 max_tokens——见 _thinking_payload 的说明。
            payload["max_tokens"] = max(payload["max_tokens"], thinking["budget_tokens"] + BUDGET_HEADROOM)
        return payload

    @staticmethod
    def _hoist_system_messages(
        messages: list[dict[str, Any]], top_level_system: str
    ) -> tuple[str, list[dict[str, Any]]]:
        """把消息序列里 ``role="system"`` 的内容**并入顶层 ``system`` 参数**。

        为什么必须做：``Message.role`` 的 Literal 里声明了 ``"system"``，
        而 Anthropic 的 ``messages`` 只接受 ``user`` / ``assistant``。
        **契约说允许、适配器不兜住，就是将一个非法载荷发给厂商换回 400。**

        为什么选择“翻译”而不是“报错”：内容仍然到达模型，只是被搬到它唯一能待的地方。
        代价是**位置信息丢失**（顶层 ``system`` 没有位置概念）——
        这是 Anthropic 协议本身的限制，不是这里的取舍。
        """
        parts: list[str] = [top_level_system] if top_level_system else []
        kept: list[dict[str, Any]] = []
        for message in messages:
            if message.get("role") != "system":
                kept.append(message)
                continue
            content = message.get("content")
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
                    parts.append(str(block["text"]))
        return "\n\n".join(parts), kept

    @staticmethod
    def _merge_adjacent_same_role(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """把相邻的同角色消息合并成一条。

        Anthropic 要求 ``user`` / ``assistant`` **严格交替**；而中立模型允许相邻同角色：
        压缩归档摘要后紧跟的提问、一批并发的 ``tool_result``
        （中立的 ``tool`` 消息映射后也是 ``user``）、思考块被剥离后的空 assistant……

        合并是**线级归一化**：不改中立模型，也不影响其它适配器
        （OpenAI 完全允许相邻同角色）。判定依据是**映射后**的角色，不是中立角色。
        """

        def blocks_of(content: Any) -> list[dict[str, Any]]:
            if isinstance(content, list):
                return list(content)
            return [{"type": "text", "text": content or ""}]

        merged: list[dict[str, Any]] = []
        for message in messages:
            if merged and merged[-1].get("role") == message.get("role"):
                combined = blocks_of(merged[-1].get("content")) + blocks_of(message.get("content"))
                # ``tool_result`` 块必须排在最前：这是 Anthropic 对“响应 tool_use 的那条消息”的格式要求
                is_result = lambda b: isinstance(b, dict) and b.get("type") == "tool_result"  # noqa: E731
                tool_results = [b for b in combined if is_result(b)]
                others = [b for b in combined if not is_result(b)]
                merged[-1] = {**merged[-1], "content": [*tool_results, *others]}
            else:
                merged.append(dict(message))
        return merged

    @staticmethod
    def _convert_tool(tool: ToolSchema) -> dict[str, Any]:
        return {
            "name": tool.name,
            "description": tool.description,
            # 注意键名是 input_schema，不是 parameters
            "input_schema": tool.parameters,
        }

    @classmethod
    def _convert_message(cls, message: Message) -> dict[str, Any]:
        """一条中立消息 → 一条 Anthropic 消息。

        **工具结果要放进 ``user`` 消息的 ``tool_result`` 块**（Anthropic 没有
        ``tool`` 角色），且必须排在 content 最前面——它要紧跟上一轮 assistant 的
        ``tool_use``。
        """
        results = message.blocks_of(ToolResultBlock)
        if results:
            # Anthropic 没有 ``tool`` 角色：工具结果必须包在 ``user`` 消息里，
            # 且 ``tool_result`` 块要排在 content 最前面。
            content: list[dict[str, Any]] = [
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": block.content,
                    # 失败结果显式标记，模型才能据此自愈（D22 的 feedable_to_model）
                    **({} if block.ok else {"is_error": True}),
                }
                for block in results
            ]
            # 同一条消息里若还带着文本，**跟着 tool_result 保留**而不是丢掉：
            # 静默丢内容比多一个文本块危险得多。
            content.extend({"type": "text", "text": block.text} for block in message.blocks_of(TextBlock) if block.text)
            return {"role": "user", "content": content}

        content = []  # 非工具结果路径：普通的 text / thinking / tool_use 混排
        for block in message.blocks:
            if isinstance(block, TextBlock) and block.text:
                content.append({"type": "text", "text": block.text})
            elif isinstance(block, ReasoningBlock) and block.text:
                # E-12：缺 signature 的 thinking 块发出去会被拒，宁可丢掉并让调用方记 warning
                if block.signature:
                    content.append(
                        {
                            "type": "thinking",
                            "thinking": block.text,
                            "signature": block.signature,
                        }
                    )
            elif isinstance(block, ToolUseBlock):
                content.append(
                    {
                        "type": "tool_use",
                        "id": block.id,
                        "name": block.name,
                        "input": block.input,
                    }
                )
        if not content and message.role == "assistant":
            content.append({"type": "text", "text": ""})
        return {"role": message.role, "content": content}

    @classmethod
    def _thinking_payload(cls, request: ChatRequest) -> dict[str, Any] | None:
        """思考档位 → ``thinking`` 参数（D42）。``auto`` / ``off`` / 不支持 → ``None``。

        **不在这里压缩预算。** 早先的写法把预算压到 ``max(DEFAULT_MAX_TOKENS, max_tokens-1)``
        以下，结果 ``high``（16384）在默认 ``max_tokens`` 下被悄悄砍成 8192——用户明确
        点了"高强度思考"却拿到中档，而且**毫无提示**。现在改为：预算按档位如实下发，
        ``max_tokens`` 不够就在 :meth:`build_payload` 里上调（E-14）。
        宁可请求体里多一个大一点的 ``max_tokens``，也不要静默降级用户的显式选择。
        """
        config = request.thinking
        if config is None or not config.active or config.effort == "off":
            return None
        if not cls.supports_thinking(request.model):
            return None  # E-13：忽略档位，不报错
        budget = THINKING_BUDGETS.get(config.effort)
        if budget is None:  # pragma: no cover - 档位已被 Literal 收窄
            return None
        return {"type": "enabled", "budget_tokens": budget}

    # ------------------------------------------------------------------ #
    # 分片翻译（纯异步生成器，可离线测试）
    # ------------------------------------------------------------------ #

    async def translate(self, chunks: RawChunks) -> AsyncIterator[ProviderEvent]:
        """Anthropic 事件流 → 统一事件。

        思考签名（E-12）怎么走
        ----------------------
        ``signature_delta`` 在思考文本**之后**才下发，所以无法把它附在已有的正文增量上。
        这里的做法是：思考增量照常即时产出（保证首字延迟与流式观感），等块结束时
        再补一条**只带签名、不带文本**的 ``DeltaEvent``。内核据此把签名设到紧邻它
        上文那个 ``ReasoningBlock`` 上，下一轮原样回传即可。
        """
        buffers: dict[int, ToolCallBuffer] = {}
        signatures: dict[int, str] = {}
        start_usage: Mapping[str, Any] | None = None
        delta_usage: Mapping[str, Any] | None = None
        stop_raw: str | None = None
        model = ""

        async for chunk in chunks:
            if not isinstance(chunk, Mapping):
                continue
            kind = chunk.get("type")

            if kind == "error":
                yield self._error_from_body(chunk.get("error"))
                return

            if kind == "message_start":
                message = chunk.get("message") or {}
                model = str(message.get("model") or "")
                start_usage = message.get("usage") or {}
                continue

            if kind == "content_block_start":
                index = int_or_none(chunk.get("index")) or 0
                block = chunk.get("content_block") or {}
                if block.get("type") == "tool_use":
                    buffers[index] = ToolCallBuffer(
                        index, call_id=str(block.get("id") or ""), name=str(block.get("name") or "")
                    )
                continue

            if kind == "content_block_stop":
                index = int_or_none(chunk.get("index")) or 0
                signature = signatures.pop(index, None)
                if signature:
                    # 只带签名的增量：文本为空，内核应把它并入**上一个**推理块
                    yield DeltaEvent(kind="reasoning", text="", vendor_data={"signature": signature})
                continue

            if kind == "content_block_delta":
                index = int_or_none(chunk.get("index")) or 0
                delta = chunk.get("delta") or {}
                delta_type = delta.get("type")

                if delta_type == "text_delta":
                    text = delta.get("text")
                    if isinstance(text, str) and text:
                        yield DeltaEvent(kind="text", text=text)
                elif delta_type == "thinking_delta":
                    thinking = delta.get("thinking")
                    if isinstance(thinking, str) and thinking:
                        yield DeltaEvent(kind="reasoning", text=thinking)
                elif delta_type == "signature_delta":
                    signature = delta.get("signature")
                    if isinstance(signature, str) and signature:
                        # 可能分多次下发，必须拼接
                        signatures[index] = signatures.get(index, "") + signature
                elif delta_type == "input_json_delta":
                    fragment = delta.get("partial_json")
                    if isinstance(fragment, str) and fragment and index in buffers:
                        buffers[index].merge(fragment=fragment)
                continue

            if kind == "message_delta":
                inner = chunk.get("delta") or {}
                if inner.get("stop_reason"):
                    stop_raw = str(inner["stop_reason"])
                if chunk.get("usage"):
                    delta_usage = chunk["usage"]
                continue

        # ---- 流结束 ----
        for index in sorted(buffers):
            finalized = buffers[index].finalize()
            if isinstance(finalized, ToolCallEvent):
                yield finalized
            else:
                # ★ D160：与 `openai_compat` **对齐**，打上 `is_truncated` 标记。
                #
                # 为什么必须打：`kernel/loop.py:607` 的 D121 自愈闭环**只认这一个标记** ——
                #     if failure.is_truncated and truncation_healing_count < max_truncation_healings:
                # 而此前**只有 OpenAI 那条路打了它**（`openai_compat.py:388`），
                # 于是 Anthropic 用户撞到"参数被输出上限截断"时，
                # 得到的仍是那个**无可挽回的闪退**，D121 承诺的自愈一次都不会触发。
                #
                # 判据：Anthropic 用 `max_tokens` 表示"被输出上限截断"（对应 OpenAI 的 `length`）。
                # 与 openai 侧同样，**只改消息与标记，不改 category**：
                # `BAD_REQUEST` 的 `feedable_to_model=True`，所以它本来就会回灌给模型，
                # 只是少了"这是截断、请分批"这条关键提示。
                base_msg = str(finalized.get("error", "工具调用装配失败"))
                is_truncated = stop_raw in ("max_tokens", "length")
                if is_truncated:
                    msg = f"{base_msg}（输出已达到 Token 上限并被截断，请简化操作或分批写入）"
                else:
                    msg = base_msg
                yield ProviderErrorEvent(
                    category=ErrorCategory.BAD_REQUEST,
                    message=msg,
                    detail=str(finalized.get("detail", "")) or None,
                    is_truncated=is_truncated,
                )

        usage = usage_from_anthropic(start_usage, delta_usage)
        if usage is not None:
            yield UsageEvent(usage=usage)

        yield StopEvent(stop_reason=normalize_stop_reason(stop_raw), model=model)

    # ------------------------------------------------------------------ #
    # 主入口
    # ------------------------------------------------------------------ #

    async def stream(self, request: ChatRequest) -> AsyncIterator[ProviderEvent]:
        """流式请求。**错误只分类，不重试**（D22）。"""
        try:
            async for event in self.translate(self._chunks_for(request)):
                yield event
        except asyncio.CancelledError:
            raise  # E-5：原样传播
        except Exception as exc:  # noqa: BLE001
            yield self._error_event(exc)

    def _chunks_for(self, request: ChatRequest) -> RawChunks:
        if self._raw_stream is not None:
            return self._raw_stream(request)
        return self._sdk_chunks(request)

    async def _sdk_chunks(self, request: ChatRequest) -> RawChunks:  # type: ignore[override]
        """生产路径：官方 SDK 流式请求，逐事件转成 dict。**延迟导入。**"""
        client = self._ensure_client()
        payload = self.build_payload(request)
        stream = await client.messages.create(**payload)
        async for event in stream:
            yield event.model_dump()

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        if not self.api_key:
            raise ValueError("未配置 API Key（请在 config.toml 里设置 api_key_env 指向的环境变量）")
        from anthropic import AsyncAnthropic  # 延迟导入

        self._client = AsyncAnthropic(api_key=self.api_key, base_url=self.base_url)
        return self._client

    # ------------------------------------------------------------------ #
    # 错误分类
    # ------------------------------------------------------------------ #

    def _error_event(self, exc: BaseException) -> ProviderErrorEvent:
        from logox.providers.openai_compat import _retry_after_from, _status_detail

        return ProviderErrorEvent(
            category=classify_exception(exc),
            message=f"{type(exc).__name__}: {exc}",
            retry_after_s=_retry_after_from(exc),
            detail=_status_detail(exc),
        )

    def _error_from_body(self, error: Any) -> ProviderErrorEvent:
        if isinstance(error, Mapping):
            message = str(error.get("message") or "未知错误")
            kind = str(error.get("type") or "")
        else:  # pragma: no cover - 防御性
            message, kind = str(error), ""
        return ProviderErrorEvent(
            category=classify_exception(RuntimeError(f"{message} {kind}")),
            message=message,
        )
