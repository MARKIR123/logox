"""Finite research goals and observable, evidence-based progress."""

import pytest

from logox.anamnesis.models import (
    AnamesisAnalysisRecord,
    AnamesisResearchItem,
    AnamesisResearchUpdate,
    MemoryProposal,
    SourceRef,
)
from logox.anamnesis.research import AnamesisResearchState


def state():
    value = AnamesisResearchState()
    value.add(
        [AnamesisResearchItem(item_id="review", question="现状？", reason="新资料", origin_source_ids=["s"])],
        source_ids={"s"},
        code_snapshot_id="code",
        allow_roots=True,
    )
    return value


def test_terminal_requires_real_analysis_and_matching_sources():
    value = state()
    analysis = AnamesisAnalysisRecord(
        item_id="review",
        record_id="a",
        stage_id="review",
        question="现状？",
        rationale="用户明确表述",
        source_ids=["s"],
        conclusion="已确认",
    )
    update = AnamesisResearchUpdate(
        item_id="review",
        expected_version=0,
        status="resolved",
        analysis_ids=["a"],
        source_ids=["s"],
        conclusion="已确认",
    )
    with pytest.raises(ValueError, match="分析未知"):
        value.update(update, {}, {"s"})
    with pytest.raises(ValueError, match="依据未知"):
        value.update(update, {"a": analysis}, set())
    value.update(update, {"a": analysis}, {"s"})
    assert value.finished
    with pytest.raises(ValueError):
        value.update(update, {"a": analysis}, {"s"})


def test_dependencies_cannot_reference_unknown_or_finished_parents():
    value = state()
    dependency = AnamesisResearchItem(
        item_id="dep",
        question="前置问题？",
        reason="需要理解接口",
        origin_kind="dependency",
        parent_item_ids=["review"],
        dependency_reason="不确认接口，无法回答现状",
    )
    value.add([dependency], source_ids={"s"}, code_snapshot_id="code", allow_roots=False)
    with pytest.raises(ValueError, match="必要前置"):
        value.add(
            [
                AnamesisResearchItem(
                    item_id="unrelated", question="无关？", reason="随便研究", origin_source_ids=["s"]
                )
            ],
            source_ids={"s"},
            code_snapshot_id="code",
            allow_roots=False,
        )
    with pytest.raises(ValueError, match="父事项未知"):
        value.add(
            [dependency.model_copy(update={"item_id": "cycle", "parent_item_ids": ["cycle"]})],
            source_ids={"s"},
            code_snapshot_id="code",
            allow_roots=False,
        )
    with pytest.raises(ValueError, match="根事项不能"):
        value.add(
            [
                AnamesisResearchItem(
                    item_id="self",
                    question="循环？",
                    reason="无效根关系",
                    origin_source_ids=["s"],
                    parent_item_ids=["self"],
                )
            ],
            source_ids={"s"},
            code_snapshot_id="code",
            allow_roots=True,
        )


def test_waiting_evidence_is_terminal_but_not_a_solved_claim():
    value = state()
    a = AnamesisAnalysisRecord(
        item_id="review",
        record_id="a",
        stage_id="review",
        question="现状？",
        rationale="只读权限无法执行测试",
        conclusion="无法验证性能",
    )
    value.update(
        AnamesisResearchUpdate(
            item_id="review",
            expected_version=0,
            status="waiting_evidence",
            analysis_ids=["a"],
            missing_evidence="缺实际测试结果",
        ),
        {"a": a},
        set(),
    )
    assert value.finished
    assert value.items["review"].conclusion == ""


def test_complete_is_required_not_an_implicit_success_flag():
    with pytest.raises(ValueError):
        MemoryProposal.model_validate({"changes": []})
    assert not state().finished
    assert MemoryProposal(complete=True).complete


def test_reminder_twice_then_pause_survives_serialization_and_fake_ids():
    value = state()
    observations = []
    for n in range(9):
        notice = value.observe("review", "read", {"path": "a.py", "call_id": str(n)}, "same")
        if notice:
            observations.append(notice)
        value = AnamesisResearchState(value.dump())
    assert [n["attempt"] for n in observations] == [1, 2, 2]
    assert [n["paused"] for n in observations] == [False, False, True]


def test_new_range_coverage_resets_stagnation_not_new_analysis_id():
    value = state()
    for n in range(3):
        value.observe("review", "record_analysis", {"record_id": str(n), "conclusion": "same"}, "recorded")
    assert next(iter(value.repeats.values()))["attempts"] == 1
    ref = SourceRef(source_id="code", kind="code", project_id="p", path="a.py", digest="digest", content="x")
    value.cover("review", ref)
    assert not value.repeats
    epoch = value.progress_epoch
    value.cover("review", ref.model_copy(update={"source_id": "new-id", "timestamp": 999}))
    assert value.progress_epoch == epoch
    value.cover("review", ref.model_copy(update={"end_line": 2}))
    assert value.progress_epoch == epoch + 1
    # A narrower range inside the old coverage is a reread, not real progress.
    value.cover("review", ref.model_copy(update={"line": 2, "end_line": 2}))
    assert value.progress_epoch == epoch + 1
    # Conversation excerpts sharing one JSONL line still have independent character coverage.
    chunk = ref.model_copy(update={"kind": "user_message", "path": "chat.jsonl", "content": "abcd"})
    value.cover("review", chunk)
    chunk_epoch = value.progress_epoch
    value.cover("review", chunk.model_copy(update={"offset": 4, "content": "efgh"}))
    assert value.progress_epoch == chunk_epoch + 1
    value.cover("review", chunk.model_copy(update={"offset": 2, "content": "cdef"}))
    assert value.progress_epoch == chunk_epoch + 1
