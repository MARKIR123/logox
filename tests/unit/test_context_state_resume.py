"""压缩状态必须随会话存活，而不是只保存计数。"""

from types import SimpleNamespace

from logox.context.builder import HierarchicalContextBuilder
from logox.context.storage import SessionTranscriptWriter
from logox.kernel.messages import Message, MessageMeta, TextBlock, ToolResultBlock
from logox.store.replay import filter_rewound_records, load_session_records, reconstruct_messages


def make_builder(path, **kwargs):
    writer = SessionTranscriptWriter(log_file=path)
    return HierarchicalContextBuilder(
        cwd=path.parent, transcript_writer=writer, project_memory_enabled=False, rehydrate_files=0, **kwargs
    )


def seed(path, turns=8, chars=5000, summary="完成本轮任务"):
    writer = SessionTranscriptWriter(log_file=path)
    for turn in range(1, turns + 1):
        writer.write_step(turn=turn, step=0, role="user", event_type="user_prompt", content=f"问题 {turn}")
        writer.write_step(
            turn=turn, step=1, role="assistant", event_type="model_output", content="答" * chars
        )
        writer.write_step(
            turn=turn, step=2, role="system", event_type="turn_finished", turn_summary=f"{summary} {turn}"
        )
    return reconstruct_messages(load_session_records(path), session_dir=path.parent)


def shape(messages):
    return [(m.role, [b.model_dump() for b in m.blocks]) for m in messages]


def restore(builder, history):
    return builder.restore_state(
        history, filter_rewound_records(load_session_records(builder.writer.log_file))
    )


def test_fold_survives_fresh_builder_and_keeps_full_history(tmp_path):
    path = tmp_path / "session.jsonl"
    history = seed(path)
    builder = make_builder(path, window_capacity=1000000)
    compacted = builder.force_compact(history)
    resumed = reconstruct_messages(load_session_records(path), session_dir=path.parent)
    fresh = make_builder(path, window_capacity=1000000)
    assert restore(fresh, resumed)
    assert len(resumed) == len(history)
    assert shape(fresh.build(resumed).messages) == shape(compacted.messages)
    assert fresh.estimate_context(resumed) == compacted.token_estimate
    assert fresh.compactor.epochs == builder.compactor.epochs


def test_pruned_tools_do_not_return_on_next_build_or_resume(tmp_path):
    path = tmp_path / "tool.jsonl"
    writer = SessionTranscriptWriter(log_file=path)
    writer.write_step(turn=1, step=0, role="user", event_type="user_prompt", content="搜索代码")
    writer.write_step(
        turn=1,
        step=1,
        role="assistant",
        event_type="model_output",
        tool_calls=[{"id": "t1", "name": "grep", "arguments": {}}],
    )
    content = "结果" * 10000
    writer.write_step(
        turn=1, step=2, role="tool", event_type="tool_result", content=content, call_id="t1", tool_name="grep"
    )
    history = reconstruct_messages(load_session_records(path), session_dir=path.parent)
    builder = make_builder(path, window_capacity=1000000, keep_recent_tool_results=0)
    compacted = builder.force_compact(history)
    assert compacted.messages[-1].blocks[0].archived
    assert shape(builder.build(history).messages) == shape(compacted.messages)
    fresh = make_builder(path, window_capacity=1000000)
    resumed = reconstruct_messages(load_session_records(path), session_dir=path.parent)
    assert restore(fresh, resumed)
    assert shape(fresh.build(resumed).messages) == shape(compacted.messages)
    assert resumed[-1].blocks[0].content == content


def test_live_parallel_tool_batch_matches_separate_disk_records(tmp_path):
    path = tmp_path / "batch.jsonl"
    writer = SessionTranscriptWriter(log_file=path)
    writer.write_step(turn=1, step=0, role="user", event_type="user_prompt", content="并行搜索")
    calls = [{"id": f"t{i}", "name": "grep", "arguments": {}} for i in range(2)]
    writer.write_step(turn=1, step=1, role="assistant", event_type="model_output", tool_calls=calls)
    results = [ToolResultBlock(id=f"t{i}", content="结果" * 10000) for i in range(2)]
    for block in results:
        writer.write_step(
            turn=1, step=2, role="tool", event_type="tool_result", content=block.content, call_id=block.id
        )
    disk = reconstruct_messages(load_session_records(path))
    live = [*disk[:2], Message(role="tool", blocks=results)]
    builder = make_builder(path, window_capacity=1000000, keep_recent_tool_results=0)
    compacted = builder.force_compact(live)
    resumed = reconstruct_messages(load_session_records(path))
    fresh = make_builder(path, window_capacity=1000000)
    assert restore(fresh, resumed)
    expected = [
        b.model_dump() for m in compacted.messages for b in m.blocks if isinstance(b, ToolResultBlock)
    ]
    actual = [
        b.model_dump()
        for m in fresh.build(resumed).messages
        for b in m.blocks
        if isinstance(b, ToolResultBlock)
    ]
    assert actual == expected


def append_turn(writer, turn, chars=5000):
    writer.write_step(turn=turn, step=0, role="user", event_type="user_prompt", content=f"问题 {turn}")
    writer.write_step(turn=turn, step=1, role="assistant", event_type="model_output", content="答" * chars)
    writer.write_step(
        turn=turn, step=2, role="system", event_type="turn_finished", turn_summary=f"完成本轮任务 {turn}"
    )


def test_appended_turns_survive_and_recompaction_keeps_old_summaries(tmp_path):
    path = tmp_path / "session.jsonl"
    history = seed(path)
    builder = make_builder(path, window_capacity=1000000)
    compacted = builder.force_compact(history)
    append_turn(builder.writer, 9)
    resumed = reconstruct_messages(load_session_records(path))
    fresh = make_builder(path, window_capacity=1000000)
    assert restore(fresh, resumed)
    assert shape(fresh.build(resumed).messages) == shape(compacted.messages + resumed[-2:])
    again = fresh.force_compact(resumed)
    third = make_builder(path, window_capacity=1000000)
    history3 = reconstruct_messages(load_session_records(path))
    assert restore(third, history3)
    assert shape(third.build(history3).messages) == shape(again.messages)
    assert "完成本轮任务 1" in "\n".join(m.text for m in again.messages)


def test_local_model_memo_survives_without_requesting_the_model_again(tmp_path):
    import asyncio
    from unittest.mock import AsyncMock

    path = tmp_path / "memo.jsonl"
    history = seed(path, chars=300, summary="事实" * 3000)
    builder = make_builder(path, window_capacity=4000)
    summarizer = AsyncMock(return_value="保留核心决策；下一步继续验证。")
    compacted = asyncio.run(builder.force_compact_async(history, summarizer=summarizer))
    assert compacted.compaction.strategy == "local-memo"
    summarizer.assert_awaited_once()
    resumed = reconstruct_messages(load_session_records(path))
    fresh = make_builder(path, window_capacity=4000)
    assert restore(fresh, resumed)
    next_summary = AsyncMock(side_effect=AssertionError("恢复不应再次调用模型"))
    rebuilt = asyncio.run(fresh.build_async(resumed, summarizer=next_summary))
    assert shape(rebuilt.messages) == shape(compacted.messages)
    next_summary.assert_not_called()
    assert len(resumed) == len(history)


def test_summary_only_index_and_state_records_do_not_grow_on_repeated_build(tmp_path):
    path = tmp_path / "summary.jsonl"
    history = seed(path, chars=500)
    builder = make_builder(path, window_capacity=2000)
    compacted = builder.force_compact(history)
    assert compacted.compaction.strategy == "summary-only"
    for _ in range(3):
        assert shape(builder.build(history).messages) == shape(compacted.messages)
    records = load_session_records(path)
    assert len([r for r in records if r["type"] == "context_state"]) == 1
    resumed = reconstruct_messages(records)
    fresh = make_builder(path, window_capacity=2000)
    assert restore(fresh, resumed)
    assert shape(fresh.build(resumed).messages) == shape(compacted.messages)


def test_restore_before_rewind_uses_only_a_matching_earlier_snapshot(tmp_path):
    path = tmp_path / "rewind.jsonl"
    history = seed(path, turns=4)
    builder = make_builder(path, window_capacity=1000000)
    before = builder.force_compact(history)
    for turn in range(5, 9):
        append_turn(builder.writer, turn)
    builder.force_compact(reconstruct_messages(load_session_records(path)))
    builder.writer.write_step(turn=5, step=0, role="system", event_type="session_rewind", to_turn=5)
    records = filter_rewound_records(load_session_records(path))
    resumed = reconstruct_messages(records)
    fresh = make_builder(path, window_capacity=1000000)
    assert fresh.restore_state(resumed, records)
    assert len(resumed) == 8
    assert shape(fresh.build(resumed).messages) == shape(before.messages)


def test_invalid_snapshots_and_old_logs_preserve_original_history(tmp_path, caplog):
    import copy

    path = tmp_path / "invalid.jsonl"
    history = seed(path)
    builder = make_builder(path, window_capacity=1000000)
    builder.force_compact(history)
    record = next(r for r in load_session_records(path) if r["type"] == "context_state")
    for field, value in [
        ("version", 900),
        ("covered", -1),
        ("history_count", 999),
        ("history_digest", "wrong"),
        ("prefix", [{}]),
        ("epochs", [{"epoch_id": "wrong"}]),
        ("working_set", 123),
        ("tools", [{"index": 999, "blocks": []}]),
    ]:
        invalid = copy.deepcopy(record)
        invalid["state"][field] = value
        assert not builder.restore_state(history, [invalid])
        assert shape(builder.build(history).messages) == shape(history)
    assert "忽略不可恢复的压缩状态" in caplog.text
    assert not builder.restore_state(history, [])
    changed = [Message(role="user", blocks=[TextBlock(text="不同的问题")]), *history[1:]]
    assert not builder.restore_state(changed, [record])


def test_corrupt_latest_snapshot_falls_back_to_valid_earlier_state(tmp_path):
    path = tmp_path / "corrupt.jsonl"
    history = seed(path)
    builder = make_builder(path, window_capacity=1000000)
    compacted = builder.force_compact(history)
    builder.writer.write_step(turn=8, step=0, role="system", event_type="context_state", state={"version": 1})
    with path.open("a", encoding="utf8") as stream:
        stream.write('{"type":"context_state","state":')
    fresh = make_builder(path, window_capacity=1000000)
    assert restore(fresh, history)
    assert shape(fresh.build(history).messages) == shape(compacted.messages)


def test_write_failure_keeps_current_compaction_and_reports_restore_limit(tmp_path, caplog):
    from unittest.mock import patch

    path = tmp_path / "failed.jsonl"
    history = seed(path)
    builder = make_builder(path, window_capacity=1000000)
    with patch.object(builder.writer, "write_step", return_value=None):
        compacted = builder.force_compact(history)
    assert compacted.token_estimate < builder.estimator.estimate_messages(history)
    assert "压缩状态写入失败" in caplog.text
    assert not any(r["type"] == "context_state" for r in load_session_records(path))
    assert shape(builder.build(history).messages) == shape(compacted.messages)
    fresh = make_builder(path, window_capacity=1000000)
    assert restore(fresh, history)


def make_runtime(tmp_path, resume_file=None):
    from logox.app import build_runtime
    from logox.config.schema import ContextConfig, LogoxConfig, ProviderConfig
    from logox.paths import LogoxPaths

    config = LogoxConfig(
        provider=ProviderConfig(name="deepseek", model="deepseek-flash"),
        context=ContextConfig(project_memory_enabled=False, rehydrate_files=0),
    )
    return build_runtime(
        SimpleNamespace(config=config, sources=[]),
        tmp_path,
        LogoxPaths.at(tmp_path / "home"),
        resume_file=resume_file,
    )


def test_startup_resume_and_hot_switch_restore_real_effective_context(tmp_path):
    import asyncio

    from logox.kernel.events import Usage

    path = tmp_path / "startup.jsonl"
    seed(path)
    first = make_runtime(tmp_path, path)
    compacted = asyncio.run(first.apply_compact())
    expected = shape(first.context_builder.build(first.kernel.history).messages)
    fresh = make_runtime(tmp_path, path)
    assert fresh.reducer.metrics.context_tokens == compacted.tokens_after
    assert shape(fresh.context_builder.build(fresh.kernel.history).messages) == expected
    assert len(fresh.kernel.history) == 16
    fresh.kernel._last_request_usage = Usage(input_tokens=1200000, output_tokens=0, context_tokens=1200000)
    fresh.create_new_session()
    assert fresh.context_builder._cache.covered == 0
    assert fresh.kernel._last_request_usage is None
    fresh.switch_session(path)
    assert fresh.reducer.metrics.context_tokens == compacted.tokens_after
    assert shape(fresh.context_builder.build(fresh.kernel.history).messages) == expected
    assert fresh.kernel._last_request_usage is None
    for runtime in [first, fresh]:
        runtime.close()


def test_hot_switch_restores_tool_archives_from_the_target_session(tmp_path):
    import asyncio

    path = tmp_path / "target.jsonl"
    writer = SessionTranscriptWriter(log_file=path)
    writer.write_step(turn=1, step=0, role="user", event_type="user_prompt", content="搜索代码")
    writer.write_step(
        turn=1,
        step=1,
        role="assistant",
        event_type="model_output",
        tool_calls=[{"id": "t1", "name": "grep", "arguments": {}}],
    )
    writer.write_step(
        turn=1, step=2, role="tool", event_type="tool_result", content="结果" * 10000, call_id="t1"
    )
    first = make_runtime(tmp_path, path)
    first.context_builder.compactor.keep_recent_tool_results = 0
    asyncio.run(first.apply_compact())
    expected = shape(first.context_builder.build(first.kernel.history).messages)
    fresh = make_runtime(tmp_path)
    fresh.switch_session(path)
    assert shape(fresh.context_builder.build(fresh.kernel.history).messages) == expected
    for runtime in [first, fresh]:
        runtime.close()


def test_million_token_tool_context_stays_compacted_after_restart(tmp_path):
    path = tmp_path / "million.jsonl"
    writer = SessionTranscriptWriter(log_file=path)
    writer.write_step(turn=1, step=0, role="user", event_type="user_prompt", content="总结搜索结果")
    writer.write_step(
        turn=1,
        step=1,
        role="assistant",
        event_type="model_output",
        tool_calls=[{"id": "large", "name": "grep", "arguments": {}}],
    )
    writer.write_step(
        turn=1, step=2, role="tool", event_type="tool_result", content="结果" * 600000, call_id="large"
    )
    history = reconstruct_messages(load_session_records(path))
    builder = make_builder(path, window_capacity=1000000, keep_recent_tool_results=0)
    compacted = builder.force_compact(history)
    assert compacted.compaction.tokens_before > 1000000
    assert compacted.token_estimate < 5000
    fresh = make_builder(path, window_capacity=1000000)
    resumed = reconstruct_messages(load_session_records(path))
    assert restore(fresh, resumed)
    assert fresh.estimate_context(resumed) == compacted.token_estimate
    assert shape(fresh.build(resumed).messages) == shape(compacted.messages)


def test_restoration_keeps_later_summary_metadata_and_excludes_reasoning(tmp_path):
    from logox.kernel.messages import ReasoningBlock

    path = tmp_path / "meta.jsonl"
    history = seed(path)
    history[0] = history[0].model_copy(update={"meta": MessageMeta(transcript_line=None)})
    history[1] = history[1].model_copy(
        update={"blocks": [ReasoningBlock(text="私有推理", signature="sig"), *history[1].blocks]}
    )
    builder = make_builder(path, window_capacity=1000000)
    compacted = builder.force_compact(history)
    resumed = reconstruct_messages(load_session_records(path))
    fresh = make_builder(path, window_capacity=1000000)
    assert restore(fresh, resumed)
    assert shape(fresh.build(resumed).messages) == shape(compacted.messages)
    assert resumed[-1].meta.turn_summary == "完成本轮任务 8"


def test_missing_archive_rejects_tool_override_and_keeps_raw_output(tmp_path, caplog):
    from unittest.mock import patch

    path = tmp_path / "missing-blob.jsonl"
    writer = SessionTranscriptWriter(log_file=path)
    writer.write_step(turn=1, step=0, role="user", event_type="user_prompt", content="搜索")
    writer.write_step(
        turn=1,
        step=1,
        role="assistant",
        event_type="model_output",
        tool_calls=[{"id": "tool", "name": "grep", "arguments": {}}],
    )
    writer.write_step(
        turn=1, step=2, role="tool", event_type="tool_result", content="结果" * 5000, call_id="tool"
    )
    history = reconstruct_messages(load_session_records(path))
    builder = make_builder(path, window_capacity=1000000, keep_recent_tool_results=0)
    builder.force_compact(history)
    with patch.object(builder.writer, "blob_path_of", return_value=None):
        assert not restore(builder, history)
    assert not builder.build(history).messages[-1].blocks[0].archived
    assert "工具原文归档缺失" in caplog.text


def test_next_actual_model_request_uses_restored_view(tmp_path):
    import asyncio

    from logox.store.persistence import SessionPersistenceSubscriber
    from tests.unit.kernel_support import install, text_chunks

    path = tmp_path / "request.jsonl"
    history = seed(path)
    builder = make_builder(path, window_capacity=1000000)
    compacted = builder.force_compact(history)
    fresh = make_builder(path, window_capacity=1000000)
    resumed = reconstruct_messages(load_session_records(path))
    assert restore(fresh, resumed)
    environment = install([text_chunks("下一步继续检查。")])
    environment.kernel._builder = fresh
    environment.kernel.history.extend(resumed)
    subscriber = SessionPersistenceSubscriber(fresh.writer)
    environment.bus.subscribe("*", subscriber.handle, name="state-test-persistence")
    requests = []
    original = environment.kernel._provider.stream

    async def capture(request):
        requests.append(request)
        async for event in original(request):
            yield event

    environment.kernel._provider.stream = capture
    asyncio.run(environment.kernel.submit("继续"))
    assert len(requests) == 1
    assert shape(requests[0].messages) == shape(
        compacted.messages + [Message(role="user", blocks=[TextBlock(text="继续")])]
    )
    assert sum(len(m.text) for m in environment.kernel.history) > sum(len(m.text) for m in requests[0].messages)


def test_folded_parallel_batch_uses_the_correct_restored_raw_cursor(tmp_path):
    path = tmp_path / "folded-batch.jsonl"
    writer = SessionTranscriptWriter(log_file=path)
    writer.write_step(turn=1, step=0, role="user", event_type="user_prompt", content="批量查询")
    writer.write_step(
        turn=1,
        step=1,
        role="assistant",
        event_type="model_output",
        tool_calls=[{"id": f"b{i}", "name": "grep", "arguments": {}} for i in range(3)],
    )
    for i in range(3):
        writer.write_step(
            turn=1, step=2, role="tool", event_type="tool_result", content="内容" * 1000, call_id=f"b{i}"
        )
    writer.write_step(turn=1, step=3, role="assistant", event_type="model_output", content="完成批次")
    writer.write_step(turn=1, step=4, role="system", event_type="turn_finished", turn_summary="完成批次查询")
    for turn in range(2, 9):
        append_turn(writer, turn)
    disk = reconstruct_messages(load_session_records(path))
    live = [*disk[:2], Message(role="tool", blocks=[m.blocks[0] for m in disk[2:5]]), *disk[5:]]
    builder = make_builder(path, window_capacity=1000000)
    compacted = builder.force_compact(live)
    fresh = make_builder(path, window_capacity=1000000)
    assert restore(fresh, disk)
    assert fresh._cache.covered == builder._cache.covered + 2
    assert shape(fresh.build(disk).messages) == shape(compacted.messages)


def test_runtime_rewind_restores_earlier_state_and_clears_usage(tmp_path):
    import asyncio

    from logox.kernel.events import Usage

    path = tmp_path / "runtime-rewind.jsonl"
    seed(path, turns=4)
    runtime = make_runtime(tmp_path, path)
    asyncio.run(runtime.apply_compact())
    expected = shape(runtime.context_builder.build(runtime.kernel.history).messages)
    for turn in range(5, 9):
        append_turn(runtime.persistence_writer, turn)
    runtime.switch_session(path)
    asyncio.run(runtime.apply_compact())
    runtime.kernel._last_request_usage = Usage(input_tokens=1200000, output_tokens=0, context_tokens=1200000)
    result = asyncio.run(runtime.rewind(5))
    assert result.success
    assert len(runtime.kernel.history) == 8
    assert runtime.kernel._last_request_usage is None
    assert shape(runtime.context_builder.build(runtime.kernel.history).messages) == expected
    assert runtime.reducer.metrics.context_tokens == runtime.context_builder.estimate_context(
        runtime.kernel.history
    )
    runtime.close()
