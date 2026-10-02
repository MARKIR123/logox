"""`[context]` 配置接线的守卫用例（D157）。

这一组防的是**同一类回归**：配置字段写好了、文档写了、用户也填了，但**装配时没传** ⇒
"设了没反应"（这次就是连续两个问题一起暴露：`app.py` 与 `cli.py` 两条装配路径都没接线）。

四类断言：
1. **端到端接线**：`build_runtime`（TUI 装配根）真的把配置传到了 compactor；
2. **第二条装配路径**：`--chat` 用的 `_context_params()` 映射与配置一致（F-07 的漂移教训）；
3. **结构性防漂移**：`ContextConfig` 的每个字段名都被 builder 接受（改字段名会立刻红）；
4. **失效字段不得复活**：三个与实现不符的字段（`compact_threshold` / `reasoning_in_context` /
   `max_tool_result_chars`）必须保持删除状态。
"""

from __future__ import annotations

import inspect
import unittest
from pathlib import Path
from types import SimpleNamespace

from logox.app import build_runtime
from logox.config.schema import ContextConfig, LogoxConfig, ProviderConfig
from logox.context.builder import HierarchicalContextBuilder
from logox.context.storage import SessionTranscriptWriter
from logox.paths import LogoxPaths
from tests.unit.support import make_temp_dir, remove_temp_dir

#: 配置字段 → builder 参数名不一致的地方（哨兵/改名映射，必须显式登记）
FIELD_MAPPING = {
    # 配置里 0 = 不封顶（TOML 无法表达 None），接线时转成 None
    "max_budget_tokens": "max_budget_tokens",
}


class ContextConfigWiringTests(unittest.TestCase):
    """① 端到端：配置 → compactor。"""

    def setUp(self) -> None:
        self.root = make_temp_dir("ctx-wiring-")
        self.addCleanup(remove_temp_dir, self.root)
        self.paths = LogoxPaths.at(self.root)

    def _runtime(self, context: ContextConfig):
        config = LogoxConfig(
            provider=ProviderConfig(name="openai-compatible", model="gpt-4o"),
            context=context,
        )
        bundle = SimpleNamespace(config=config, sources=[])
        return build_runtime(bundle, self.root, self.paths)

    def test_t01_context_config_reaches_the_compactor(self) -> None:
        runtime = self._runtime(
            ContextConfig(
                reserve_tokens=1234,
                low_watermark_ratio=0.25,
                keep_recent_turns=7,
                keep_recent_tool_results=9,
                rehydrate_files=1,
                rehydrate_max_chars=321,
                max_budget_tokens=5000,
            )
        )
        # `Runtime` 不暴露 context_builder（内核持有它），测试直接取私有属性；
        # 这比"为了测试给生产加一个字段"更合适。
        builder = runtime.kernel._builder  # noqa: SLF001 - 见上
        compactor = builder.compactor

        self.assertEqual(compactor.reserve_tokens, 1234, "reserve_tokens 没接线")
        self.assertEqual(compactor.low_watermark, int(compactor.window_capacity * 0.25))
        self.assertEqual(compactor.keep_recent_turns, 7, "keep_recent_turns 没接线")
        self.assertEqual(compactor.keep_recent_tool_results, 9)
        self.assertEqual(compactor.rehydrate_files, 1)
        self.assertEqual(compactor.rehydrate_max_chars, 321)
        # 哨兵：配置 5000（非 0）⇒ 传给 Compactor 的是 5000
        self.assertEqual(compactor.high_watermark, min(compactor.window_capacity - 1234, 5000))

    def test_t02_zero_budget_means_no_cap(self) -> None:
        runtime = self._runtime(ContextConfig(reserve_tokens=1000, max_budget_tokens=0))
        compactor = runtime.kernel._builder.compactor  # noqa: SLF001
        self.assertEqual(
            compactor.high_watermark,
            compactor.window_capacity - 1000,
            "0 必须被理解成「不封顶」，而不是「窗口为 0」",
        )


class ChatPathMappingTests(unittest.TestCase):
    """② 第二条装配路径（`logox --chat`）不能漂移。"""

    def test_t03_chat_mapping_matches_the_config(self) -> None:
        from logox.cli import _context_params

        config = LogoxConfig(
            provider=ProviderConfig(name="openai-compatible", model="gpt-4o"),
            context=ContextConfig(
                reserve_tokens=2048,
                keep_recent_turns=3,
                project_memory_enabled=False,
                max_budget_tokens=0,
            ),
        )
        params = _context_params(SimpleNamespace(config=config))

        self.assertEqual(params["reserve_tokens"], 2048)
        self.assertEqual(params["keep_recent_turns"], 3)
        self.assertIs(params["project_memory_enabled"], False)
        self.assertIsNone(params["max_budget_tokens"], "0 应转成 None（不封顶）")

    def test_t04_chat_mapping_is_accepted_by_the_builder(self) -> None:
        """映射出来的关键字必须真的被 builder 接受（防止改名后悄悄 TypeError）。"""
        from logox.cli import _context_params

        config = LogoxConfig(
            provider=ProviderConfig(name="openai-compatible", model="gpt-4o"),
            context=ContextConfig(),
        )
        params = _context_params(SimpleNamespace(config=config))
        signature = inspect.signature(HierarchicalContextBuilder.__init__)
        for name in params:
            self.assertIn(name, signature.parameters, f"{name} 不是 builder 的参数")


class StructuralDriftTests(unittest.TestCase):
    """③ 结构性：schema 的字段名与 builder 参数名必须一一对应。"""

    def test_t05_every_context_field_is_accepted_by_the_builder(self) -> None:
        signature = inspect.signature(HierarchicalContextBuilder.__init__).parameters
        for name in ContextConfig.model_fields:
            target = FIELD_MAPPING.get(name, name)
            with self.subTest(field=name):
                self.assertIn(
                    target,
                    signature,
                    f"`[context]` 的 {name} 没有对应的 builder 参数 ⇒ 又会出现「设了没反应」",
                )

    def test_t06_removed_fields_stay_removed(self) -> None:
        """三个与实现不符的字段必须保持删除（它们的名字会让用户以为能调节某些东西）。"""
        for dead in ("compact_threshold", "reasoning_in_context", "max_tool_result_chars"):
            with self.subTest(field=dead):
                self.assertNotIn(
                    dead,
                    ContextConfig.model_fields,
                    f"{dead} 又回来了 —— 它与实现不符（见 D157），留着比没有更糟",
                )


class ProjectMemorySwitchTests(unittest.TestCase):
    """④ 行为：`project_memory_enabled=False` 必须真的不读 `LOGOX.md`。"""

    def _builder(self, cwd: Path, *, enabled: bool) -> HierarchicalContextBuilder:
        return HierarchicalContextBuilder(
            system="sys",
            cwd=cwd,
            transcript_writer=SessionTranscriptWriter(base_dir=cwd / "s", session_id="mem"),
            project_memory_enabled=enabled,
        )

    def test_t07_switch_off_means_no_memory_at_all(self) -> None:
        tmp = make_temp_dir("ctx-memory-")
        try:
            (tmp / ".git").mkdir()  # 让 find_project_memory 认定这是项目根
            (tmp / "LOGOX.md").write_text("# 项目约定\n永远先写测试。", encoding="utf-8")

            with_memory = self._builder(tmp, enabled=True)
            without = self._builder(tmp, enabled=False)

            self.assertTrue(with_memory.memory.sources, "开着开关却没读到 LOGOX.md")
            self.assertIn("永远先写测试", with_memory.build([]).system)
            self.assertEqual(without.memory.sources, [], "关掉开关却仍读到了记忆")
            self.assertNotIn("永远先写测试", without.build([]).system)
        finally:
            remove_temp_dir(tmp)


if __name__ == "__main__":
    unittest.main()
