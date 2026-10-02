"""2026-09-29 审计回归：数据保留、真实边界调用与有界处理。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from logox.app import Runtime, build_runtime
from logox.config.schema import LogoxConfig, ProviderConfig
from logox.context.compaction import PRUNE_MIN_CHARS, Compactor
from logox.context.tokens import TokenEstimator, estimate_text_tokens
from logox.kernel.bus import EventBus
from logox.kernel.messages import Message, MessageMeta, ReasoningBlock, TextBlock, ToolResultBlock
from logox.kernel.registry import ToolRegistry
from logox.kernel.scheduler import AllowAllDecider, Scheduler
from logox.kernel.turn import Turn
from logox.mcp.adapter import McpMetaTool
from logox.paths import LogoxPaths
from logox.providers.base import ToolCallBuffer, ToolCallEvent
from logox.store.blob import BlobStore
from logox.store.manager import SessionManager
from logox.store.rewind import execute_rewind
from logox.tools.base import ToolContext, ToolResult
from logox.tools.fs_grep import GrepArgs, GrepTool


def test_compaction_skips_estimation_only_with_valid_measured_tokens():
    estimator = TokenEstimator()
    compactor = Compactor(estimator=estimator)
    messages = [Message(role="user", blocks=[TextBlock(text="hello")])]
    with patch.object(estimator, "estimate_messages", wraps=estimator.estimate_messages) as estimate:
        result = compactor.compact(messages, current_tokens=42)
        assert (result.strategy, result.tokens_before, result.tokens_after) == ("none", 42, 42)
        estimate.assert_not_called()
        for value in (None, 0, -1):
            result = compactor.compact(messages, current_tokens=value)
            assert result.tokens_before == estimator.estimate_messages(messages)
        assert estimate.call_count == 6  # 三次业务估算 + 三次参照结果。


def test_reasoning_filter_preserves_metadata_and_independent_blocks():
    meta = MessageMeta(turn_summary="decision", transcript_line=7)
    message = Message(role="assistant", blocks=[ReasoningBlock(text="hidden"), TextBlock(text="answer")], meta=meta)
    result = Compactor()._strip_reasoning([message])
    assert result[0].text == "answer"
    assert result[0].meta == meta
    result[0].blocks.append(TextBlock(text="new"))
    assert len(message.blocks) == 2
    assert isinstance(message.blocks[0], ReasoningBlock)
    assert Compactor()._strip_reasoning([Message(role="assistant", blocks=[ReasoningBlock(text="hidden")])]) == []


def test_archive_write_failure_keeps_original_result():
    writer = Mock()
    writer.blob_path_of.return_value = None
    writer.save_tool_blob.return_value = None
    block = ToolResultBlock(id="call-1", content="evidence\n" * (PRUNE_MIN_CHARS + 1))
    message = Message(role="tool", blocks=[block])
    compactor = Compactor(transcript_writer=writer, keep_recent_tool_results=0)
    result, count = compactor._prune_tool_results([message])
    assert count == 0
    assert result[0].blocks[0] == block
    assert not result[0].blocks[0].archived
    writer.save_tool_blob.assert_called_once_with(block.id, block.content, force=True)


@pytest.mark.parametrize("text, expected", [("", 0), ("ASCII", 1), ("中英文abc", 3), ("😀abc", 1), ("Ａ，。", 3)])
@pytest.mark.parametrize("factor", [0.5, 1.0, 1.75])
def test_text_token_estimation_preserves_mixed_language_formula(text, expected, factor):
    cjk = sum(1 for char in text if "\u4e00" <= char <= "\u9fff" or "\u3000" <= char <= "\u303f" or "\uff00" <= char <= "\uffef")
    expected = max(1, int((cjk + (len(text) - cjk) * 0.28) * factor)) if text else 0
    assert estimate_text_tokens(text, factor) == expected


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"])
def test_digest_keeps_first_line_for_unicode_line_breaks(newline):
    assert ToolResult(ok=True, content="  first" + newline + "later" * 5000 + "  ").digest == "first"


@pytest.mark.parametrize("size", [0, 79, 80, 81, 100000])
def test_digest_preserves_clipping_boundary(size):
    expected = "x" * size if size <= 80 else "x" * 77 + "…"
    assert ToolResult(ok=True, content="  " + "x" * size + "  ").digest == expected


def test_tool_call_fragments_remain_readable_assignable_and_finalizable():
    buffer = ToolCallBuffer(2, name="fs_write")
    raw = json.dumps({"content": "x" * 18000, "path": "a.txt"})
    for offset in range(0, len(raw), 3):
        buffer.merge(fragment=raw[offset:offset + 3])
    assert buffer.arguments == raw
    assert buffer.finalize().arguments == json.loads(raw)
    assert buffer.finalize().call_id == "call_2"
    buffer.arguments = '{"path":'
    assert isinstance(buffer.finalize(), dict)
    buffer.merge(fragment='"b.txt"}')
    assert buffer.finalize().arguments == {"path": "b.txt"}
    buffer.arguments = ""
    assert buffer.finalize().arguments == {}


def test_grep_global_limit_is_not_a_per_file_limit(tmp_path):
    for index in range(5):
        (tmp_path / f"file-{index}.txt").write_text("needle\n" * 4, encoding="utf8")
    result = asyncio.run(GrepTool().run(GrepArgs(pattern="needle", max_matches=3), ToolContext(cwd=tmp_path)))
    assert result.ok
    assert result.display.payload["count"] == 3
    assert result.display.payload["truncated"]


def test_grep_binary_data_is_not_exposed(tmp_path):
    target = tmp_path / "binary.dat"
    target.write_bytes(b"needle\x00" * 10000)
    result = asyncio.run(GrepTool().run(GrepArgs(pattern="needle", path=target.name), ToolContext(cwd=tmp_path)))
    assert result.ok
    assert result.display.payload["count"] == 0


def test_grep_cancelled_in_empty_directory(tmp_path):
    result = asyncio.run(GrepTool().run(GrepArgs(pattern="needle"), ToolContext(cwd=tmp_path, is_cancelled=lambda: True)))
    assert not result.ok
    assert result.error.category.value == "cancelled"


def test_recent_session_matches_full_listing_without_parsing_older_logs(tmp_path):
    manager = SessionManager(tmp_path / "sessions")
    directory = manager.get_project_dir(tmp_path)
    directory.mkdir(parents=True)
    for index in range(5):
        path = directory / f"session-{index}.jsonl"
        path.write_text(json.dumps({"type": "session_init", "title": str(index)}) + "\n", encoding="utf8")
        os.utime(path, (100 + index, 100 + index))
    expected = manager.list_sessions(tmp_path)[0]
    with patch.object(manager, "scan_session_metadata", wraps=manager.scan_session_metadata) as scan:
        assert manager.find_most_recent(tmp_path) == expected
        scan.assert_called_once_with(expected.file_path, cwd=str(tmp_path))
    # 同时间保持 glob 枚举顺序；最新文件不可读时尝试次新，不改变列表接口。
    for path in directory.glob("*.jsonl"):
        os.utime(path, (100, 100))
    assert manager.find_most_recent(tmp_path) == manager.list_sessions(tmp_path)[0]
    original = manager.scan_session_metadata
    first = list(directory.glob("*.jsonl"))[0]
    with patch.object(manager, "scan_session_metadata", side_effect=lambda path, cwd: None if path == first else original(path, cwd)):
        assert manager.find_most_recent(tmp_path).file_path != first


@pytest.mark.parametrize("action", ["list", "call"])
def test_mcp_runs_through_real_scheduler_after_parameter_validation(tmp_path, action):
    client = SimpleNamespace(list_tools=AsyncMock(return_value=[{"name": "echo", "inputSchema": {"properties": {"message": {"type": ["string", "null"]}}}}]), call_tool=AsyncMock(return_value=ToolResult(ok=True, content="reply")))
    manager = SimpleNamespace(clients={"local": client}, get_client=lambda name: client, generate_meta_tool_description=lambda: "local tools")
    registry = ToolRegistry()
    registry.register(McpMetaTool(manager))
    scheduler = Scheduler(EventBus(session_id="audit"), registry, AllowAllDecider(), cwd=tmp_path)
    call = ToolCallEvent(call_id="mcp-1", name="mcp", arguments={"server": "local", "action": action, "tool": "echo", "arguments": {"message": "hello"}})
    results = asyncio.run(scheduler.run_batch(Turn(1), [call]))
    assert results[0].ok
    if action == "call":
        assert results[0].content == "reply"
        client.call_tool.assert_awaited_once_with("echo", {"message": "hello"})
    else:
        assert "echo" in results[0].content
        assert "str | null" in results[0].content
        client.list_tools.assert_awaited_once()


def test_runtime_wires_summarizer_into_normal_kernel_context_path(tmp_path):
    callback = AsyncMock(return_value="memo")
    config = LogoxConfig(provider=ProviderConfig(name="deepseek", model="deepseek-chat"))
    with patch.object(Runtime, "create_memo_summarizer", return_value=callback) as create:
        runtime = build_runtime(SimpleNamespace(config=config, sources=[]), tmp_path, LogoxPaths.at(tmp_path / ".logox"))
    assert isinstance(runtime, Runtime)
    create.assert_called_once()
    assert runtime.kernel.memo_summarizer is callback
    builder = runtime.context_builder
    with patch.object(builder, "build_async", wraps=builder.build_async) as build:
        asyncio.run(runtime.kernel._build_context(Turn(1)))
    assert build.call_args.kwargs["summarizer"] is callback


def test_rewind_missing_snapshot_prevents_all_file_changes(tmp_path):
    file = tmp_path / "a.txt"
    file.write_bytes(b"current")
    created = tmp_path / "new.txt"
    created.write_bytes(b"new")
    records = [
        {"type": "checkpoint", "turn": 1, "path": "new.txt", "before_hash": None, "after_hash": hashlib.sha256(b"new").hexdigest()},
        {"type": "checkpoint", "turn": 1, "path": "a.txt", "before_hash": "0" * 64, "after_hash": hashlib.sha256(b"current").hexdigest()},
    ]
    result = execute_rewind(records, 1, tmp_path, BlobStore(tmp_path / "blobs"))
    assert not result.success
    assert created.read_bytes() == b"new"
    assert file.read_bytes() == b"current"
    assert result.deleted_files == []


@pytest.mark.parametrize("failure", [False, OSError("disk full")])
def test_rewind_failed_restore_reports_failure(tmp_path, failure):
    store = BlobStore(tmp_path / "blobs")
    before = store.put_bytes(b"original")
    (tmp_path / "a.txt").write_bytes(b"current")
    records = [{"type": "checkpoint", "turn": 1, "path": "a.txt", "before_hash": before, "after_hash": hashlib.sha256(b"current").hexdigest()}]
    kwargs = {"side_effect": failure} if isinstance(failure, OSError) else {"return_value": failure}
    with patch.object(store, "restore_to_file", **kwargs):
        result = execute_rewind(records, 1, tmp_path, store)
    assert not result.success
    assert result.restored_files == []
    assert (tmp_path / "a.txt").read_bytes() == b"current"


def test_rewind_failed_delete_reports_failure(tmp_path):
    (tmp_path / "a.txt").write_bytes(b"current")
    records = [{"type": "checkpoint", "turn": 1, "path": "a.txt", "before_hash": None, "after_hash": hashlib.sha256(b"current").hexdigest()}]
    store = BlobStore(tmp_path / "blobs")
    with patch.object(Path, "unlink", side_effect=OSError("access denied")):
        result = execute_rewind(records, 1, tmp_path, store)
    assert not result.success
    assert result.deleted_files == []
    assert (tmp_path / "a.txt").exists()


def test_runtime_failed_rewind_does_not_commit_history_or_event(tmp_path):
    config = LogoxConfig(provider=ProviderConfig(name="deepseek", model="deepseek-chat"))
    runtime = build_runtime(SimpleNamespace(config=config, sources=[]), tmp_path, LogoxPaths.at(tmp_path / ".logox"))
    original = Message(role="user", blocks=[TextBlock(text="preserve my task")])
    runtime.kernel.history.append(original)
    (tmp_path / "a.txt").write_bytes(b"current")
    log_file = tmp_path / "history.jsonl"
    record = {"type": "checkpoint", "turn": 1, "path": "a.txt", "before_hash": "0" * 64, "after_hash": hashlib.sha256(b"current").hexdigest()}
    log_file.write_text(json.dumps(record) + "\n", encoding="utf8")
    runtime.resume_file = log_file
    with patch.object(runtime.bus, "publish", new=AsyncMock()) as publish:
        result = asyncio.run(runtime.rewind(1))
    assert not result.success
    assert runtime.kernel.history == [original]
    publish.assert_not_awaited()
    assert log_file.read_text(encoding="utf8") == json.dumps(record) + "\n"
