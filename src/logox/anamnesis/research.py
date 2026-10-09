"""Pure research rules. No filesystem, provider, permission or UI dependencies."""

from __future__ import annotations

import json

from logox.anamnesis.models import (
    AnamesisAnalysisRecord,
    AnamesisResearchItem,
    AnamesisResearchUpdate,
    digest_text,
)

TERMINAL = {"resolved", "waiting_evidence"}


class AnamesisResearchState:
    def __init__(self, data: dict | None = None, *, repeat_trigger_count=3, self_check_max_attempts=2):
        data = data or {}
        self.items = {i["item_id"]: AnamesisResearchItem.model_validate(i) for i in data.get("items", [])}
        self.version = int(data.get("version", 0))
        self.progress_epoch = int(data.get("progress_epoch", 0))
        self.coverage = set(data.get("coverage", []))
        self.covered_ranges = dict(data.get("covered_ranges", {}))
        self.repeats = dict(data.get("repeats", {}))
        self.roots_open = bool(data.get("roots_open", True))
        self.pending_reminders = list(data.get("pending_reminders", []))
        self.trigger = repeat_trigger_count
        self.max_attempts = self_check_max_attempts
        if self.trigger < 1 or self.max_attempts < 1:
            raise ValueError("重复触发与自检次数必须为正数")

    def dump(self) -> dict:
        return {
            "items": [i.model_dump() for i in self.items.values()],
            "version": self.version,
            "progress_epoch": self.progress_epoch,
            "coverage": sorted(self.coverage),
            "covered_ranges": self.covered_ranges,
            "repeats": self.repeats,
            "roots_open": self.roots_open,
            "pending_reminders": self.pending_reminders,
        }

    @property
    def finished(self) -> bool:
        return bool(self.items) and all(i.status in TERMINAL for i in self.items.values())

    def current(self) -> AnamesisResearchItem | None:
        blocked = {p for i in self.items.values() if i.status not in TERMINAL for p in i.parent_item_ids}
        available = [i for i in self.items.values() if i.item_id not in blocked]
        return next((i for i in available if i.status == "active"), None) or next(
            (i for i in available if i.status == "pending"), None
        )

    def require_current(self, item_id: str) -> AnamesisResearchItem:
        item = self.current()
        if item is None or item.item_id != item_id:
            raise ValueError("操作须关联当前未完成事项 item_id")
        return item

    def add(self, items, *, source_ids: set[str], code_snapshot_id: str, allow_roots: bool) -> list:
        planned = dict(self.items)
        for item in items:
            if (
                item.status != "pending"
                or item.version
                or item.analysis_ids
                or item.source_ids
                or item.conclusion
                or item.missing_evidence
            ):
                raise ValueError("新事项只能声明 pending，不得伪造已完成记录")
            if item.item_id in planned:
                raise ValueError("事项 ID 已存在，使用 update_research 更新")
            if item.origin_kind == "dependency":
                if not item.parent_item_ids or not item.dependency_reason.strip():
                    raise ValueError("前置事项须有父事项和必要性理由")
                if any(p not in planned or planned[p].status in TERMINAL for p in item.parent_item_ids):
                    raise ValueError("父事项未知或已结束，不能追加无关目标")
            elif not allow_roots:
                raise ValueError("研究中只可追加当前目标的必要前置事项；其它发现放后续建议")
            elif item.parent_item_ids or item.dependency_reason:
                raise ValueError("根事项不能声明前置关系；使用 dependency 并关联已有父事项")
            elif item.origin_kind == "source_review" and not item.origin_source_ids:
                raise ValueError("资料事项须引用本批真实来源")
            elif item.origin_kind == "code_change" and (
                not code_snapshot_id or item.code_snapshot_id != code_snapshot_id
            ):
                raise ValueError("代码事项须关联本次代码快照")
            if not set(item.origin_source_ids) <= source_ids:
                raise ValueError("事项来源不属于当前选定资料")
            planned[item.item_id] = item
        self.items = planned
        self.version += 1
        return items

    def update(
        self,
        update: AnamesisResearchUpdate,
        analyses: dict[str, AnamesisAnalysisRecord],
        valid_sources: set[str],
    ) -> AnamesisResearchItem:
        item = self.require_current(update.item_id)
        if update.expected_version != item.version:
            raise ValueError(f"事项版本冲突，当前版本为 {item.version}")
        if update.status == item.status:
            raise ValueError("事项状态没有变化，不能重复声明进展")
        if not set(update.source_ids) <= valid_sources:
            raise ValueError("事项依据未知或已失效")
        records = [analyses.get(i) for i in update.analysis_ids]
        if any(a is None or a.item_id != item.item_id for a in records):
            raise ValueError("事项分析未知或不属于当前事项")
        if update.status in TERMINAL:
            if not records:
                raise ValueError("事项终态必须引用实际分析")
            if update.status == "resolved":
                cited = {s for a in records for s in a.source_ids}
                if (
                    not update.conclusion.strip()
                    or not update.source_ids
                    or not set(update.source_ids) <= cited
                ):
                    raise ValueError("结论须有对应分析中的真实依据")
            elif not update.missing_evidence.strip():
                raise ValueError("待验证须明确缺少的依据或不能推进原因")
        elif update.analysis_ids or update.source_ids or update.conclusion or update.missing_evidence:
            raise ValueError("active 只声明开始研究，不声明结论")
        updated = item.model_copy(
            update={**update.model_dump(exclude={"expected_version"}), "version": item.version + 1}
        )
        self.items[item.item_id] = updated
        self.version += 1
        if update.status in TERMINAL:
            self.advance()
        return updated

    def advance(self) -> None:
        self.progress_epoch += 1
        self.repeats.clear()
        self.pending_reminders.clear()

    def cover(self, item_id: str, source) -> None:
        # Related source/range/content is coverage; new display IDs or timestamps are not.
        unit = [item_id, source.path, source.kind, source.digest]
        if source.kind == "code":
            start, end = source.line, max(source.line, source.end_line)
        else:
            # Several excerpt chunks can share one JSONL physical line and raw digest.
            unit.extend([source.line, source.end_line])
            start, end = source.offset, source.offset + max(1, len(source.content)) - 1
        key = digest_text(json.dumps(unit, ensure_ascii=False))
        ranges = self.covered_ranges.get(key, [])
        if any(left <= start and right >= end for left, right in ranges):
            return
        merged = []
        for left, right in sorted([*ranges, [start, end]]):
            if merged and left <= merged[-1][1] + 1:
                merged[-1][1] = max(right, merged[-1][1])
            else:
                merged.append([left, right])
        self.covered_ranges[key] = merged
        self.coverage.add(key)
        self.advance()

    def observe(self, item_id: str, tool: str, arguments: dict, result: str) -> dict | None:
        self.require_current(item_id)

        def meaningful(value):
            if isinstance(value, dict):
                return {
                    k: meaningful(v)
                    for k, v in value.items()
                    if k
                    not in {
                        "item_id",
                        "record_id",
                        "call_id",
                        "analysis_record_id",
                        "analysis_ids",
                        "timestamp",
                    }
                }
            if isinstance(value, list):
                return [meaningful(v) for v in value]
            return value

        args = meaningful(arguments)
        semantic = result
        try:
            value = json.loads(result)
            if isinstance(value, dict) and "source_id" in value and "digest" in value:
                semantic = json.dumps(
                    {k: value.get(k) for k in ("path", "line", "end_line", "digest", "content")},
                    sort_keys=True,
                    ensure_ascii=False,
                )
        except (ValueError, TypeError):
            pass
        signature = digest_text(
            json.dumps([item_id, tool, args, semantic], sort_keys=True, ensure_ascii=False)
        )
        record = dict(self.repeats.get(signature, {"count": 0, "attempts": 0}))
        record["count"] += 1
        self.repeats[signature] = record
        if record["count"] < self.trigger:
            return None
        paused = record["attempts"] >= self.max_attempts
        record["count"] = 0
        if not paused:
            record["attempts"] += 1
        item = self.items[item_id]
        return {
            "item_id": item_id,
            "signature": signature,
            "attempt": record["attempts"],
            "paused": paused,
            "progress_epoch": self.progress_epoch,
            "prompt": f"当前事项：{item.question}。相同请求与结果已重复 {self.trigger} 次，"
            f"资料覆盖与事项处置没有推进。操作：{tool} {json.dumps(args, ensure_ascii=False)}；"
            f"结果摘要：{semantic[:1200]}。请自检检索范围、假设或引用方式是否有误，"
            "说明调整理由并选择可补充相关依据的合法只读路线；若无法推进，"
            "用 update_research 具体说明缺少什么。换分析 ID 或声称已自愈不代表进展。",
        }
