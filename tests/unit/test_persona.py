"""用户级人设（`~/.logox/LOGOX.md`）接入的守卫用例。

这一组防的是**已经真实发生过的那类回归**：文件写好了、脚本说会同步、文档说它是
"记忆文件"，但**没有任何读取方** ⇒ 用户改了它却毫无反应。

覆盖四层：
1. `PersonaMemory` 的边界（缺失／空白／超大／符号链接）；
2. 装配顺序（人设是 system 第一段，且无文件时与改动前逐字一致）；
3. 来源可见（`memory_source_paths` 要能说出它）；
4. 两条装配路径（`app.build_runtime` 与 `--chat` 的 `_context_params`）都没漏接线。
"""

from __future__ import annotations

import inspect
import unittest
from pathlib import Path
from types import SimpleNamespace

from logox.app import build_runtime
from logox.config.schema import ContextConfig, LogoxConfig, ProviderConfig
from logox.context.builder import HierarchicalContextBuilder
from logox.context.persona import MAX_PERSONA_BYTES, PersonaMemory
from logox.context.storage import SessionTranscriptWriter
from logox.paths import LogoxPaths
from tests.unit.support import make_temp_dir, remove_temp_dir

MARKER = "言出于理，行成于证，事毕于明"


def _builder(cwd: Path, *, persona: Path | None, **kwargs) -> HierarchicalContextBuilder:
    return HierarchicalContextBuilder(
        system="内置基础人设",
        cwd=cwd,
        session_id="persona",
        transcript_writer=SessionTranscriptWriter(base_dir=cwd / "s", session_id="persona"),
        persona_path=persona,
        **kwargs,
    )


class PersonaMemoryEdgeTests(unittest.TestCase):
    """① 边界：读不进来时必须是"整份跳过 + 说得出原因"，不是半截注入。"""

    def setUp(self) -> None:
        self.tmp = make_temp_dir("persona-")
        self.addCleanup(remove_temp_dir, self.tmp)

    def test_t01_missing_file_is_normal_not_an_error(self) -> None:
        persona = PersonaMemory(self.tmp / "LOGOX.md")
        self.assertEqual(persona.block, "")
        self.assertFalse(persona.loaded)
        self.assertEqual(persona.skipped, "", "文件不存在是正常情况，不该报成错误")

    def test_t02_blank_file_behaves_like_missing(self) -> None:
        path = self.tmp / "LOGOX.md"
        path.write_text("   \n\n\t\n", encoding="utf-8")
        persona = PersonaMemory(path)
        self.assertFalse(persona.loaded)
        self.assertEqual(persona.skipped, "")

    def test_t03_oversized_is_skipped_whole(self) -> None:
        path = self.tmp / "LOGOX.md"
        path.write_text("A" * (MAX_PERSONA_BYTES + 1), encoding="utf-8")
        persona = PersonaMemory(path)
        self.assertFalse(persona.loaded, "超限必须整份跳过（截断会得到半截自相矛盾的规则）")
        self.assertIn("64KiB", persona.skipped)

    def test_t04_undecodable_is_skipped(self) -> None:
        path = self.tmp / "LOGOX.md"
        path.write_bytes(b"\xff\xfe\x00bad utf8 \x80")
        persona = PersonaMemory(path)
        self.assertFalse(persona.loaded)
        self.assertIn("读取失败", persona.skipped)

    def test_t05_symlink_is_skipped(self) -> None:
        real = self.tmp / "real.md"
        real.write_text(MARKER, encoding="utf-8")
        link = self.tmp / "LOGOX.md"
        try:
            link.symlink_to(real)
        except (OSError, NotImplementedError) as exc:  # Windows 需要开发者模式
            self.skipTest(f"当前环境无法创建符号链接：{exc}")
        persona = PersonaMemory(link)
        self.assertFalse(persona.loaded)
        self.assertIn("符号链接", persona.skipped)

    def test_t06_refresh_picks_up_new_content(self) -> None:
        path = self.tmp / "LOGOX.md"
        path.write_text("第一版人设", encoding="utf-8")
        persona = PersonaMemory(path)
        self.assertEqual(persona.block, "第一版人设")
        path.write_text("第二版人设", encoding="utf-8")
        persona.load()
        self.assertEqual(persona.block, "第二版人设", "重读必须拿到新内容（/reload 依赖它）")


class PersonaAssemblyTests(unittest.TestCase):
    """② 装配顺序与回落。"""

    def setUp(self) -> None:
        self.tmp = make_temp_dir("persona-asm-")
        self.addCleanup(remove_temp_dir, self.tmp)
        (self.tmp / ".git").mkdir()  # 让项目记忆的攀爬在此停住，不读到仓库自身
        (self.tmp / "AGENTS.md").write_text("# 项目规则\nPROJECT-RULE", encoding="utf-8")
        # ⚠️ 人设刻意放**子目录** `home/`：项目记忆的发现规则会看 `<dir>/LOGOX.md`
        #    和 `<dir>/.logox/LOGOX.md`，若把夹具放在 cwd 链上，量到的就不是"人设段"
        #    而是"项目记忆"，用例会为了错误的原因变绿（本文件第一次就是这么写的）。
        (self.tmp / "home").mkdir()
        self.persona = self.tmp / "home" / "LOGOX.md"
        self.content = "# Logox\n" + MARKER
        self.persona.write_text(self.content, encoding="utf-8")

    def test_t07_persona_is_the_first_segment(self) -> None:
        snap = _builder(self.tmp, persona=self.persona).system_prompt_snapshot()
        self.assertIn(MARKER, snap)
        self.assertIn("PROJECT-RULE", snap)
        self.assertLess(
            snap.index(MARKER),
            snap.index("PROJECT-RULE"),
            "人设应在项目规范之前（身份先立，规则随后）",
        )
        self.assertLess(snap.index(MARKER), snap.index("内置基础人设"))

    def test_t08_missing_file_keeps_todays_system_verbatim(self) -> None:
        with_persona = _builder(self.tmp, persona=self.persona).system_prompt_snapshot()
        without = _builder(self.tmp, persona=None).system_prompt_snapshot()
        self.assertNotIn(MARKER, without)
        self.assertIn("内置基础人设", without, "没有文件时必须回落内置人设，而不是变成空")
        self.assertEqual(
            with_persona,
            self.content + "\n\n" + without,
            "有文件时应当只是**在原有 system 前面多一段**，其余部分逐字不变",
        )

    def test_t09_oversized_file_does_not_reach_the_system(self) -> None:
        self.persona.write_text("B" * (MAX_PERSONA_BYTES + 1), encoding="utf-8")
        builder = _builder(self.tmp, persona=self.persona)
        self.assertNotIn("B" * 100, builder.system_prompt_snapshot())
        self.assertIn("64KiB", builder.refresh_persona().skipped)

    def test_t10_project_memory_switch_does_not_disable_persona(self) -> None:
        builder = _builder(self.tmp, persona=self.persona, project_memory_enabled=False)
        snap = builder.system_prompt_snapshot()
        self.assertIn(MARKER, snap, "project_memory_enabled 管的是项目规范，不该连人设一起关掉")
        self.assertNotIn("PROJECT-RULE", snap)

    def test_t11_dedup_when_cwd_is_also_the_persona_home(self) -> None:
        """cwd 恰为 home 时，同一份文件会被两条规则发现 ⇒ 只能出现一次。"""
        home = make_temp_dir("persona-home-")
        self.addCleanup(remove_temp_dir, home)
        (home / ".git").mkdir()
        (home / ".logox").mkdir()
        target = home / ".logox" / "LOGOX.md"
        target.write_text("# Logox\n" + MARKER, encoding="utf-8")

        builder = _builder(home, persona=target)
        self.assertTrue(
            any(str(s.path) == str(target) for s in builder.memory.sources),
            "前置条件：这份文件应当也能被项目记忆发现（否则本用例没有意义）",
        )
        snap = builder.system_prompt_snapshot()
        self.assertEqual(snap.count(MARKER), 1, "同一段人设不得在 system 里出现两次")

    def test_t12_reload_refreshes_persona_and_sources(self) -> None:
        builder = _builder(self.tmp, persona=self.persona)
        before = builder.memory_source_paths()
        self.assertEqual(before[0], str(self.persona).replace("\\", "/"), "人设应排在来源列表首位")

        self.persona.write_text("# Logox\n新版人设 NEW-PERSONA", encoding="utf-8")
        persona = builder.refresh_persona()
        self.assertIn("NEW-PERSONA", persona.block)
        self.assertIn("NEW-PERSONA", builder.system_prompt_snapshot(), "/reload 后必须生效")

        self.persona.unlink()
        builder.refresh_persona()
        self.assertNotIn(str(self.persona).replace("\\", "/"), builder.memory_source_paths())

class PersonaWiringTests(unittest.TestCase):
    """③ 两条装配路径都不能漏（F-07 的漂移教训）。

    这里刻意让 **项目目录 `root` 与用户级 home `root/home` 分开**，与生产一致
    （生产里 cwd 是仓库、home 是 ``~/.logox``）。若两者合一，量到的会是项目记忆。
    """

    def setUp(self) -> None:
        self.root = make_temp_dir("persona-wire-")
        self.addCleanup(remove_temp_dir, self.root)
        (self.root / ".git").mkdir()
        self.paths = LogoxPaths.at(self.root / "home")
        self.paths.memory.parent.mkdir(parents=True, exist_ok=True)

    def _runtime(self):
        config = LogoxConfig(
            provider=ProviderConfig(name="openai-compatible", model="gpt-4o"),
            context=ContextConfig(),
        )
        return build_runtime(SimpleNamespace(config=config, sources=[]), self.root, self.paths)

    def test_t13_app_runtime_injects_the_user_persona(self) -> None:
        self.paths.memory.write_text("# Logox\n" + MARKER, encoding="utf-8")
        runtime = self._runtime()
        builder = runtime.kernel._builder  # noqa: SLF001 - 见 test_context_config_wiring
        self.assertEqual(builder.persona.path, self.paths.memory, "paths.memory 没接线")
        self.assertIn(MARKER, builder.system_prompt_snapshot())

    def test_t14_reload_reports_the_persona_item(self) -> None:
        self.paths.memory.write_text("# Logox\n" + MARKER, encoding="utf-8")
        report = self._runtime().reload_resources()
        items = {item.name: item for item in report.items}
        self.assertIn("人设", items, "`/reload` 必须能说出人设的状态")
        self.assertIn("LOGOX.md", items["人设"].detail)
        self.assertEqual(items["人设"].error, "")

    def test_t15_reload_surfaces_a_broken_persona(self) -> None:
        self.paths.memory.write_text("C" * (MAX_PERSONA_BYTES + 1), encoding="utf-8")
        report = self._runtime().reload_resources()
        items = {item.name: item for item in report.items}
        self.assertIn("64KiB", items["人设"].error, "读不进来时用户要能看到原因")

    def test_t16_chat_path_mapping_carries_persona_path(self) -> None:
        from logox.cli import _context_params

        config = LogoxConfig(
            provider=ProviderConfig(name="openai-compatible", model="gpt-4o"),
            context=ContextConfig(),
        )
        params = _context_params(SimpleNamespace(config=config), self.paths)
        self.assertEqual(params["persona_path"], self.paths.memory)
        signature = inspect.signature(HierarchicalContextBuilder.__init__).parameters
        self.assertIn("persona_path", signature, "映射出来的关键字必须被 builder 接受")

    def test_t17_session_start_lists_the_persona(self) -> None:
        from logox.app import build_session_start

        self.paths.memory.write_text("# Logox\n" + MARKER, encoding="utf-8")
        event = build_session_start(self._runtime(), terminal_kind="wt")
        self.assertIn(
            str(self.paths.memory).replace("\\", "/"),
            event.memory_sources,
            "会话开始要能看出人设已经生效（否则用户只能靠猜）",
        )


if __name__ == "__main__":
    unittest.main()
