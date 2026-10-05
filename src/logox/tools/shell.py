"""``shell`` 工具——异步子进程命令执行与沙箱防卫（L4）。

特性：
1. **Shell 后端自适应探测**（D19）：
   Windows 环境自动按 Git Bash -> pwsh -> powershell -> cmd 优先级探测；
   POSIX 环境默认使用 bash / sh。
2. **进程树彻底清理（Kill Process Tree）**：
   命令超时或取消时，递归终止所有子进程（Windows 使用 ``taskkill /F /T``，POSIX 使用 ``os.killpg``），
   防止后台孤儿进程锁死端口或资源。
3. **输出双向截断保护**：
   合并 stdout 与 stderr，若超出 8000 字符，保留前 2000 字符与后 2000 字符，
   中间插入截断提示，保护模型上下文预算。
4. **PowerShell 退出码陷阱防卫**：
   PowerShell 在非终止性错误时可能返回 exitcode=0，通过检测 stderr 关键字（如 ``+ CategoryInfo``）
   辅助判定错误并回灌模型自愈。
5. **编码与环境变量注入**：
   自动注入 ``PYTHONUTF8=1`` 与 ``PYTHONIOENCODING=utf-8``，支持 utf-8 / gbk 自适应解码。
"""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import os
import shutil
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import Field

from logox.errors import ErrorCategory
from logox.tools.base import DisplayHint, ToolArgs, ToolContext, ToolResult, ToolSpec

__all__ = ["ShellArgs", "ShellBackend", "ShellTool", "build", "detect_shell", "truncate_output"]

MAX_OUTPUT_CHARS = 8000
TRUNCATE_HEAD_CHARS = 2000
TRUNCATE_TAIL_CHARS = 2000


@dataclass(frozen=True)
class ShellBackend:
    """Shell 后端描述信息。"""

    name: Literal["gitbash", "wsl", "powershell", "cmd", "posix"]
    executable: str
    args_prefix: list[str]

    def build_cmd(self, command: str) -> list[str]:
        if self.name == "powershell":
            trimmed = command.strip()
            # 若命令以引号开头（如 "\"path/to/exe\" args"），PowerShell 语法解析需要调用操作符 &
            if (trimmed.startswith('"') or trimmed.startswith("'")) and not trimmed.startswith("&"):
                command = f"& {command}"
            # 解决 PowerShell 原生命令退出码陷阱：若原生命令产生了非 0 退出码，确保透传该退出码
            command = f"{command}; if ($LASTEXITCODE -ne $null -and $LASTEXITCODE -ne 0) {{ exit $LASTEXITCODE }}"
        return [*self.args_prefix, command]


def _find_git_bash() -> Path | None:
    """在 Windows 上查找 Git Bash 的真实路径。"""
    candidates = [
        Path(r"C:\Program Files\Git\bin\bash.exe"),
        Path(r"C:\Program Files (x86)\Git\bin\bash.exe"),
    ]
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        candidates.append(Path(local_app_data) / "Programs" / "Git" / "bin" / "bash.exe")

    for candidate in candidates:
        if candidate.is_file():
            return candidate

    which_bash = shutil.which("bash.exe") or shutil.which("bash")
    if which_bash:
        p = Path(which_bash)
        # 排除 WindowsApps 下的 0 字节别名跳转
        if "WindowsApps" not in p.parts and p.is_file():
            return p

    return None


def detect_shell(preferred: str = "auto") -> ShellBackend:
    """探测当前系统的最佳 Shell 后端。"""
    if sys.platform != "win32":
        sh = shutil.which("bash") or "/bin/sh"
        return ShellBackend(name="posix", executable=sh, args_prefix=[sh, "-c"])

    # Windows 平台
    if preferred == "gitbash":
        gb = _find_git_bash()
        if gb:
            return ShellBackend(name="gitbash", executable=str(gb), args_prefix=[str(gb), "-c"])
    elif preferred == "wsl":
        wsl = shutil.which("wsl.exe")
        if wsl:
            return ShellBackend(name="wsl", executable=wsl, args_prefix=[wsl, "-e", "bash", "-c"])
    elif preferred == "powershell":
        pwsh = shutil.which("pwsh.exe") or shutil.which("pwsh")
        if pwsh:
            return ShellBackend(
                name="powershell",
                executable=pwsh,
                args_prefix=[pwsh, "-NoProfile", "-NonInteractive", "-Command"],
            )
        ps = shutil.which("powershell.exe") or shutil.which("powershell") or "powershell.exe"
        return ShellBackend(
            name="powershell",
            executable=ps,
            args_prefix=[ps, "-NoProfile", "-NonInteractive", "-Command"],
        )

    # auto 探测优先级: Git Bash -> pwsh -> powershell -> cmd
    gb = _find_git_bash()
    if gb:
        return ShellBackend(name="gitbash", executable=str(gb), args_prefix=[str(gb), "-c"])

    pwsh = shutil.which("pwsh.exe") or shutil.which("pwsh")
    if pwsh:
        return ShellBackend(
            name="powershell",
            executable=pwsh,
            args_prefix=[pwsh, "-NoProfile", "-NonInteractive", "-Command"],
        )

    ps = shutil.which("powershell.exe") or shutil.which("powershell")
    if ps:
        return ShellBackend(
            name="powershell",
            executable=ps,
            args_prefix=[ps, "-NoProfile", "-NonInteractive", "-Command"],
        )

    cmd = shutil.which("cmd.exe") or "cmd.exe"
    return ShellBackend(name="cmd", executable=cmd, args_prefix=[cmd, "/c"])


def truncate_output(text: str) -> tuple[str, bool]:
    """输出超出 8000 字符时双向截断。"""
    if len(text) <= MAX_OUTPUT_CHARS:
        return text, False
    truncated_count = len(text) - (TRUNCATE_HEAD_CHARS + TRUNCATE_TAIL_CHARS)
    truncated_text = (
        f"{text[:TRUNCATE_HEAD_CHARS]}\n"
        f"\n[... 已截断 {truncated_count} 字符输出以保护上下文预算 ...]\n\n"
        f"{text[-TRUNCATE_TAIL_CHARS:]}"
    )
    return truncated_text, True


class _BoundedOutput:
    def __init__(self):
        self.total = 0
        self.head = ""
        self.tail = ""
        self.trimmed = (0, "", "")
        self.error_marker = False
        self._marker_tail = ""

    def append(self, text):
        probe = self._marker_tail + text
        self.error_marker |= any(marker in probe for marker in ("+ CategoryInfo", "+ FullyQualifiedErrorId"))
        self._marker_tail = probe[-32:]
        trimmed = text.rstrip()
        if trimmed:
            self.trimmed = (self.total + len(trimmed), (self.head + trimmed)[:MAX_OUTPUT_CHARS], (self.tail + trimmed)[-MAX_OUTPUT_CHARS:])
        self.total += len(text)
        self.head = (self.head + text)[:MAX_OUTPUT_CHARS]
        self.tail = (self.tail + text)[-MAX_OUTPUT_CHARS:]

    def rstrip(self):
        other = _BoundedOutput()
        other.total, other.head, other.tail = self.trimmed
        return other

    def extend(self, other):
        self.total += other.total
        self.head = (self.head + other.head)[:MAX_OUTPUT_CHARS]
        self.tail = (self.tail + other.tail)[-MAX_OUTPUT_CHARS:]

    def render(self):
        if self.total <= MAX_OUTPUT_CHARS:
            return self.head, False
        removed = self.total - TRUNCATE_HEAD_CHARS - TRUNCATE_TAIL_CHARS
        return (f"{self.head[:TRUNCATE_HEAD_CHARS]}\n\n[... 已截断 {removed} 字符输出以保护上下文预算 ...]\n\n{self.tail[-TRUNCATE_TAIL_CHARS:]}", True)


async def _capture_stream(stream):
    candidates = [(codecs.getincrementaldecoder(enc)(errors="strict"), _BoundedOutput()) for enc in ("utf-8", "gbk", "latin-1")]
    while True:
        raw = await stream.read(65536)
        kept = []
        for decoder, output in candidates:
            try:
                output.append(decoder.decode(raw, final=not raw))
                kept.append((decoder, output))
            except UnicodeDecodeError:
                pass
        candidates = kept
        if not raw:
            return candidates[0][1]


async def _collect_process(process):
    readers = [asyncio.create_task(_capture_stream(stream)) for stream in (process.stdout, process.stderr)]
    try:
        stdout, stderr = await asyncio.gather(*readers)
        await process.wait()
        return stdout, stderr
    finally:
        for task in readers:
            if not task.done():
                task.cancel()
        await asyncio.gather(*readers, return_exceptions=True)


async def _kill_process_tree(pid: int) -> None:
    """彻底终止进程树。"""
    if sys.platform == "win32":
        with contextlib.suppress(Exception):
            proc = await asyncio.create_subprocess_exec(
                "taskkill",
                "/F",
                "/T",
                "/PID",
                str(pid),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
    else:
        with contextlib.suppress(Exception):
            os.killpg(os.getpgid(pid), signal.SIGKILL)


class ShellArgs(ToolArgs):
    """``shell`` 工具参数模型。"""

    command: str = Field(description="要在终端中执行的完整命令字符串")
    timeout_seconds: int = Field(default=60, ge=1, le=600, description="执行超时时间（秒，默认 60s）")


class ShellTool:
    """异步子进程命令执行工具。"""

    spec = ToolSpec(
        name="shell",
        description=(
            "在系统终端中异步执行命令。支持自动探测当前环境的最佳 Shell（Git Bash / WSL / PowerShell）。"
            "具有超时与进程树彻底清理保护，输出超出 8000 字符时自动安全截断。"
        ),
        params=ShellArgs,
        readonly=False,
        requires_permission=True,
        summary_template="执行 {command}",
    )

    def __init__(self, backend: str | ShellBackend = "auto") -> None:
        if isinstance(backend, ShellBackend):
            self.backend = backend
        else:
            self.backend = detect_shell(backend)

    async def run(self, args: ToolArgs, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, ShellArgs)

        if ctx.is_cancelled():
            return ToolResult.failure(ErrorCategory.CANCELLED, "命令执行已被取消")

        cmd_args = self.backend.build_cmd(args.command)
        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"

        extra_kwargs: dict = {}
        if sys.platform == "win32":
            extra_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            extra_kwargs["start_new_session"] = True

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd_args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(ctx.cwd),
                env=env,
                **extra_kwargs,
            )
        except Exception as exc:
            return ToolResult.failure(
                ErrorCategory.TOOL_FAILURE,
                f"启动终端子进程失败：{type(exc).__name__}: {exc}",
                detail=f"后端：{self.backend.name}，命令：{args.command}",
            )

        try:
            stdout, stderr = await asyncio.wait_for(
                _collect_process(process),
                timeout=float(args.timeout_seconds),
            )
        except TimeoutError:
            await _kill_process_tree(process.pid)
            with contextlib.suppress(Exception):
                process.kill()
            with contextlib.suppress(Exception):
                await process.wait()
            return ToolResult.failure(
                ErrorCategory.TOOL_FAILURE,
                f"命令执行超时（超过 {args.timeout_seconds} 秒）",
                detail="子进程树已由守护清理机制彻底终止。请精简命令或调整 timeout_seconds 参数。",
            )
        except asyncio.CancelledError:
            await _kill_process_tree(process.pid)
            with contextlib.suppress(Exception):
                process.kill()
            with contextlib.suppress(Exception):
                await process.wait()
            raise

        combined = _BoundedOutput()
        if stdout.total and stderr.total:
            combined.extend(stdout.rstrip())
            combined.append("\n\n[stderr]:\n")
            combined.extend(stderr)
        else:
            combined.extend(stdout if stdout.total else stderr)
        returncode = process.returncode if process.returncode is not None else 0
        pwsh_has_error = self.backend.name == "powershell" and stderr.error_marker
        truncated_output, was_truncated = combined.render()

        if returncode == 0 and not pwsh_has_error:
            content = truncated_output if truncated_output.strip() else "(命令执行成功，无输出)"
            return ToolResult(
                ok=True,
                content=content,
                display=DisplayHint(
                    kind="text",
                    payload={
                        "command": args.command,
                        "exit_code": 0,
                        "backend": self.backend.name,
                        "truncated": was_truncated,
                    },
                ),
            )
        else:
            effective_code = returncode if returncode != 0 else 1
            err_msg = f"命令执行失败，退出码: {effective_code}"
            if pwsh_has_error and returncode == 0:
                err_msg += " (PowerShell 内部错误)"

            return ToolResult.failure(
                ErrorCategory.TOOL_FAILURE,
                err_msg,
                detail=truncated_output,
                content=truncated_output if truncated_output.strip() else err_msg,
            )


def build() -> ShellTool:
    return ShellTool()
