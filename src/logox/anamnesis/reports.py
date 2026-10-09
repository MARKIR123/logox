"""Reports describe recorded analysis and actual commits, never invented test results."""

from __future__ import annotations

from logox.anamnesis.models import AnamesisAnalysisRecord


def build_report(
    mode: str,
    analyses: list[AnamesisAnalysisRecord],
    changes: list[dict],
    *,
    outcome: str,
    issues: list[str],
    findings: list[str],
    plan: list[str],
    remaining: int,
    sources: dict | None = None,
    items: list | None = None,
    self_checks: list[dict] | None = None,
    reason: str = "",
) -> str:
    saved = sum(c["result"] == "已保存" for c in changes)
    candidates = sum(c.get("status") == "candidate" for c in changes)
    parts = [
        "# Anamnesis 报告" + ({"sleep": " · 历史长眠", "nap": " · 历史小憩"}.get(mode, "")),
        "",
        f"状态：{outcome}；剩余会话片段：{remaining}",
        f"说明：{reason or '见事项结果与覆盖记录'}",
        f"整理结果：已保存 {saved} 条；候选（未入档）{candidates} 条。",
        "本次为只读整理／研究，未修改源码或执行测试。",
        "",
        "## 研究事项",
    ]
    for item in items or []:
        parts.extend(
            [
                f"### {item.item_id} · {item.status} · {item.question}",
                f"纳入原因：{item.reason}",
                f"结论：{item.conclusion or '尚未形成'}",
                f"缺失依据：{item.missing_evidence or '无额外说明'}",
                f"分析：{', '.join(item.analysis_ids)}；来源：{', '.join(item.source_ids)}",
                f"必要前置关系：{', '.join(item.parent_item_ids)}；{item.dependency_reason}",
            ]
        )
    parts.extend(["", "## 停滞自检（提醒不代表已自愈）"])
    for check in self_checks or []:
        parts.append(
            f"- 事项 {check['item_id']}；提醒 {check['attempt']}；暂停 {check['paused']}；进展版本 {check['progress_epoch']}\n  {check['prompt']}"
        )
    parts.extend(["", "## 阶段分析"])
    for a in analyses:
        parts.extend(
            [
                f"### {a.question}",
                f"依据：{a.evidence_summary}",
                f"判断原因：{a.rationale}",
                f"取舍：{'；'.join(a.alternatives) or '未提出替代方案'}",
                f"结论：{a.conclusion}",
                f"不确定性：{'；'.join(a.uncertainties) or '无额外说明'}",
                f"来源：{', '.join(a.source_ids)}",
            ]
        )
        if a.revises_record_id:
            parts.append(f"修订此前分析：{a.revises_record_id}")
    parts.extend(["", "## 记忆变化与保存结果"])
    for change in changes:
        parts.append(
            f"- {change['scope']} / {change['entry_id']}：{change['result']}\n  原：{change['old_value']}\n  新：{change['new_value']}\n  理由：{change['rationale']}\n  变更版本：{change.get('change_id', '')}"
        )
        if change.get("proposed_status"):
            parts.append(f"  原提案状态：{change['proposed_status']}；处理状态：{change['status']}")
    parts.extend(
        [
            "",
            "## 只读发现（待验证）",
            *[f"- {v}" for v in findings],
            "",
            "## 下一步建议",
            *[f"- {v}" for v in plan],
            "",
            "## 未覆盖资料／异常",
            *[f"- {v}" for v in issues],
        ]
    )
    identities = {s for a in analyses for s in a.source_ids} | {
        s for c in changes for s in c.get("source_ids", [])
    }
    if identities:
        parts.extend(["", "## 依据定位"])
        for ident in sorted(identities):
            source = (sources or {}).get(ident)
            parts.append(
                f"- {ident}：{source.path}:{source.line}；摘要值 {source.digest}；片段偏移 {source.offset}"
                if source
                else f"- {ident}：当前来源未加载，核对运行记录"
            )
    return "\n".join(parts) + "\n"
