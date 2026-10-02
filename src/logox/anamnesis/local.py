"""Local-only transport with no proxies, redirects, credentials or cloud fallback."""

from __future__ import annotations

import json
from urllib.parse import urlparse

from logox.anamnesis.runner import AnamesisRunner
from logox.providers.openai_compat import OpenAICompatProvider


def local_base_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("入梦模型必须是本机回环端点，拒绝远端服务")
    if (
        parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path.rstrip("/") not in {"", "/v1"}
    ):
        raise ValueError("入梦本地端点格式不合法")
    return url.rstrip("/").removesuffix("/v1")


async def make_local_runner(registry, config, collector, permission=None):
    if not config.model.strip():
        raise ValueError("未配置入梦模型：请设置 [anamnesis].model")
    if config.provider not in {"ollama", "lm-studio"}:
        raise ValueError("入梦只支持 Ollama／LM Studio 本地模型")
    spec = registry.spec(config.provider)
    if spec.kind != "openai_compat":
        raise ValueError("入梦端点必须使用本地 OpenAI 兼容协议")
    root = local_base_url(spec.base_url)
    import httpx

    window = (
        spec.window_for(config.model)
        if hasattr(spec, "window_for")
        else getattr(spec, "context_window", None)
    )
    if not isinstance(window, int) or window < 4096:
        raise ValueError(
            "未配置或未查到足够的静态入梦窗口（至少 4096）；请在 config.toml 的 [providers.<name>] 配置 context_window 或 model_windows"
        )
    adapter = OpenAICompatProvider(context_window=window, models=[config.model])

    async def chunks(request):
        payload = adapter.build_payload(request)
        try:
            async with (
                httpx.AsyncClient(
                    timeout=config.request_timeout_s, follow_redirects=False, trust_env=False
                ) as client,
                client.stream("POST", root + "/v1/chat/completions", json=payload) as response,
            ):
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if line.startswith("data:"):
                        body = line[5:].strip()
                        if body == "[DONE]":
                            runner.note_response_activity()
                            break
                        if body:
                            chunk = json.loads(body)
                            if isinstance(chunk, dict) and (
                                chunk.get("choices") or chunk.get("usage") or chunk.get("error")
                            ):
                                runner.note_response_activity()
                            yield chunk
        except httpx.TimeoutException as exc:
            raise TimeoutError(
                f"本地模型连接／传输连续 {config.request_timeout_s:g} 秒未响应（{type(exc).__name__}）；已保留过程，未提交档案"
            ) from exc

    adapter._raw_stream = chunks
    runner = AnamesisRunner(
        adapter, config.model, window, collector, timeout=config.request_timeout_s, permission=permission
    )
    return runner
