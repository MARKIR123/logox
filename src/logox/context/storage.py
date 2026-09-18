"""会话持久化日志与大对象 (Blob) 换页存储引擎。

实现 WAL (Write-Ahead Log) 式单调追加 JSONL 记录（1 步 1 行，崩溃安全），
并将超过阈值的巨大工具输出外置落盘至独立日志文件（Out-of-Band Blob Store），
保证主会话日志始终轻量紧凑。
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "TOOL_BLOB_THRESHOLD_BYTES",
    "SessionTranscriptWriter",
    "TranscriptLine",
]

#: 触发独立文件外置落盘的阈值 (1.5 KB)
TOOL_BLOB_THRESHOLD_BYTES = 1536


@dataclass
class TranscriptLine:
    line: int
    turn: int
    step: int
    role: str
    event_type: str
    content: str = ""
    tool_name: Optional[str] = None
    call_id: Optional[str] = None
    blob_file: Optional[str] = None
    is_error: Optional[bool] = None
    meta: Optional[Dict[str, Any]] = None


class SessionTranscriptWriter:
    """会话事务日志追加器。"""

    def __init__(
        self,
        base_dir: str | Path = ".logox/runs",
        session_id: str = "default_session",
        *,
        log_file: str | Path | None = None,
    ) -> None:
        if log_file is not None:
            self.log_file = Path(log_file).resolve()
            self.session_dir = self.log_file.parent
        else:
            self.session_dir = Path(base_dir).resolve() / session_id
            self.log_file = self.session_dir / "transcript.jsonl"
        self.tools_dir = self.session_dir / "tools"
        self.current_line = 0

        self._ensure_dirs()
        self._init_current_line()

    def switch_target(self, log_file: str | Path) -> None:
        """切换目标日志文件（供会话热切换重定向使用）。"""
        self.log_file = Path(log_file).resolve()
        self.session_dir = self.log_file.parent
        self.tools_dir = self.session_dir / "tools"
        self._ensure_dirs()
        self._init_current_line()

    def _ensure_dirs(self) -> None:
        try:
            self.tools_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("创建日志目录失败：%s", exc)

    def _init_current_line(self) -> None:
        """如果日志已存在，统计已有行数以保证行号严格单调自增。"""
        if self.log_file.is_file():
            try:
                with open(self.log_file, "r", encoding="utf-8", errors="replace") as f:
                    self.current_line = sum(1 for _ in f)
            except OSError:
                self.current_line = 0

    def write_step(
        self,
        *,
        turn: int,
        step: int,
        role: str,
        event_type: str,
        content: str = "",
        tool_name: Optional[str] = None,
        call_id: Optional[str] = None,
        blob_file: Optional[str] = None,
        is_error: Optional[bool] = None,
        meta: Optional[Dict[str, Any]] = None,
        **extra: Any,
    ) -> int:
        """追加一行事件到 transcript.jsonl，返回所写入的行号。"""
        self.current_line += 1
        record = {
            "line": self.current_line,
            "turn": turn,
            "step": step,
            "role": role,
            "type": event_type,
        }
        if content:
            record["content"] = content
        if tool_name:
            record["tool"] = tool_name
        if call_id:
            record["call_id"] = call_id
        if blob_file:
            record["blob"] = blob_file
        if is_error is not None:
            record["is_error"] = is_error
        if meta:
            record["meta"] = meta
        if extra:
            record.update(extra)

        try:
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
                f.flush()
        except OSError as exc:
            logger.warning("写入 transcript.jsonl 失败：%s", exc)

        return self.current_line

    def save_tool_blob(
        self,
        call_id: str,
        raw_output: str,
        *,
        force: bool = False,
    ) -> Optional[str]:
        """将超大工具输出写入独立文件 tools/<call_id>.log。

        若内容小于阈值且未强制落盘，返回 None 表示无需外置存储。
        返回相对于 session_dir 的相对路径，如 'tools/call_123.log'。
        """
        raw_bytes = len(raw_output.encode("utf-8", errors="replace"))
        if not force and raw_bytes < TOOL_BLOB_THRESHOLD_BYTES:
            return None

        clean_id = call_id.replace("/", "_").replace("\\", "_")
        filename = f"tool_{clean_id}.log"
        blob_path = self.tools_dir / filename

        try:
            with open(blob_path, "w", encoding="utf-8", errors="replace") as f:
                f.write(raw_output)
            return f"tools/{filename}"
        except OSError as exc:
            logger.warning("保存工具 Blob 日志失败：%s", exc)
            return None
