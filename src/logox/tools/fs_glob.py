"""``glob`` 工具——路径通配与文件发现（L4）。

支持通过通配符搜索工作区中的文件与目录。
特性：
1. **自动剪枝过滤**：在目录遍历阶段自动跳过巨型依赖与缓存目录（``.git``、``node_modules``、``.venv`` 等），
   避免遍历数万个文件造成几秒钟的 I/O 卡顿。
2. **通配符增强**：支持 ``*``、``**/*``（0 层或多层跨目录）、``?`` 等模式。
3. **安全上限截断**：默认最多返回 200 条匹配结果，超出时优雅截断并提示，防止打爆上下文。
"""

from __future__ import annotations

import fnmatch
import os
from collections.abc import Callable
from pathlib import Path

from pydantic import Field

from logox.errors import ErrorCategory
from logox.paths import is_sensitive_path
from logox.tools.base import DisplayHint, ToolArgs, ToolContext, ToolResult, ToolSpec
from logox.tools.fs_read import run_readonly_worker

__all__ = ["GlobArgs", "GlobTool", "build"]

#: 默认剪枝跳过的目录名称（遍历时直接 prune，不递归进入）
_DEFAULT_IGNORES = frozenset({
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".test-tmp",
    "build",
    "dist",
    ".logox",
    ".idea",
    ".vscode",
})

#: 默认单次返回的最大条目数（防止输出过大撑爆上下文）
DEFAULT_MAX_RESULTS = 200


def _resolve(cwd: Path, raw_path: str) -> Path:
    p = Path(raw_path)
    return p.resolve() if p.is_absolute() else (cwd.resolve() / p).resolve()


def _matches(rel: str, pattern: str) -> bool:
    """智能通配匹配：支持普通通配、** 跨目录通配、以及零层目录折叠。"""
    rel = rel.replace("\\", "/")
    pattern = pattern.replace("\\", "/")

    if fnmatch.fnmatch(rel, pattern):
        return True
    if pattern.startswith("**/") and fnmatch.fnmatch(rel, pattern[3:]):
        return True
    if "/**/" in pattern and fnmatch.fnmatch(rel, pattern.replace("/**/", "/")):
        return True
    return "/" not in pattern and fnmatch.fnmatch(os.path.basename(rel), pattern)


class GlobArgs(ToolArgs):
    """``glob`` 工具参数模型。"""

    pattern: str = Field(description="搜索通配符，如 '**/*.py'、'src/*.ts' 或 '*.md'")
    path: str = Field(default=".", description="搜索的起始目录（默认当前工作目录）")
    max_results: int = Field(
        default=DEFAULT_MAX_RESULTS,
        ge=1,
        le=1000,
        description=f"最多返回的匹配条目数（默认 {DEFAULT_MAX_RESULTS}）",
    )


class GlobTool:
    """基于通配符的文件与目录搜索工具。"""

    spec = ToolSpec(
        name="glob",
        description=(
            "按通配符模式搜索文件与目录路径。支持 *（单层通配）、**/*（跨目录递归通配）。"
            "会自动跳过 .git、node_modules、.venv 等常见巨型目录。"
        ),
        params=GlobArgs,
        readonly=True,
        requires_permission=False,
        summary_template="查找 {pattern}",
    )

    def __init__(self, *, path_filter: Callable[[Path], bool] | None = None) -> None:
        self.path_filter = path_filter

    async def run(self, args: ToolArgs, ctx: ToolContext) -> ToolResult:
        return await run_readonly_worker(self._run_sync, args, ctx)

    def _run_sync(self, args: ToolArgs, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, GlobArgs)
        root = _resolve(ctx.cwd, args.path)

        if not root.exists():
            return ToolResult.failure(
                ErrorCategory.BAD_REQUEST,
                f"搜索起始目录不存在：{root}",
                detail="请检查 path 参数是否正确。",
            )
        if not root.is_dir():
            return ToolResult.failure(
                ErrorCategory.BAD_REQUEST,
                f"搜索路径是一个文件，不是目录：{root}",
                detail="若要检查该文件内容，请使用 read 工具。",
            )

        allow_sensitive = is_sensitive_path(root) or is_sensitive_path(args.pattern)

        def permitted(path: Path) -> bool:
            if self.path_filter is not None and not self.path_filter(path):
                return False
            try:
                resolved = path.resolve()
                resolved.relative_to(root)
            except (OSError, ValueError):
                return False
            return allow_sensitive or not (is_sensitive_path(path) or is_sensitive_path(resolved))

        matches: list[str] = []

        try:
            # 使用 os.walk 并对 dirnames 原地切片，实现目录级剪枝
            for dirpath, dirnames, filenames in os.walk(root):
                if ctx.is_cancelled():
                    return ToolResult.failure(ErrorCategory.CANCELLED, "搜索操作已被取消")

                # 剪枝：剔除忽略的目录
                dirnames[:] = [d for d in dirnames if (d not in _DEFAULT_IGNORES or (allow_sensitive and d in {".git", ".logox"})) and not d.startswith(".tmp") and permitted(Path(dirpath) / d)]

                # 检查目录本身是否匹配（排除根目录自身）
                if dirpath != str(root):
                    rel_dir = os.path.relpath(dirpath, root)
                    if _matches(rel_dir, args.pattern):
                        matches.append(rel_dir.replace("\\", "/") + "/")

                # 检查文件是否匹配
                for fname in filenames:
                    full_p = os.path.join(dirpath, fname)
                    if ctx.is_cancelled():
                        return ToolResult.failure(ErrorCategory.CANCELLED, "搜索操作已被取消")
                    if not permitted(Path(full_p)):
                        continue
                    rel_file = os.path.relpath(full_p, root)
                    if _matches(rel_file, args.pattern):
                        matches.append(rel_file.replace("\\", "/"))

            # 按字母顺序排序
            matches.sort()

            total_found = len(matches)
            if total_found == 0:
                return ToolResult(
                    ok=True,
                    content=f"未找到与模式 '{args.pattern}' 匹配的文件或目录。",
                    display=DisplayHint(kind="lines", payload={"count": 0, "lines": []}),
                )

            truncated = total_found > args.max_results
            shown_matches = matches[: args.max_results]
            body = "\n".join(shown_matches)
            if truncated:
                body += f"\n\n… 另有 {total_found - args.max_results} 个匹配项已截断省略。请指定更精确的通配符。"

            return ToolResult(
                ok=True,
                content=body,
                display=DisplayHint(
                    kind="lines",
                    payload={"count": total_found, "truncated": truncated, "lines": shown_matches},
                ),
            )
        except Exception as exc:
            return ToolResult.failure(
                ErrorCategory.TOOL_FAILURE,
                f"执行 glob 搜索失败：{type(exc).__name__}: {exc}",
            )


def build() -> GlobTool:
    return GlobTool()
