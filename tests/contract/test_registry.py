"""Provider 注册表契约测试（MODULE_providers §3 的 ``registry.py``）。

注册表是把「配置里的一个名字」变成「可用的适配器」的唯一入口，因此它必须做到：
**名字错了要说清可用值**、**密钥缺失要早报且不回显**、**用户配置只覆盖写过的字段**。
"""

from __future__ import annotations

import asyncio
import unittest

from pydantic import ValidationError

from logox.errors import MissingApiKeyError, UnknownProviderError
from logox.providers.anthropic import AnthropicProvider
from logox.providers.base import DeltaEvent
from logox.providers.openai_compat import OpenAICompatProvider
from logox.providers.registry import (
    BUILTIN_SPECS,
    NO_AUTH_PLACEHOLDER,
    ProviderRegistry,
    ProviderSpec,
    build_provider,
    merge_spec,
    resolve_api_key,
)
from tests.contract.support import make_request


async def _drain(agen):  # type: ignore[no-untyped-def]
    return [item async for item in agen]


class SpecTests(unittest.TestCase):
    def test_t01_spec_is_frozen_and_closed(self) -> None:
        spec = ProviderSpec(name="x")
        with self.assertRaises(ValidationError):
            spec.name = "y"  # type: ignore[misc]
        with self.assertRaises(ValidationError):
            ProviderSpec(name="x", base_uri="typo")  # type: ignore[call-arg]

    def test_t02_defaults_are_local_friendly(self) -> None:
        spec = ProviderSpec(name="x")
        self.assertEqual(spec.kind, "openai_compat")
        self.assertEqual(spec.base_url, "")
        self.assertEqual(spec.api_key_env, "")  # 本地端点不该被逼着配密钥


class BuiltinSpecTests(unittest.TestCase):
    def test_t03_builtin_names_are_canonical(self) -> None:
        self.assertIn("openai-compatible", BUILTIN_SPECS)
        self.assertIn("deepseek", BUILTIN_SPECS)
        self.assertIn("anthropic", BUILTIN_SPECS)

    def test_t04_every_builtin_key_matches_its_name(self) -> None:
        for key, spec in BUILTIN_SPECS.items():
            with self.subTest(key=key):
                self.assertEqual(key, spec.name)

    def test_t05_local_endpoints_need_no_key(self) -> None:
        for name in ("ollama", "lm-studio"):
            with self.subTest(name=name):
                self.assertEqual(BUILTIN_SPECS[name].api_key_env, "")


class ApiKeyResolutionTests(unittest.TestCase):
    def test_t06_reads_from_the_environment(self) -> None:
        spec = ProviderSpec(name="p", api_key_env="LOGOX_TEST_KEY")
        self.assertEqual(resolve_api_key(spec, {"LOGOX_TEST_KEY": "sk-abc"}), "sk-abc")

    def test_t07_no_env_var_configured_yields_none(self) -> None:
        self.assertIsNone(resolve_api_key(ProviderSpec(name="p"), {}))

    def test_t08_missing_variable_raises_with_the_variable_name(self) -> None:
        spec = ProviderSpec(name="deepseek", api_key_env="DEEPSEEK_API_KEY")
        with self.assertRaises(MissingApiKeyError) as ctx:
            resolve_api_key(spec, {})
        message = str(ctx.exception)
        self.assertIn("DEEPSEEK_API_KEY", message)
        self.assertIn("deepseek", message)

    def test_t09_blank_value_counts_as_missing(self) -> None:
        spec = ProviderSpec(name="p", api_key_env="K")
        with self.assertRaises(MissingApiKeyError):
            resolve_api_key(spec, {"K": "   "})

    def test_t10_error_message_never_echoes_a_secret(self) -> None:
        """E-16：错误消息会进日志与截图，**只许出现变量名**。"""
        spec = ProviderSpec(name="p", api_key_env="K")
        with self.assertRaises(MissingApiKeyError) as ctx:
            resolve_api_key(spec, {"K": ""})
        self.assertNotIn("sk-", str(ctx.exception))
        self.assertIn("K", str(ctx.exception))


class MergeTests(unittest.TestCase):
    def test_t11_user_fields_override_defaults(self) -> None:
        base = BUILTIN_SPECS["deepseek"]
        override = ProviderSpec(name="deepseek", base_url="http://192.168.1.9:8000/v1")
        merged = merge_spec(base, override)
        self.assertEqual(merged.base_url, "http://192.168.1.9:8000/v1")

    def test_t12_unwritten_fields_are_inherited(self) -> None:
        """只写一行 ``models`` 不该把 ``base_url`` 弄丢——那是最难查的一类配置坑。"""
        base = BUILTIN_SPECS["deepseek"]
        merged = merge_spec(base, ProviderSpec(name="deepseek", models=("deepseek-flash",)))
        self.assertEqual(merged.base_url, base.base_url)
        self.assertEqual(merged.api_key_env, base.api_key_env)
        self.assertEqual(merged.models, ("deepseek-flash",))

    def test_t13_explicitly_cleared_field_is_respected(self) -> None:
        """``base_url = ""`` 是"用 SDK 默认端点"的**显式**选择，不能被预设顶回来。"""
        base = BUILTIN_SPECS["deepseek"]
        merged = merge_spec(base, ProviderSpec(name="deepseek", base_url=""))
        self.assertEqual(merged.base_url, "")

    def test_t14_name_follows_the_key_not_the_field(self) -> None:
        base = BUILTIN_SPECS["deepseek"]
        merged = merge_spec(base, ProviderSpec(name="whatever", base_url="http://x"))
        self.assertEqual(merged.name, "deepseek")


class BuildProviderTests(unittest.TestCase):
    def test_t15_picks_the_adapter_by_kind(self) -> None:
        openai = build_provider(ProviderSpec(name="a", kind="openai_compat"))
        anthropic = build_provider(ProviderSpec(name="b", kind="anthropic"))
        self.assertIsInstance(openai, OpenAICompatProvider)
        self.assertIsInstance(anthropic, AnthropicProvider)

    def test_t16_kind_is_swappable_without_touching_the_rest(self) -> None:
        """同一个 spec 只改 ``kind`` 就能换协议——装配根不必知道适配器的细节。"""
        spec = ProviderSpec(name="proxy", kind="anthropic", base_url="http://gw.local", api_key_env="GW_KEY")
        adapter = build_provider(spec, api_key="k")
        assert isinstance(adapter, AnthropicProvider)
        self.assertEqual(adapter.base_url, "http://gw.local")
        self.assertEqual(adapter.name, "anthropic")

    def test_t17_empty_base_url_falls_back_to_the_adapter_default(self) -> None:
        adapter = build_provider(ProviderSpec(name="a"))
        assert isinstance(adapter, OpenAICompatProvider)
        self.assertEqual(adapter.base_url, "https://api.openai.com/v1")

    def test_t18_spec_models_flow_into_list_models(self) -> None:
        adapter = build_provider(ProviderSpec(name="a", models=("deepseek-reasoner", "mystery"), context_window=1000))
        infos = {info.id: info for info in adapter.list_models()}
        self.assertEqual(infos["deepseek-reasoner"].context_window, 1000)
        self.assertTrue(infos["deepseek-reasoner"].supports_thinking)
        self.assertIsNone(infos["mystery"].input_price_per_mtok)  # 未知模型不猜价格


class RegistryTests(unittest.TestCase):
    def test_t19_builtins_are_available_by_default(self) -> None:
        registry = ProviderRegistry.with_builtins()
        self.assertIn("anthropic", registry.names())
        self.assertIn("openai-compatible", registry.names())

    def test_t20_user_instance_is_added(self) -> None:
        registry = ProviderRegistry.with_builtins({"my-gw": {"kind": "anthropic", "base_url": "http://gw.local"}})
        spec = registry.spec("my-gw")
        self.assertEqual(spec.kind, "anthropic")
        self.assertEqual(spec.base_url, "http://gw.local")

    def test_t21_user_instance_overrides_a_builtin(self) -> None:
        registry = ProviderRegistry.with_builtins({"deepseek": {"base_url": "http://192.168.1.9:8000/v1"}})
        spec = registry.spec("deepseek")
        self.assertEqual(spec.base_url, "http://192.168.1.9:8000/v1")
        self.assertEqual(spec.api_key_env, "DEEPSEEK_API_KEY")  # 其余仍继承预设

    def test_t22_unknown_name_lists_the_available_ones(self) -> None:
        """D44：只说"找不到"用户无从下手，可用值列表才能让他立刻改对。"""
        registry = ProviderRegistry.with_builtins()
        with self.assertRaises(UnknownProviderError) as ctx:
            registry.spec("opnai")
        message = str(ctx.exception)
        self.assertIn("opnai", message)
        self.assertIn("openai-compatible", message)

    def test_t23_build_resolves_the_key_from_the_injected_environment(self) -> None:
        registry = ProviderRegistry.with_builtins(environ={"ANTHROPIC_API_KEY": "sk-test"})
        adapter = registry.build("anthropic")
        assert isinstance(adapter, AnthropicProvider)
        self.assertEqual(adapter.api_key, "sk-test")

    def test_t24_build_on_a_local_endpoint_needs_no_key(self) -> None:
        """本地端点（Ollama / LM Studio）配好就能用，**不该被密钥问题挡住**。

        这里断言的是一个**占位符**而非 ``None``：官方 SDK 的 ``api_key`` 参数不能为空
        （``AsyncOpenAI(api_key=None)`` 会去读 ``OPENAI_API_KEY``，读不到就抛错），
        所以"无需鉴权"必须表达成一个非空字符串。实测踩到过：不换占位符时
        Ollama 会以一个 ``auth`` 错误启动失败，而配置完全正确。
        """
        registry = ProviderRegistry.with_builtins(environ={})
        adapter = registry.build("ollama")
        assert isinstance(adapter, OpenAICompatProvider)
        self.assertEqual(adapter.api_key, NO_AUTH_PLACEHOLDER)
        self.assertTrue(adapter.api_key, "SDK 要求 api_key 非空")

    def test_t24b_local_endpoint_can_actually_construct_its_client(self) -> None:
        """真正把 SDK 客户端建起来——上一条只断言了字段，这条断言"能建"。"""
        registry = ProviderRegistry.with_builtins(environ={})
        adapter = registry.build("ollama")
        client = adapter._ensure_client()  # noqa: SLF001 - 刻意验证 SDK 不接受空密钥这一点
        self.assertIsNotNone(client)

    def test_t25_build_without_a_key_fails_early(self) -> None:
        registry = ProviderRegistry.with_builtins(environ={})
        with self.assertRaises(MissingApiKeyError):
            registry.build("anthropic")

    def test_t26_explicit_key_bypasses_the_environment(self) -> None:
        registry = ProviderRegistry.with_builtins(environ={})
        adapter = registry.build("anthropic", api_key="explicit")
        assert isinstance(adapter, AnthropicProvider)
        self.assertEqual(adapter.api_key, "explicit")

    def test_t27_raw_stream_seam_passes_through(self) -> None:
        """注册表不得挡住 M2 最重要的可测试性接缝——用**行为**验证，不摸私有属性。"""
        seen: list[str] = []

        def factory(request):  # type: ignore[no-untyped-def]
            seen.append(request.model)

            async def generator():  # type: ignore[no-untyped-def]
                yield {"choices": [{"index": 0, "delta": {"content": "来自夹具"}, "finish_reason": "stop"}]}

            return generator()

        registry = ProviderRegistry.with_builtins(environ={})
        adapter = registry.build("ollama", raw_stream=factory)
        events = asyncio.run(_drain(adapter.stream(make_request(model="qwen3:8b"))))
        self.assertEqual(seen, ["qwen3:8b"])  # 接缝确实被走到了
        self.assertEqual([e.text for e in events if isinstance(e, DeltaEvent)], ["来自夹具"])

    def test_t28_list_models_does_not_touch_the_network(self) -> None:
        """列举模型只读预设 + 配置；未配密钥也必须能跑（TUI 启动时要显示选择器）。"""
        registry = ProviderRegistry.with_builtins(environ={})
        infos = registry.list_models("anthropic")
        self.assertTrue(infos)
        self.assertTrue(all(info.provider == "anthropic" for info in infos))

    def test_t29_all_models_and_ids_are_flat_and_ordered(self) -> None:
        registry = ProviderRegistry.with_builtins(environ={})
        ids = registry.model_ids()
        names = [name for name, _ in ids]
        self.assertEqual(names, sorted(names))
        self.assertIn(("deepseek", "deepseek-v4-pro"), ids)

    def test_t30_default_model_follows_the_declared_capability(self) -> None:
        """首选模型由 ``ProviderSpec.default_model`` 决定，**不是"列表里的第一个"**。

        deepseek 的预设里默认模型是**多模态**那个（实测三个名字里只有它和
        `deepseek-flash` 认得出图片，`deepseek-v4-pro` 看到的是 `[Unsupported Image]`）——
        "列表第一个"只是排版顺序，而默认值要按能力挑。
        """
        registry = ProviderRegistry.with_builtins(environ={})
        self.assertEqual(
            registry.default_model("deepseek"), "deepseek-v4.1-flash-expires-on-0910"
        )
        # 没显式声明 `default_model` 的 provider → 回退到第一个（老行为）
        self.assertEqual(registry.default_model("openai-compatible"), "gpt-4o")
        self.assertIsNone(registry.default_model("lm-studio"))  # 本地端点模型由用户给

    def test_t30b_deepseek_default_is_multimodal(self) -> None:
        """★ 回归：deepseek 的默认模型必须是**能看图**的那个。

        为什么值得一条专门的用例：默认值一旦退回纯文本模型，用户贴图时
        既不会报错也不会生效（D67 的 `_THINKING_MODELS` 就是同一类"改名即静默失效"）。
        这里把**实测过的结论**固定下来——三个名字里哪两个支持图片。
        """
        spec = ProviderRegistry.with_builtins(environ={}).spec("deepseek")
        self.assertEqual(spec.default_model, "deepseek-v4.1-flash-expires-on-0910")
        self.assertIn(spec.default_model, spec.models)
        # 实测：以下两个认得出图片；deepseek-v4-pro 明确回 "[Unsupported Image]"
        self.assertIn("deepseek-flash", spec.models)
        self.assertNotEqual(spec.default_model, "deepseek-v4-pro")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
