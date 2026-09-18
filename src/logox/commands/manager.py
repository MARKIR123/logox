"""L1 提示词模板命令管理器（M10 / D3 / D112）。

设计原则：
1. 零代码门槛：用户仅需在 `.logox/commands/<name>.md` 放置 Markdown 文件，即可拥有专属斜杠命令；
2. 零外部依赖：纯 Python 解析头部 Frontmatter，绝不引入 PyYAML 膨胀依赖树；
3. 参数安全插值：支持 `{{args}}` 与 `$ARGUMENTS` 占位符替换，杜绝代码注入。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from logox.commands.models import CommandTemplate

logger = logging.getLogger("logox.commands")


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """解析 Markdown 文件开头的 YAML Frontmatter。

    返回 (metadata_dict, body_text)。
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text

    metadata: dict[str, str] = {}
    closed = False
    body_start = len(lines)
    for i in range(1, len(lines)):
        line = lines[i].strip()
        if line == "---":
            body_start = i + 1
            closed = True
            break
        if ":" in line:
            key, val = line.split(":", 1)
            clean_key = key.strip()
            clean_val = val.strip().strip("'\"")
            metadata[clean_key] = clean_val

    if not closed:
        return {}, text

    body = "\n".join(lines[body_start:]).strip()
    return metadata, body



class CommandManager:
    """L1 模板命令发现与渲染管理器。"""

    def __init__(
        self,
        cwd: Path | str,
        user_dir: Path | str | None = None,
    ) -> None:
        self.cwd = Path(cwd).resolve()
        self.user_dir = Path(user_dir).resolve() if user_dir else Path.home() / ".logox"
        self._commands: dict[str, CommandTemplate] = {}
        self.reload()

    def reload(self) -> None:
        """重新扫描并载入所有模板命令。"""
        self._commands.clear()

        # 1. 扫描用户全局目录 ~/.logox/commands/*.md
        user_cmd_dir = self.user_dir / "commands"
        if user_cmd_dir.is_dir():
            for f in sorted(user_cmd_dir.glob("*.md")):
                self._load_file(f, scope="global")

        # 2. 扫描项目目录 <cwd>/.logox/commands/*.md（近者优先，覆盖全局同名命令）
        proj_cmd_dir = self.cwd / ".logox" / "commands"
        if proj_cmd_dir.is_dir():
            for f in sorted(proj_cmd_dir.glob("*.md")):
                self._load_file(f, scope="project")

    def _load_file(self, path: Path, scope: str) -> None:
        """读取单个命令文件并注册。"""
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
            meta, body = parse_frontmatter(content)

            cmd_name = path.stem.lower()
            description = meta.get("description", "")
            if not description:
                # 尝试从正文首行提取摘要
                for line in body.splitlines():
                    clean_line = line.strip().lstrip("#").strip()
                    if clean_line:
                        description = clean_line[:60]
                        break
            if not description:
                description = f"自定义命令 /{cmd_name}"

            template = body or content

            self._commands[cmd_name] = CommandTemplate(
                name=cmd_name,
                description=description,
                template=template,
                path=path,
                scope=scope,  # type: ignore[arg-type]
            )
            logger.debug("已载入模板命令: /%s (%s)", cmd_name, path)
        except Exception as exc:
            logger.warning("解析命令模板文件失败: %s, 错误: %s", path, exc)

    def list_commands(self) -> list[CommandTemplate]:
        """返回已载入的所有模板命令列表。"""
        return sorted(self._commands.values(), key=lambda c: c.name)

    def get_command(self, name: str) -> CommandTemplate | None:
        """根据名称获取模板命令。"""
        clean_name = name.lstrip("/").lower().strip()
        return self._commands.get(clean_name)

    def render(self, name: str, args: str = "") -> str | None:
        """渲染指定命令的提示词模板。

        替换规则：
        - `{{args}}` / `{{ args }}` -> 用户传入的参数字符串；
        - `$ARGUMENTS` / `$args` -> 用户传入的参数字符串。
        """
        cmd = self.get_command(name)
        if not cmd:
            return None

        clean_args = args.strip()
        rendered = cmd.template

        # 占位符安全正则替换
        rendered = re.sub(r"\{\{\s*args\s*\}\}", clean_args, rendered, flags=re.IGNORECASE)
        rendered = re.sub(r"\$ARGUMENTS|\$args\b", clean_args, rendered)

        return rendered.strip()
