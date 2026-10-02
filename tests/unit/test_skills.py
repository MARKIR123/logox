"""tests/unit/test_skills.py - 大模型技能包（Skills）管理器单元测试。

测试覆盖：
1. 技能包目录扫描与发现（SKILL.md / skill.md、项目级与用户全局级、优先级覆盖）；
2. Frontmatter 元数据提取与兜底回退机制（提取 name/description，无元数据时从文件夹名及正文首行回退）；
3. 完整正文读取（read_skill_content 按需索取）；
4. 两阶段渐进式披露索引构建（build_prompt_index 紧凑输出、空技能无害）；
5. 上下文构建器集成（HierarchicalContextBuilder 组装系统提示词）。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from logox.context.builder import HierarchicalContextBuilder
from logox.context.storage import SessionTranscriptWriter
from logox.skills.manager import SkillManager


class SkillManagerTests(unittest.TestCase):
    """技能包管理器核心逻辑测试。"""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.cwd = Path(self.tmp_dir.name).resolve()
        self.user_dir = self.cwd / "user_home"
        self.user_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_discover_skills_and_precedence(self) -> None:
        """测试技能包发现与优先级（项目级覆盖全局同名技能）。"""
        proj_skills_dir = self.cwd / ".logox" / "skills"
        user_skills_dir = self.user_dir / "skills"
        proj_skills_dir.mkdir(parents=True, exist_ok=True)
        user_skills_dir.mkdir(parents=True, exist_ok=True)

        # 1. 全局技能 git-workflow
        user_git = user_skills_dir / "git-workflow"
        user_git.mkdir()
        (user_git / "SKILL.md").write_text(
            "---\nname: git-workflow\ndescription: Global git workflow\n---\nGlobal SOP",
            encoding="utf-8",
        )

        # 2. 项目级技能 git-workflow（覆盖全局）
        proj_git = proj_skills_dir / "git-workflow"
        proj_git.mkdir()
        (proj_git / "SKILL.md").write_text(
            "---\nname: git-workflow\ndescription: Project git workflow\n---\nProject SOP",
            encoding="utf-8",
        )

        # 3. 项目级小写 skill.md 格式的 python-audit
        proj_py = proj_skills_dir / "python-audit"
        proj_py.mkdir()
        (proj_py / "skill.md").write_text(
            "---\nname: python-audit\ndescription: Python code audit SOP\n---\nAudit SOP",
            encoding="utf-8",
        )

        # 4. 无效目录（无 SKILL.md）
        empty_dir = proj_skills_dir / "empty-dir"
        empty_dir.mkdir()

        mgr = SkillManager(cwd=self.cwd, user_dir=self.user_dir)
        skills = mgr.list_skills()
        skill_dict = {s.name.lower(): s for s in skills}

        self.assertEqual(len(skills), 2)
        self.assertIn("git-workflow", skill_dict)
        self.assertIn("python-audit", skill_dict)

        # 验证覆盖与作用域
        git_skill = skill_dict["git-workflow"]
        self.assertEqual(git_skill.description, "Project git workflow")
        self.assertEqual(git_skill.scope, "project")

    def test_fallback_name_and_description(self) -> None:
        """无 frontmatter 时从目录名和正文首行自动兜底。"""
        proj_skills_dir = self.cwd / ".logox" / "skills" / "docker-expert"
        proj_skills_dir.mkdir(parents=True, exist_ok=True)
        (proj_skills_dir / "SKILL.md").write_text(
            "# Docker deployment expert guide\nDetailed steps for dockerfile optimization.\n",
            encoding="utf-8",
        )

        mgr = SkillManager(cwd=self.cwd, user_dir=self.user_dir)
        skill = mgr.get_skill("docker-expert")

        self.assertIsNotNone(skill)
        self.assertEqual(skill.name, "docker-expert")
        self.assertEqual(skill.description, "Docker deployment expert guide")

    def test_read_skill_content(self) -> None:
        """按需读取技能的完整正文。"""
        proj_skills_dir = self.cwd / ".logox" / "skills" / "refactor"
        proj_skills_dir.mkdir(parents=True, exist_ok=True)
        raw_text = "---\nname: refactor\n---\n## Refactoring Rules\n1. Always test first."
        (proj_skills_dir / "SKILL.md").write_text(raw_text, encoding="utf-8")

        mgr = SkillManager(cwd=self.cwd, user_dir=self.user_dir)
        content = mgr.read_skill_content("refactor")
        none_content = mgr.read_skill_content("unknown")

        self.assertEqual(content, raw_text)
        self.assertIsNone(none_content)

    def test_build_prompt_index_empty(self) -> None:
        """无技能时 build_prompt_index 返回空字符串，不产生任何 Token 损耗。"""
        mgr = SkillManager(cwd=self.cwd, user_dir=self.user_dir)
        self.assertEqual(mgr.build_prompt_index(), "")

    def test_build_prompt_index_compact_disclosure(self) -> None:
        """有技能时生成紧凑渐进式元数据列表。"""
        proj_skills_dir = self.cwd / ".logox" / "skills" / "test-runner"
        proj_skills_dir.mkdir(parents=True, exist_ok=True)
        (proj_skills_dir / "SKILL.md").write_text(
            "---\nname: test-runner\ndescription: Run automated tests safely\n---\nSOP",
            encoding="utf-8",
        )

        mgr = SkillManager(cwd=self.cwd, user_dir=self.user_dir)
        index_prompt = mgr.build_prompt_index()

        self.assertIn("## 可用专业技能 (Available Skills)", index_prompt)
        self.assertIn("- **test-runner**: Run automated tests safely", index_prompt)
        self.assertIn(".logox/skills/test-runner/SKILL.md", index_prompt)

    def test_hierarchical_context_builder_integration(self) -> None:
        """测试将 SkillManager 注入 HierarchicalContextBuilder 系统提示词构建。"""
        proj_skills_dir = self.cwd / ".logox" / "skills" / "sec-audit"
        proj_skills_dir.mkdir(parents=True, exist_ok=True)
        (proj_skills_dir / "SKILL.md").write_text(
            "---\nname: sec-audit\ndescription: Security vulnerability auditing\n---\nRules",
            encoding="utf-8",
        )

        mgr = SkillManager(cwd=self.cwd, user_dir=self.user_dir)
        builder = HierarchicalContextBuilder(
            system="You are a helpful assistant.",
            cwd=self.cwd,
            skill_manager=mgr,
            # D153：writer 必填；落在本用例自己的临时目录里，不碰仓库
            transcript_writer=SessionTranscriptWriter(
                base_dir=self.cwd / "sessions", session_id="skills"
            ),
        )

        bundle = builder.build([])
        self.assertIn("You are a helpful assistant.", bundle.system)
        self.assertIn("## 可用专业技能 (Available Skills)", bundle.system)
        self.assertIn("sec-audit", bundle.system)



if __name__ == "__main__":
    unittest.main()
