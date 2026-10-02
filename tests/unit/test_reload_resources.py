"""`/reload` 的装配根侧用例（MODULE_08 §5.2）。

这里测的是**世界发生了什么**（记忆对象换了没有、配置被替换了没有），
不是屏幕上出现了什么 —— 提示文案与拒绝执行由 `tests/tui/test_render_commands.py`
的 `ReloadCommandTests` 覆盖。两类合起来才是完整的一条链。

为什么用真 `build_runtime`：`reload_resources` 的全部价值都在"**它认识哪几个对象**"
（builder / 技能包 / 模板命令 / 配置）。用一个自己拼的假 Runtime 测它，
就恰好绕过了唯一会出错的地方 —— 忘了重扫某一个。
"""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace

from logox.app import Runtime, build_runtime
from logox.config.schema import LogoxConfig, ProviderConfig
from logox.paths import LogoxPaths
from tests.unit.support import make_temp_dir, remove_temp_dir


class ReloadResourcesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = make_temp_dir("reload-")
        self.addCleanup(remove_temp_dir, self.root)
        self.paths = LogoxPaths.at(self.root)
        self.cwd = Path(self.root)

    def _runtime(self) -> Runtime:
        config = LogoxConfig(
            provider=ProviderConfig(name="openai-compatible", model="gpt-4o"),
        )
        bundle = SimpleNamespace(config=config, sources=[])
        return build_runtime(bundle, self.cwd, self.paths)

    def _memory_file(self, text: str) -> Path:
        path = self.cwd / "AGENTS.md"
        path.write_text(text, encoding="utf-8")
        return path

    # ------------------------------------------------------------------ #

    def test_t01_rereads_project_memory_from_disk(self) -> None:
        """① 改 AGENTS.md 后重载，构建器看到的记忆**内容真的变了**。

        这是 `/reload` 存在的理由：此前它只在 `builder.__init__` 里扫一次，
        改完必须重启进程。
        """
        self._memory_file("# 记忆 v1\n\n- 规则甲\n")
        runtime = self._runtime()
        builder = runtime.context_builder

        self.assertIn("规则甲", builder.memory.render_system_prompt_block())

        self._memory_file("# 记忆 v2\n\n- 规则乙\n")
        report = runtime.reload_resources()

        self.assertIn("规则乙", builder.memory.render_system_prompt_block())
        self.assertNotIn("规则甲", builder.memory.render_system_prompt_block())

        memory_item = next(item for item in report.items if item.name == "项目记忆")
        self.assertEqual(memory_item.error, "")
        self.assertIn("AGENTS.md", memory_item.detail)
        self.assertEqual(report.failures, [])

    def test_t02_prefix_changes_only_when_memory_changes(self) -> None:
        """② 前缀代价只在真的变了时才报（否则每次 /reload 都喊"要花钱"= 噪音）。"""
        self._memory_file("# 记忆 v1\n")
        runtime = self._runtime()

        unchanged = runtime.reload_resources()
        self.assertFalse(unchanged.prefix_changed, "文件没改，不该说前缀失效了")
        self.assertEqual(unchanged.system_tokens_before, unchanged.system_tokens_after)
        self.assertGreater(unchanged.system_tokens_before, 0)

        self._memory_file("# 记忆 v1 改过了\n\n- 新增一条更长的规则，用来把 token 数拉开\n")
        changed = runtime.reload_resources()
        self.assertTrue(changed.prefix_changed)
        self.assertNotEqual(changed.system_tokens_before, changed.system_tokens_after)

    def test_t03_one_failure_does_not_stop_the_others(self) -> None:
        """④ 每项独立 try/except：技能包炸了，记忆照样要刷。"""
        self._memory_file("# 记忆 v1\n")
        runtime = self._runtime()

        class Exploding:
            def reload(self) -> None:
                raise PermissionError("拒绝访问")

            def list_skills(self) -> list[object]:
                return []

            def build_prompt_index(self) -> str:
                return ""

        runtime.skill_manager = Exploding()
        report = runtime.reload_resources()

        self.assertIn("PermissionError", next(i for i in report.items if i.name == "技能包").error)
        memory_item = next(item for item in report.items if item.name == "项目记忆")
        self.assertEqual(memory_item.error, "", "另一项不该被上游失败带走")
        self.assertEqual(len(report.failures), 1)

    def test_t04_broken_config_is_reported_but_not_applied(self) -> None:
        """⑤ 配置只校验不替换：坏配置要报出来，运行中的 config **一个字节都不动**。"""
        runtime = self._runtime()
        before = runtime.config

        self.paths.config.write_text("[provider\nname = 这行没有闭合\n", encoding="utf-8")
        report = runtime.reload_resources()

        config_item = next(item for item in report.items if item.name == "配置")
        self.assertNotEqual(config_item.error, "", "坏配置必须报出来")
        self.assertIn("config.toml", config_item.error)

        self.assertIs(runtime.config, before, "运行中的配置不该被半替换（会造成两份事实）")
        self.assertEqual(runtime.provider_name, before.provider.name)

    def test_t05_says_what_still_needs_a_restart(self) -> None:
        """⑥ 诚实清单：不重载的东西必须列出来，其中最要紧的是"代码"。"""
        runtime = self._runtime()
        report = runtime.reload_resources()

        joined = " / ".join(report.not_reloaded)
        self.assertIn("Python 代码", joined)
        self.assertIn("MCP", joined)

    def test_t06_missing_builder_is_explained_not_silently_skipped(self) -> None:
        """构建器没接上空着时报"没有接上下文构建器"，而不是假装重载成功。"""
        runtime = self._runtime()
        runtime.context_builder = None

        report = runtime.reload_resources()

        memory_item = next(item for item in report.items if item.name == "项目记忆")
        self.assertIn("没有接上下文构建器", memory_item.error)
        # 其他项不受影响：技能包与模板命令是独立对象
        self.assertEqual(next(i for i in report.items if i.name == "模板命令").error, "")


class SystemPromptSnapshotTests(unittest.TestCase):
    """`system_prompt_snapshot()` 是公开的只读快照（装配根不去碰私有名）。"""

    def setUp(self) -> None:
        self.root = make_temp_dir("reload-snap-")
        self.addCleanup(remove_temp_dir, self.root)

    def test_snapshot_is_pure_and_matches_the_private_assembler(self) -> None:
        from logox.context.builder import HierarchicalContextBuilder
        from logox.context.storage import SessionTranscriptWriter

        builder = HierarchicalContextBuilder(
            system="你是助手",
            cwd=self.root,
            transcript_writer=SessionTranscriptWriter(log_file=Path(self.root) / "t.jsonl"),
        )
        first = builder.system_prompt_snapshot()
        second = builder.system_prompt_snapshot()

        self.assertEqual(first, second, "快照必须是纯函数")
        self.assertEqual(first, builder._assemble_system_prompt())  # noqa: SLF001 - 就是在盯这条等价


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
