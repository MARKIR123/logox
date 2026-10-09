"""Real SDK transport: local requests must bypass an environment proxy."""

from __future__ import annotations

import asyncio
import json
import os
import threading
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from logox.kernel.messages import user_message
from logox.providers.base import ChatRequest, DeltaEvent, ProviderErrorEvent, StopEvent, UsageEvent
from logox.providers.discovery import discover_models
from logox.providers.openai_compat import OpenAICompatProvider


@contextmanager
def endpoint(*, status: int = 200):
    requests: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def respond(self, body: bytes, content_type: str):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            requests.append(self.path)
            body = (
                json.dumps(
                    {
                        "object": "list",
                        "data": [
                            {"id": "local-test", "object": "model", "created": 0, "owned_by": "test"},
                        ],
                    }
                ).encode()
                if status == 200
                else b""
            )
            self.respond(body, "application/json")

        def do_POST(self):
            requests.append(self.path)
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            if status != 200:
                self.respond(b"", "text/plain")
                return
            common = {"id": "test", "object": "chat.completion.chunk", "created": 0, "model": "local-test"}
            chunks = [
                {
                    **common,
                    "choices": [{"index": 0, "delta": {"content": "LOCAL_OK"}, "finish_reason": None}],
                },
                {**common, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
                {
                    **common,
                    "choices": [],
                    "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
                },
            ]
            body = "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n"
            self.respond(body.encode(), "text/event-stream")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def proxy_environment(url: str) -> dict[str, str]:
    values = {
        key: url
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
    }
    values.update(NO_PROXY="", no_proxy="")
    return values


class LocalTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_discovery_and_stream_bypass_a_502_proxy(self):
        with (
            endpoint() as (local_url, local_requests),
            endpoint(status=502) as (proxy_url, proxy_requests),
            patch.dict(os.environ, proxy_environment(proxy_url)),
        ):
            provider = OpenAICompatProvider(api_key="not-needed", base_url=local_url + "/v1")
            try:
                provider._ensure_client().max_retries = 0
                result = await discover_models(provider, timeout_s=5)
                self.assertTrue(result.ok, result.error)
                self.assertEqual(result.models, ["local-test"])
                async with asyncio.timeout(5):
                    events = [
                        event
                        async for event in provider.stream(
                            ChatRequest(
                                model="local-test",
                                messages=[user_message("ping")],
                                max_tokens=16,
                            )
                        )
                    ]
                self.assertFalse([event for event in events if isinstance(event, ProviderErrorEvent)])
                self.assertEqual(
                    "".join(event.text for event in events if isinstance(event, DeltaEvent)), "LOCAL_OK"
                )
                self.assertTrue(any(isinstance(event, UsageEvent) for event in events))
                self.assertIsInstance(events[-1], StopEvent)
                self.assertEqual(events[-1].stop_reason, "end_turn")
                self.assertEqual(proxy_requests, [])
                self.assertEqual(local_requests, ["/v1/models", "/v1/chat/completions"])
            finally:
                await provider._client.close()

    async def test_remote_endpoint_retains_environment_proxy(self):
        with endpoint() as (proxy_url, proxy_requests), patch.dict(os.environ, proxy_environment(proxy_url)):
            provider = OpenAICompatProvider(api_key="test", base_url="http://remote.invalid/v1")
            try:
                result = await discover_models(provider, timeout_s=5)
                self.assertTrue(result.ok, result.error)
                self.assertEqual(proxy_requests, ["http://remote.invalid/v1/models"])
            finally:
                await provider._client.close()

    async def test_local_client_ignores_unavailable_socks_dependency(self):
        with patch.dict(os.environ, proxy_environment("socks5h://127.0.0.1:1")):
            provider = OpenAICompatProvider(api_key="not-needed", base_url="http://127.0.0.1:8080/v1")
            try:
                client = provider._ensure_client()
                self.assertIs(client, provider._ensure_client())
                self.assertFalse(client._client.trust_env)
            finally:
                if provider._client is not None:
                    await provider._client.close()

    def test_only_literal_loopback_hosts_disable_environment(self):
        local_urls = (
            "http://localhost:8080/v1",
            "http://LOCALHOST:8080/v1",
            "http://127.0.0.2:8080/v1",
            "http://[::1]:8080/v1",
        )
        remote_urls = (
            "https://api.openai.com/v1",
            "http://192.168.1.5:8080/v1",
            "http://localhost.example/v1",
            "http://127.0.0.1.example/v1",
        )
        with patch("openai.AsyncOpenAI") as sdk, patch("openai.DefaultAsyncHttpxClient") as transport:
            for url in (*local_urls, *remote_urls):
                with self.subTest(url=url):
                    sdk.reset_mock()
                    transport.reset_mock()
                    OpenAICompatProvider(api_key="test", base_url=url)._ensure_client()
                    if url in local_urls:
                        transport.assert_called_once_with(trust_env=False)
                        self.assertIs(sdk.call_args.kwargs["http_client"], transport.return_value)
                    else:
                        transport.assert_not_called()
                        self.assertNotIn("http_client", sdk.call_args.kwargs)
