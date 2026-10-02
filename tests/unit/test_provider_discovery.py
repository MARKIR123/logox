"""从端点**抓取真实模型列表**（D65）。

它解决什么问题
==============

在此之前 `/model` 的候选来自本地预设表，而那张表**一定会过期**——
用户为某个新模型付了钱，却在自己的工具里选不到它，会怀疑"这工具是不是坏的"。

因此这里要钉住四件事：

1. **抓到了什么**：从 SDK 返回值里正确取出 id（**鸭子类型**，不 import 厂商类型）；
2. **抓不到怎么办**：超时 / 密钥无效 / 端点不支持 —— 统统只是"没抓到"，
   **绝不抛异常**，回退到预设表（抓取是登录流程里的一步，它失败不该挡住登录）；
3. **合并顺序**：抓到的排前面（那是这个账号真实可用的），预设补在后面去重；
4. **缓存**：抓到就写 `state.toml`，这样 `/model` 弹窗零网络、瞬时打开。
"""

from __future__ import annotations

import asyncio
import unittest

from logox.providers.discovery import (
    DiscoveryResult,
    as_model_infos,
    discover_models,
    merge_models,
)
from tests.unit.support import make_temp_dir, remove_temp_dir

# --------------------------------------------------------------------------- #
# 假的 SDK 客户端（形状照抄 openai / anthropic 的 models.list() 返回）
# --------------------------------------------------------------------------- #


class _Model:
    def __init__(self, model_id: str, **extra: object) -> None:
        self.id = model_id
        for key, value in extra.items():
            setattr(self, key, value)


class _Page:
    """一页结果。OpenAI 与 Anthropic 都是 ``{data: [...]}``。"""

    def __init__(self, items: list[object], **extra: object) -> None:
        self.data = items
        for key, value in extra.items():
            setattr(self, key, value)


class _ModelsAPI:
    def __init__(self, page: object | BaseException, *, delay: float = 0.0) -> None:
        self._page = page
        self._delay = delay

    def list(self) -> object:
        async def _call() -> object:
            if self._delay:
                await asyncio.sleep(self._delay)
            if isinstance(self._page, BaseException):
                raise self._page
            return self._page

        return _call()


class _Client:
    def __init__(self, models: _ModelsAPI) -> None:
        self.models = models


class FakeProvider:
    """只有 ``_ensure_client`` 的极简替身（D65 的鸭子类型契约就这么多）。"""

    name = "fake"

    def __init__(self, page: object | BaseException, *, delay: float = 0.0, client_error: Exception | None = None) -> None:
        self._page = page
        self._delay = delay
        self._client_error = client_error
        self.set_models_calls: list[list[str]] = []

    def _ensure_client(self) -> object:
        if self._client_error is not None:
            raise self._client_error
        return _Client(_ModelsAPI(self._page, delay=self._delay))

    def set_models(self, models: list[str]) -> None:
        self.set_models_calls.append(list(models))


class NoClientProvider:
    """没有 ``_ensure_client`` 的对象（测试里的假 provider 就长这样）。"""

    name = "no-client"


# --------------------------------------------------------------------------- #
# 1) 抓取
# --------------------------------------------------------------------------- #


class DiscoverModelsTests(unittest.IsolatedAsyncioTestCase):
    async def test_extracts_ids_from_a_page(self) -> None:
        provider = FakeProvider(_Page([_Model("gpt-4o"), _Model("gpt-4o-mini")]))
        result = await discover_models(provider)  # type: ignore[arg-type]
        self.assertTrue(result.ok)
        self.assertEqual(result.models, ["gpt-4o", "gpt-4o-mini"])
        self.assertTrue(result.attempted)

    async def test_handles_anthropic_style_extra_fields(self) -> None:
        """Anthropic 的项多带 ``display_name`` / ``created_at``——我们只要 id。"""
        provider = FakeProvider(
            _Page([_Model("claude-sonnet-4-5", display_name="Claude Sonnet 4.5", created_at="2025-01-01")])
        )
        result = await discover_models(provider)  # type: ignore[arg-type]
        self.assertEqual(result.models, ["claude-sonnet-4-5"])

    async def test_handles_dict_shaped_items(self) -> None:
        """有些兼容实现返回裸 dict（不是 pydantic 对象）——两种都要能读。"""
        provider = FakeProvider({"data": [{"id": "qwen3:8b"}, {"id": "llama3.2"}]})
        result = await discover_models(provider)  # type: ignore[arg-type]
        self.assertEqual(result.models, ["qwen3:8b", "llama3.2"])

    async def test_deduplicates_but_keeps_the_endpoints_order(self) -> None:
        """★ **刻意不排序**：厂商返回的顺序本身带信息（新模型往往排在前面），
        而 `/model` 弹窗的第一屏正是用户最先看到的东西。排序会把这条信息抹掉，
        换来一个谁也没要求的"整齐"。"""
        provider = FakeProvider(_Page([_Model("newest"), _Model("older"), _Model("newest")]))
        result = await discover_models(provider)  # type: ignore[arg-type]
        self.assertEqual(result.models, ["newest", "older"])

    async def test_skips_blank_and_non_string_ids(self) -> None:
        provider = FakeProvider(_Page([_Model("ok"), _Model("   "), _Model(None), "not-a-model"]))  # type: ignore[arg-type]
        result = await discover_models(provider)  # type: ignore[arg-type]
        self.assertEqual(result.models, ["ok"])

    async def test_empty_page_reports_an_error_not_success(self) -> None:
        """空结果**不算成功**——否则会把预设表覆盖成空，用户反而选不到东西。"""
        provider = FakeProvider(_Page([]))
        result = await discover_models(provider)  # type: ignore[arg-type]
        self.assertFalse(result.ok)
        self.assertIn("没有返回任何模型", result.error)
        self.assertTrue(result.attempted)

    async def test_provider_without_a_client_is_not_an_error(self) -> None:
        """测试用的假 provider 没有 SDK 客户端——这是"不支持"，不是"失败"。"""
        result = await discover_models(NoClientProvider())  # type: ignore[arg-type]
        self.assertFalse(result.attempted)
        self.assertEqual(result.models, [])
        self.assertIn("不支持", result.summary())

    async def test_client_construction_failure_is_reported_not_raised(self) -> None:
        """缺密钥 / SDK 装不上 → 报告，不抛。"""
        provider = FakeProvider(_Page([]), client_error=ValueError("未配置 API Key"))
        result = await discover_models(provider)  # type: ignore[arg-type]
        self.assertFalse(result.ok)
        self.assertIn("未配置 API Key", result.error)

    async def test_http_error_is_reported_not_raised(self) -> None:
        """401 / 网络错误 → 报告，不抛（登录流程要继续走完）。"""

        class _AuthError(Exception):
            status_code = 401

        provider = FakeProvider(_AuthError("Invalid API key"))
        result = await discover_models(provider)  # type: ignore[arg-type]
        self.assertFalse(result.ok)
        self.assertIn("Invalid API key", result.error)

    async def test_timeout_is_reported_not_raised(self) -> None:
        """★ 超时必须只是"没抓到"——用户站在登录流程里等，不能被挂住。"""
        provider = FakeProvider(_Page([_Model("x")]), delay=5.0)
        result = await discover_models(provider, timeout_s=0.05)  # type: ignore[arg-type]
        self.assertFalse(result.ok)
        self.assertIn("超时", result.error)

    async def test_cancellation_propagates(self) -> None:
        """★ 用户在抓取过程中按 Esc：`CancelledError` **必须原样传播**。

        吞掉它会让"取消"静默失效——这与内核的 E-5 是同一条规矩。
        """
        provider = FakeProvider(_Page([_Model("x")]), delay=5.0)
        task = asyncio.ensure_future(discover_models(provider, timeout_s=10))  # type: ignore[arg-type]
        await asyncio.sleep(0.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_error_text_is_truncated_and_single_line(self) -> None:
        """错误要能塞进一行通知里，且**不泄露密钥**（SDK 报错偶尔会带上它）。"""
        provider = FakeProvider(ValueError("boom\n第二行\n第三行" + "x" * 500))
        result = await discover_models(provider)  # type: ignore[arg-type]
        self.assertNotIn("\n", result.error)
        self.assertLessEqual(len(result.error), 160)


# --------------------------------------------------------------------------- #
# 2) 合并与转换
# --------------------------------------------------------------------------- #


class MergeModelsTests(unittest.TestCase):
    def test_discovered_comes_first(self) -> None:
        """抓到的排前面——那是这个账号**真实可用**的，用户多半就在里面选。"""
        self.assertEqual(merge_models(["b", "a"], ["a", "c"]), ["b", "a", "c"])

    def test_deduplicates_keeping_order(self) -> None:
        self.assertEqual(merge_models(["a", "b"], ["b", "a"]), ["a", "b"])

    def test_empty_discovered_falls_back_to_preset(self) -> None:
        self.assertEqual(merge_models([], ["p1", "p2"]), ["p1", "p2"])

    def test_blank_ids_are_dropped(self) -> None:
        self.assertEqual(merge_models(["", "a"], ["", "b"]), ["a", "b"])


class AsModelInfosTests(unittest.TestCase):
    def test_carries_provider_capabilities(self) -> None:
        class P:
            name = "fake"
            context_window = 65_536

            @staticmethod
            def supports_thinking(model: str) -> bool:
                return model.startswith("o")

        infos = as_model_infos(["o3", "gpt-4o"], P())  # type: ignore[arg-type]
        self.assertEqual([info.id for info in infos], ["o3", "gpt-4o"])
        self.assertTrue(infos[0].supports_thinking)
        self.assertFalse(infos[1].supports_thinking)
        self.assertEqual(infos[0].context_window, 65_536)

    def test_unknown_capabilities_are_none_not_fabricated(self) -> None:
        """拿不到的能力就是 ``None``（状态栏显示 `—`），**绝不编一个数字**（D39）。"""

        class P:
            name = "bare"

        infos = as_model_infos(["m"], P())  # type: ignore[arg-type]
        self.assertIsNone(infos[0].context_window)
        self.assertFalse(infos[0].supports_thinking)


# --------------------------------------------------------------------------- #
# 3) Runtime 层的缓存与合并
# --------------------------------------------------------------------------- #


class RuntimeRefreshTests(unittest.IsolatedAsyncioTestCase):
    """`Runtime.refresh_models` 的两个副作用：缓存进 `state.toml`、送进适配器。"""

    def setUp(self) -> None:
        self.root = make_temp_dir("disco-")
        self.addCleanup(remove_temp_dir, self.root)

    def _runtime(self, provider_page: object):  # type: ignore[no-untyped-def]
        from logox.app import Runtime
        from logox.config.state import StateStore
        from logox.kernel.bus import EventBus
        from logox.paths import LogoxPaths

        class Registry:
            def names(self) -> list[str]:
                return ["fake"]

            def spec(self, name: str):  # type: ignore[no-untyped-def]
                from logox.providers.registry import ProviderSpec

                # ★ D188：这个假 provider 模拟的是**云端**端点（有密钥）⇒ 必须声明
                #   `api_key_env`，否则它会被判成"本地免鉴权端点"，`list_models` 的语义
                #   就变成"以端点为准"（不与预设合并）—— 本文件的合并断言即不再成立。
                return ProviderSpec(
                    name="fake", api_key_env="FAKE_API_KEY", models=("preset-a", "preset-b")
                )

            def build(self, name: str, *, api_key: str | None = None, **_: object) -> object:
                return FakeProvider(provider_page)

        paths = LogoxPaths.at(self.root)
        store = StateStore(paths.state)
        return (
            Runtime(
                bus=EventBus(session_id="t"),
                kernel=None,  # type: ignore[arg-type]
                reducer=None,  # type: ignore[arg-type]
                theme=None,  # type: ignore[arg-type]
                config=None,  # type: ignore[arg-type]
                provider_name="fake",
                model="m",
                cwd=self.root,
                tools=[],
                state_store=store,
                registry=Registry(),
            ),
            store,
        )

    async def test_successful_fetch_is_cached_and_merged(self) -> None:
        runtime, store = self._runtime(_Page([_Model("live-1"), _Model("preset-a")]))
        result = await runtime.refresh_models("fake")
        self.assertTrue(result.ok)
        self.assertEqual(result.models, ["live-1", "preset-a"])

        # ① 缓存：下次启动零网络就能看到
        self.assertEqual(store.cached_models("fake"), ["live-1", "preset-a"])
        # ② 合并顺序：抓到的在前，预设里没抓到的补在后
        self.assertEqual(runtime.list_models("fake"), ["live-1", "preset-a", "preset-b"])

    async def test_failed_fetch_leaves_the_preset_untouched(self) -> None:
        """★ 抓不到就**回退到预设表**，而且不该把已有缓存清空。"""
        runtime, store = self._runtime(ValueError("network down"))
        store.set_models("fake", ["old-cache"])
        result = await runtime.refresh_models("fake")
        self.assertFalse(result.ok)
        self.assertEqual(store.cached_models("fake"), ["old-cache"], "失败的抓取不该清掉缓存")
        self.assertEqual(runtime.list_models("fake"), ["old-cache", "preset-a", "preset-b"])

    async def test_empty_result_does_not_overwrite_cache(self) -> None:
        runtime, store = self._runtime(_Page([]))
        store.set_models("fake", ["kept"])
        await runtime.refresh_models("fake")
        self.assertEqual(store.cached_models("fake"), ["kept"])

    async def test_list_models_is_offline_safe_without_a_state_store(self) -> None:
        """没有 `state.toml` 时 `/model` 仍要能列出预设（**抓取失败不该让选择器打不开**）。"""
        runtime, store = self._runtime(_Page([_Model("x")]))
        runtime.state_store = None
        self.assertEqual(runtime.list_models("fake"), ["preset-a", "preset-b"])

    async def test_broken_state_file_does_not_break_the_picker(self) -> None:
        runtime, _store = self._runtime(_Page([_Model("x")]))

        class Exploding:
            def cached_models(self, _name: str) -> list[str]:
                raise OSError("状态文件坏了")

        runtime.state_store = Exploding()
        self.assertEqual(runtime.list_models("fake"), ["preset-a", "preset-b"])


class DiscoveryResultTests(unittest.TestCase):
    def test_summary_distinguishes_three_outcomes(self) -> None:
        """三种结局要说三种话：抓到了 / 不支持 / 失败——**不要笼统说"出错了"**。"""
        ok = DiscoveryResult(models=["a", "b"], attempted=True)
        self.assertIn("2 个", ok.summary())

        unsupported = DiscoveryResult(attempted=False)
        self.assertIn("不支持", unsupported.summary())

        failed = DiscoveryResult(error="超时（12s）", attempted=True)
        self.assertIn("超时", failed.summary())
        self.assertIn("回退", failed.summary())


if __name__ == "__main__":
    unittest.main()
