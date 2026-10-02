"""Extreme-window policy: summary-only history, local memo and honest failure."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from logox.app import Runtime
from logox.config.schema import LogoxConfig
from logox.context.builder import HierarchicalContextBuilder
from logox.context.compaction import MEMO_MARKER, TRUNCATE_MARKER, Compactor, format_messages_for_summary
from logox.context.storage import SessionTranscriptWriter
from logox.kernel.messages import Message, MessageMeta, TextBlock, ToolResultBlock, ToolUseBlock
from logox.providers.base import DeltaEvent, StopEvent


def history(turns=15, chars=2500, summary_chars=20):
    result = []
    for i in range(turns):
        result.extend(
            [
                Message(role="user", blocks=[TextBlock(text=f"QUESTION{i}:" + "X" * chars)]),
                Message(
                    role="assistant",
                    blocks=[TextBlock(text="ANSWER")],
                    meta=MessageMeta(turn_summary=f"SUMMARY{i}:" + "S" * summary_chars),
                ),
            ]
        )
    return result


def run(coro):
    return asyncio.run(coro)


def test_normal_context_does_not_call_local_model():
    model = AsyncMock()
    result = run(
        Compactor(window_capacity=32768, reserve_tokens=2000).compact_async(
            history(4, 50), force=True, memo_summarizer=model
        )
    )
    assert model.call_count == 0
    assert result.strategy in {"prune", "prune+fold"}


def test_summary_only_drops_all_old_user_anchors_without_request():
    model = AsyncMock()
    source = history()
    result = run(
        Compactor(window_capacity=16384, reserve_tokens=2000).compact_async(
            source, force=True, memo_summarizer=model
        )
    )
    assert result.strategy == "summary-only"
    assert model.call_count == 0
    assert all(f"SUMMARY{i}:" in "\n".join(m.text for m in result.messages) for i in range(14))
    assert all(source[i * 2].text not in [m.text for m in result.messages] for i in range(14))
    assert result.messages[-2:] == source[-2:]
    assert result.tokens_after < result.tokens_before


def test_local_memo_only_when_summaries_still_exceed_budget():
    source = history(15, 4000, 2500)
    compactor = Compactor(window_capacity=6000, reserve_tokens=1000)
    model = AsyncMock(return_value="All prior changes preserved in one memo")
    result = run(compactor.compact_async(source, force=True, memo_summarizer=model))
    assert model.call_count == 1
    sent = model.call_args.args[0]
    assert all(f"SUMMARY{i}:" in format_messages_for_summary(sent) for i in range(14))
    assert "QUESTION0:" not in format_messages_for_summary(sent)
    assert source[-2].text not in format_messages_for_summary(sent)
    assert result.strategy == "local-memo"
    assert len(result.messages) == 3
    assert result.messages[0].text.startswith(MEMO_MARKER)
    assert result.messages[-2:] == source[-2:]
    assert result.tokens_after < compactor.high_watermark


def test_local_failure_retains_every_summary_and_no_hard_truncation():
    source = history(15, 4000, 2500)
    compactor = Compactor(window_capacity=6000, reserve_tokens=1000)
    model = AsyncMock(side_effect=RuntimeError("local unavailable"))
    result = run(compactor.compact_async(source, force=True, memo_summarizer=model))
    assert model.call_count == 1
    assert result.strategy == "summary-only" and result.degraded
    assert result.tokens_after >= compactor.high_watermark
    text = "\n".join(m.text for m in result.messages)
    assert all(f"SUMMARY{i}:" in text for i in range(14))
    assert TRUNCATE_MARKER not in text


def test_old_memo_body_is_in_next_local_input():
    previous = Message(
        role="assistant",
        blocks=[TextBlock(text=MEMO_MARKER + "\nEARLY_CRITICAL_DECISION")],
        meta=MessageMeta(source="compaction", turn_summary="generic label"),
    )
    assert "EARLY_CRITICAL_DECISION" in format_messages_for_summary([previous])
    model = AsyncMock(return_value="new memo")
    run(
        Compactor(window_capacity=6000, reserve_tokens=1000).compact_async(
            [previous, *history(15, 4000, 2500)], force=True, memo_summarizer=model
        )
    )
    assert model.call_count == 1
    assert "EARLY_CRITICAL_DECISION" in format_messages_for_summary(model.call_args.args[0])


def test_summary_only_cache_survives_next_turn(tmp_path):
    source = history(chars=4000)
    builder = HierarchicalContextBuilder(
        cwd=tmp_path,
        transcript_writer=SessionTranscriptWriter(log_file=tmp_path / "session.jsonl"),
        window_capacity=16384,
        reserve_tokens=2000,
    )
    first = run(builder.build_async(source, summarizer=AsyncMock()))
    assert first.compaction.strategy == "summary-only"
    source.extend(history(1, 5))
    unused = AsyncMock()
    second = run(builder.build_async(source, summarizer=unused))
    assert unused.call_count == 0
    assert "SUMMARY0:" in "\n".join(m.text for m in second.messages)
    assert source[-2:] == second.messages[-2:]


def test_all_tool_results_offloaded_even_current_short_output(tmp_path):
    source = history()
    source.insert(1, Message(role="assistant", blocks=[ToolUseBlock(id="old", name="read", input={})]))
    source.insert(2, Message(role="tool", blocks=[ToolResultBlock(id="old", content="OLD_PAYLOAD")]))
    source.insert(
        len(source) - 1, Message(role="assistant", blocks=[ToolUseBlock(id="current", name="read", input={})])
    )
    source.insert(
        len(source) - 1,
        Message(role="tool", blocks=[ToolResultBlock(id="current", content="CURRENT_PAYLOAD")]),
    )
    writer = SessionTranscriptWriter(log_file=tmp_path / "session.jsonl")
    result = run(
        Compactor(window_capacity=16384, reserve_tokens=2000, transcript_writer=writer).compact_async(
            source, force=True
        )
    )
    assert result.strategy == "summary-only"
    for call_id, payload in [("old", "OLD_PAYLOAD"), ("current", "CURRENT_PAYLOAD")]:
        pointer = writer.blob_path_of(call_id)
        assert pointer and (writer.session_dir / pointer).read_text(encoding="utf-8") == payload
    current = [b for m in result.messages for b in m.blocks if isinstance(b, ToolResultBlock)]
    assert len(current) == 1 and current[0].id == "current" and current[0].archived
    assert "CURRENT_PAYLOAD" not in current[0].content
    assert any(isinstance(b, ToolUseBlock) and b.id == "current" for m in result.messages for b in m.blocks)


def test_archive_failure_keeps_original_tool_payload(tmp_path):
    source = history()
    source.insert(1, Message(role="tool", blocks=[ToolResultBlock(id="failed", content="KEEP_ME")]))
    writer = SessionTranscriptWriter(log_file=tmp_path / "session.jsonl")
    writer.save_tool_blob = MagicMock(return_value=None)
    result = run(
        Compactor(window_capacity=16384, reserve_tokens=2000, transcript_writer=writer).compact_async(
            source, force=True
        )
    )
    assert result.strategy == "archive-blocked"
    assert result.folded_from_index == 0
    assert any(
        isinstance(b, ToolResultBlock) and b.content == "KEEP_ME" for m in result.messages for b in m.blocks
    )


class RecordingProvider:
    def __init__(self):
        self.requests = []

    async def stream(self, request):
        self.requests.append(request)
        yield DeltaEvent(kind="text", text="LOCAL_MEMO")
        yield StopEvent(stop_reason="end_turn")


def make_runtime(tmp_path, provider_name, provider):
    registry = MagicMock()
    registry.spec.return_value = SimpleNamespace(base_url="http://127.0.0.1:11434/v1", context_window=32768)
    return Runtime(
        bus=MagicMock(),
        kernel=SimpleNamespace(_provider=provider, _model="local-model"),
        reducer=MagicMock(),
        theme=MagicMock(),
        config=LogoxConfig(),
        provider_name=provider_name,
        model="local-model",
        cwd=tmp_path,
        tools=[],
        registry=registry,
    )


def test_router_reuses_local_and_never_builds_cloud(tmp_path):
    provider = RecordingProvider()
    runtime = make_runtime(tmp_path, "ollama", provider)
    assert run(runtime.create_memo_summarizer()(history(2, 10), target_tokens=200)) == "LOCAL_MEMO"
    assert len(provider.requests) == 1
    runtime.registry.build.assert_not_called()


def test_active_cloud_without_configured_local_is_not_used(tmp_path):
    provider = RecordingProvider()
    runtime = make_runtime(tmp_path, "deepseek", provider)
    assert run(runtime.create_memo_summarizer()(history(2, 10))) is None
    assert provider.requests == []
    runtime.registry.build.assert_not_called()


def test_memo_cache_keeps_old_facts_on_later_compaction(tmp_path):
    source = history(15, 4000, 2500)
    builder = HierarchicalContextBuilder(
        system="sys",
        cwd=tmp_path,
        transcript_writer=SessionTranscriptWriter(log_file=tmp_path / "session.jsonl"),
        window_capacity=7000,
        reserve_tokens=1000,
    )
    first = run(builder.build_async(source, summarizer=AsyncMock(return_value="EARLY_CRITICAL_DECISION")))
    assert first.compaction.strategy == "local-memo"
    source.extend(history(15, 4000, 2500))
    model = AsyncMock(return_value="EARLY_CRITICAL_DECISION plus NEW_FACTS")
    second = run(builder.build_async(source, summarizer=model))
    assert model.call_count == 1
    assert "EARLY_CRITICAL_DECISION" in format_messages_for_summary(model.call_args.args[0])
    assert "NEW_FACTS" in "\n".join(m.text for m in second.messages)


def test_local_summary_request_respects_local_input_window(tmp_path):
    provider = RecordingProvider()
    runtime = make_runtime(tmp_path, "ollama", provider)
    runtime.window_for = lambda model: 256
    assert run(runtime.create_memo_summarizer()(history(2, 4000))) is None
    assert provider.requests == []


def test_filtered_reasoning_does_not_shift_fold_cache_cursor(tmp_path):
    from logox.kernel.messages import ReasoningBlock

    source = history(15, 4000)
    source.insert(1, Message(role="assistant", blocks=[ReasoningBlock(text="private thoughts")]))
    builder = HierarchicalContextBuilder(
        cwd=tmp_path,
        window_capacity=16384,
        reserve_tokens=2000,
        transcript_writer=SessionTranscriptWriter(log_file=tmp_path / "s.jsonl"),
    )
    first = run(builder.build_async(source))
    second = run(builder.build_async(source))
    assert first.messages == second.messages
    assert first.messages[-2:] == source[-2:]
    assert builder._cache.covered == len(source) - 2


def test_extreme_single_current_turn_archives_protected_result(tmp_path):
    source = [
        Message(role="user", blocks=[TextBlock(text="CURRENT")]),
        Message(role="assistant", blocks=[ToolUseBlock(id="large", name="read", input={})]),
        Message(role="tool", blocks=[ToolResultBlock(id="large", content="X" * 40000)]),
    ]
    writer = SessionTranscriptWriter(log_file=tmp_path / "s.jsonl")
    compactor = Compactor(window_capacity=4000, reserve_tokens=1000, transcript_writer=writer)
    result = run(compactor.compact_async(source))
    assert result.tokens_after < compactor.high_watermark
    assert result.messages[0] == source[0]
    assert result.messages[-1].blocks[0].archived
    assert (writer.session_dir / writer.blob_path_of("large")).read_text() == "X" * 40000


def test_reasoning_only_message_does_not_double_count_anchor_delta(tmp_path):
    from logox.kernel.events import Usage
    from logox.kernel.messages import ReasoningBlock

    source = history(1, 10)
    source.insert(1, Message(role="assistant", blocks=[ReasoningBlock(text="private")]))
    builder = HierarchicalContextBuilder(
        system="sys",
        cwd=tmp_path,
        window_capacity=100000,
        project_memory_enabled=False,
        transcript_writer=SessionTranscriptWriter(log_file=tmp_path / "s.jsonl"),
    )
    first = builder.build(source)
    usage = Usage(input_tokens=first.token_estimate, output_tokens=1, context_tokens=first.token_estimate)
    second = builder.build(source, last_usage=usage)
    assert second.token_estimate == usage.context_tokens
    assert first.messages == second.messages
