"""MCP 工具适配器与 Schema 极限脱水器单元测试（M9 / D106 / D107）。"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from logox.mcp.adapter import McpMetaTool, McpProxyTool, condense_mcp_tool
from logox.tools.base import ToolContext, ToolResult


class McpAdapterTests(unittest.IsolatedAsyncioTestCase):
    def test_condense_mcp_tool_primitive_types(self) -> None:
        tool = {
            "name": "create_issue",
            "description": "Create a new issue on GitHub repository with title and body",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "owner": {"type": "string", "description": "Repository owner"},
                    "repo": {"type": "string", "description": "Repository name"},
                    "title": {"type": "string", "description": "Issue title"},
                    "body": {"type": "string", "description": "Issue body"},
                    "count": {"type": "integer"},
                    "ratio": {"type": "number"},
                    "is_bug": {"type": "boolean"},
                    "labels": {"type": "array"},
                    "metadata": {"type": "object"},
                },
                "required": ["owner", "repo", "title"],
            },
        }
        sig = condense_mcp_tool(tool)
        # 必填项不带问号，可选项带问号
        self.assertIn("- create_issue(", sig)
        self.assertIn("owner: str", sig)
        self.assertIn("repo: str", sig)
        self.assertIn("title: str", sig)
        self.assertIn("body?: str", sig)
        self.assertIn("count?: int", sig)
        self.assertIn("ratio?: float", sig)
        self.assertIn("is_bug?: bool", sig)
        self.assertIn("labels?: list", sig)
        self.assertIn("metadata?: dict", sig)
        self.assertIn(": Create a new issue on GitHub", sig)

    def test_condense_mcp_tool_description_truncation(self) -> None:
        very_long_desc = "A" * 100
        tool = {
            "name": "long_tool",
            "description": very_long_desc,
            "inputSchema": {"properties": {}},
        }
        sig = condense_mcp_tool(tool)
        self.assertTrue(sig.endswith("..."))
        # 截断在 60 字符以内
        desc_part = sig.split(": ")[1]
        self.assertEqual(len(desc_part), 60)

    def test_condense_mcp_tool_empty_schema(self) -> None:
        tool = {"name": "ping", "description": "health check"}
        sig = condense_mcp_tool(tool)
        self.assertEqual(sig, "- ping(): health check")

    def test_proxy_tool_naming_and_delegation(self) -> None:
        mock_client = MagicMock()
        proxy = McpProxyTool(
            server_name="github",
            raw_name="create_issue",
            client=mock_client,
            description="Create issue",
        )
        self.assertEqual(proxy.spec.name, "mcp__github__create_issue")
        self.assertEqual(proxy.spec.source, "mcp")
        self.assertEqual(proxy.spec.description, "Create issue")

    async def test_proxy_tool_async_run(self) -> None:
        mock_client = MagicMock()
        mock_client.call_tool = AsyncMock(return_value=ToolResult(ok=True, content="issue #42 created"))

        proxy = McpProxyTool(
            server_name="github",
            raw_name="create_issue",
            client=mock_client,
            description="Create issue",
        )
        ctx = ToolContext(cwd=Path.cwd())
        res = await proxy.run({"title": "bug"}, ctx)
        self.assertTrue(res.ok)
        self.assertEqual(res.content, "issue #42 created")
        mock_client.call_tool.assert_called_once_with("create_issue", {"title": "bug"}, timeout=30.0)

    async def test_meta_tool_list_action(self) -> None:
        mock_manager = MagicMock()
        mock_client = MagicMock()
        mock_client.list_tools = AsyncMock(
            return_value=[
                {"name": "query", "description": "Run SQL query", "inputSchema": {"properties": {"sql": {"type": "string"}}, "required": ["sql"]}}
            ]
        )
        mock_manager.get_client.return_value = mock_client
        mock_manager.generate_meta_tool_description.return_value = "MCP meta tool"

        meta_tool = McpMetaTool(mock_manager)
        ctx = ToolContext(cwd=Path.cwd())

        res = await meta_tool.run({"server": "db", "action": "list"}, ctx)
        self.assertTrue(res.ok)
        self.assertIn("MCP 服务 [db] 可用工具清单", res.content)
        self.assertIn("- query(sql: str): Run SQL query", res.content)

    async def test_meta_tool_call_action(self) -> None:
        mock_manager = MagicMock()
        mock_client = MagicMock()
        mock_client.call_tool = AsyncMock(return_value=ToolResult(ok=True, content="query ok: 1 row"))
        mock_manager.get_client.return_value = mock_client
        mock_manager.generate_meta_tool_description.return_value = "MCP meta tool"

        meta_tool = McpMetaTool(mock_manager)
        ctx = ToolContext(cwd=Path.cwd())

        res = await meta_tool.run(
            {"server": "db", "action": "call", "tool": "query", "arguments": {"sql": "SELECT 1"}},
            ctx,
        )
        self.assertTrue(res.ok)
        self.assertEqual(res.content, "query ok: 1 row")
        mock_client.call_tool.assert_called_once_with("query", {"sql": "SELECT 1"})

    async def test_meta_tool_unknown_server(self) -> None:
        mock_manager = MagicMock()
        mock_manager.get_client.return_value = None
        mock_manager.clients = {"github": MagicMock()}

        meta_tool = McpMetaTool(mock_manager)
        ctx = ToolContext(cwd=Path.cwd())

        res = await meta_tool.run({"server": "db", "action": "list"}, ctx)
        self.assertFalse(res.ok)
        self.assertIn("未找到 MCP 服务 [db]", res.error.message if res.error else "")


if __name__ == "__main__":
    unittest.main()
