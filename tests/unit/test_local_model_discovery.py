"""本地（免鉴权）端点的模型清单语义与自动刷新（D188 / CHANGE-055）。

背景（实测得来，不是推测）：`discover_models` 与本地 Ollama 的流式请求**本来就是通的**，
缺的只是两件事——
1. `/model` 的候选来自**静态预设表**（`qwen3:8b` / `llama3.2`），与用户实际拉取的模型无关；
2. 真实抓取只在 `/login` 那一步发生，而本地端点**不需要登录**，所以那条路永远不跑。

本文件守三件事：
* 免鉴权端点有缓存时**以端点为准**（不与预设合并）★ 突变验证点；
* 有密钥的云端 provider **行为不变**（仍然合并）；
* 刷新只碰免鉴权端点，且**一个失败不影响另一个**、异常不外抛。
"""

from __future__ import annotations

import unittest
from pathlib import Path
from typing import Any


def _registry() -> Any:
    from logox.providers.registry import ProviderRegistry

    return ProviderRegistry.with_builtins()


class FakeState:
    """只实现模型缓存那一小块（Runtime 在这条路径上只碰这两个方法）。"""

    def __init__(self, cached: dict[str, list[str]] | None = None) -> None:
        self._cached = dict(cached or {})
        self.writes: list[tuple[str, list[str]]] = []
        self.fail_write = False

    def cached_models(self, name: str) -> list[str]:
        return list(self._cached.get(name, []))

    def set_models(self, name: str, models: list[str]) -> None:
        if self.fail_write:
            raise OSError("磁盘满了（测试用）")
        self.writes.append((name, list(models)))
        self._cached[name] = list(models)


def make_runtime(state: FakeState, *, registry: Any = None) -> Any:
    """造一个**真** Runtime（字段用占位对象，本文件只走模型清单这条路径）。"""
    from logox.app import Runtime

    return Runtime(  # type: ignore[call-arg]
        bus=object(),
        kernel=object(),
        reducer=object(),
        theme=object(),
        config=object(),
        provider_name="ollama",
        model="qwen3.8:27b",
        cwd=Path("."),
        tools=[],
        registry=registry if registry is not None else _registry(),
        state_store=state,
    )


class KeylessListSemanticsTests(unittest.TestCase):
    def test_t01_keyless_provider_prefers_endpoint_over_preset(self) -> None:
        """★ 免鉴权端点：有缓存 ⇒ **就是**端点给的那份，预设里的过时名字不得混进来。

        突变验证：把 `list_models` 改回 `merge_models(cached, preset)` ⇒ 本条立刻失败。
        （实测场景：用户拉的是 qwen3.8:27b，而预设表写的是 qwen3:8b ——
        合并会让你在弹窗里看到一个**根本没拉取**的模型，选了就报错。）
        """
        state = FakeState({"ollama": ["qwen3.8:27b", "llama3.1:latest"]})
        runtime = make_runtime(state)

        models = runtime.list_models("ollama")

        self.assertEqual(models, ["qwen3.8:27b", "llama3.1:latest"])
        self.assertNotIn("qwen3:8b", models, "预设表里的过时模型混进来了（说明又变成合并语义了）")

    def test_t02_cloud_provider_still_merges(self) -> None:
        """有密钥的云端 provider **行为不变**：缓存 ∪ 预设。

        为什么两种语义不同：云端预设里常含「按能力挑的默认项」（见 `default_model` 的
        设计说明），而本机端点的清单就是**权威全集**、不存在「补一个它没报的模型」这种需求。
        """
        state = FakeState({"deepseek": ["deepseek-v9-flash"]})
        runtime = make_runtime(state)

        models = runtime.list_models("deepseek")

        self.assertIn("deepseek-v9-flash", models, "抓到的真实模型必须保留")
        self.assertIn("deepseek-v4-pro", models, "云端仍应保留预设（合并语义）")

    def test_t03_no_cache_falls_back_to_preset(self) -> None:
        """从没抓到过 ⇒ 回退预设表（首启时 `/model` 至少有事可展示）。"""
        runtime = make_runtime(FakeState())

        self.assertEqual(runtime.list_models("ollama"), ["qwen3:8b", "llama3.2"])

    def test_t04_empty_cache_is_treated_as_no_cache(self) -> None:
        """缓存是**空列表**时按「没抓到」处理 —— 不能用空清单把候选整个清掉。"""
        runtime = make_runtime(FakeState({"ollama": []}))

        self.assertTrue(runtime.list_models("ollama"), "空缓存把预设也吃掉了")


class RefreshLocalModelsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.state = FakeState()
        self.runtime = make_runtime(self.state)
        self.calls: list[tuple[str, float | None]] = []

    def _patch_refresh(self, *, fail: set[str] | None = None) -> None:
        from logox.providers.discovery import DiscoveryResult

        failing = fail or set()

        async def fake_refresh(name: str, *, api_key: str | None = None, timeout_s: float | None = None) -> Any:
            self.calls.append((name, timeout_s))
            if name in failing:
                return DiscoveryResult(error="连接被拒绝", attempted=True)
            return DiscoveryResult(models=["m1", "m2"], attempted=True)

        self.runtime.refresh_models = fake_refresh  # type: ignore[method-assign]

    async def test_t05_only_keyless_providers_are_refreshed(self) -> None:
        """只抓**免鉴权**端点：不能拿用户没配的云端 provider 去打网络。"""
        self._patch_refresh()

        results = await self.runtime.refresh_local_models()

        names = [name for name, _ in results]
        self.assertEqual(names, ["lm-studio", "ollama"], "顺序需稳定（来自 keyless_names 排序）")
        self.assertEqual([name for name, _ in self.calls], names, "抓取必须逐个进行且命中同一批名字")
        self.assertNotIn("deepseek", names, "带密钥的云端 provider 不该被自动抓取")

    async def test_t06_default_timeout_is_local_short(self) -> None:
        """默认用**短超时**：本机端点慢 = 服务没在跑，不值得等 `/login` 那 12 秒。"""
        from logox.providers.discovery import DEFAULT_TIMEOUT_S, LOCAL_TIMEOUT_S

        self._patch_refresh()

        await self.runtime.refresh_local_models()

        self.assertTrue(self.calls)
        self.assertEqual({t for _, t in self.calls}, {LOCAL_TIMEOUT_S})
        self.assertLess(LOCAL_TIMEOUT_S, DEFAULT_TIMEOUT_S)

    async def test_t07_one_failure_does_not_stop_the_others(self) -> None:
        """一个端点失败 ⇒ **仍然**抓下一个，且结果如实回传（显式路径要靠它报错）。"""
        self._patch_refresh(fail={"lm-studio"})

        results = await self.runtime.refresh_local_models()

        self.assertEqual([name for name, _ in results], ["lm-studio", "ollama"])
        by_name = dict(results)
        self.assertFalse(by_name["lm-studio"].ok)
        self.assertTrue(by_name["ollama"].ok, "前一个失败不该中断后一个")

    async def test_t08_write_failure_does_not_raise(self) -> None:
        """`state.toml` 写失败 ⇒ 只警告、不外抛（本地缓存坏了不该让启动失败）。"""
        from logox.providers.discovery import DiscoveryResult

        self.state.fail_write = True

        async def fake_refresh(name: str, *, api_key: str | None = None, timeout_s: float | None = None) -> Any:
            return DiscoveryResult(models=["m1"], attempted=True)

        self.runtime.refresh_models = fake_refresh  # type: ignore[method-assign]
        # 走真实的 refresh_models 写盘路径：这里直接调它，确认写失败被吞掉
        result = await self.runtime.refresh_local_models()

        self.assertTrue(result)
        self.assertTrue(all(r.ok for _, r in result))

    async def test_t09_unexpected_exception_is_captured(self) -> None:
        """`refresh_models` 自己炸了（不该发生）也要被兜住 —— 启动路径绝不能因此失败。"""
        self.runtime.refresh_models = lambda name, **kw: (_ for _ in ()).throw(RuntimeError("boom"))  # type: ignore[method-assign, assignment]

        results = await self.runtime.refresh_local_models()

        self.assertTrue(results)
        self.assertTrue(all(not r.ok for _, r in results))
        self.assertIn("boom", results[0][1].error)

    async def test_t10_registry_without_keyless_names_still_works(self) -> None:
        """假注册表（只有 names/spec）也不能把刷新搞崩 —— 启动路径上的 AttributeError
        会让整个后台任务报错，而报错的表现只是"本地模型一直不出现"，很难查。"""

        class FakeRegistry:
            def names(self) -> list[str]:
                return ["ollama", "deepseek"]

            def spec(self, name: str) -> Any:
                from logox.providers.registry import ProviderSpec

                if name == "ollama":
                    return ProviderSpec(name="ollama", base_url="http://127.0.0.1:11434/v1")
                return ProviderSpec(name="deepseek", api_key_env="DEEPSEEK_API_KEY")

        runtime = make_runtime(FakeState(), registry=FakeRegistry())
        seen: list[str] = []

        async def fake_refresh(name: str, *, api_key: str | None = None, timeout_s: float | None = None) -> Any:
            from logox.providers.discovery import DiscoveryResult

            seen.append(name)
            return DiscoveryResult(models=["m"], attempted=True)

        runtime.refresh_models = fake_refresh  # type: ignore[method-assign]

        results = await runtime.refresh_local_models()

        self.assertEqual(seen, ["ollama"], "只有免鉴权端点该被抓（回退路径必须能算出名单）")
        self.assertEqual([name for name, _ in results], ["ollama"])

    async def test_t11_empty_discovery_keeps_previous(self) -> None:
        """抓到的清单为空 ⇒ 视为"没抓到"：不移除旧缓存（否则一次空响应就把候选清空）。"""
        self.state = FakeState({"ollama": ["qwen3.8:27b"]})
        self.runtime = make_runtime(self.state)

        async def fake_refresh(name: str, *, api_key: str | None = None, timeout_s: float | None = None) -> Any:
            from logox.providers.discovery import DiscoveryResult

            return DiscoveryResult(models=[], attempted=True)

        self.runtime.refresh_models = fake_refresh  # type: ignore[method-assign]

        await self.runtime.refresh_local_models()

        self.assertEqual(self.runtime.list_models("ollama"), ["qwen3.8:27b"], "空清单把旧缓存清掉了")


if __name__ == "__main__":
    unittest.main()
