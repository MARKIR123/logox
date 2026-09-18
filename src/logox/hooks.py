"""生命周期观察型钩子调度器（M10 / D23 / D110）。

设计原则：
1. 纯观察型（Observable Only）：在关键检查点派生子进程，不影响内核决策；
2. 零阻断（Fail-Safe）：外部脚本报错、挂死、退出码非 0 均记录日志，绝不抛出异常打断主会话；
3. 硬超时与进程树消杀：默认 5.0 秒超时，超时时递归杀灭子进程树，防止僵尸进程堆积。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from logox.config.schema import HooksConfig

logger = logging.getLogger("logox.hooks")

HookEventName = Literal[
    "session_start",
    "post_tool_use",
    "pre_compact",
    "notification",
    "stop",
]

_SUPPORTED_EXTENSIONS = ("", ".sh", ".py", ".ps1", ".cmd", ".bat")


async def _kill_process_tree(pid: int) -> None:
    """彻底递归终止子进程树。"""
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



@dataclass
class HookExecutionResult:
    """单个钩子命令的执行结果。"""

    event: str
    command: str
    ok: bool
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: float
    timed_out: bool = False


class HookRunner:
    """生命周期观察型钩子调度器。"""

    def __init__(
        self,
        config: HooksConfig,
        cwd: Path | str,
        user_dir: Path | str | None = None,
    ) -> None:
        self.config = config
        self.cwd = Path(cwd).resolve()
        self.user_dir = Path(user_dir).resolve() if user_dir else Path.home() / ".logox"
        self._history: list[HookExecutionResult] = []

    @property
    def history(self) -> list[HookExecutionResult]:
        """获取已执行钩子的历史记录。"""
        return list(self._history)

    def discover_hooks(self, event_name: str) -> list[str]:
        """按优先级发现当前事件绑定的全部钩子命令。

        来源：
        1. config.toml 中的显式 [[hooks.entries]]；
        2. <cwd>/.logox/hooks/<event_name> 脚本文件；
        3. ~/.logox/hooks/<event_name> 脚本文件。
        """
        if not self.config.enabled:
            return []

        commands: list[str] = []


        # 1. 配置文件中的显式项
        for entry in self.config.entries:
            if entry.event == event_name and entry.command.strip():
                commands.append(entry.command.strip())

        # 2. 项目级目录文件扫描
        project_hooks_dir = self.cwd / ".logox" / "hooks"
        if project_hooks_dir.is_dir():
            for ext in _SUPPORTED_EXTENSIONS:
                candidate = project_hooks_dir / f"{event_name}{ext}"
                if candidate.is_file():
                    commands.append(str(candidate))
                    break

        # 3. 用户全局目录文件扫描
        user_hooks_dir = self.user_dir / "hooks"
        if user_hooks_dir.is_dir():
            for ext in _SUPPORTED_EXTENSIONS:
                candidate = user_hooks_dir / f"{event_name}{ext}"
                if candidate.is_file():
                    # 避免重复
                    cmd_str = str(candidate)
                    if cmd_str not in commands:
                        commands.append(cmd_str)
                    break

        return commands

    async def execute_command(
        self,
        command: str,
        event_name: str,
        payload: dict[str, Any] | None = None,
        env_vars: dict[str, str] | None = None,
    ) -> HookExecutionResult:
        """执行单条钩子命令，保证 5s 硬超时与 Fail-Safe 零阻断。"""
        start_time = time.perf_counter()

        merged_env = dict(os.environ)
        merged_env["LOGOX_EVENT"] = event_name
        merged_env["LOGOX_CWD"] = str(self.cwd)
        if env_vars:
            for k, v in env_vars.items():
                merged_env[k] = str(v)


        # 构造 stdin 序列化 JSON
        try:
            stdin_data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        except Exception:
            stdin_data = b"{}"

        timed_out = False
        exit_code = -1
        stdout_str = ""
        stderr_str = ""

        proc: asyncio.subprocess.Process | None = None
        try:
            # 如果是具体脚本文件且非 Windows 可执行格式，支持自动附带解释器
            cmd_to_run = command
            script_path = Path(command)
            if script_path.is_file():
                if script_path.suffix == ".py":
                    cmd_to_run = f'"{sys.executable}" "{script_path}"'
                elif script_path.suffix == ".ps1" and sys.platform == "win32":
                    cmd_to_run = f'powershell.exe -ExecutionPolicy Bypass -File "{script_path}"'

            proc = await asyncio.create_subprocess_shell(
                cmd_to_run,
                cwd=str(self.cwd),
                env=merged_env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(input=stdin_data),
                timeout=self.config.timeout_s,
            )
            exit_code = proc.returncode or 0
            stdout_str = stdout_bytes.decode("utf-8", errors="replace")
            stderr_str = stderr_bytes.decode("utf-8", errors="replace")
        except TimeoutError:
            timed_out = True
            logger.warning(
                "钩子命令超时 (%0.1fs)，强制杀灭子进程: %s",
                self.config.timeout_s,
                command,
            )
            if proc:
                if proc.pid:
                    await _kill_process_tree(proc.pid)
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(proc.wait(), timeout=1.0)
                if getattr(proc, "_transport", None) is not None:
                    with contextlib.suppress(Exception):
                        proc._transport.close()
            stderr_str = f"Hook timed out after {self.config.timeout_s}s"



        except Exception as exc:
            logger.warning("执行钩子命令异常: %s, 错误: %s", command, exc)
            stderr_str = str(exc)

        duration_ms = (time.perf_counter() - start_time) * 1000.0
        ok = not timed_out and exit_code == 0

        res = HookExecutionResult(
            event=event_name,
            command=command,
            ok=ok,
            exit_code=exit_code,
            stdout=stdout_str,
            stderr=stderr_str,
            duration_ms=duration_ms,
            timed_out=timed_out,
        )
        self._history.append(res)
        return res

    async def dispatch(
        self,
        event_name: HookEventName,
        payload: dict[str, Any] | None = None,
        env_vars: dict[str, str] | None = None,
    ) -> list[HookExecutionResult]:
        """向特定事件分发所有绑定的钩子。"""
        if not self.config.enabled:
            return []

        commands = self.discover_hooks(event_name)
        if not commands:
            return []

        payload_dict = payload or {}
        env_dict = env_vars or {}

        results: list[HookExecutionResult] = []
        for cmd in commands:
            res = await self.execute_command(cmd, event_name, payload_dict, env_dict)
            results.append(res)

        return results

    def attach_to_bus(self, bus: Any) -> None:
        """将 HookRunner 挂接到事件总线上作为普通订阅者。"""
        from logox.kernel.events import (
            CompactionStarted,
            SessionStart,
            ToolCallFinished,
            TurnFinished,
        )

        async def _on_session_start(ev: SessionStart) -> None:
            await self.dispatch(
                "session_start",
                payload={"session_id": ev.session_id, "cwd": ev.cwd, "model": ev.model},
                env_vars={"LOGOX_SESSION_ID": ev.session_id},
            )

        async def _on_tool_finished(ev: ToolCallFinished) -> None:
            tool_name = getattr(ev, "name", "")
            env_vars = {
                "LOGOX_TOOL_NAME": tool_name,
                "LOGOX_TOOL_CALL_ID": ev.call_id,
            }
            payload = {
                "call_id": ev.call_id,
                "name": tool_name,
                "ok": ev.ok,
                "duration_ms": ev.duration_ms,
                "result_digest": getattr(ev, "result_digest", ""),
            }
            await self.dispatch("post_tool_use", payload=payload, env_vars=env_vars)


        async def _on_compact(ev: CompactionStarted) -> None:
            await self.dispatch("pre_compact", payload={"tokens": ev.tokens_before})

        async def _on_turn_finished(ev: TurnFinished) -> None:
            await self.dispatch("stop", payload={"turn_index": ev.turn_index})

        bus.subscribe(SessionStart, _on_session_start, name="hook_runner.session_start")
        bus.subscribe(ToolCallFinished, _on_tool_finished, name="hook_runner.post_tool_use")
        bus.subscribe(CompactionStarted, _on_compact, name="hook_runner.pre_compact")
        bus.subscribe(TurnFinished, _on_turn_finished, name="hook_runner.stop")

