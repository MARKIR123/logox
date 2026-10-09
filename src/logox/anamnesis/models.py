"""Neutral, bounded records shared by storage, runner and UI."""

from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SourceRef(Record):
    source_id: str
    kind: Literal[
        "user_message", "assistant_statement", "tool_result", "code", "manual_archive", "history_revision"
    ]
    project_id: str
    path: str
    session_id: str = ""
    line: int = 1
    end_line: int = 1
    digest: str
    content: str
    turn: int = 0
    turn_state: str = "unknown"
    offset: int = 0
    timestamp: float | None = None

    @field_validator("timestamp", mode="before")
    @classmethod
    def _known_timestamp(cls, value: object) -> float | None:
        """旧 0 哨兵及无效时刻均为未知，不伪装成 1970 年或当前时间。"""
        if value is None or isinstance(value, bool):
            return None
        try:
            stamp = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return stamp if math.isfinite(stamp) and stamp > 0 else None


class AnamesisAnalysisRecord(Record):
    item_id: str = ""  # Empty only for historical records.
    record_id: str = Field(min_length=1, max_length=120)
    stage_id: str = Field(min_length=1, max_length=120)
    question: str = Field(min_length=1, max_length=2000)
    rationale: str = Field(min_length=1, max_length=12000)
    evidence_summary: str = Field(default="", max_length=6000)
    source_ids: list[str] = Field(default_factory=list, max_length=100)
    alternatives: list[str] = Field(default_factory=list, max_length=20)
    conclusion: str = Field(min_length=1, max_length=6000)
    scope: Literal["user", "project", "research"] = "project"
    uncertainties: list[str] = Field(default_factory=list, max_length=30)
    decision: Literal["ignore", "candidate", "propose", "revise"] = "candidate"
    revises_record_id: str | None = None
    status: Literal["complete", "validated", "rejected", "interrupted"] = "complete"


class MemoryChange(Record):
    entry_id: str = Field(min_length=1, max_length=120, pattern=r"^[\w.-]+$")
    action: Literal["add", "replace", "delete"]
    scope: Literal["user", "project"]
    category: str = Field(default="当前状态", max_length=100)
    old_value: str = Field(default="", max_length=12000)
    new_value: str = Field(default="", max_length=12000)
    source_ids: list[str] = Field(min_length=1, max_length=100)
    rationale: str = Field(min_length=1, max_length=6000)
    analysis_record_id: str
    status: Literal["explicit", "observed", "candidate"] = "candidate"


class MemoryEntry(Record):
    entry_id: str
    category: str
    value: str
    source_ids: list[str] = Field(default_factory=list)


class MemoryProposal(Record):
    analyses: list[AnamesisAnalysisRecord] = Field(default_factory=list, max_length=50)
    changes: list[MemoryChange] = Field(default_factory=list, max_length=50)
    review_findings: list[str] = Field(default_factory=list, max_length=30)
    next_plan: list[str] = Field(default_factory=list, max_length=30)
    complete: bool


class AnamesisResearchItem(Record):
    item_id: str = Field(min_length=1, max_length=120, pattern=r"^[\w.-]+$")
    question: str = Field(min_length=1, max_length=2000)
    reason: str = Field(min_length=1, max_length=6000)
    scope: Literal["user", "project", "research"] = "project"
    origin_kind: Literal["source_review", "code_change", "dependency"] = "source_review"
    origin_source_ids: list[str] = Field(default_factory=list, max_length=100)
    code_snapshot_id: str = ""
    parent_item_ids: list[str] = Field(default_factory=list, max_length=50)
    dependency_reason: str = ""
    status: Literal["pending", "active", "resolved", "waiting_evidence"] = "pending"
    version: int = Field(default=0, ge=0)
    analysis_ids: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)
    conclusion: str = ""
    missing_evidence: str = ""


class AnamesisResearchUpdate(Record):
    item_id: str
    expected_version: int = Field(ge=0)
    status: Literal["active", "resolved", "waiting_evidence"]
    analysis_ids: list[str] = Field(default_factory=list, max_length=100)
    source_ids: list[str] = Field(default_factory=list, max_length=100)
    conclusion: str = Field(default="", max_length=6000)
    missing_evidence: str = Field(default="", max_length=6000)


class AnamesisResearchPlan(Record):
    items: list[AnamesisResearchItem] = Field(min_length=1, max_length=50)


class AnamesisSegmentResult(Record):
    kind: Literal["proposal", "checkpoint", "finished", "paused"]
    reason: str = ""
    proposal: MemoryProposal | None = None
    state_version: int = 0


class ArchiveSnapshot(Record):
    scope: str
    project_id: str
    digest: str
    text: str = ""
    entries: list[MemoryEntry] = Field(default_factory=list)
    managed: bool = False


class AnamesisStatus(Record):
    run_id: str = ""
    mode: str = ""
    phase: str = "idle"
    question: str = ""
    reason: str = ""
    model: str = ""
    started_at: float = 0
    steps: int = 0
    changes: int = 0
    remaining: int = 0


class AnamesisEvent(Record):
    schema_version: int = 1
    kind: str
    run_id: str
    mode: str = ""
    phase: str = ""
    question: str = ""
    reason: str = ""
    started_at: float = 0
    analysis: AnamesisAnalysisRecord | None = None
    change: MemoryChange | None = None
    result: str = ""
    report_path: str = ""
    session_id: str = ""
    sequence: int = 0
    timestamp: float = 0
    delta: str = ""
    operation: dict | None = None
    findings: list[str] = Field(default_factory=list)
    plan: list[str] = Field(default_factory=list)
    item: AnamesisResearchItem | None = None
    research_state: dict | None = None
    self_check: dict | None = None


def digest_text(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()
