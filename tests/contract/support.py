"""契约测试的公共脚手架（D45：标准库 unittest，全部离线）。

**绝不发起网络请求**：所有分片都来自 ``tests/fixtures/sse/*.jsonl``，
通过可注入的「原始分片源」接缝（``providers.base.RawStream``）喂给适配器。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any

from logox.providers.base import ChatRequest, ProviderEvent, RawChunks, RawStream

__all__ = [
    "FIXTURES",
    "collect",
    "drain",
    "load_chunks",
    "make_request",
    "raising_stream",
    "stream_of",
    "translate_events",
]

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "sse"


def load_chunks(name: str) -> list[dict[str, Any]]:
    """读取一个夹具文件（每行一个厂商分片）。"""
    path = FIXTURES / f"{name}.jsonl"
    if not path.is_file():
        raise FileNotFoundError(f"缺少夹具 {path}")
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def stream_of(chunks: list[Mapping[str, Any]]) -> RawStream:
    """把分片列表包成一个「原始分片源」。"""

    def factory(_request: ChatRequest) -> RawChunks:
        async def generator() -> AsyncIterator[Mapping[str, Any]]:
            for chunk in chunks:
                yield chunk

        return generator()

    return factory


def raising_stream(exc: BaseException) -> RawStream:
    """一个在中途抛异常的源（模拟网络中断 / HTTP 错误）。"""

    def factory(_request: ChatRequest) -> RawChunks:
        async def generator() -> AsyncIterator[Mapping[str, Any]]:
            yield {"choices": [{"index": 0, "delta": {"content": "部分输出"}, "finish_reason": None}]}
            raise exc

        return generator()

    return factory


def make_request(**overrides: Any) -> ChatRequest:
    payload: dict[str, Any] = {"model": "test-model", "system": "你是 Logox"}
    payload.update(overrides)
    return ChatRequest(**payload)


async def drain(agen: AsyncIterator[Any]) -> list[Any]:
    return [item async for item in agen]


def collect(agen: AsyncIterator[Any]) -> list[Any]:
    """同步地跑完一个异步生成器（unittest 里最省事的写法）。"""
    return asyncio.run(drain(agen))


def translate_events(provider: Any, chunks: list[Mapping[str, Any]], request: ChatRequest | None = None) -> list[ProviderEvent]:
    """把夹具喂给适配器的 ``translate()``，取回全部统一事件。

    ``translate()`` 不碰 SDK、不发网络请求，因此这条路可以完全离线跑通。
    """
    payload = request if request is not None else make_request(model="test-model")
    return collect(provider.translate(stream_of(chunks)(payload)))


def stream_events(provider: Any, request: ChatRequest) -> list[ProviderEvent]:
    """跑适配器的 ``stream()``（含错误分类编排）。"""
    return collect(provider.stream(request))
