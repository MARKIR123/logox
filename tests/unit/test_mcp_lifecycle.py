"""MCP 客户端生命周期与 Mock Stdio 服务集成测试（M9 / D106 / D108）。

通过本地轻量 Python Mock Server 模拟标准 JSON-RPC 2.0 协议交互，
100% 离线，零网络，无外部 npm 依赖。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

from logox.config.schema import McpServerConfig
from logox.errors import ErrorCategory
from logox.kernel.bus import EventBus
from logox.mcp.client import McpClient
from logox.mcp.models import McpConnectionState

MOCK_SERVER_SCRIPT = Path(__file__).parent / "mock_mcp_server.py"


class McpLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.bus = EventBus(session_id="test-mcp")
        self.config = McpServerConfig(
            name="mock",
            command=sys.executable,
            args=[str(MOCK_SERVER_SCRIPT)],
            timeout=5.0,
            startup_timeout_s=5.0,
        )
        self.client = McpClient(name="mock", config=self.config, bus=self.bus)

    async def asyncTearDown(self) -> None:
        await self.client.close()

    async def test_lazy_connection_initial_state(self) -> None:
        # 初始未建立连接（惰性按需拉起）
        self.assertEqual(self.client.state, McpConnectionState.STOPPED)
        self.assertEqual(self.client.tool_count, 0)

    async def test_handshake_and_list_tools(self) -> None:
        tools = await self.client.list_tools()
        self.assertEqual(self.client.state, McpConnectionState.CONNECTED)
        self.assertGreaterEqual(len(tools), 4)

        names = [getattr(t, "name", t.get("name") if isinstance(t, dict) else "") for t in tools]
        self.assertIn("echo", names)
        self.assertIn("sleep_tool", names)
        self.assertIn("fail_tool", names)
        self.assertIn("crash_tool", names)

    async def test_call_tool_success(self) -> None:
        res = await self.client.call_tool("echo", {"text": "hello mcp"})
        self.assertTrue(res.ok)
        self.assertEqual(res.content, "echo: hello mcp")

    async def test_call_tool_reported_error(self) -> None:
        res = await self.client.call_tool("fail_tool", {})
        self.assertFalse(res.ok)
        self.assertIsNotNone(res.error)
        self.assertEqual(res.error.category, ErrorCategory.TOOL_FAILURE)
        self.assertIn("Something went wrong", res.content)

    async def test_call_tool_timeout(self) -> None:
        # 工具休眠 3 秒，单次超时设为 0.5 秒
        res = await self.client.call_tool("sleep_tool", {"seconds": 3.0}, timeout=0.5)
        self.assertFalse(res.ok)
        self.assertIsNotNone(res.error)
        self.assertEqual(res.error.category, ErrorCategory.TOOL_FAILURE)
        self.assertIn("超时", res.error.message)

    async def test_server_crash_fail_safe_and_auto_reconnect(self) -> None:
        # crash_tool 会在子进程中 sys.exit(1)
        res = await self.client.call_tool("crash_tool", {})
        self.assertFalse(res.ok)
        self.assertEqual(self.client.state, McpConnectionState.OFFLINE)
        self.assertIsNotNone(res.error)
        self.assertEqual(res.error.category, ErrorCategory.TOOL_FAILURE)

        # 验证再次调用时能够自动拉起新进程并自愈成功
        res2 = await self.client.call_tool("echo", {"text": "after crash"})
        self.assertTrue(res2.ok)
        self.assertEqual(res2.content, "echo: after crash")
        self.assertEqual(self.client.state, McpConnectionState.CONNECTED)

    async def test_invalid_command_offline_fail_safe(self) -> None:
        bad_config = McpServerConfig(
            name="invalid",
            command="non_existent_command_xyz_12345",
            timeout=2.0,
            startup_timeout_s=2.0,
        )
        bad_client = McpClient(name="invalid", config=bad_config, bus=self.bus)
        res = await bad_client.call_tool("echo", {})
        self.assertFalse(res.ok)
        self.assertEqual(bad_client.state, McpConnectionState.OFFLINE)
        self.assertIn("离线", res.error.message if res.error else "")
        await bad_client.close()

    async def test_graceful_close(self) -> None:
        await self.client.ensure_connected()
        self.assertEqual(self.client.state, McpConnectionState.CONNECTED)
        await self.client.close()
        self.assertEqual(self.client.state, McpConnectionState.STOPPED)


if __name__ == "__main__":
    unittest.main()
