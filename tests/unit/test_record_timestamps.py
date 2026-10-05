"""Occurrence time survives persistence; unknown legacy dates are read-only placeholders."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from logox.context.storage import SessionTranscriptWriter
from logox.kernel import events as ev
from logox.store.manager import SessionManager
from logox.store.persistence import SessionPersistenceSubscriber
from logox.store.replay import load_session_records, reconstruct_messages


@pytest.mark.parametrize(
    ("event_class", "fields", "record_type"),
    [
        (ev.UserPromptSubmit, {"text": "任务", "text_chars": 2}, "user_prompt"),
        (
            ev.ModelRequestFinished,
            {"usage": ev.Usage(input_tokens=1, output_tokens=1), "duration_ms": 3},
            "model_output",
        ),
        (
            ev.ToolCallFinished,
            {"call_id": "c1", "ok": True, "duration_ms": 4, "content": "观察结果"},
            "tool_result",
        ),
        (ev.CompactionFinished, {"tokens_after": 2, "message_count_after": 1}, "compaction"),
        (ev.CheckpointCreated, {"files": ["test.py"]}, "checkpoint"),
        (ev.RewindPerformed, {"to_turn": 1}, "session_rewind"),
        (
            ev.TurnFinished,
            {
                "turn_index": 1,
                "duration_ms": 5,
                "tool_call_count": 0,
                "usage": ev.Usage(input_tokens=1, output_tokens=1),
                "turn_summary": "已整理",
            },
            "turn_finished",
        ),
    ],
    ids=["user", "model", "tool", "compaction", "checkpoint", "rewind", "turn"],
)
def test_all_persisted_events_keep_occurrence_time(tmp_path, event_class, fields, record_type):
    path = tmp_path / "conversation.jsonl"
    writer = SessionTranscriptWriter(log_file=path)
    subscriber = SessionPersistenceSubscriber(writer)
    subscriber.apply(ev.ModelDelta(session_id="s", turn=1, kind="text", delta="已输出正文", request_index=1))
    event = event_class(session_id="s", turn=1, ts=1700000000.125, **fields)
    # Saving can happen much later than the original event; never replace that time.
    with patch("logox.context.storage.time.time", return_value=1800000000):
        subscriber.apply(event)
    record = json.loads(path.read_text(encoding="utf-8").split("\n")[0])
    assert record["type"] == record_type
    assert record["timestamp"] == event.ts
    if record_type == "model_output":
        assert record["content"] == "已输出正文"
    if record_type == "tool_result":
        assert record["meta"]["duration_ms"] == 4


@pytest.mark.parametrize("record_type", ["context_state", "anamnesis_ref"])
def test_internal_records_have_creation_time(tmp_path, record_type):
    writer = SessionTranscriptWriter(log_file=tmp_path / "internal.jsonl")
    with patch("logox.context.storage.time.time", return_value=1700000001.25):
        writer.write_step(turn=0, step=0, role="system", event_type=record_type)
    record = json.loads(writer.log_file.read_text(encoding="utf-8"))
    assert record["timestamp"] == 1700000001.25


def test_explicit_unknown_time_is_not_replaced_by_current_time(tmp_path):
    writer = SessionTranscriptWriter(log_file=tmp_path / "unknown.jsonl")
    writer.write_step(turn=1, step=1, role="assistant", event_type="model_output", timestamp=None)
    record = json.loads(writer.log_file.read_text(encoding="utf-8"))
    assert "timestamp" in record
    assert record["timestamp"] is None


def test_new_session_header_uses_the_same_time_as_created_at(tmp_path):
    info = SessionManager(tmp_path / "sessions").create_session(tmp_path / "project")
    header = json.loads(info.file_path.read_text(encoding="utf-8").split("\n")[0])
    assert header["timestamp"] == header["created_at"] == info.created_at


def test_legacy_placeholders_preserve_bytes_mtime_messages_and_known_times(tmp_path):
    path = tmp_path / "legacy.jsonl"
    records = [
        {"type": "user_prompt", "role": "user", "turn": 1, "content": "旧问题", "ts": 1700000000},
        {"type": "model_output", "role": "assistant", "turn": 1, "content": "旧回答"},
        {
            "type": "tool_result",
            "role": "tool",
            "turn": 1,
            "call_id": "c1",
            "tool": "read",
            "content": "旧观察",
        },
        {"type": "compaction", "role": "system", "turn": 1, "timestamp": None, "ts": 1700000010},
        {"type": "checkpoint", "role": "system", "turn": 1, "timestamp": 1700000020},
    ]
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n", encoding="utf-8")
    original, stat = path.read_bytes(), path.stat()
    loaded = load_session_records(path)
    assert [r["timestamp"] for r in loaded] == [1700000000, None, None, None, 1700000020]
    assert path.read_bytes() == original
    assert path.stat().st_mtime_ns == stat.st_mtime_ns
    assert [m.model_dump() for m in reconstruct_messages(loaded)] == [
        m.model_dump() for m in reconstruct_messages(records)
    ]
