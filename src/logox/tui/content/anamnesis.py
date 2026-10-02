"""One bounded, explainable card per logical run; complete records remain on disk."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from rich.console import Console
from rich.text import Text

from logox.tui.content.timeline import SPINNER_FRAMES

PHASES = {
    "collecting": "收集资料",
    "summarizing": "回顾总结",
    "reviewing": "分析研究",
    "validating": "核验依据",
    "pausing": "正在暂停",
    "paused": "已暂停／可续做",
    "continuing": "批次已保存／等待续做",
    "completed": "已完成",
    "failed": "失败",
    "interrupted": "已中断／原窗口结束",
    "restoring": "正在恢复记录",
}


@dataclass
class AnamesisCard:
    run_id: str
    mode: str = "nap"
    phase: str = "collecting"
    question: str = ""
    reason: str = ""
    analyses: dict = field(default_factory=dict)
    changes: dict = field(default_factory=dict)
    report_path: str = ""
    session_id: str = ""
    sequence: int = 0
    reasoning: str = ""
    operations: dict = field(default_factory=dict)
    findings: list[str] = field(default_factory=list)
    plan: list[str] = field(default_factory=list)
    revision: int = 0
    detail_revision: int = 0
    started_at: float = 0
    elapsed: int = 0
    animation_frame: int = 0
    _rendered_key: tuple | None = None
    _rendered: Text | None = None
    _body_key: tuple | None = None
    _body: Text | None = None
    _thought_key: tuple | None = None
    _thought: Text | None = None

    def ingest(self, event) -> None:
        if event.sequence and event.sequence <= self.sequence:
            return
        self.sequence = event.sequence or self.sequence
        self.session_id = event.session_id or self.session_id
        self.mode = event.mode or self.mode
        self.phase = event.phase or self.phase
        self.question = event.question or self.question
        self.reason = event.reason if event.kind == "started" else event.reason or self.reason
        self.report_path = event.report_path or self.report_path
        self.started_at = event.started_at or self.started_at
        if event.timestamp and self.started_at:
            self.elapsed = int(max(0, event.timestamp - self.started_at))
        if event.delta:
            self.reasoning = (self.reasoning + event.delta)[-12000:]
        if event.operation:
            op = {**event.operation}
            for key in ("result", "error"):
                if key in op:
                    op[key] = str(op[key])[:2000]
            self.operations[op.get("call_id", str(self.revision))] = op
            while len(self.operations) > 32:
                del self.operations[next(iter(self.operations))]
            self.detail_revision += 1
        if event.findings or event.plan:
            self.findings, self.plan = event.findings[-30:], event.plan[-30:]
            self.detail_revision += 1
        if event.analysis is not None:
            self.analyses[event.analysis.record_id] = event.analysis
            self.detail_revision += 1
        if event.change is not None:
            self.changes[(event.change.scope, event.change.entry_id)] = (
                event.change,
                event.result,
                event.kind,
            )
            self.detail_revision += 1
        # Preview state is bounded; complete explanations/diffs are in the report and trace.
        for values, limit in ((self.analyses, 64), (self.changes, 50)):
            while len(values) > limit:
                del values[next(iter(values))]
        if self.phase in {"paused", "failed", "interrupted"}:
            for op in self.operations.values():
                if op.get("state") == "running":
                    op["state"] = "interrupted"
        self.revision += 1

    @classmethod
    def from_preview(cls, preview: dict, *, active: bool = False):
        from types import SimpleNamespace

        card = cls(preview["run_id"])
        for event in preview.get("events", []):
            card.ingest(event)
        card.reasoning = preview.get("reasoning", "")[-12000:]
        card.operations = dict(list(preview.get("operations", {}).items())[-32:])
        card.findings = preview.get("findings", [])[-30:]
        card.plan = preview.get("plan", [])[-30:]
        phase = preview.get("phase", "interrupted")
        if not active and phase == "continuing":
            phase = "paused"
        if not active and phase not in {"completed", "paused", "failed"}:
            phase = "interrupted"
        card.ingest(
            SimpleNamespace(
                kind="restored",
                run_id=card.run_id,
                phase=phase,
                mode=preview.get("mode", "nap"),
                session_id=preview.get("session_id", ""),
                sequence=preview.get("sequence", 0),
                started_at=preview.get("started_at", 0),
                timestamp=preview.get("timestamp", 0),
                question=preview.get("question", ""),
                reason=preview.get("reason", ""),
                report_path=preview.get("report_path", ""),
                analysis=None,
                change=None,
                operation=None,
                delta="",
                findings=[],
                plan=[],
                result="",
            )
        )
        card.detail_revision += 1
        return card

    def tick(self, now: float | None = None) -> bool:
        if self.phase in {"completed", "paused", "continuing", "failed", "interrupted", "restoring"}:
            return False
        current = time.time() if now is None else now
        elapsed = int(max(0, current - self.started_at)) if self.started_at else 0
        frame = int(current * 10) % len(SPINNER_FRAMES)
        if elapsed == self.elapsed and frame == self.animation_frame:
            return False
        self.elapsed, self.animation_frame = elapsed, frame
        self.revision += 1
        return True

    def _render_reasoning(self, width: int, color: str) -> Text:
        key = (self.reasoning, width, color)
        if self._thought_key != key or self._thought is None:
            text = Text("\n  模型返回的思考（最近片段；尚未核验）\n", style="bold")
            text.append(
                "    "
                + (self.reasoning or "尚未收到模型返回的思考文本；阶段说明以模型实际输出为准。")
                + "\n",
                style="",
            )
            self._thought = Text("\n").join(text.wrap(Console(width=width), width=width, overflow="fold"))
            self._thought_key = key
        return self._thought

    def render(self, *, expanded: bool, width: int, color: str) -> Text:
        key = (self.revision, expanded, width, color)
        if self._rendered_key == key and self._rendered is not None:
            return self._rendered
        out = Text()
        mode = "长眠" if self.mode == "sleep" else "小憩"
        phase = PHASES.get(self.phase, self.phase)
        terminal_glyphs = {
            "completed": "✓",
            "failed": "✗",
            "paused": "Ⅱ",
            "continuing": "↻",
            "interrupted": "■",
            "restoring": "·",
        }
        activity = terminal_glyphs.get(self.phase, SPINNER_FRAMES[self.animation_frame])
        out.append(
            f"{'▾' if expanded else '▸'} {activity} Anamnesis · {mode} · {phase} · {self.elapsed // 60}m{self.elapsed % 60:02d}s\n",
            style=f"bold {color}",
        )
        if self.question:
            label = (
                "本次判断" if self.phase in {"completed", "paused", "failed", "interrupted"} else "正在判断"
            )
            out.append(f"  {label}：{self.question[:240]}\n")
        if self.reason:
            out.append(f"  {self.reason[:500]}\n")
        body_key = (self.detail_revision, width, color)
        if expanded and self._body_key == body_key and self._body is not None:
            wrapped = Text("\n").join(out.wrap(Console(width=width), width=width, overflow="fold"))
            wrapped.append_text(self._render_reasoning(width, color))
            wrapped.append_text(self._body)
            self._rendered_key, self._rendered = key, wrapped
            return wrapped
        header = out
        out = Text()
        if expanded:
            for analysis in self.analyses.values():
                out.append(f"\n  阶段 {analysis.stage_id}：{analysis.question[:350]}\n", style="bold")
                for label, value in (
                    ("依据", analysis.evidence_summary),
                    ("判断原因", analysis.rationale),
                    ("比较", "；".join(analysis.alternatives)),
                    ("结论", analysis.conclusion),
                    ("待确认", "；".join(analysis.uncertainties)),
                ):
                    if value:
                        out.append(
                            f"    {label}：{value[:1200]}{' …（全文见报告）' if len(value) > 1200 else ''}\n"
                        )
                if analysis.revises_record_id:
                    out.append(f"    修订此前结论：{analysis.revises_record_id}\n")
            for change, result, kind in self.changes.values():
                action = {"add": "增加", "replace": "修改", "delete": "删除"}[change.action]
                scope = "用户" if change.scope == "user" else "项目"
                out.append(f"\n  {scope}档案 · {action} {change.entry_id}\n", style="bold")
                out.append(f"    原因：{change.rationale[:1200]}\n")
                out.append(f"    旧值：{change.old_value[:800] or '（无）'}\n")
                out.append(f"    新值：{change.new_value[:800] or '（删除）'}\n")
                out.append(f"    来源：{', '.join(change.source_ids)}\n")
                out.append(f"    {'已保存版本' if kind == 'committed' else '核验结果'}：{result}\n")
            for label, values in (("只读发现（待验证）", self.findings), ("下一步建议", self.plan)):
                if values:
                    out.append(f"\n  {label}\n", style="bold")
                    for value in values:
                        out.append(f"    • {value[:1200]}\n")
            if self.operations:
                out.append("\n  文件／来源读取明细（最近 32 项）\n", style="bold")
                for op in self.operations.values():
                    out.append(
                        f"    {op.get('tool', '')} · {op.get('state', '')} · {str(op.get('arguments', {}))[:500]}\n"
                    )
                    detail = op.get("error") or op.get("result", "")
                    if detail:
                        out.append(f"      {detail[:2000]}\n")
            out.append(f"\n  /anamnesis report {self.run_id} 查看完整报告\n", style="dim")
            out.append(f"  /anamnesis trace {self.run_id} 查看完整原始过程\n", style="dim")
        body = Text("\n").join(out.wrap(Console(width=width), width=width, overflow="fold"))
        if expanded:
            self._body_key, self._body = body_key, body
        wrapped = Text("\n").join(header.wrap(Console(width=width), width=width, overflow="fold"))
        if expanded:
            wrapped.append_text(self._render_reasoning(width, color))
        wrapped.append_text(body)
        self._rendered_key, self._rendered = key, wrapped
        return wrapped
