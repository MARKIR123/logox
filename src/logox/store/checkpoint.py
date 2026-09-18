"""检查点数据结构与索引提取器（D102 / L4）。

负责解析会话事务记录（transcript.jsonl），提取各个轮次（Turn）的文件修改快照与
用户指令摘要，支持识别 session_rewind 截断标记。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "CheckpointTracker",
    "ConflictInfo",
    "FileSnapshot",
    "RewindResult",
    "TurnCheckpoint",
]


@dataclass(frozen=True)
class FileSnapshot:
    """单个文件在某次工具调用中的修改快照。"""

    path: str
    before_hash: str | None
    after_hash: str


@dataclass(frozen=True)
class TurnCheckpoint:
    """某一个对话轮次中产生的所有文件修改聚合与语义里程碑。"""

    turn: int
    files: list[FileSnapshot] = field(default_factory=list)
    user_prompt: str = ""
    created_at: float = 0.0
    turn_summary: str = ""


@dataclass(frozen=True)
class ConflictInfo:
    """外部漂移冲突信息。"""

    path: str
    disk_hash: str
    expected_hash: str


@dataclass(frozen=True)
class RewindResult:
    """回滚执行结果。"""

    success: bool
    to_turn: int
    restored_files: list[str] = field(default_factory=list)
    deleted_files: list[str] = field(default_factory=list)
    conflicts: list[ConflictInfo] = field(default_factory=list)
    message: str = ""


class CheckpointTracker:
    """从会话记录中提取和维护检查点与时空里程碑。"""

    @staticmethod
    def extract_turn_checkpoints(records: list[dict[str, Any]]) -> list[TurnCheckpoint]:
        """从会话 JSONL 记录中解析出所有对话轮次的检查点列表（包含纯对话轮次）。

        自动识别并应用 ``session_rewind`` 截断：被回滚掉的轮次不会出现在活跃列表中。
        返回结果按轮次升序排列。
        """
        all_turns: set[int] = set()
        user_prompts: dict[int, str] = {}
        turn_summaries: dict[int, str] = {}
        # turn -> dict[path, FileSnapshot]
        turn_snapshots: dict[int, dict[str, FileSnapshot]] = {}
        turn_timestamps: dict[int, float] = {}

        for record in records:
            event_type = record.get("type")
            turn = int(record.get("turn", 0))
            if turn > 0:
                all_turns.add(turn)

            ts = float(record.get("ts", 0.0) or 0.0)
            if turn > 0 and (turn not in turn_timestamps or ts > 0):
                turn_timestamps[turn] = ts

            # 1. 记录用户 prompt 摘要
            if record.get("role") == "user" or event_type in ("user_prompt", "UserPromptSubmit"):
                content = str(record.get("content", "")).strip()
                summary = content.splitlines()[0] if content else ""
                if len(summary) > 25:
                    summary = summary[:22] + "…"
                user_prompts[turn] = summary

            # 记录回合语义摘要
            if record.get("turn_summary"):
                turn_summaries[turn] = str(record["turn_summary"]).strip()
            elif event_type == "turn_finished" and record.get("content"):
                turn_summaries[turn] = str(record.get("content")).strip()

            # 2. 捕获检查点
            elif event_type == "checkpoint":
                path = str(record.get("path", "")).replace("\\", "/")
                before_hash = record.get("before_hash")
                after_hash = str(record.get("after_hash", ""))

                if turn not in turn_snapshots:
                    turn_snapshots[turn] = {}

                # 同一轮次内多次修改同一文件，保留初始 before_hash 与最新 after_hash
                if path in turn_snapshots[turn]:
                    old_snap = turn_snapshots[turn][path]
                    turn_snapshots[turn][path] = FileSnapshot(
                        path=path,
                        before_hash=old_snap.before_hash,
                        after_hash=after_hash,
                    )
                else:
                    turn_snapshots[turn][path] = FileSnapshot(
                        path=path,
                        before_hash=before_hash if before_hash is not None else None,
                        after_hash=after_hash,
                    )

            # 3. 截断标记（时空穿梭）：若发生回滚，截除 >= to_turn 的所有轮次
            elif event_type == "session_rewind":
                rewind_to = int(record.get("to_turn", 0))
                turns_to_drop = [t for t in all_turns if t >= rewind_to]
                for t in turns_to_drop:
                    all_turns.discard(t)
                    turn_snapshots.pop(t, None)
                    turn_timestamps.pop(t, None)
                    user_prompts.pop(t, None)
                    turn_summaries.pop(t, None)

        checkpoints: list[TurnCheckpoint] = []
        for t in sorted(all_turns):
            files = list(turn_snapshots.get(t, {}).values())
            prompt_text = user_prompts.get(t, f"轮次 {t}")
            summary_text = turn_summaries.get(t, "")
            checkpoints.append(
                TurnCheckpoint(
                    turn=t,
                    files=files,
                    user_prompt=prompt_text,
                    created_at=turn_timestamps.get(t, 0.0),
                    turn_summary=summary_text,
                )
            )
        return checkpoints
