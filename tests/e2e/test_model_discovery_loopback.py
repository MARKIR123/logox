"""对**真实 HTTP 端点**验证模型抓取（D65）——只打本机回环，不打任何外部服务。

为什么要这一层
==============

其它用例都把 SDK 客户端换成了替身，因此它们证明的是"**给定一页结果，我能正确解析**"。
但真正的风险在另一处：**我有没有问对地方、取对形状**。
这不是假想——它**真的发生了**：官方 SDK 的 `AsyncModels.list()` 不是协程，
而是返回一个 `AsyncPaginator`（异步可迭代、顶层没有 `.data`），
于是最初的实现把"不是协程"当成"已经是一页"，去读 `.data` 拿到空列表，
把一个**完全正常的端点报成"没有模型"**。所有替身用例都是绿的。

因此这里起一个**真的 HTTP 服务器**（`http.server` 绑在 `127.0.0.1` 的随机端口上），
让官方 SDK 真的去请求它。

**为什么这个文件在 `tests/e2e/` 而不是 `tests/contract/`**
----------------------------------------------------------

`tests/contract/` 有一条**离线红线**：`test_adapter_hygiene.py::test_t05` 用 AST 断言
该目录下**不得 import 任何网络模块**（含 `http` / `socket`）。那条规矩是对的——
契约测试必须能在完全离线的环境里跑。

而本文件的目的是"真的走一次 HTTP"，因此它**主动**违反那条规矩，于是放在
`tests/e2e/`（那里本来就是"真文件 + 真工具 + 真内核"的端到端层）。
**它只连 `127.0.0.1`，不会碰任何外部服务**——下面有一条断言钉住这一点。
"""

from __future__ import annotations

import asyncio
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from logox.providers.discovery import discover_models
from logox.providers.openai_compat import OpenAICompatProvider

MODELS = ["live-model-a", "live-model-b", "live-model-c"]


class _Handler(BaseHTTPRequestHandler):
    """只认 ``GET /v1/models``；其它路径一律 404（**这正是我们要验证的**）。"""

    requests: list[tuple[str, str]] = []

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 的约定
        type(self).requests.append(("GET", self.path))
        if self.path.rstrip("/").endswith("/models"):
            body = json.dumps(
                {
                    "object": "list",
                    "data": [{"id": model, "object": "model", "created": 1, "owned_by": "test"} for model in MODELS],
                }
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"error": {"message": "not found"}}')

    def log_message(self, *_args: object) -> None:
        """静音：测试输出不该被 HTTP 日志淹没。"""


class RealEndpointTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        _Handler.requests = []
        cls.server = HTTPServer(("127.0.0.1", 0), _Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def test_only_loopback_is_ever_used(self) -> None:
        """★ 这个文件**只许连 127.0.0.1**（它故意绕过了 contract/ 的离线红线）。

        因为它主动 import 了 `http.server`（`tests/contract/` 明令禁止的模块），
        所以必须自己把边界钉死——否则将来有人把 base_url 改成真厂商地址，
        整套测试就会在别人的机器上**偷偷发真实请求**，而契约测试的离线保证
        也就名存实亡了。

        做法：**不看遍所有字符串，只看两处真正的目标**——`HTTPServer(...)` 绑定到哪、
        `self._provider("http://…")` 请求的是谁。这既是最小的必要检查，
        也天然避开了"扫到自己"（前三版分别被匹配模式、文档举例、文档字符串坑过）。
        """
        import ast

        tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))

        def literal(node: ast.AST) -> str | None:
            """取``ast.Constant`` 字符串；对 f-string 取它的字面前缀部分。"""
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                return node.value
            if isinstance(node, ast.JoinedStr) and node.values:
                head = node.values[0]
                if isinstance(head, ast.Constant) and isinstance(head.value, str):
                    return head.value
            return None

        bound: list[str] = []
        requested: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name == "HTTPServer" and node.args:
                first = node.args[0]
                if isinstance(first, ast.Tuple) and first.elts:
                    text = literal(first.elts[0])
                    if text:
                        bound.append(text)
            elif name == "_provider" and node.args:
                text = literal(node.args[0])
                if text and text.startswith("http://"):
                    requested.append(text.removeprefix("http://"))

        self.assertTrue(bound, "应当能读到服务器绑定的地址，否则这条断言形同虚设")
        self.assertTrue(requested, "应当能读到请求的目标地址，否则这条断言形同虚设")
        for host in bound:
            with self.subTest(bound=host):
                self.assertIn(host, ("127.0.0.1", "localhost"), f"只允许绑定回环地址，实际 {host}")
        for target in requested:
            with self.subTest(url=target):
                self.assertTrue(
                    target.startswith(("127.0.0.1:", "localhost:")),
                    f"只允许请求回环地址，实际 {target}",
                )

    def _provider(self, base_url: str) -> OpenAICompatProvider:
        return OpenAICompatProvider(api_key="sk-test-not-real", base_url=base_url)

    async def test_sdk_really_hits_the_models_endpoint(self) -> None:
        """★ 让官方 SDK 真的发一次请求，验证它打的是 ``{base_url}/models``。"""
        _Handler.requests = []
        provider = self._provider(f"http://127.0.0.1:{self.port}/v1")
        result = await discover_models(provider, timeout_s=10)

        self.assertTrue(result.ok, f"应当抓到模型，实际：{result.error}")
        self.assertEqual(result.models, MODELS)
        self.assertTrue(
            any(path.rstrip("/").endswith("/models") for _method, path in _Handler.requests),
            f"SDK 应当请求 /models，实际请求了：{_Handler.requests}",
        )

    async def test_base_url_without_v1_still_finds_the_endpoint(self) -> None:
        """`base_url` 少写 `/v1` 时的行为——**记录下来**，免得以后有人以为它坏了。

        官方 SDK 是拿 `base_url` 直接拼 `/models` 的，因此 `http://host` 会打到
        `http://host/models`。我们的测试服务器对**任何**以 `/models` 结尾的路径都应答
        （真实厂商不一定这么宽容），这条用例的目的是说明：
        **抓取成功与否取决于用户配的 base_url 是否正确**，而不是我们的解析有问题。
        """
        _Handler.requests = []
        provider = self._provider(f"http://127.0.0.1:{self.port}")
        result = await discover_models(provider, timeout_s=10)
        self.assertTrue(result.ok)
        self.assertTrue(
            any(path.rstrip("/").endswith("/models") for _method, path in _Handler.requests),
            "无论有没有 /v1，末段都应当是 /models",
        )

    async def test_404_is_reported_as_a_failure_not_a_crash(self) -> None:
        """端点不支持 `/models`（部分兼容实现）→ 报告失败，回退预设表。"""

        class _NotFound(_Handler):
            def do_GET(self) -> None:  # noqa: N802
                self.send_response(404)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error": {"message": "no such route"}}')

        server = HTTPServer(("127.0.0.1", 0), _NotFound)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            provider = self._provider(f"http://127.0.0.1:{server.server_address[1]}/v1")
            result = await discover_models(provider, timeout_s=10)
            self.assertFalse(result.ok)
            self.assertTrue(result.attempted)
            self.assertTrue(result.error, "必须给出一个原因")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    async def test_bad_key_surfaces_as_a_failure(self) -> None:
        """401 → 报告失败（并让用户看出是鉴权问题），而不是抛异常。"""

        class _Unauthorized(_Handler):
            def do_GET(self) -> None:  # noqa: N802
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error": {"message": "Invalid API key provided"}}')

        server = HTTPServer(("127.0.0.1", 0), _Unauthorized)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            provider = self._provider(f"http://127.0.0.1:{server.server_address[1]}/v1")
            result = await discover_models(provider, timeout_s=10)
            self.assertFalse(result.ok)
            self.assertIn("Invalid API key", result.error)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    async def test_cancel_during_a_real_request_propagates(self) -> None:
        """真实请求中途取消：`CancelledError` 必须原样传播（E-5 同一条规矩）。"""
        provider = self._provider(f"http://127.0.0.1:{self.port}/v1")
        task = asyncio.ensure_future(discover_models(provider, timeout_s=10))
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task


if __name__ == "__main__":
    unittest.main()
