"""切换模型及早压缩（Eager Compaction）与动态窗口单元测试。"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from logox.app import build_runtime
from logox.config.schema import ContextConfig, LogoxConfig, ProviderConfig
from logox.kernel.messages import Message, TextBlock
from logox.paths import LogoxPaths
from logox.providers.registry import BUILTIN_SPECS
from tests.unit.support import make_temp_dir, remove_temp_dir


def make_history(turns: int, *, chars: int = 200) -> list[Message]:
    """构造较长的会话历史，便于跨过小模型的水位线。"""
    out: list[Message] = []
    for index in range(turns):
        out.append(Message(role="user", blocks=[TextBlock(text=f"第{index}问" + "字" * chars)]))
        out.append(Message(role="assistant", blocks=[TextBlock(text=f"第{index}答" + "字" * chars)]))
    return out


class EagerCompactionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.root = make_temp_dir("eager-compact-")
        self.addCleanup(remove_temp_dir, self.root)
        self.paths = LogoxPaths.at(self.root)

    def _runtime(self, *, model: str = "deepseek-flash"):
        config = LogoxConfig(
            provider=ProviderConfig(name="deepseek", model=model),
            context=ContextConfig(),
        )
        return build_runtime(SimpleNamespace(config=config, sources=[]), self.root, self.paths)

    def test_ollama_builtin_spec_has_safety_baseline(self) -> None:
        """验证 BUILTIN_SPECS['ollama'] 具备 32,768 的安全基线上下文窗口。"""
        ollama_spec = BUILTIN_SPECS.get("ollama")
        self.assertIsNotNone(ollama_spec)
        self.assertEqual(ollama_spec.context_window, 32_768)

    def test_dynamic_model_windows_takes_precedence_in_window_for(self) -> None:
        """验证动态探查到的模型窗口优先于预设表。"""
        runtime = self._runtime()
        self.assertIsNone(runtime.window_for("custom-local-model"))

        # 动态探测结果写入缓存
        runtime._dynamic_model_windows["custom-local-model"] = 65_536
        self.assertEqual(runtime.window_for("custom-local-model"), 65_536)

    async def test_eager_compact_noop_when_history_empty(self) -> None:
        """空历史时换模型不触发压缩。"""
        runtime = self._runtime()
        report = await runtime.eager_compact_if_needed()
        self.assertIsNone(report)

    async def test_eager_compact_triggers_when_switching_to_smaller_window(self) -> None:
        """从大窗口模型切到小窗口模型，若历史超出新模型高水位线，自动触发及早压缩。"""
        runtime = self._runtime(model="deepseek-flash")  # 默认 1M 窗口
        # 给内核注入一段足够长（> 1500 tokens）的历史
        runtime.kernel.history = make_history(15, chars=150)

        # 切换到自定义小窗口模型（例如窗口仅 1000 tokens）
        runtime._dynamic_model_windows["tiny-model"] = 1000
        window = runtime.apply_model("tiny-model")
        self.assertEqual(window, 1000)

        # 检查及早压缩是否触发
        report = await runtime.eager_compact_if_needed()
        self.assertIsNotNone(report, "超出小模型高水位线时应当触发及早压缩")
        self.assertTrue(report.tokens_before > report.tokens_after)
        self.assertEqual(runtime.reducer.metrics.context_tokens, report.tokens_after)

    async def test_eager_compact_noop_when_within_budget(self) -> None:
        """若历史未超出新模型高水位线，切换模型时不触发压缩。"""
        runtime = self._runtime(model="deepseek-flash")
        # 仅注入很短的历史（2 轮短文本）
        runtime.kernel.history = make_history(2, chars=10)

        # 切换到 128k 模型
        runtime._dynamic_model_windows["medium-model"] = 128_000
        runtime.apply_model("medium-model")

        report = await runtime.eager_compact_if_needed()
        self.assertIsNone(report, "未超过水位线时不应触发冗余压缩")

    async def test_scale_up_exemption_skips_compaction_d193(self) -> None:
        """D193: 从 32k 扩容至 1048k，且当前有效 token 处于安全区，直接豁免及早压缩。"""
        runtime = self._runtime(model="deepseek-flash")
        # 初始设置为 32k 窗口
        runtime._dynamic_model_windows["small-32k"] = 32_768
        runtime.apply_model("small-32k")
        self.assertEqual(runtime.context_builder.window_capacity, 32_768)

        # 模拟当前已有一段历史，且在状态栏中记录当前活跃 token 为 30,515
        runtime.kernel.history = make_history(15, chars=100)
        runtime.reducer.metrics.context_tokens = 30_515

        # 扩容切换至 1048k 模型（如 deepseek-flash）
        runtime._dynamic_model_windows["large-1048k"] = 1_048_576
        runtime.apply_model("large-1048k")
        self.assertEqual(runtime._previous_window, 32_768)
        self.assertEqual(runtime.context_builder.window_capacity, 1_048_576)

        # 判定及早压缩：30,515 远低于 1048k 的高水位线（约 101.5 万），触发扩容安全豁免
        report = await runtime.eager_compact_if_needed()
        self.assertIsNone(report, "安全扩容场景下应触发 D193 豁免，绝不调用及早压缩")

    async def test_safe_scale_down_skips_compaction_d193(self) -> None:
        """D193: 从 1048k 缩容至 32k，但当前有效 token 仅 5,000，未超过新高水位线，安全放行。"""
        runtime = self._runtime(model="deepseek-flash")
        runtime._dynamic_model_windows["large-1048k"] = 1_048_576
        runtime.apply_model("large-1048k")

        # 模拟当前有效 token 为 5,000
        runtime.kernel.history = make_history(5, chars=50)
        runtime.reducer.metrics.context_tokens = 5_000

        # 缩容至 32k
        runtime._dynamic_model_windows["small-32k"] = 32_768
        runtime.apply_model("small-32k")
        self.assertEqual(runtime._previous_window, 1_048_576)
        self.assertEqual(runtime.context_builder.window_capacity, 32_768)

        report = await runtime.eager_compact_if_needed()
        self.assertIsNone(report, "缩容但有效 token 未超标时，应安全放行而不触发压缩")

    def test_compactor_degraded_state_reset_d193(self) -> None:
        """D193: Compactor 每次执行压缩前强制重置 _last_degraded，防止历史降级标志向后泄漏。"""
        from logox.context.compaction import Compactor

        compactor = Compactor(window_capacity=32_768)
        compactor._last_degraded = True
        compactor._last_folded_turns = 5

        # 传入未超标简短消息
        messages = [
            Message(role="user", blocks=[TextBlock(text="hi")]),
            Message(role="assistant", blocks=[TextBlock(text="hello")]),
        ]
        res = compactor.compact(messages, force=False)
        self.assertFalse(compactor._last_degraded)
        self.assertEqual(compactor._last_folded_turns, 0)
        self.assertFalse(res.degraded)
