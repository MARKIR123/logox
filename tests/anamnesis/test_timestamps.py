"""Anamnesis sees real dates or explicit unknowns without rewriting provenance."""

from __future__ import annotations

import asyncio
import json
import threading
from datetime import datetime
from unittest.mock import patch

import pytest

from logox.anamnesis.models import (
    AnamesisAnalysisRecord,
    MemoryChange,
    MemoryProposal,
    SourceRef,
    digest_text,
)
from logox.anamnesis.runner import AnamesisRunner
from logox.anamnesis.sources import SourceCollector
from logox.context.storage import SessionTranscriptWriter
from logox.kernel import events as ev
from logox.providers.base import DeltaEvent, StopEvent
from logox.store.manager import SessionManager
from logox.store.persistence import SessionPersistenceSubscriber
from tests.anamnesis.support import research


@pytest.fixture
def project_history(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    sessions = tmp_path / "sessions"
    info = SessionManager(sessions).create_session(project)
    return project, sessions, info.file_path


def append(path, record):
    raw = json.dumps(record, ensure_ascii=False) + "\n"
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(raw)
    return raw


@pytest.mark.parametrize(
    "value", [None, 0, -1, "", "unknown", "nan", float("nan"), float("inf"), float("-inf"), True, False]
)
def test_unknown_or_invalid_source_dates_are_null_and_cannot_establish_freshness(project_history, value):
    project, sessions, path = project_history
    append(path, {"role": "user", "turn": 1, "content": "历史项目状态", "timestamp": value})
    collector = SourceCollector(sessions, project)
    refs = collector.collect()
    assert len(refs) == 1
    assert refs[0].model_dump()["timestamp"] is None
    assert collector.project_latest_timestamp == 0
    assert collector.last_submission(path.stem) == 0
    assert "时间不明确" in collector.project_freshness_reason(refs, latest=1700000000)
    # The old checkpoint schema used numeric zero; reloading it keeps an explicit unknown.
    restored = SourceRef.model_validate({**refs[0].model_dump(), "timestamp": value})
    assert restored.timestamp is None


@pytest.mark.parametrize(
    ("record", "expected"),
    [
        ({"timestamp": 1700000000.25}, 1700000000.25),
        ({"timestamp": "1700000000.25"}, 1700000000.25),
        ({"ts": 1700000000.25}, 1700000000.25),
        (
            {"timestamp": "2026-10-05T12:00:00+08:00"},
            datetime.fromisoformat("2026-10-05T12:00:00+08:00").timestamp(),
        ),
        ({"timestamp": None, "ts": 1700000000}, None),
    ],
)
def test_existing_dates_remain_usable_and_explicit_null_is_not_guessed(project_history, record, expected):
    project, sessions, path = project_history
    append(path, {"role": "tool", "turn": 1, "content": "读取观察", **record})
    refs = SourceCollector(sessions, project).collect()
    assert refs[0].timestamp == expected


def test_missing_dates_use_null_without_changing_bytes_or_source_identity(project_history):
    project, sessions, path = project_history
    raw = append(path, {"role": "assistant", "turn": 1, "content": "旧回复"})
    original, stat = path.read_bytes(), path.stat()
    collector = SourceCollector(sessions, project)
    ref = collector.collect()[0]
    assert ref.timestamp is None
    assert ref.digest == digest_text(raw)
    assert collector.verify(ref.source_id)
    assert collector.collect()[0].source_id == ref.source_id
    assert path.read_bytes() == original
    assert path.stat().st_mtime_ns == stat.st_mtime_ns


def test_persisted_tool_time_is_available_as_project_evidence_without_promoting_assistant_dates(
    project_history,
):
    project, sessions, path = project_history
    writer = SessionTranscriptWriter(log_file=path)
    subscriber = SessionPersistenceSubscriber(writer)
    subscriber.apply(
        ev.UserPromptSubmit(session_id=path.stem, turn=1, ts=1000, text="读取文件", text_chars=4)
    )
    subscriber.apply(ev.ToolCallRequested(session_id=path.stem, turn=1, name="read", call_id="c1"))
    subscriber.apply(
        ev.ToolCallFinished(
            session_id=path.stem, turn=1, ts=2000, call_id="c1", ok=True, duration_ms=10, content="实际观察"
        )
    )
    subscriber.apply(
        ev.ModelDelta(session_id=path.stem, turn=1, kind="text", delta="模型转述", request_index=1)
    )
    subscriber.apply(
        ev.ModelRequestFinished(
            session_id=path.stem,
            turn=1,
            ts=9000,
            usage=ev.Usage(input_tokens=1, output_tokens=1),
            duration_ms=10,
        )
    )
    collector = SourceCollector(sessions, project)
    refs = collector.collect()
    tool = next(ref for ref in refs if ref.kind == "tool_result")
    assistant = next(ref for ref in refs if ref.kind == "assistant_statement")
    assert tool.timestamp == 2000
    assert assistant.timestamp == 9000
    assert collector.project_latest_timestamp == 2000
    assert collector.project_freshness_reason([tool], latest=2000) == ""
    assert collector.project_freshness_reason([assistant], latest=2000)


class RecordingProvider:
    def __init__(self):
        self.requests = []

    async def stream(self, request):
        self.requests.append(request)
        payload = json.loads(request.messages[0].text)
        response = (
            {"accepted_entry_ids": [change["entry_id"] for change in payload["changes"]], "reasons": {}}
            if "changes" in payload
            else {"complete": True, "analyses": [], "changes": []}
        )
        yield DeltaEvent(kind="text", text=json.dumps(response))
        yield StopEvent(stop_reason="end_turn")


async def noop(_):
    pass


def test_organizing_and_review_requests_receive_current_time_and_unknown_placeholders(project_history):
    asyncio.run(_verify_request_times(project_history))


async def _verify_request_times(project_history):
    project, sessions, path = project_history
    append(path, {"role": "user", "turn": 1, "content": "我喜欢古典艺术"})
    collector = SourceCollector(sessions, project)
    refs = collector.collect()
    provider = RecordingProvider()
    runner = AnamesisRunner(provider, "local-test", 32768, collector)
    with patch("logox.anamnesis.runner.time.time", return_value=1800000000.25):
        proposal = await runner.run_segment(
            research_state=research(refs),
            state_event=noop,
            sources=refs,
            snapshots={},
            stop=threading.Event(),
            emit=noop,
            operation=noop,
        )
    assert proposal.proposal.complete
    analysis = AnamesisAnalysisRecord(
        record_id="a",
        stage_id="review",
        question="偏好是否明确？",
        rationale="用户直接表述",
        conclusion="长期偏好有依据",
        source_ids=[refs[0].source_id],
    )
    change = MemoryChange(
        entry_id="user.style",
        action="add",
        scope="user",
        new_value="喜欢古典艺术",
        rationale="用户直接表述",
        source_ids=[refs[0].source_id],
        analysis_record_id="a",
        status="explicit",
    )
    with patch("logox.anamnesis.runner.time.time", return_value=1800000001.5):
        accepted, reasons = await runner.validate(
            MemoryProposal(complete=True, analyses=[analysis], changes=[change]), {}
        )
    assert accepted == [change]
    assert reasons == {}
    assert len(provider.requests) == 2
    for request, stamp in zip(provider.requests, [1800000000.25, 1800000001.5], strict=True):
        payload = json.loads(request.messages[0].text)
        assert payload["sources"][0]["timestamp"] is None
        assert payload["current_timestamp"] == stamp
        parsed = datetime.fromisoformat(payload["current_time"])
        assert parsed.tzinfo is not None
        assert parsed.timestamp() == int(stamp)
        assert "时间未知" in request.system
        assert "偏好不按项目" in request.system
