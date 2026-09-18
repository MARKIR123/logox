"""JSONL 事件日志（D25：可观测性的唯一抓手）。

职责
----
把事件流**异步、非阻塞**地落盘成 JSONL（按会话分文件），并在写入前脱敏。

不负责
------
不做业务决策、不做事件归约（那是 ``tui/metrics.py``）、不阻塞 Agent 循环。

解耦方式
--------
本模块以 ``blocking=False`` 订阅总线：总线内部的有界队列负责把日志写入与
Agent 循环解耦（队列满时丢弃最旧事件并计数，K2）。因此本模块**不再自建队列**，
只需一个内存缓冲 + 周期性批量 flush。

失败策略
--------
写盘失败（磁盘满、权限不足、文件被占用）**只记日志并计数，绝不阻断会话**
（P-5 / E-9）。``/debug`` 面板会显示失败与丢弃计数。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
from pathlib import Path

from logox.kernel.events import Event

__all__ = ["EventLog", "redact_text", "session_log_path"]

logger = logging.getLogger("logox.telemetry")

DEFAULT_BATCH_SIZE = 64
DEFAULT_FLUSH_INTERVAL_S = 0.2

# 脱敏：API Key 形态与常见的密钥字段。宁可多打码，也不能把密钥写进日志。
_REDACT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._\-]{8,}"),
    re.compile(r"(?i)(api[_-]?key\"?\s*[:=]\s*\")([^\"]{4,})"),
    re.compile(r"(?i)(authorization\"?\s*[:=]\s*\")([^\"]{4,})"),
)


def redact_text(text: str) -> str:
    """把疑似密钥的内容打码（保留少量前缀便于排查是哪个 key）。"""
    redacted = _REDACT_PATTERNS[0].sub(lambda m: m.group(0)[:6] + "***", text)
    redacted = _REDACT_PATTERNS[1].sub(lambda m: f"{m.group(1)} ***", redacted)
    redacted = _REDACT_PATTERNS[2].sub(lambda m: f"{m.group(1)}***", redacted)
    redacted = _REDACT_PATTERNS[3].sub(lambda m: f"{m.group(1)}***", redacted)
    return redacted


class EventLog:
    """按会话分文件的 JSONL 事件日志。

    典型用法::

        log = EventLog(paths.logs / f"{session_id}.jsonl")
        await log.start()
        bus.subscribe(Event, log.handle, name="telemetry", blocking=False)
        ...
        await log.aclose()   # flush 残余并停掉后台任务
    """

    def __init__(
        self,
        path: Path,
        *,
        redact: bool = True,
        batch_size: int = DEFAULT_BATCH_SIZE,
        flush_interval_s: float = DEFAULT_FLUSH_INTERVAL_S,
    ) -> None:
        self._path = Path(path)
        self._redact = redact
        self._batch_size = max(1, batch_size)
        self._flush_interval_s = max(0.01, flush_interval_s)
        self._buffer: list[str] = []
        self._flusher: asyncio.Task[None] | None = None
        self._write_failures = 0
        self._lines_written = 0

    # ------------------------------------------------------------------ #
    # 只读属性
    # ------------------------------------------------------------------ #

    @property
    def path(self) -> Path:
        return self._path

    @property
    def write_failures(self) -> int:
        return self._write_failures

    @property
    def lines_written(self) -> int:
        return self._lines_written

    # ------------------------------------------------------------------ #
    # 订阅者接口
    # ------------------------------------------------------------------ #

    async def handle(self, event: Event) -> None:
        """总线订阅者回调（注册为 ``blocking=False``）。"""
        try:
            payload = event.model_dump(mode="json")
            line = json.dumps(payload, ensure_ascii=False)
        except (TypeError, ValueError):  # pragma: no cover - 事件模型保证可序列化
            logger.exception("事件无法序列化，已跳过：%s", getattr(event, "type", "?"))
            return

        if self._redact:
            line = redact_text(line)

        self._buffer.append(line)
        if len(self._buffer) >= self._batch_size:
            await self._flush()

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if self._flusher is None or self._flusher.done():
            self._flusher = asyncio.get_running_loop().create_task(self._flush_loop())

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(self._flush_interval_s)
            await self._flush()

    async def _flush(self) -> None:
        if not self._buffer:
            return
        batch, self._buffer = self._buffer, []
        try:
            # 文件写入是阻塞操作，绝不能占用事件循环（否则会卡住 TUI）。
            await asyncio.to_thread(self._write_sync, batch)
            self._lines_written += len(batch)
        except Exception as exc:  # noqa: BLE001 - 任何写盘失败都不得中断会话
            self._write_failures += 1
            logger.warning("事件日志写入失败（第 %d 次）：%s", self._write_failures, exc)

    def _write_sync(self, batch: list[str]) -> None:
        with open(self._path, "a", encoding="utf-8", newline="\n") as handle:
            handle.write("\n".join(batch))
            handle.write("\n")

    async def aclose(self) -> None:
        """停掉后台任务并 flush 残余（**先 flush 再取消**，否则最后几条会丢）。"""
        if self._flusher is not None:
            self._flusher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._flusher
            self._flusher = None
        await self._flush()


def session_log_path(logs_dir: Path, session_id: str, *, now: float | None = None) -> Path:
    """``<logs>/2026-09-08/7f3a1b2c.jsonl``——按日期分层，避免单目录堆积成千上万文件。"""
    stamp = time.strftime("%Y-%m-%d", time.localtime(now if now is not None else time.time()))
    safe_id = "".join(char for char in session_id if char.isalnum() or char in "-_") or "session"
    return Path(logs_dir) / stamp / f"{safe_id}.jsonl"
