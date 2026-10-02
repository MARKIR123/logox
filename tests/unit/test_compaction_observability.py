"""新增：压缩可观测性用例（D156 / CHANGE-026）。

覆盖四件事：
1. 真的压缩时，`ContextBundle.compaction` 报告字段正确；
2. 没发生压缩时，报告为 `None`（订阅者不该收到假事件）；
3. 内核把报告**发布**成 `CompactionFinished`（F-54 的那根缺失的线）；
4. 持久化把这条事件写成 `compaction` 记录（事后可查证）；
5. dev 转储（`dump_compaction=True`）写出"压缩前/后"现场。
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import unittest
from unittest import mock

from logox.context.builder import HierarchicalContextBuilder
from logox.context.memory import ProjectMemory
from logox.context.storage import SessionTranscriptWriter
from logox.kernel import events as ev
from logox.kernel.bus import EventBus
from logox.kernel.loop import CompactionReport, ContextBundle, KernelLoop
from logox.kernel.messages import Message, MessageMeta, TextBlock
from logox.kernel.turn import Turn
from tests.unit.support import make_temp_dir, remove_temp_dir

WINDOW = {"window_capacity": 2000, "max_budget_tokens": 1500, "target_budget_tokens": 1000}
INDEX_MARK = "[历史归档索引]"


def history_with_summaries(turns: int, *, chars: int = 200) -> list[Message]:
    """生产形态历史：锚点轮是完整的一轮，每条助手消息都带 `turn_summary`（D135 之后的真实形态）。"""
    messages = [
        Message(role="user", blocks=[TextBlock(text="初始目标" + "初" * chars)]),
        Message(
            role="assistant",
            blocks=[TextBlock(text="初始答" + "答" * chars)],
            meta=MessageMeta(turn_summary="第0轮摘要"),
        ),
    ]
    for index in range(1, turns + 1):
        messages.append(Message(role="user", blocks=[TextBlock(text=f"第{index}问" + "问" * chars)]))
        messages.append(
            Message(
                role="assistant",
                blocks=[TextBlock(text=f"第{index}答" + "答" * chars)],
                meta=MessageMeta(turn_summary=f"第{index}轮摘要"),
            )
        )
    return messages


def make_builder(cwd: pathlib.Path, **kwargs) -> HierarchicalContextBuilder:
    writer = SessionTranscriptWriter(base_dir=pathlib.Path(cwd) / "sessions", session_id="obs")
    with mock.patch(
        "logox.context.builder.find_project_memory",
        return_value=ProjectMemory(sources=[], total_tokens=0),
    ):
        return HierarchicalContextBuilder(
            system="你是 Logox",
            cwd=pathlib.Path(cwd),
            transcript_writer=writer,
            **{**WINDOW, **kwargs},
        )


class CompactionReportTests(unittest.TestCase):
    """报告本身：有压缩就有报告，没压缩就没有。"""

    def setUp(self) -> None:
        self.tmp = make_temp_dir("obs-report-")

    def tearDown(self) -> None:
        remove_temp_dir(self.tmp)

    def test_t01_report_is_produced_when_compaction_happens(self) -> None:
        builder = make_builder(self.tmp)
        bundle = builder.build(history_with_summaries(turns=30))

        report = bundle.compaction
        self.assertIsNotNone(report, "发生了压缩却没有报告 ⇒ 内核无法发布事件（F-54 复发）")
        assert report is not None
        self.assertGreater(report.tokens_before, report.tokens_after, "压缩后不该更大")
        self.assertGreaterEqual(report.folded_turns, 1)
        self.assertEqual(report.strategy, "summary-only")
        self.assertEqual(report.message_count_after, len(bundle.messages))
        self.assertFalse(report.degraded, "历史每轮都有 turn_summary，不该走兜底摘要")

    def test_t02_no_report_when_nothing_happened(self) -> None:
        builder = make_builder(self.tmp, window_capacity=16384, max_budget_tokens=None, target_budget_tokens=None)
        bundle = builder.build(history_with_summaries(turns=1))
        self.assertIsNone(bundle.compaction, "没压缩就不该有报告（否则订阅者收到假事件）")


class CompactionEventTests(unittest.IsolatedAsyncioTestCase):
    """内核必须把报告**发到总线上**（F-54 的核心）。"""

    async def test_t03_kernel_publishes_compaction_finished(self) -> None:
        from logox.kernel.registry import ToolRegistry

        class _FakeBuilder:
            def build(self, history, *, last_usage=None):  # noqa: ANN001, ANN202
                return ContextBundle(
                    system="sys",
                    messages=[Message(role="user", blocks=[TextBlock(text="hi")])],
                    token_estimate=42,
                    compaction=CompactionReport(
                        tokens_before=1000,
                        tokens_after=400,
                        message_count_before=20,
                        message_count_after=6,
                        pruned_count=3,
                        folded_turns=12,
                        strategy="prune+fold",
                    ),
                )

        bus = EventBus(session_id="obs")
        seen: list[ev.CompactionFinished] = []

        async def _on(event: ev.CompactionFinished) -> None:
            seen.append(event)

        bus.subscribe(ev.CompactionFinished, _on, name="probe")

        class _NoProvider:
            async def stream(self, request):  # noqa: ANN001, ANN202
                if False:  # pragma: no cover - 本用例不发请求
                    yield None

        kernel = KernelLoop(bus, _NoProvider(), ToolRegistry(), _FakeBuilder(), model="m")  # type: ignore[arg-type]
        await kernel._build_context(Turn(0))
        await asyncio.sleep(0)  # 让总线把事件投递出去

        self.assertEqual(len(seen), 1, "内核没有发布 CompactionFinished（F-54 复发）")
        event = seen[0]
        self.assertEqual(event.tokens_before, 1000)
        self.assertEqual(event.tokens_after, 400)
        self.assertEqual(event.pruned_count, 3)
        self.assertEqual(event.folded_turns, 12)
        self.assertEqual(event.strategy, "prune+fold")
        self.assertEqual(event.message_count_after, 6)


class CompactionRecordTests(unittest.TestCase):
    """持久化：压缩事件要写进 transcript，事后可查证。"""

    def test_t04_compaction_is_recorded_in_the_transcript(self) -> None:
        from logox.store.persistence import SessionPersistenceSubscriber

        tmp = make_temp_dir("obs-record-")
        try:
            log_file = tmp / "transcript.jsonl"
            writer = SessionTranscriptWriter(log_file=log_file)
            subscriber = SessionPersistenceSubscriber(writer)
            subscriber.apply(
                ev.CompactionFinished(
                    session_id="s",
                    turn=3,
                    tokens_after=400,
                    message_count_after=6,
                    tokens_before=1000,
                    pruned_count=3,
                    folded_turns=12,
                    strategy="prune+fold",
                )
            )
            if hasattr(writer, "flush"):
                writer.flush()

            records = [
                json.loads(line)
                for line in log_file.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            compaction = [r for r in records if r.get("type") == "compaction"]
            self.assertEqual(len(compaction), 1, "压缩没有落盘 ⇒ 事后查不了（可观测性缺口）")
            meta = compaction[0]["meta"]
            self.assertEqual(meta["tokens_before"], 1000)
            self.assertEqual(meta["tokens_after"], 400)
            self.assertEqual(meta["folded_turns"], 12)
            self.assertEqual(meta["strategy"], "prune+fold")
        finally:
            remove_temp_dir(tmp)


class CompactionDumpTests(unittest.TestCase):
    """dev 转储：`LOGOX_DUMP_COMPACTION=1` / `dump_compaction=True` 时保留压缩现场。"""

    def test_t05_dump_keeps_before_and_after(self) -> None:
        tmp = make_temp_dir("obs-dump-")
        try:
            builder = make_builder(tmp, dump_compaction=True)
            bundle = builder.build(history_with_summaries(turns=30))
            self.assertIsNotNone(bundle.compaction)

            dump_dir = pathlib.Path(builder.writer.session_dir) / "compaction"
            markdown = sorted(dump_dir.glob("*.md"))
            payloads = sorted(dump_dir.glob("*.json"))
            self.assertEqual(len(markdown), 1, "应当恰好留下一份压缩现场")
            self.assertEqual(len(payloads), 1)

            text = markdown[0].read_text(encoding="utf-8")
            self.assertIn("压缩现场", text)
            self.assertIn("summary-only", text)
            self.assertIn(INDEX_MARK, text, "现场里必须能看到归档索引（否则无法诊断 F-53 那类问题）")
            self.assertIn("## system（逐字）", text)

            data = json.loads(payloads[0].read_text(encoding="utf-8"))
            self.assertIn("before", data)
            self.assertIn("after", data)
            # ★ CHANGE-052：**度量换了** —— 新结构下"压缩"不再是"消息条数变少"
            #   （每轮 2 条进、2 条出：user 逐字保留 + 回答换成摘要），
            #   而是**每条变短**。所以这里比 **tokens**，不比条数。
            #   旧断言 `len(before) > len(after)` 在新结构下恒为假（实测 62 → 63，
            #   多出来的那条正是**归档索引**）。
            report = data["report"]
            self.assertLess(
                report["tokens_after"],
                report["tokens_before"],
                "压缩必须让 token 变少（这才是本设计的度量口径）",
            )
            # ⚠️ dump 里是 `model_dump(mode="json")` 的结果 ⇒ 是 **dict**，不是对象
            def _mass(messages: list[dict]) -> int:
                return sum(
                    len(block.get("text") or block.get("content") or "")
                    for message in messages
                    for block in message.get("blocks", [])
                )

            before_mass, after_mass = _mass(data["before"]), _mass(data["after"])
            self.assertLess(after_mass, before_mass, "文本量必须真的减少")
            self.assertEqual(report["strategy"], "summary-only")

            # 原子写不留半截文件
            self.assertEqual(list(dump_dir.glob("*.tmp")), [])
        finally:
            remove_temp_dir(tmp)

    def test_t06_dump_is_off_by_default(self) -> None:
        tmp = make_temp_dir("obs-dump-off-")
        try:
            with mock.patch.dict("os.environ", {}, clear=False):
                import os

                os.environ.pop("LOGOX_DUMP_COMPACTION", None)
                builder = make_builder(tmp)
                builder.build(history_with_summaries(turns=30))
            self.assertFalse(builder.dump_compaction, "默认必须关闭（不能给正常会话写盘）")
            self.assertEqual(
                list((pathlib.Path(builder.writer.session_dir) / "compaction").glob("*"))
                if (pathlib.Path(builder.writer.session_dir) / "compaction").exists()
                else [],
                [],
                "默认不该产生任何转储文件",
            )
        finally:
            remove_temp_dir(tmp)


if __name__ == "__main__":
    unittest.main()
