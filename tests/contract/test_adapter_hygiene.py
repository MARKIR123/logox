"""适配层的两条"卫生"红线（MODULE_providers §8 的验收标准）。

1. **厂商专有字段不得出现在任何统一事件里**——这是 D9 的全部意义所在。
   一旦 ``reasoning_content`` 或 ``prompt_tokens_details`` 漏到上游，内核就要开始
   认识厂商，整个分层随即腐化。
2. **契约测试本身不得联网**——否则"离线可测"就是一句空话，"测试通过"也不再
   说明适配器正确，只说明当时网络通。

两条都用**黑名单断言**而不是人工审查：审查会漏，断言不会。
"""

from __future__ import annotations

import ast
import unittest
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from logox.providers.anthropic import AnthropicProvider
from logox.providers.base import ChatRequest
from logox.providers.openai_compat import OpenAICompatProvider
from tests.contract.support import FIXTURES, load_chunks, make_request, translate_events

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACT_DIR = Path(__file__).resolve().parent

#: 绝不允许出现在统一事件里的**厂商专有键名**。
#: 注意 ``reasoning_tokens`` / ``cached_input_tokens`` 是**中立字段**（D39 规定的口径），
#: 不在黑名单里——它们的左侧对应项才在黑名单里。
VENDOR_KEYS = frozenset(
    {
        # OpenAI 兼容
        "choices",
        "delta",
        "finish_reason",
        "prompt_tokens",
        "completion_tokens",
        "prompt_tokens_details",
        "completion_tokens_details",
        "cached_tokens",
        "tool_calls",
        "tool_call_id",
        "function_call",
        "reasoning_content",
        "prompt_cache_hit_tokens",
        # Anthropic
        "content_block",
        "content_block_start",
        "content_block_delta",
        "content_block_stop",
        "message_start",
        "message_delta",
        "message_stop",
        "input_json_delta",
        "partial_json",
        "thinking_delta",
        "signature_delta",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
        "tool_use_id",
        "input_schema",
        "is_error",
        "budget_tokens",
    }
)

#: ``vendor_data`` 是"厂商不透明数据的**受限**通道"，目前只许放这一个键。
#: 如果哪天这里需要加第二个键，说明该重新设计，而不是悄悄放行。
ALLOWED_VENDOR_DATA_KEYS = frozenset({"signature"})

#: 契约测试不得 import 的网络客户端。
NETWORK_MODULES = frozenset({"httpx", "openai", "anthropic", "requests", "aiohttp", "socket", "urllib", "http", "ssl"})

FIXTURE_USERS: tuple[tuple[str, str, str], ...] = (
    ("openai_compat", "openai_text", "gpt-4o-mini"),
    ("openai_compat", "openai_tool_call", "gpt-4o-mini"),
    ("openai_compat", "openai_multi_tool", "gpt-4o-mini"),
    ("openai_compat", "openai_no_cache", "local-model"),
    ("openai_compat", "openai_cache_zero", "gpt-4o-mini"),
    ("openai_compat", "openai_truncated_tool", "gpt-4o-mini"),
    ("openai_compat", "openai_error_chunk", "gpt-4o-mini"),
    ("openai_compat", "deepseek_reasoning", "deepseek-reasoner"),
    ("anthropic", "anthropic_text", "claude-sonnet-4-5"),
    ("anthropic", "anthropic_tool_use", "claude-sonnet-4-5"),
    ("anthropic", "anthropic_thinking", "claude-sonnet-4-5"),
    ("anthropic", "anthropic_error", "claude-sonnet-4-5"),
)


def walk_keys(node: Any, path: str = "") -> Iterator[tuple[str, str]]:
    """递归产出 ``(路径, 键名)``。"""
    if isinstance(node, dict):
        for key, value in node.items():
            here = f"{path}.{key}" if path else str(key)
            yield here, str(key)
            yield from walk_keys(value, here)
    elif isinstance(node, list):
        for index, item in enumerate(node):
            yield from walk_keys(item, f"{path}[{index}]")


def all_events() -> list[tuple[str, str, list[Any]]]:
    """把全部夹具跑一遍两个适配器，收集 **(夹具名, 适配器, 事件)**。"""
    collected: list[tuple[str, str, list[Any]]] = []
    for adapter_name, fixture, model in FIXTURE_USERS:
        adapter: Any = (
            AnthropicProvider() if adapter_name == "anthropic" else OpenAICompatProvider()
        )
        request: ChatRequest = make_request(model=model)
        events = translate_events(adapter, load_chunks(fixture), request)
        collected.append((fixture, adapter_name, events))
    return collected


class VendorFieldBlacklistTests(unittest.TestCase):
    def test_t01_no_vendor_field_leaks_into_any_event(self) -> None:
        checked = 0
        for fixture, adapter, events in all_events():
            for event in events:
                dumped = event.model_dump(mode="json")
                for path, key in walk_keys(dumped):
                    checked += 1
                    with self.subTest(fixture=fixture, adapter=adapter, path=path):
                        self.assertNotIn(
                            key,
                            VENDOR_KEYS,
                            f"{adapter} 的事件里出现了厂商专有字段 {key}（路径 {path}）",
                        )
        self.assertGreater(checked, 100, "应检查到足够多的键，否则黑名单形同虚设")

    def test_t02_vendor_data_channel_stays_limited_to_the_signature(self) -> None:
        """``vendor_data`` 是刻意保留的**唯一**例外，不能变成第二个后门。"""
        for fixture, adapter, events in all_events():
            for event in events:
                payload = event.model_dump(mode="json")
                vendor_data = payload.get("vendor_data")
                if vendor_data is None:
                    continue
                with self.subTest(fixture=fixture, adapter=adapter):
                    self.assertEqual(set(vendor_data), set(ALLOWED_VENDOR_DATA_KEYS))

    def test_t03_event_models_forbid_unknown_fields(self) -> None:
        """所有统一事件都是 ``extra="forbid"``：谁想加字段都必须改 ``base.py``。"""
        for fixture, adapter, events in all_events():
            for event in events:
                with self.subTest(fixture=fixture, adapter=adapter, event=type(event).__name__):
                    self.assertEqual(event.model_config.get("extra"), "forbid")

    def test_t04_every_fixture_is_actually_used(self) -> None:
        """夹具也会腐烂：没人引用的夹具说明它测的场景已经被删或漏测。"""
        on_disk = {path.stem for path in FIXTURES.glob("*.jsonl")}
        used = {fixture for _, fixture, _ in FIXTURE_USERS}
        self.assertEqual(on_disk - used, set(), "存在没有任何契约测试引用的夹具")
        self.assertEqual(used - on_disk, set(), "契约测试引用了不存在的夹具")


class OfflineGuaranteeTests(unittest.TestCase):
    def test_t05_contract_tests_import_no_network_client(self) -> None:
        """"离线可测"必须由断言保证，否则某天有人顺手加个网络调用也没人发现。"""
        scanned = 0
        for path in sorted(CONTRACT_DIR.glob("*.py")):
            scanned += 1
            tree = ast.parse(path.read_text(encoding="utf-8"))
            roots: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    roots.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                    roots.add(node.module.split(".")[0])
            for root in sorted(roots):
                with self.subTest(file=path.name, imported=root):
                    self.assertNotIn(root, NETWORK_MODULES, f"{path.name} 不得 import {root}")
        self.assertGreaterEqual(scanned, 5, "应扫描到 contract 包的全部模块")

    def test_t06_adapters_import_vendor_sdks_lazily(self) -> None:
        """SDK 只能在**真正发请求时** import。

        否则任何 ``import logox.providers.*`` 都会顺带拉起 openai / anthropic，
        拖慢启动（D29 的 ``--version`` < 300ms）——而大多数命令根本不需要 SDK。
        """
        for path in sorted((REPO_ROOT / "src" / "logox" / "providers").glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            top_level: set[str] = set()
            for node in tree.body:  # 只看模块顶层；函数内部的延迟导入不算
                if isinstance(node, ast.Import):
                    top_level.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                    top_level.add(node.module.split(".")[0])
            for name in sorted(top_level):
                with self.subTest(file=path.name, imported=name):
                    self.assertNotIn(name, ("openai", "anthropic", "httpx"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
