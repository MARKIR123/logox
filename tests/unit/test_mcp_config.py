"""MCP 配置解析与环境变量展开测试（M9 / D109）。"""

from __future__ import annotations

import os
import unittest
from unittest import mock

from logox.config.schema import McpConfig, McpServerConfig


class McpConfigTests(unittest.TestCase):
    def test_mcp_server_config_defaults(self) -> None:
        cfg = McpServerConfig(name="github", command="npx")
        self.assertEqual(cfg.name, "github")
        self.assertEqual(cfg.transport, "stdio")
        self.assertEqual(cfg.command, "npx")
        self.assertEqual(cfg.args, [])
        self.assertEqual(cfg.timeout, 30.0)
        self.assertEqual(cfg.startup_timeout_s, 20.0)
        self.assertFalse(cfg.disabled)
        self.assertTrue(cfg.is_enabled())

    def test_mcp_server_disabled_flag(self) -> None:
        cfg1 = McpServerConfig(name="s1", command="x", disabled=True)
        self.assertFalse(cfg1.is_enabled())

        cfg2 = McpServerConfig(name="s2", command="x", enabled=False)
        self.assertFalse(cfg2.is_enabled())

    def test_env_var_expansion(self) -> None:
        with mock.patch.dict(os.environ, {"MY_SECRET_KEY": "sk-123456", "HOST": "localhost"}):
            cfg = McpServerConfig(
                name="test",
                command="node",
                env={
                    "AUTH_TOKEN": "${MY_SECRET_KEY}",
                    "URL": "http://$HOST:8080",
                    "PLAIN": "literal",
                    "MISSING": "${UNSET_VAR}",
                },
            )
            resolved = cfg.resolved_env()
            self.assertEqual(resolved["AUTH_TOKEN"], "sk-123456")
            self.assertEqual(resolved["URL"], "http://localhost:8080")
            self.assertEqual(resolved["PLAIN"], "literal")
            self.assertEqual(resolved["MISSING"], "")

    def test_mcp_config_dict_servers_normalization(self) -> None:
        cfg = McpConfig(
            servers={
                "github": McpServerConfig(command="npx", args=["-y", "github-mcp"]),
                "db": McpServerConfig(name="custom_db", command="python", args=["db.py"]),
            }
        )
        servers = cfg.get_servers()
        self.assertIn("github", servers)
        self.assertEqual(servers["github"].name, "github")
        self.assertEqual(servers["github"].command, "npx")
        self.assertEqual(servers["db"].name, "custom_db")

    def test_mcp_config_list_servers_normalization(self) -> None:
        cfg = McpConfig(
            servers=[
                McpServerConfig(name="s1", command="c1"),
                McpServerConfig(name="s2", command="c2"),
            ]
        )
        servers = cfg.get_servers()
        self.assertEqual(set(servers.keys()), {"s1", "s2"})
        self.assertEqual(servers["s1"].command, "c1")


if __name__ == "__main__":
    unittest.main()
