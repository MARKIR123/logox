"""OpenAI 兼容适配器（D9）。

覆盖面：OpenAI 官方、**DeepSeek**、**Ollama**、**LM Studio**、OpenRouter 等所有
提供 ``/chat/completions`` 兼容端点的服务。一个适配器打通绝大多数厂商，这正是选它的理由。

分三层，只有第一层需要 SDK
--------------------------
1. ``build_payload()`` —— 中立请求 → OpenAI 请求体（纯函数）
2. ``translate()``     —— 原始分片 → 统一事件（纯异步生成器）
3. ``stream()``        —— 编排 + 错误分类（唯一需要 SDK 的地方是 ``_sdk_chunks``）

上面两层**不依赖 openai SDK**，因此可以在离线环境穷尽测试——而它们正是最容易出错的部分。
"""

from __future__ import annotations

import asyncio
import json
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

__all__ = ["DEFAULT_BASE_URL", "OpenAICompatProvider", "usage_from_openai"]

DEFAULT_BASE_URL = "https://api.openai.com/v1"

#: 已知**支持**思考档位的模型前缀（会被映射为 ``reasoning_effort``）。
#:
#: ⚠️ 这份名单**直接决定 `/effort` 是否生效**（D49 的保守判定：只对已知前缀发参数）。
#: 因此厂商换代时**必须同步更新这里**，否则 `/effort` 会显示得好像能用、
#: 实际请求里一个参数都没发——静默失效，最难发现的一类问题。
#:
#: DeepSeek 的两个条目是 2026-09 按官方文档补的：现在的模型是
#: ``deepseek-flash`` / ``deepseek-v4-pro``，**两者默认就是思考模式**，
#: 且官方文档里明确给了 ``reasoning_effort`` 参数。旧的 ``deepseek-reasoner``
#: 前缀保留只是为了兼容还写着旧名的用户配置（官方说旧名仍被接受）。
_THINKING_MODELS = (
    "o1",
    "o3",
    "o4",
    "gpt-5",
    "deepseek-v4",
    "deepseek-flash",
    "deepseek-reasoner",
    "deepseek-r1",
    "qwq",
    "qwen3",
    "magistral",
)

#: OpenAI 请求体里**不接受**的字段（把它们列出来是为了显式剔除，而不是靠记忆）
_UNSUPPORTED_FIELDS = ("reasoning", "thinking")


def usage_from_openai(raw: Mapping[str, Any] | None) -> Usage | None:
    """把 OpenAI 兼容的 ``usage`` 归一化。

    **两个方向都严格遵守 D39**：

    * ``input`` / ``output`` 任一缺失 → 返回 ``None``（**不发 UsageEvent，不伪造 0**，E-6）
    * 缓存字段缺失 → ``cached_input_tokens=None``（**不是 0**，E-7）
    * 缓存字段显式为 0 → ``0``（与上一条语义完全不同）
    """
    if not raw:
        return None
    input_tokens = int_or_none(raw.get("prompt_tokens"))
    output_tokens = int_or_none(raw.get("completion_tokens"))
    if input_tokens is None or output_tokens is None:
        return None  # 少任何一项都不足以构成有意义的用量

    cached = int_or_none(_nested(raw, "prompt_tokens_details", "cached_tokens"))
    if cached is None:
        cached = int_or_none(raw.get("prompt_cache_hit_tokens"))  # DeepSeek 的字段名
    if cached is None:
        cached = int_or_none(raw.get("cache_hit_tokens"))
    if cached is None:
        cached = int_or_none(raw.get("cached_tokens"))
    reasoning = int_or_none(_nested(raw, "completion_tokens_details", "reasoning_tokens"))
    return Usage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_input_tokens=cached,
        reasoning_tokens=reasoning,
        # ★ 与 Anthropic **相反**：这里的 ``prompt_tokens`` **本身就是总量**
        #   （命中缓存的部分含在里面），所以直接照抄，不做任何加法。
        #   两个适配器各填各的，下游（如上下文压缩的触发判据）就不用猜口径了。
        context_tokens=input_tokens,
    )


def _nested(source: Mapping[str, Any] | None, *path: str) -> Any:
    current: Any = source
    for key in path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


class OpenAICompatProvider:
    """OpenAI 兼容端点的适配器。"""

    name = "openai_compat"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        raw_stream: RawStream | None = None,
        price_table: PriceTable | None = None,
        context_window: int | None = None,
        models: list[str] | None = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url or DEFAULT_BASE_URL
        #: 注入的原始分片源——测试用它喂夹具；为 ``None`` 时走 SDK
        self._raw_stream = raw_stream
        self.price_table = price_table
        self.context_window = context_window
        self._models = list(models or [])
        self._client: Any = None  # 懒建，避免仅导入就拉起 SDK

    # ------------------------------------------------------------------ #
    # 模型信息
    # ------------------------------------------------------------------ #

    def list_models(self) -> list[ModelInfo]:
        return [
            ModelInfo(
                id=model_id,
                provider=self.name,
                supports_thinking=self.supports_thinking(model_id),
                context_window=self.context_window,
                input_price_per_mtok=(price.input_per_mtok if (price := price_for(model_id, self.price_table)) else None),
                output_price_per_mtok=(price.output_per_mtok if price else None),
            )
            for model_id in self._models
        ]

    def set_models(self, models: list[str]) -> None:
        """替换本地模型列表（`/login` 抓到真实列表后调用）。

        存在的理由：``list_models()`` 的契约是**同步、只读本地、不发网络**
        （这样 `/model` 在离线时也打得开），因此"抓到的真实列表"必须由外部
        通过这个方法送进来，而不是让 ``list_models()`` 自己去请求。
        """
        self._models = list(models)

    def supports_thinking(self, model: str) -> bool:
        """模型是否支持思考档位。

        **保守判定**：只对已知支持的前缀返回 ``True``。理由见 ``MODULE_providers.md``
        的实现结果——若对未知模型也发送 ``reasoning_effort``，Ollama 这类严格端点会
        直接返回 400，**每个请求都失败比静默忽略一个可选参数糟糕得多**。
        """
        lowered = model.lower()
        return any(lowered.startswith(prefix) for prefix in _THINKING_MODELS)

    # ------------------------------------------------------------------ #
    # 请求构造（纯函数，可离线测试）
    # ------------------------------------------------------------------ #

    def build_payload(self, request: ChatRequest) -> dict[str, Any]:
        """中立请求 → OpenAI 请求体。"""
        messages: list[dict[str, Any]] = []
        if request.system:
            # OpenAI 把系统提示放在 messages[0]
            messages.append({"role": "system", "content": request.system})
        for message in request.messages:
            messages.extend(self._convert_message(message, model=request.model))

        # 协议校验与兜底（OpenAI Tool Role Invariant Guard）：
        # 严格遵守规范：每个 role="tool" 消息之前必须存在声明该 tool_call_id 的 assistant 消息
        sanitized_messages: list[dict[str, Any]] = []
        for entry in messages:
            if entry.get("role") == "tool":
                call_id = entry.get("tool_call_id")
                matched_assistant: dict[str, Any] | None = None
                if sanitized_messages and sanitized_messages[-1].get("role") == "assistant":
                    matched_assistant = sanitized_messages[-1]
                elif len(sanitized_messages) >= 2 and sanitized_messages[-1].get("role") == "tool":
                    for prev_entry in reversed(sanitized_messages):
                        if prev_entry.get("role") == "assistant":
                            matched_assistant = prev_entry
                            break
                        elif prev_entry.get("role") != "tool":
                            break

                if matched_assistant is not None:
                    tool_calls = matched_assistant.setdefault("tool_calls", [])
                    if not any(tc.get("id") == call_id for tc in tool_calls):
                        tool_calls.append({
                            "id": call_id,
                            "type": "function",
                            "function": {"name": "tool", "arguments": "{}"},
                        })
            sanitized_messages.append(entry)

        payload: dict[str, Any] = {
            "model": request.model,
            "messages": sanitized_messages,
            "stream": True,
            # 不加这一项，流式响应里**根本拿不到 usage**（D39 的度量全靠它）
            "stream_options": {"include_usage": True},
        }
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens
        if request.tools:
            payload["tools"] = [self._convert_tool(tool) for tool in request.tools if isinstance(tool, ToolSchema)]

        payload.update(self._thinking_payload(request))
        for field in _UNSUPPORTED_FIELDS:
            payload.pop(field, None)
        return payload

    @staticmethod
    def _convert_message(message: Message, *, model: str = "") -> list[dict[str, Any]]:
        """一条中立消息 → 一条或多条 OpenAI 消息。

        工具结果要拆成**独立的 ``role="tool"`` 消息**（OpenAI 的格式要求），
        因此返回值是列表而非单条。
        """
        if message.role == "tool":
            return [
                {
                    "role": "tool",
                    "tool_call_id": block.id,
                    "content": block.content,
                }
                for block in message.blocks_of(ToolResultBlock)
            ]

        results: list[dict[str, Any]] = []
        texts = [block.text for block in message.blocks_of(TextBlock) if block.text]
        tool_uses = message.blocks_of(ToolUseBlock)
        reasoning_blocks = list(message.blocks_of(ReasoningBlock))
        reasoning_text = "".join(b.text for b in reasoning_blocks) if reasoning_blocks else None
        is_deepseek = "deepseek" in (model or "").lower()

        if tool_uses:
            entry: dict[str, Any] = {
                "role": message.role,
                "content": "".join(texts) or None,
                "tool_calls": [
                    {
                        "id": block.id,
                        "type": "function",
                        "function": {
                            "name": block.name,
                            # OpenAI 要求 arguments 是 **JSON 字符串**
                            "arguments": json.dumps(block.input, ensure_ascii=False),
                        },
                    }
                    for block in tool_uses
                ],
            }
            if is_deepseek and reasoning_text:
                entry["reasoning_content"] = reasoning_text
            results.append(entry)
            return results

        if texts:
            entry = {"role": message.role, "content": "".join(texts)}
            if is_deepseek and message.role == "assistant" and reasoning_text:
                entry["reasoning_content"] = reasoning_text
            results.append(entry)
        elif message.role == "assistant":
            entry = {"role": "assistant", "content": ""}
            if is_deepseek and reasoning_text:
                entry["reasoning_content"] = reasoning_text
            results.append(entry)
        # 普通模型不传 reasoning_content，避免不兼容；DeepSeek 官方要求多轮 assistant 带回 reasoning_content 保证 KV 缓存命中与工具调用有效
        return results

    @staticmethod
    def _convert_tool(tool: ToolSchema) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            },
        }

    def _thinking_payload(self, request: ChatRequest) -> dict[str, Any]:
        """思考档位 → ``reasoning_effort``（D42）。``auto`` / 不支持 / ``None`` → 不传。"""
        config = request.thinking
        if config is None or not config.active:
            return {}
        if not self.supports_thinking(request.model):
            return {}  # E-13：忽略档位，不报错（警告由调用方记）
        return {"reasoning_effort": "minimal" if config.effort == "off" else config.effort}

    # ------------------------------------------------------------------ #
    # 分片翻译（纯异步生成器，可离线测试）
    # ------------------------------------------------------------------ #

    async def translate(self, chunks: RawChunks) -> AsyncIterator[ProviderEvent]:
        """原始分片 → 统一事件。

        装配规则：文本与推理**立即产出**；工具调用**只累积、结束时一次性产出**；
        用量与结束原因同样在结尾产出。
        """
        buffers: dict[int, ToolCallBuffer] = {}
        raw_usage: Mapping[str, Any] | None = None
        stop_raw: str | None = None
        model = ""

        async for chunk in chunks:
            if not isinstance(chunk, Mapping):
                continue
            if error := chunk.get("error"):
                yield self._error_from_body(error)
                return

            if chunk.get("model"):
                model = str(chunk["model"])
            if usage := chunk.get("usage"):
                raw_usage = usage  # type: ignore[assignment]

            for choice in chunk.get("choices") or []:
                if not isinstance(choice, Mapping):
                    continue
                delta = choice.get("delta") or {}

                # 推理内容（DeepSeek 用 reasoning_content）
                reasoning = delta.get("reasoning_content")
                if isinstance(reasoning, str) and reasoning:
                    yield DeltaEvent(kind="reasoning", text=reasoning)

                content = delta.get("content")
                if isinstance(content, str) and content:
                    yield DeltaEvent(kind="text", text=content)

                for call in delta.get("tool_calls") or []:
                    if not isinstance(call, Mapping):
                        continue
                    index = int_or_none(call.get("index")) or 0
                    buffer = buffers.setdefault(index, ToolCallBuffer(index))
                    function = call.get("function") or {}
                    buffer.merge(
                        call_id=call.get("id"),
                        name=function.get("name"),
                        fragment=function.get("arguments"),
                    )

                if choice.get("finish_reason"):
                    stop_raw = str(choice["finish_reason"])

        # ---- 流结束：先补上装配完成的工具调用，再报用量与结束原因 ----
        for index in sorted(buffers):
            finalized = buffers[index].finalize()
            if isinstance(finalized, ToolCallEvent):
                yield finalized
            else:
                base_msg = str(finalized.get("error", "工具调用装配失败"))
                is_truncated = stop_raw in ("length", "max_tokens")
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

        usage = usage_from_openai(raw_usage)
        if usage is not None:
            yield UsageEvent(usage=usage)

        yield StopEvent(stop_reason=normalize_stop_reason(stop_raw), model=model)

    # ------------------------------------------------------------------ #
    # 主入口
    # ------------------------------------------------------------------ #

    async def stream(self, request: ChatRequest) -> AsyncIterator[ProviderEvent]:
        """流式请求。**错误只分类，不重试**（重试属于内核，D22）。"""
        try:
            async for event in self.translate(self._chunks_for(request)):
                yield event
        except asyncio.CancelledError:
            # E-5：**必须原样传播**——吞掉它会让 Esc 中断静默失效
            raise
        except Exception as exc:  # noqa: BLE001 - 任何厂商异常都要变成分类事件
            yield self._error_event(exc)

    def _chunks_for(self, request: ChatRequest) -> RawChunks:
        if self._raw_stream is not None:
            return self._raw_stream(request)
        return self._sdk_chunks(request)

    async def _sdk_chunks(self, request: ChatRequest) -> RawChunks:  # type: ignore[override]
        """生产路径：用官方 SDK 流式请求，并把每个分片转成 dict。

        **延迟导入**：只有真正发请求时才 import openai，保证 ``--version`` 快速路径不受影响。
        """
        client = self._ensure_client()
        payload = self.build_payload(request)
        stream = await client.chat.completions.create(**payload)
        async for chunk in stream:
            yield chunk.model_dump()

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        if not self.api_key:
            raise ValueError("未配置 API Key（请在 config.toml 里设置 api_key_env 指向的环境变量）")
        from openai import AsyncOpenAI  # 延迟导入

        self._client = AsyncOpenAI(api_key=self.api_key, base_url=self.base_url)
        return self._client

    # ------------------------------------------------------------------ #
    # 错误分类
    # ------------------------------------------------------------------ #

    def _error_event(self, exc: BaseException) -> ProviderErrorEvent:
        category = classify_exception(exc)
        retry_after = _retry_after_from(exc)
        return ProviderErrorEvent(
            category=category,
            message=f"{type(exc).__name__}: {exc}",
            retry_after_s=retry_after,
            detail=_status_detail(exc),
        )

    def _error_from_body(self, error: Any) -> ProviderErrorEvent:
        """分片里内嵌的错误（部分兼容端点会这样做）。"""
        if isinstance(error, Mapping):
            message = str(error.get("message") or error.get("code") or "未知错误")
            kind = str(error.get("type") or error.get("code") or "")
            retry_after = error.get("retry_after")
        else:  # pragma: no cover - 防御性
            message, kind, retry_after = str(error), "", None
        hint = f" {kind}" if kind else ""
        return ProviderErrorEvent(
            category=classify_exception(RuntimeError(f"{message}{hint}")),
            message=message,
            retry_after_s=float(retry_after) if isinstance(retry_after, (int, float)) else None,
        )


# --------------------------------------------------------------------------- #
# 错误辅助
# --------------------------------------------------------------------------- #


def _retry_after_from(exc: BaseException) -> float | None:
    """从 ``Retry-After`` 头或错误体里取建议等待秒数；取不到返回 ``None``。

    **不猜**——猜出来的退避时间可能让内核等错时长。
    """
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers is not None:
        try:
            raw = headers.get("retry-after")
        except Exception:  # pragma: no cover - 防御性
            raw = None
        if raw is not None:
            try:
                return float(raw)
            except (TypeError, ValueError):
                return None
    for attr in ("retry_after", "retry_after_s"):
        value = getattr(exc, attr, None)
        if isinstance(value, (int, float)):
            return float(value)
    return None


def _status_detail(exc: BaseException) -> str | None:
    code = getattr(exc, "status_code", None)
    return f"HTTP {code}" if isinstance(code, int) else None
