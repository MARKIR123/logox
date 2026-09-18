"""大模型技能包（Skills）管理器（M10 / D113）。

设计原则：
1. 两阶段渐进式披露（Progressive Disclosure）：系统提示词只注入单行元数据（~50 Token），模型按需阅读详细 SOP；
2. 零外部依赖：纯 Python 提取头部 Frontmatter，容错性强；
3. 动态按需加载：无技能时返回空，不增加任何 Prompt 负担。
"""

from __future__ import annotations

import logging
from pathlib import Path

from logox.commands.manager import parse_frontmatter
from logox.skills.models import SkillMeta

logger = logging.getLogger("logox.skills")


class SkillManager:
    """技能包管理器。"""

    def __init__(
        self,
        cwd: Path | str,
        user_dir: Path | str | None = None,
    ) -> None:
        self.cwd = Path(cwd).resolve()
        self.user_dir = Path(user_dir).resolve() if user_dir else Path.home() / ".logox"
        self._skills: dict[str, SkillMeta] = {}
        self.reload()

    def reload(self) -> None:
        """扫描并重载所有技能包。"""
        self._skills.clear()

        # 1. 扫描全局技能 ~/.logox/skills/*/SKILL.md
        user_skills_dir = self.user_dir / "skills"
        if user_skills_dir.is_dir():
            for folder in sorted(user_skills_dir.iterdir()):
                if folder.is_dir():
                    self._load_skill_dir(folder, scope="global")

        # 2. 扫描项目级技能 <cwd>/.logox/skills/*/SKILL.md（近者优先）
        proj_skills_dir = self.cwd / ".logox" / "skills"
        if proj_skills_dir.is_dir():
            for folder in sorted(proj_skills_dir.iterdir()):
                if folder.is_dir():
                    self._load_skill_dir(folder, scope="project")

    def _load_skill_dir(self, folder: Path, scope: str) -> None:
        """加载单个技能目录。"""
        # 寻找 SKILL.md 或 skill.md
        skill_file = folder / "SKILL.md"
        if not skill_file.is_file():
            skill_file = folder / "skill.md"
        if not skill_file.is_file():
            return

        try:
            content = skill_file.read_text(encoding="utf-8", errors="replace")
            meta, body = parse_frontmatter(content)

            skill_name = meta.get("name", "").strip() or folder.name
            clean_name = skill_name.lower().strip()

            description = meta.get("description", "").strip()
            if not description:
                # 尝试从正文首行提取
                for line in body.splitlines():
                    clean_l = line.strip().lstrip("#").strip()
                    if clean_l:
                        description = clean_l[:80]
                        break
            if not description:
                description = f"专业技能 {skill_name}"

            self._skills[clean_name] = SkillMeta(
                name=skill_name,
                description=description,
                path=skill_file,
                skill_dir=folder,
                scope=scope,  # type: ignore[arg-type]
            )
            logger.debug("已注册技能: %s (%s)", skill_name, skill_file)
        except Exception as exc:
            logger.warning("解析技能目录失败: %s, 错误: %s", folder, exc)

    def list_skills(self) -> list[SkillMeta]:
        """返回已发现的所有技能元数据列表。"""
        return sorted(self._skills.values(), key=lambda s: s.name.lower())

    def get_skill(self, name: str) -> SkillMeta | None:
        """获取指定名称的技能元数据。"""
        return self._skills.get(name.lower().strip())

    def read_skill_content(self, name: str) -> str | None:
        """读取指定技能的完整正文内容。"""
        meta = self.get_skill(name)
        if not meta or not meta.path.is_file():
            return None
        return meta.path.read_text(encoding="utf-8", errors="replace")

    def build_prompt_index(self) -> str:
        """生成供大模型系统提示词消费的紧凑渐进式索引摘要。

        若无技能，返回空字符串；
        若有技能，每条格式化为：
        - <name>: <description> (指引文件: <relative_or_abs_path>)
        """
        if not self._skills:
            return ""

        lines = [
            "## 可用专业技能 (Available Skills)",
            "当你面对特定领域的专业任务时，请根据文件路径查阅对应的技能说明书（可直接使用内置 read 工具阅读）：",
        ]
        for skill in self.list_skills():
            try:
                rel_path = skill.path.relative_to(self.cwd)
                path_str = str(rel_path).replace("\\", "/")
            except ValueError:
                path_str = str(skill.path).replace("\\", "/")
            lines.append(f"- **{skill.name}**: {skill.description} (文件: `{path_str}`)")

        lines.append("")
        return "\n".join(lines)
