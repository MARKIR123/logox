"""``grep`` 工具——跨文件正则与内容检索（L4）。

通过独立 ripgrep 子进程搜索正则或关键字，不在 UI 的 Python 进程执行正则。
依赖 PATH 中的 rg；不支持 Python 正则的前后查找和反向引用。
特性：
1. **自动剪枝与二进制过滤**：跳过常见忽略目录（``.git``、``node_modules`` 等）与包含 NUL 字节的二进制文件。
2. **带行号结构化输出**：以 ``path:line: content`` 标准格式返回，让模型能直接用这些行号做后续的精准分析与编辑。
3. **安全上限截断**：默认最多返回 100 处匹配，防止全盘广谱搜索打爆模型上下文。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import shutil
import subprocess
from pathlib import Path

from pydantic import Field

from logox.errors import ErrorCategory
from logox.paths import is_sensitive_path
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

DEFAULT_MAX_MATCHES = 100


def _resolve(cwd: Path, raw_path: str) -> Path:
    p = Path(raw_path)
    return p.resolve() if p.is_absolute() else (cwd.resolve() / p).resolve()


class GrepArgs(ToolArgs):
    """``grep`` 工具参数模型。"""

    pattern: str = Field(description="ripgrep 正则或关键字（默认 Rust 引擎，不支持前后查找和反向引用）")
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
            "使用 ripgrep 默认正则、auto 编码，自动跳过二进制与忽略目录。"
        ),
        params=GrepArgs,
        readonly=True,
        requires_permission=False,
        summary_template="搜索 {pattern}",
    )

    def __init__(self, *, excluded_globs: tuple[str, ...] = ()) -> None:
        # Host-only restrictions; never supplied by the model or shared between instances.
        self.excluded_globs = excluded_globs

    async def run(self, args: ToolArgs, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, GrepArgs)
        if ctx.is_cancelled():
            return ToolResult.failure(ErrorCategory.CANCELLED, "检索操作已被取消")
        target = _resolve(ctx.cwd, args.path)
        if not target.exists():
            return ToolResult.failure(ErrorCategory.BAD_REQUEST, f"搜索目标不存在：{target}")
        executable = shutil.which("rg")
        if executable is None:
            return ToolResult.failure(
                ErrorCategory.TOOL_FAILURE, "grep 需要 ripgrep，但 PATH 中未找到 rg",
                detail="请安装 ripgrep 并确认 rg --version 可运行，再重试。",
            )

        command = _command(executable, args, target)
        restrictions = [value for pattern in self.excluded_globs for value in ("--iglob", f"!{pattern}")]
        command[-2:-2] = restrictions
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        launch = asyncio.create_task(asyncio.create_subprocess_exec(
            *command, cwd=ctx.cwd, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, **options,
        ))
        try:
            process = await asyncio.shield(launch)
        except asyncio.CancelledError:
            # shield 防止启动任务被一起取消；拿到句柄才能清理已创建的进程。
            try:
                process = await launch
            except Exception:
                pass
            else:
                await _shutdown(process, [])
            raise
        except OSError as exc:
            return ToolResult.failure(ErrorCategory.TOOL_FAILURE, f"无法启动 ripgrep：{exc}")

        readers = [
            asyncio.create_task(_matches(process, args, target, ctx.cwd)),
            asyncio.create_task(_stderr_tail(process.stderr)),
        ]
        cancelled = asyncio.create_task(_watch_cancel(ctx))
        try:
            done, _ = await asyncio.wait([readers[0], cancelled], return_when=asyncio.FIRST_COMPLETED)
            if cancelled in done:
                return ToolResult.failure(ErrorCategory.CANCELLED, "检索操作已被取消")
            matches, truncated = await readers[0]
            code = await process.wait()
            stderr = await readers[1]
            if not truncated and code not in (0, 1):
                invalid_pattern = any(marker in stderr for marker in (
                    "regex parse error", "regex error:", "error parsing regex", "is not allowed in a regex",
                ))
                category = ErrorCategory.BAD_REQUEST if invalid_pattern else ErrorCategory.TOOL_FAILURE
                label = "正则表达式不合法或 ripgrep 不支持" if invalid_pattern else "ripgrep 检索失败"
                return ToolResult.failure(category, f"{label}：{stderr or f'退出码 {code}'}")
            if not matches:
                return ToolResult(
                    ok=True, content=f"在 '{args.path}' 下未找到匹配模式 '{args.pattern}' 的内容。",
                    display=DisplayHint(kind="lines", payload={"count": 0, "lines": []}),
                )
            body = "\n".join(matches)
            if truncated:
                body += f"\n\n… 匹配项已达到上限 {args.max_matches} 条，搜索已提前停止。请指定更精确的正则表达式或更小的子目录。"
            return ToolResult(
                ok=True, content=body,
                display=DisplayHint(kind="lines", payload={
                    "count": len(matches), "truncated": truncated, "lines": matches,
                }),
            )
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return ToolResult.failure(ErrorCategory.TOOL_FAILURE, f"ripgrep 结果读取失败：{exc}")
        finally:
            await _shutdown(process, [*readers, cancelled])


def _command(executable: str, args: GrepArgs, target: Path) -> list[str]:
    command = [
        executable, "--no-config", "--engine=default", "--json", "--color=never",
        "--hidden", "--no-ignore", "--encoding=auto", "--crlf", "--line-buffered",
        f"--max-count={args.max_matches}",
    ]
    allow_sensitive = is_sensitive_path(target)
    for directory in sorted(_DEFAULT_IGNORES):
        if allow_sensitive and directory in {".git", ".logox"}:
            continue
        command.extend(["--glob", f"!**/{directory}/**"])
    command.extend(["--glob", "!**/.tmp*/**"])
    if not allow_sensitive:
        for pattern in ("!**/.env*", "!**/.logox/config.toml", "!**/.logox/permissions.toml"):
            command.extend(["--glob", pattern])
    if not args.case_sensitive:
        command.append("--ignore-case")
    command.extend(["--regexp", args.pattern, "--", str(target)])
    return command


def _json_text(value: dict[str, str], *, path: bool = False) -> str:
    if "text" in value:
        return value["text"]
    raw = base64.b64decode(value["bytes"], validate=True)
    return os.fsdecode(raw) if path else raw.decode("utf8", errors="replace")


async def _matches(process, args: GrepArgs, target: Path, cwd: Path) -> tuple[list[str], bool]:
    matches: list[str] = []
    truncated = False
    pending = bytearray()
    file_matches: list[str] = []
    file_open = False
    resolved_cwd = cwd.resolve()
    allow_sensitive = is_sensitive_path(target)
    while raw := await process.stdout.read(65536):
        # 已缓冲的 read 可能立即返回；每批让输入、计时器和取消都有机会运行。
        await asyncio.sleep(0)
        if truncated:
            continue  # 终止后仍排空管道，避免 wait 等待未读取的输出。
        records = raw.split(b"\n")
        if len(records) == 1:
            pending.extend(raw)
            continue
        # 只扫描新字节找换行，避免超长事件每批重扫不断增长的 pending。
        records[0] = pending + records[0]
        pending = bytearray(records.pop())
        for record in records:
            if not record:
                continue
            event = json.loads(record)
            if event["type"] == "begin":
                if file_open:
                    raise ValueError("ripgrep 文件事件缺少结束记录")
                file_matches = []
                file_open = True
                continue
            if event["type"] == "end":
                # JSON 模式可能先报告匹配、再报告 NUL。等文件结束才能决定是否展示。
                if event["data"].get("binary_offset") is None:
                    matches.extend(file_matches[:args.max_matches - len(matches)])
                file_matches = []
                file_open = False
                if len(matches) >= args.max_matches:
                    truncated = True
                    _kill(process)
                    pending.clear()
                    break
                continue
            if event["type"] != "match":
                continue
            data = event["data"]
            path = Path(_json_text(data["path"], path=True))
            if not path.is_absolute():
                path = cwd / path
            resolved = path.resolve()
            if target.is_dir():
                try:
                    resolved.relative_to(target)
                except ValueError:
                    continue
                if not allow_sensitive and (is_sensitive_path(path) or is_sensitive_path(resolved)):
                    continue
            try:
                relative = str(path.relative_to(resolved_cwd)).replace("\\", "/")
            except ValueError:
                relative = str(path).replace("\\", "/")
            line = _json_text(data["lines"]).strip()
            if len(line) > 200:
                line = line[:197] + "…"
            if len(file_matches) < args.max_matches:
                file_matches.append(f"{relative}:{data['line_number']}: {line}")
    if (pending or file_open) and not truncated:
        raise ValueError("ripgrep 返回未完整结束的 JSON 事件")
    return matches, truncated


async def _stderr_tail(stream) -> str:
    tail = bytearray()
    while raw := await stream.read(65536):
        tail.extend(raw)
        del tail[:-4096]
    return tail.decode("utf8", errors="replace").strip()


async def _watch_cancel(ctx: ToolContext) -> None:
    while not ctx.is_cancelled():
        await asyncio.sleep(.05)


def _kill(process) -> None:
    if process.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            process.kill()


async def _drain(stream) -> None:
    while await stream.read(65536):
        pass


async def _shutdown(process, tasks) -> None:
    _kill(process)
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await asyncio.gather(_drain(process.stdout), _drain(process.stderr), process.wait())


def build() -> GrepTool:
    return GrepTool()
