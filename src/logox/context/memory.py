"""分层项目级长期记忆体系 (LOGOX.md)。

遵循“探针由底向上查找（Discovery: Cwd -> Root），Prompt 由顶向下组装（Assembly: Root -> Cwd）”
的黄金拓扑原则。顺从 Transformer 注意力的近因效应（Recency Bias），使最具体、最紧迫的当前工作区
规则处于 Prompt 末尾的高注意力区，并实现级联覆盖（Cascading Override）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from logox.context.tokens import estimate_text_tokens

__all__ = [
    "MAX_MEMORY_FILE_BYTES",
    "MEMORY_FILENAMES",
    "MemorySource",
    "ProjectMemory",
    "find_project_memory",
]

#: 单个记忆文件最大允许读取字节数 (64KB)，防止恶意超大文件撑爆上下文
MAX_MEMORY_FILE_BYTES = 64 * 1024


@dataclass(frozen=True)
class MemorySource:
    """单个生效的记忆源文件。"""

    path: Path
    depth: int  # 距离 Project Root 的层级深度（0 为根目录，数字越大越靠近 cwd）
    content: str
    token_count: int


@dataclass
class ProjectMemory:
    """项目级分层记忆集合。

    sources 列表保证严格按 depth 从小到大升序排序（Root 在前，Cwd 在后）。
    """

    sources: list[MemorySource]
    total_tokens: int

    @property
    def is_empty(self) -> bool:
        return len(self.sources) == 0

    def render_system_prompt_block(self) -> str:
        """从顶向下（Root -> Intermediate -> Cwd）组装为 Markdown 记忆块。

        若没有任何生效记忆，返回空字符串。
        """
        if not self.sources:
            return ""

        parts = ["# Project Memory Guidelines (Hierarchical: Root -> Cwd)"]
        for src in self.sources:
            norm_path = str(src.path).replace("\\", "/")
            level_label = (
                "Project Root"
                if src.depth == 0
                else f"Submodule Depth {src.depth}"
            )
            parts.append(
                f"\n## [Memory Level {src.depth}: {norm_path} ({level_label})]\n{src.content.strip()}"
            )
        return "\n".join(parts) + "\n"


#: 规范文件优先级候选清单 (D119: LOGOX.md > AGENTS.md > CLAUDE.md)
MEMORY_FILENAMES: tuple[str, ...] = ("LOGOX.md", "AGENTS.md", "CLAUDE.md")


def find_project_memory(cwd: str | Path | None = None) -> ProjectMemory:
    """从当前工作目录向上逐级回溯至 Git 根目录，搜集并按 Top-Down 组装记忆源。

    步骤：
    1. 探针阶段：从 start_dir 开始，每次检查当前目录及 .logox/ 下的规范文件，
       严格遵循优先级：LOGOX.md > AGENTS.md > CLAUDE.md。
    2. 边界判定：若发现 .git 目录或到达文件系统根目录，标记为根节点并停止向上回溯。
    3. 拓扑翻转：按距离项目根节点的深度升序排序（确保 Root=0 在最先，Cwd 在最后）。
    """
    start_dir = Path(cwd).resolve() if cwd else Path.cwd().resolve()
    current = start_dir

    raw_found: list[tuple[Path, Path]] = []  # (found_file_path, directory)
    project_root: Path = start_dir

    # 1. 向上攀爬收集
    while True:
        # 按优先级探查规范记忆文件
        found_file: Path | None = None
        for name in MEMORY_FILENAMES:
            candidate_1 = current / name
            candidate_2 = current / ".logox" / name
            if candidate_1.is_file():
                found_file = candidate_1
                break
            if candidate_2.is_file():
                found_file = candidate_2
                break

        if found_file:
            raw_found.append((found_file, current))

        # 检查是否到达 Git 根目录
        git_dir = current / ".git"
        if git_dir.exists():
            project_root = current
            break

        # 检查是否已到驱动器或文件系统根目录
        parent = current.parent
        if parent == current:
            project_root = current
            break
        current = parent

    # 2. 计算各层级相对于 project_root 的深度并升序重排 (Top-Down: Root -> Cwd)
    sources: list[MemorySource] = []
    total_tokens = 0

    for file_path, dir_path in raw_found:
        try:
            # 计算深度（相对路径的 parts 长度）
            rel = dir_path.relative_to(project_root)
            depth = len(rel.parts)
        except ValueError:
            depth = 0

        # 安全读取并截断
        try:
            with open(file_path, encoding="utf-8", errors="replace") as f:
                content = f.read(MAX_MEMORY_FILE_BYTES)
        except OSError:
            continue

        tokens = estimate_text_tokens(content)
        sources.append(
            MemorySource(
                path=file_path,
                depth=depth,
                content=content,
                token_count=tokens,
            )
        )
        total_tokens += tokens

    # 严格按 depth 升序排序：Root (depth 0) 最前，Cwd (depth 最大) 最后！
    sources.sort(key=lambda s: s.depth)

    return ProjectMemory(sources=sources, total_tokens=total_tokens)
