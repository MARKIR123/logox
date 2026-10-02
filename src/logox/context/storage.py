"""会话持久化日志与大对象 (Blob) 换页存储引擎。

追加 JSONL 记录并将工具输出归档到会话隔离目录。成功追加后提交行号，
写失败后重试先重建物理行号。JSONL 仍保存工具全文；没有 fsync、持久队列
或整体事务保证，不能承诺断电完整性或主日志始终轻量。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "TOOL_BLOB_THRESHOLD_BYTES",
    "SessionTranscriptWriter",
    "TranscriptLine",
]

#: 触发独立文件外置落盘的阈值 (1.5 KB)
TOOL_BLOB_THRESHOLD_BYTES = 1536

#: 从一行 JSONL 里抽 ``"turn": N``。刻意**不**做整行反序列化：
#: 重建行号映射只需要这一个字段，而大会话整行 `json.loads` 是不必要的开销。
_TURN_FIELD_RE = re.compile(r'"turn"\s*:\s*(\d+)')


@dataclass
class TranscriptLine:
    line: int
    turn: int
    step: int
    role: str
    event_type: str
    content: str = ""
    tool_name: str | None = None
    call_id: str | None = None
    blob_file: str | None = None
    is_error: bool | None = None
    meta: dict[str, Any] | None = None


class SessionTranscriptWriter:
    """会话 JSONL 追加与工具归档写出器。"""

    def __init__(
        self,
        base_dir: str | Path | None = None,
        session_id: str = "default_session",
        *,
        log_file: str | Path | None = None,
    ) -> None:
        # ★ D153：这里**故意没有默认值**。
        #
        # 曾经的默认值是相对路径 ``".logox/runs"`` —— 于是**每一个忘了传参的调用点**
        # 都会在当前工作目录里静默建目录：`logox chat` 每运行一次建一个 `chat-<pid>/`，
        # 一条测试每跑一次建一个 `recheck/`（实测仓库里积了 113 个空目录，且删了还会长回来）。
        #
        # 为什么不是"真正的必填位置参数"：多数调用点走 `log_file=`（测试为主），
        # 强制位置参数只会逼它们写 `base_dir=None` —— 那是**噪音**而不是安全。
        # 要消灭的是"静默 fallback"，**构造期抛错**已经足够响亮，而且消息自解释。
        if base_dir is None and log_file is None:
            raise ValueError(
                "SessionTranscriptWriter 必须显式指定 base_dir 或 log_file。"
                "（这里故意没有默认值：旧默认值 '.logox/runs' 是相对路径，"
                "会让忘了传参的调用点在当前工作目录里悄悄建目录。）"
                "落盘到用户目录请传 base_dir=paths.sessions，"
                "或直接给 log_file=<具体 JSONL 路径>。"
            )
        if log_file is not None:
            self.log_file = Path(log_file).resolve()
            self.session_dir = self.log_file.parent
        else:
            self.session_dir = Path(base_dir).resolve() / session_id
            self.log_file = self.session_dir / "transcript.jsonl"
        self.tools_dir = self.session_dir / "tools"
        self.blob_dir = self.tools_dir / hashlib.sha256(self.log_file.name.encode("utf-8")).hexdigest()[:20]
        self.current_line = 0
        self._append_failed = False
        #: ``turn -> (该轮首行, 该轮末行)``，**真实行号**（D6）。
        #: 压缩器靠它把归档索引里的行号写成真值，而不是启发式估算。
        self.turn_lines: dict[int, tuple[int, int]] = {}
        #: 轮次号在文件里**不连续地重复出现**的集合（F-32 的历史遗留）。
        #: 这类号无法区分是哪一次会话产生的，查行号时必须**拒给数字**而不是猜一个。
        self.collided_turns: set[int] = set()

        self._ensure_dirs()
        self._init_current_line()

    def switch_target(self, log_file: str | Path) -> None:
        """切换目标日志文件（供会话热切换重定向使用）。"""
        self.log_file = Path(log_file).resolve()
        self.session_dir = self.log_file.parent
        self.tools_dir = self.session_dir / "tools"
        self.blob_dir = self.tools_dir / hashlib.sha256(self.log_file.name.encode("utf-8")).hexdigest()[:20]
        self._ensure_dirs()
        self._init_current_line()

    def _ensure_dirs(self) -> None:
        try:
            self.blob_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("创建日志目录失败：%s", exc)

    def _init_current_line(self) -> None:
        """如果日志已存在，重建行号与「轮次 → 行号区间」映射。

        ⚠️ 重建这个映射是 `/resume` 之后仍能给出**真实行号**的前提（D6）。
        行号仍然是 1-based 且与 ``write_step`` 严格一致。
        """
        self.current_line = 0
        self._needs_separator = False
        self.turn_lines = {}
        self.collided_turns = set()
        if not self.log_file.is_file():
            return
        try:
            with open(self.log_file, encoding="utf-8", errors="replace") as f:
                for line in f:
                    self.current_line += 1
                    self._needs_separator = not line.endswith("\n")
                    match = _TURN_FIELD_RE.search(line)
                    if match is not None:
                        self._record_turn_line(int(match.group(1)))
        except OSError:
            self.current_line = 0
            self.turn_lines = {}

    def _record_turn_line(self, turn: int) -> None:
        """把当前行号并入 ``turn`` 的区间（首次出现时同时作为起点）。

        ⚠️ 如果同一个轮次号**不连续地**再次出现，说明这个号被两个进程复用过
        （F-32：每未同步轮次计数器，每个新进程都从 turn=1 开始）。
        这类号的区间是**没有意义的**（会从第一段的开头跨到第二段的结尾），
        所以把它记进 ``collided_turns``，查行号时直接拒给数字。
        """
        previous = self.turn_lines.get(turn)
        if previous is None:
            self.turn_lines[turn] = (self.current_line, self.current_line)
            return
        first, last = previous
        if self.current_line != last + 1:
            self.collided_turns.add(turn)
        self.turn_lines[turn] = (first, self.current_line)

    def turn_lines_of(self, from_turn: int, to_turn: int) -> tuple[int, int] | None:
        """返回 ``[from_turn, to_turn]`` 区间的**真实行号范围**；无记录时返回 ``None``。

        替掉早先的启发式估算（``current_line - len(messages) * 2`` 与 ``current_line``）
        —— 那两个数指向的是**最新**的行，而不是被归档区间的行，**会让模型读到错的内容
        而它不会发现自己错了**（F-18）。
        """
        spans = [
            self.turn_lines[turn]
            for turn in range(from_turn, to_turn + 1)
            if turn in self.turn_lines and turn not in self.collided_turns
        ]
        if not spans:
            return None
        # 区间里只要有一个轮次号是“带歧义”的，就整体不给数字 —— 宁可说“行号未知”，
        # 也不能让模型按一个错的区间去 fs_read（它会读到错的内容且不自知）。
        if any(turn in self.collided_turns for turn in range(from_turn, to_turn + 1)):
            return None
        return min(start for start, _ in spans), max(end for _, end in spans)

    def write_step(
        self,
        *,
        turn: int,
        step: int,
        role: str,
        event_type: str,
        content: str = "",
        tool_name: str | None = None,
        call_id: str | None = None,
        blob_file: str | None = None,
        is_error: bool | None = None,
        meta: dict[str, Any] | None = None,
        **extra: Any,
    ) -> int | None:
        """追加成功返回行号；失败不提交行号或轮次区间。"""
        if self._append_failed:
            self._init_current_line()
            self._append_failed = False
        next_line = self.current_line + 1
        record = {
            "line": next_line,
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
            needs_separator = self._needs_separator
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(("\n" if needs_separator else "") + json.dumps(record, ensure_ascii=False) + "\n")
                f.flush()
        except OSError as exc:
            logger.warning("写入 transcript.jsonl 失败：%s", exc)
            self._append_failed = True
            return None

        self.current_line = next_line
        self._needs_separator = False
        self._record_turn_line(turn)
        return self.current_line

    @staticmethod
    def _blob_filename(call_id: str) -> str:
        """``call_id`` → blob 文件名。**落盘与查询必须共用它**，否则两边会漂移。"""
        return f"tool_{call_id.replace('/', '_').replace(chr(92), '_')}.log"

    def blob_path_of(self, call_id: str) -> str | None:
        """若当前会话的工具文件已存在则返回其相对路径，否则 ``None``（F-17）。

        给压缩器用来**避免重复落盘**：持久化订阅者在工具结束时已经写过一次，
        压缩器只需要"确认它在"，而不是每次折叠都再写一遍。
        """
        filename = self._blob_filename(call_id)
        return (self.blob_dir / filename).relative_to(self.session_dir).as_posix() if (self.blob_dir / filename).is_file() else None

    def save_tool_blob(
        self,
        call_id: str,
        raw_output: str,
        *,
        force: bool = False,
    ) -> str | None:
        """将工具输出写入 tools/<日志命名空间>/tool_<安全 call_id>.log。

        若内容小于阈值且未强制落盘，返回 None 表示无需外置存储。
        返回相对于 session_dir 的相对路径，如 'tools/<命名空间>/tool_call_123.log'。
        """
        raw_bytes = len(raw_output.encode("utf-8", errors="replace"))
        if not force and raw_bytes < TOOL_BLOB_THRESHOLD_BYTES:
            return None

        blob_path = self.blob_dir / self._blob_filename(call_id)

        try:
            with open(blob_path, "w", encoding="utf-8", errors="replace") as f:
                f.write(raw_output)
            return blob_path.relative_to(self.session_dir).as_posix()
        except OSError as exc:
            logger.warning("保存工具 Blob 日志失败：%s", exc)
            return None
