"""轻量 Mock MCP Server 脚本（用于离线生命周期单元测试）。

通过标准输入输出（stdin / stdout）响应标准 JSON-RPC 2.0 协议消息。
不依赖任何外部网络或 node 环境。
"""

from __future__ import annotations

import json
import sys
import time


def main() -> None:
    while True:
        line = sys.stdin.readline()
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            continue

        method = req.get("method")
        msg_id = req.get("id")

        if method == "initialize":
            resp = {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "mock-server", "version": "1.0.0"},
                },
            }
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()
        elif method == "notifications/initialized":
            pass  # 通知消息，无需回复
        elif method == "tools/list":
            resp = {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "tools": [
                        {
                            "name": "echo",
                            "description": "Echo back text",
                            "inputSchema": {
                                "type": "object",
                                "properties": {"text": {"type": "string"}},
                                "required": ["text"],
                            },
                        },
                        {
                            "name": "sleep_tool",
                            "description": "Sleeps for timeout testing",
                            "inputSchema": {
                                "type": "object",
                                "properties": {"seconds": {"type": "number"}},
                            },
                        },
                        {
                            "name": "fail_tool",
                            "description": "Returns tool error",
                            "inputSchema": {"type": "object"},
                        },
                        {
                            "name": "crash_tool",
                            "description": "Crashes process",
                            "inputSchema": {"type": "object"},
                        },
                    ]
                },
            }
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()
        elif method == "tools/call":
            params = req.get("params") or {}
            name = params.get("name")
            arguments = params.get("arguments") or {}

            if name == "echo":
                text = arguments.get("text", "")
                resp = {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "content": [{"type": "text", "text": f"echo: {text}"}],
                        "isError": False,
                    },
                }
                sys.stdout.write(json.dumps(resp) + "\n")
                sys.stdout.flush()
            elif name == "sleep_tool":
                sec = float(arguments.get("seconds", 2.0))
                time.sleep(sec)
                resp = {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "content": [{"type": "text", "text": "done sleeping"}],
                        "isError": False,
                    },
                }
                sys.stdout.write(json.dumps(resp) + "\n")
                sys.stdout.flush()
            elif name == "fail_tool":
                resp = {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "content": [{"type": "text", "text": "Something went wrong in tool"}],
                        "isError": True,
                    },
                }
                sys.stdout.write(json.dumps(resp) + "\n")
                sys.stdout.flush()
            elif name == "crash_tool":
                # 模拟外部服务子进程突发崩溃闪退
                sys.exit(1)
            else:
                resp = {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {"code": -32601, "message": f"Method {name} not found"},
                }
                sys.stdout.write(json.dumps(resp) + "\n")
                sys.stdout.flush()
        elif method == "ping":
            resp = {"jsonrpc": "2.0", "id": msg_id, "result": {}}
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
