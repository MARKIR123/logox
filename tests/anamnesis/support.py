"""Scripted model responses follow the same explicit research contract as real models."""

from logox.anamnesis.models import AnamesisAnalysisRecord, AnamesisResearchItem
from logox.anamnesis.research import AnamesisResearchState
from logox.providers.base import ToolCallEvent


async def noop(value):
    pass


def research(refs):
    state = AnamesisResearchState()
    state.add(
        [
            AnamesisResearchItem(
                item_id="review",
                question="核对事实？",
                reason="当前资料",
                origin_source_ids=[r.source_id for r in refs],
            )
        ],
        source_ids={r.source_id for r in refs},
        code_snapshot_id="",
        allow_roots=True,
    )
    return state


def finish_calls(payload, response):
    response = {**response, "complete": response.get("complete", True)}
    supplied = response.get("analyses", [])
    for item in payload.get("research_items", []):
        analyses = (
            [dict(a, item_id=item["item_id"]) for a in supplied]
            if item == payload.get("current_item")
            else []
        )
        if not analyses:
            analyses = [
                AnamesisAnalysisRecord(
                    item_id=item["item_id"],
                    record_id="review." + item["item_id"],
                    stage_id="review",
                    question=item["question"],
                    rationale="核对本批资料，缺失部分明确保留",
                    conclusion="已回顾资料" if item["origin_source_ids"] else "缺真实代码核查",
                    source_ids=item["origin_source_ids"],
                ).model_dump()
            ]
        for index, analysis in enumerate(analyses):
            yield ToolCallEvent(
                call_id=f"{item['item_id']}.a{index}", name="record_analysis", arguments=analysis
            )
        refs = list(dict.fromkeys(s for a in analyses for s in a["source_ids"]))
        args = {
            "item_id": item["item_id"],
            "expected_version": item["version"],
            "analysis_ids": [a["record_id"] for a in analyses],
        }
        if refs:
            args.update(status="resolved", source_ids=refs, conclusion=analyses[-1]["conclusion"])
        else:
            args.update(status="waiting_evidence", missing_evidence="离线模型没有真实代码核查结果")
        yield ToolCallEvent(call_id=item["item_id"] + ".u", name="update_research", arguments=args)
    yield ToolCallEvent(call_id="p", name="propose_memory", arguments={**response, "analyses": []})
