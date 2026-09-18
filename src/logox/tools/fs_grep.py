"""``grep`` 工具——跨文件正则与内容检索（L4）。

支持在文件内容中搜索正则表达式或关键字。
特性：
1. **自动剪枝与二进制过滤**：跳过常见忽略目录（``.git``、``node_modules`` 等）与包含 NUL 字节的二进制文件。
2. **带行号结构化输出**：以 ``path:line: content`` 标准格式返回，让模型能直接用这些行号做后续的精准分析与编辑。
3. **安全上限截断**：默认最多返回 100 处匹配，防止全盘广谱搜索打爆模型上下文。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from pydantic import Field

from logox.errors import ErrorCategory
from logox.tools.base import DisplayHint, ToolArgs, ToolContext, ToolResult, ToolSpec

__all__ = ["GrepArgs", "GrepTool", "build"]

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

_BINARY_PROBE_BYTES = 4096
DEFAULT_MAX_MATCHES = 100


def _resolve(cwd: Path, raw_path: str) -> Path:
    p = Path(raw_path)
    return p.resolve() if p.is_absolute() else (cwd.resolve() / p).resolve()


class GrepArgs(ToolArgs):
    """``grep`` 工具参数模型。"""

    pattern: str = Field(description="要搜索的正则表达式或关键字")
    path: str = Field(default=".", description="搜索的根目录或单个文件路径（默认当前工作目录）")
    case_sensitive: bool = Field(default=True, description="是否区分大小写（默认 True）")
    max_matches: int = Field(
        default=DEFAULT_MAX_MATCHES,
        ge=1,
        le=500,
        description=f"最多返回的匹配条数（默认 {DEFAULT_MAX_MATCHES}）",
    )


class GrepTool:
    """跨文件内容正则检索工具。"""

    spec = ToolSpec(
        name="grep",
        description=(
            "在指定目录的文件内容中搜索正则表达式或关键字。返回包含文件路径、行号和匹配行文本。"
            "会自动跳过二进制与忽略目录。"
        ),
        params=GrepArgs,
        readonly=True,
        requires_permission=False,
        summary_template="搜索 {pattern}",
    )

    async def run(self, args: ToolArgs, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, GrepArgs)
        target = _resolve(ctx.cwd, args.path)

        if not target.exists():
            return ToolResult.failure(
                ErrorCategory.BAD_REQUEST,
                f"搜索目标不存在：{target}",
                detail="请检查 path 参数是否正确。",
            )

        # 1. 正则编译
        flags = 0 if args.case_sensitive else re.IGNORECASE
        try:
            regex = re.compile(args.pattern, flags)
        except re.error as exc:
            return ToolResult.failure(
                ErrorCategory.BAD_REQUEST,
                f"正则表达式不合法：{exc}",
                detail="请检查正则表达式语法，例如转义保留字符（如 '*', '+', '?'）或核对括号配对。",
            )

        matches: list[str] = []
        truncated = False
        resolved_cwd = ctx.cwd.resolve()

        try:
            # 2. 收集待搜索文件
            if target.is_file():
                file_list = [target]
            else:
                file_list = []
                for dirpath, dirnames, filenames in os.walk(target):
                    if ctx.is_cancelled():
                        return ToolResult.failure(ErrorCategory.CANCELLED, "检索操作已被取消")
                    dirnames[:] = [d for d in dirnames if d not in _DEFAULT_IGNORES and not d.startswith(".tmp")]
                    for fname in filenames:
                        file_list.append(Path(dirpath) / fname)

            # 3. 逐个文件扫描
            for file_path in file_list:
                if ctx.is_cancelled():
                    return ToolResult.failure(ErrorCategory.CANCELLED, "检索操作已被取消")

                try:
                    raw = file_path.read_bytes()
                except Exception:
                    continue

                # 二进制探测：包含 NUL 字节则跳过
                if b"\x00" in raw[:_BINARY_PROBE_BYTES]:
                    continue

                # 解码文本
                try:
                    text = raw.decode("utf-8")
                except UnicodeDecodeError:
                    try:
                        text = raw.decode("gbk")
                    except UnicodeDecodeError:
                        text = raw.decode("latin-1", errors="replace")

                try:
                    rel_p = str(file_path.relative_to(resolved_cwd)).replace("\\", "/")
                except ValueError:
                    rel_p = str(file_path).replace("\\", "/")

                lines = text.splitlines()
                for line_idx, line_text in enumerate(lines, 1):
                    if regex.search(line_text):
                        # 截断过长行（超过 200 字符单行只展示前 200）
                        display_line = line_text.strip()
                        if len(display_line) > 200:
                            display_line = display_line[:197] + "…"
                        matches.append(f"{rel_p}:{line_idx}: {display_line}")

                        if len(matches) >= args.max_matches:
                            truncated = True
                            break

                if truncated:
                    break

            # 4. 组装结果
            if not matches:
                return ToolResult(
                    ok=True,
                    content=f"在 '{args.path}' 下未找到匹配模式 '{args.pattern}' 的内容。",
                    display=DisplayHint(kind="lines", payload={"count": 0, "lines": []}),
                )

            body = "\n".join(matches)
            if truncated:
                body += f"\n\n… 匹配项已达到上限 {args.max_matches} 条，搜索已提前停止。请指定更精确的正则表达式或更小的子目录。"

            return ToolResult(
                ok=True,
                content=body,
                display=DisplayHint(
                    kind="lines",
                    payload={"count": len(matches), "truncated": truncated, "lines": matches},
                ),
            )
        except Exception as exc:
            return ToolResult.failure(
                ErrorCategory.TOOL_FAILURE,
                f"执行 grep 检索失败：{type(exc).__name__}: {exc}",
            )


def build() -> GrepTool:
    return GrepTool()
