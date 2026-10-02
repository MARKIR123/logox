"""`/resume` 之后必须保真的东西（CHANGE-003 / F-32 + F-33 + 摘要兜底）。

为什么单独一个文件
==================

这些缺陷有一个共同特征：**只在"跨进程恢复会话"这条路径上出现**。

单测要么**直接构造** `list[Message]`（`meta` 是手写上去的），要么在**同一个进程内**
跑完整流程 —— 没有任何用例会去"重建一份从磁盘读回来的历史"。
于是就出现了这个局面：**1325 个测试全绿，而 resume 路径完全裸露**。

而 resume 是**每天都会走**的路径，不是边角情况。真实会话实测（`tui-34248.jsonl`）：

* 磁盘上有 **5** 条带 `turn_summary` 的记录，重建出的 182 条消息里带摘要的 **0** 条；
* 同一个文件里 `turn=1` 出现了 **3 次**（每次进程重启都从 1 重新开始），
  于是 `turn_lines` 把三次合并成 `{1: (1, 238)}` —— "第 1 轮"覆盖了整个文件。

本文件盯住三条不变量
====================

* **I-1（F-33）**：`turn_finished` 记录里的摘要必须回到对应轮次的 assistant 消息上。
* **I-2（F-32）**：轮次号**从历史推导**，因此跨 resume 单调，不与文件里已有的号冲突。
* **I-3（兜底）**：某一轮没有摘要时（老会话 / 模型没按契约输出），
  索引必须给出**确定性的可读兜底**，而不是「（本轮无摘要）」。
"""

from __future__ import annotations

import unittest
from typing import Any

from logox.context.compaction import Compactor
from logox.kernel.messages import Message, MessageMeta, TextBlock
from logox.store.replay import reconstruct_messages
from tests.unit.kernel_support import install, text_chunks

__all__ = [
    "TurnNumberingAcrossResumeTests",
    "ResumeReplayFidelityTests",
    "MissingSummaryFallbackTests",
    "TranscriptLineStampingTests",
]


# --------------------------------------------------------------------------- #
# I-1：摘要必须从 turn_finished 记录回到 assistant 消息上
# --------------------------------------------------------------------------- #


def _record(kind: str, turn: int, role: str, content: str, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"type": kind, "turn": turn, "role": role, "content": content}
    payload.update(extra)
    return payload


class ResumeReplayFidelityTests(unittest.TestCase):
    def test_turn_summary_is_restored_from_turn_finished_records(self) -> None:
        """**I-1（F-33）**：`/resume` 之后摘要必须回到对应轮次的 assistant 上。

        不回刻的后果：压缩索引的每一条目只能是「（本轮无摘要）」——
        而"每轮做了什么"正是压缩后最容易丢的信息。
        """
        records = [
            _record("user_prompt", 1, "user", "第一问"),
            _record("model_output", 1, "assistant", "第一答"),
            _record("turn_finished", 1, "system", "改了 fs_edit 并补用例", turn_summary="改了 fs_edit 并补用例"),
            _record("user_prompt", 2, "user", "第二问"),
            _record("model_output", 2, "assistant", "第二答"),
            _record("turn_finished", 2, "system", "加了缓存", turn_summary="加了缓存"),
        ]
        messages = reconstruct_messages(records)
        summaries = [m.meta.turn_summary for m in messages if m.role == "assistant"]
        self.assertEqual(summaries, ["改了 fs_edit 并补用例", "加了缓存"])

    def test_summary_lands_on_the_last_assistant_of_its_turn(self) -> None:
        """摘要要挂在**该轮最后一条** assistant 上（一轮可能有多次模型请求）。"""
        records = [
            _record("user_prompt", 1, "user", "问"),
            _record("model_output", 1, "assistant", "第一段"),
            _record("tool_result", 1, "tool", "结果", call_id="c1"),
            _record("model_output", 1, "assistant", "第二段"),
            _record("turn_finished", 1, "system", "摘要", turn_summary="摘要"),
        ]
        messages = reconstruct_messages(records)
        assistants = [m for m in messages if m.role == "assistant"]
        self.assertEqual(len(assistants), 2)
        self.assertIsNone(assistants[0].meta.turn_summary)
        self.assertEqual(assistants[1].meta.turn_summary, "摘要")

    def test_summary_source_is_restored_alongside_the_summary(self) -> None:
        """摘要**来源**必须随摘要一起回刻（Q-D：历史消息要能自证是谁写的）。

        只带回 `turn_summary` 的后果：磁盘上写得清清楚楚的来源
        （`model_last_line` / `model_fallback` / `deterministic`），
        `/resume` 之后全变 `None` —— 实测 55 条有来源 → 重建后 0 条。
        """
        records = [
            _record("user_prompt", 1, "user", "问"),
            _record("model_output", 1, "assistant", "答"),
            _record(
                "turn_finished", 1, "system", "摘要",
                turn_summary="摘要", summary_source="model_last_line",
            ),
        ]
        messages = reconstruct_messages(records)
        self.assertEqual(messages[-1].meta.turn_summary, "摘要")
        self.assertEqual(messages[-1].meta.summary_source, "model_last_line")

    def test_missing_summary_source_stays_none(self) -> None:
        """老会话 / 失败轮次没有来源字段时不得编造（诚实降级为 `None`）。"""
        records = [
            _record("user_prompt", 1, "user", "问"),
            _record("model_output", 1, "assistant", "答"),
            _record("turn_finished", 1, "system", "摘要", turn_summary="摘要"),
        ]
        messages = reconstruct_messages(records)
        self.assertEqual(messages[-1].meta.turn_summary, "摘要")
        self.assertIsNone(messages[-1].meta.summary_source)

    def test_meta_has_no_never_written_fields(self) -> None:
        """`MessageMeta` 不得再留从来没人写过/读过的字段。

        （`created_at` / `token_estimate` 两个字段零写入零读取，已删除；
        留下它们只会让人以为"这里有时间戳与缓存"。）
        """
        for name in ("created_at", "token_estimate"):
            self.assertNotIn(name, MessageMeta.model_fields, f"{name} 又长回来了")

    def test_turn_finished_without_summary_does_not_break_replay(self) -> None:
        """`turn_finished` 没有摘要时不得抛异常（老会话 / 失败轮次）。"""
        records = [
            _record("user_prompt", 1, "user", "问"),
            _record("model_output", 1, "assistant", "答"),
            _record("turn_finished", 1, "system", "", reason="error"),
        ]
        messages = reconstruct_messages(records)
        self.assertEqual([m.role for m in messages], ["user", "assistant"])


# --------------------------------------------------------------------------- #
# I-2：轮次号跨 resume 单调
# --------------------------------------------------------------------------- #


class TurnNumberingAcrossResumeTests(unittest.IsolatedAsyncioTestCase):
    async def test_turn_index_continues_from_the_restored_history(self) -> None:
        """**I-2（F-32）**：恢复 3 轮历史之后，新回合必须是第 4 轮。

        改之前：`turn_index = len(self._turns) + 1`，而 `_turns` 是**进程级**的
        —— 新进程里从 1 重新开始，于是同一个会话文件里出现多个 `turn=1`。
        """
        env = install([text_chunks("好的")])

        restored: list[Message] = []
        for index in range(1, 4):
            restored.append(Message(role="user", blocks=[TextBlock(text=f"历史第{index}问")]))
            restored.append(Message(role="assistant", blocks=[TextBlock(text=f"历史第{index}答")]))
        env.kernel.history.extend(restored)

        turn = await env.kernel.submit("新的一轮")

        self.assertEqual(turn.turn_index, 4, "轮次号必须接着历史往下数")
        self.assertEqual(env.kernel.turn_index, 4, "内核对外暴露的轮次号也应一致")
        self.assertEqual(
            env.recorder.find("turn_finished").turn_index,
            4,
            "事件里的轮次号也必须是 4（transcript 靠它做行号映射）",
        )

    async def test_fresh_session_still_numbers_from_one(self) -> None:
        """空历史时仍从 1 开始（不能把修复做过头）。"""
        env = install([text_chunks("a"), text_chunks("b")])
        first = await env.kernel.submit("一")
        second = await env.kernel.submit("二")
        self.assertEqual((first.turn_index, second.turn_index), (1, 2))


# --------------------------------------------------------------------------- #
# I-3：摘要缺失时的确定性兜底
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# I-4（CHANGE-052）：`transcript.jsonl` 的行号必须跟着消息一起重建出来
# --------------------------------------------------------------------------- #


class TranscriptLineStampingTests(unittest.TestCase):
    """**I-4（CHANGE-052）**：replay 必须把每条消息来自第几行带下来。

    为什么值得看护：行号是"模型按行号 `fs_read` 回原文"这条路**唯一可靠**的引用键。
    从前压缩器只能拿**轮次号**去查行号，而那是**视图相对**的编号：

    * `/resume` 之前的旧会话文件里，轮次号会被两个进程复用（F-32；新日志已修）；
    * 更根本的是 —— 压缩后视图里的"第 5 轮"**不等于** transcript 里的 `turn=5`
      （视图里前面的轮次已被折成一对），于是查出来的行号指向**别的段**。

    后果不是"少一个数字"，而是**静默读到错的内容**：守卫因此宁可整体拒给
    （`turn_lines_of` 一票否决），实测区间查询 **100% 返回 None** ——
    索引里的"可按行号读取"长期是一句空话。
    """

    def test_line_numbers_are_stamped_on_every_message(self) -> None:
        records = [
            dict(_record("user_prompt", 1, "user", "第一问"), line=1),
            dict(_record("model_output", 1, "assistant", "第一答"), line=2),
            dict(_record("tool_result", 1, "tool", "结果", call_id="c1"), line=3),
            dict(_record("turn_finished", 1, "system", "", turn_summary="摘要"), line=4),
        ]
        messages = reconstruct_messages(records)
        self.assertEqual(
            [m.meta.transcript_line for m in messages],
            [1, 2, 3],
            "每条消息都要带上它来自的行号（turn_finished 不产生消息）",
        )

    def test_restamped_summary_keeps_its_line_number(self) -> None:
        """★ F-33 的回刻（`model_copy` 改 meta）不得把行号弄丢。"""
        records = [
            dict(_record("user_prompt", 1, "user", "问"), line=7),
            dict(_record("model_output", 1, "assistant", "答"), line=8),
            dict(_record("turn_finished", 1, "system", "摘要"), line=9, turn_summary="摘要"),
        ]
        messages = reconstruct_messages(records)
        assistant = next(m for m in messages if m.role == "assistant")
        self.assertEqual(assistant.meta.transcript_line, 8)
        self.assertEqual(assistant.meta.turn_summary, "摘要", "两个字段必须共存")

    def test_turn_line_range_is_computable_from_messages(self) -> None:
        """轮的**行区间** = 该轮消息行号的 min/max（压缩器标注行号的口径）。"""
        records = [
            dict(_record("user_prompt", 1, "user", "第一问"), line=2),
            dict(_record("model_output", 1, "assistant", "第一答"), line=3),
            dict(_record("turn_finished", 1, "system", ""), line=4, turn_summary="a"),
            dict(_record("user_prompt", 2, "user", "第二问"), line=15),
            dict(_record("tool_result", 2, "tool", "结果", call_id="c1"), line=16),
        ]
        messages = reconstruct_messages(records)
        lines = [m.meta.transcript_line for m in messages if m.meta.transcript_line]
        self.assertEqual((min(lines), max(lines)), (2, 16))
        self.assertEqual(lines, sorted(lines), "append-only 日志里行号必须单调不减")

    def test_records_without_line_field_do_not_break_replay(self) -> None:
        """没有 `line` 字段的记录（手写 / 极老格式）⇒ 行号为 `None`，**不得抛异常**。

        `_record()` 刻意**不带** `line` —— 本文件其余用例走的都是这条降级路径，
        所以这条断言同时是"整个文件都没被行号改动破坏"的证据。
        """
        messages = reconstruct_messages([_record("user_prompt", 1, "user", "问")])
        self.assertEqual(len(messages), 1)
        self.assertIsNone(messages[0].meta.transcript_line)

    def test_stamping_covers_self_healed_assistant(self) -> None:
        """历史的**自愈**路径（补一个 assistant 容器）同样必须带行号。

        漏掉它的症状：该轮行区间少一段 ⇒ min/max 偏小 ⇒ 模型按它读会读到**别的段**。
        """
        records = [
            dict(_record("user_prompt", 1, "user", "问"), line=1),
            dict(_record("tool_result", 1, "tool", "孤儿结果", call_id="c1"), line=5),
        ]
        messages = reconstruct_messages(records)
        self.assertTrue(
            all(m.meta.transcript_line is not None for m in messages),
            "自愈补出来的 assistant 也必须被盖上行号",
        )


class MissingSummaryFallbackTests(unittest.TestCase):
    """I-3：没有 `turn_summary` 时的**确定性兜底**。

    ★ CHANGE-052 调整了**断言的落点**：旧版查"索引里有没有 `提问：`"，
    而摘要现在按轮保留在各自的 assistant 消息上、**不再汇总进索引**
    （汇总会构成双事实来源）。所以这里改查**折叠轮自己的摘要**。
    """

    @staticmethod
    def _history_without_summaries(turns: int) -> list[Message]:
        messages = [Message(role="user", blocks=[TextBlock(text="初始目标")])]
        for index in range(1, turns + 1):
            messages.append(
                Message(role="user", blocks=[TextBlock(text=f"第{index}轮要做的事情")])
            )
            messages.append(
                Message(role="assistant", blocks=[TextBlock(text=f"第{index}轮的回答")])
            )
        return messages

    @staticmethod
    def _folded_summaries(messages: list[Message]) -> list[str]:
        return [
            m.meta.turn_summary or ""
            for m in messages
            if m.role == "assistant" and m.meta.source == "compaction"
        ]

    def test_falls_back_to_the_question_when_no_summary_exists(self) -> None:
        """**I-3**：没有 `turn_summary` 时（老会话）必须给出可读兜底。

        兜底取**用户提问原文** —— 折叠轮的用途是让未来的模型判断
        "要不要去读原文"，而"那一轮要做什么"在没有"做了什么"的情况下
        是次优但可用的信息。
        """
        compactor = Compactor(window_capacity=10**9, keep_recent_turns=2)
        out = compactor.compact(self._history_without_summaries(6), force=True).messages

        summaries = self._folded_summaries(out)
        self.assertTrue(summaries, "必须有折叠轮摘要")
        self.assertTrue(
            any("提问：" in text for text in summaries), "应当退回到用户提问"
        )
        self.assertNotIn("无摘要", " ".join(summaries), "不得留下「（本轮无摘要）」这种空占位")

    def test_real_summary_wins_over_the_fallback(self) -> None:
        """有真摘要时必须用真摘要（兜底只在缺失时生效）。"""
        messages = [
            Message(role="user", blocks=[TextBlock(text="初始目标")]),
            Message(role="user", blocks=[TextBlock(text="第1轮提问")]),
            Message(
                role="assistant",
                blocks=[TextBlock(text="第1轮回答")],
                meta=MessageMeta(turn_summary="真正的摘要内容"),
            ),
            Message(role="user", blocks=[TextBlock(text="第2轮提问")]),
            Message(role="assistant", blocks=[TextBlock(text="第2轮回答")]),
            Message(role="user", blocks=[TextBlock(text="第3轮提问")]),
            Message(role="assistant", blocks=[TextBlock(text="第3轮回答")]),
        ]
        compactor = Compactor(window_capacity=10**9, keep_recent_turns=2)
        out = compactor.compact(messages, force=True).messages

        self.assertIn("真正的摘要内容", " ".join(self._folded_summaries(out)))

    def test_long_answer_is_compressed_not_kept_whole(self) -> None:
        """★ CHANGE-052：折叠轮的回答**一律压成摘要**（含第 1 轮）。

        旧行为是"锚点轮没摘要就不压、整条留着"（`_head_anchor` 的 fallback）。
        锚点概念删除后这条特例也删了 —— 用户裁定第 1 轮与其余轮**处理完全相同**。

        ⚠️ 所以这里断言的是**相反**的行为：长回答不再原样保留，
        而是走兜底摘要（否则"折叠"对第一轮等于没做）。
        """
        messages = [
            Message(role="user", blocks=[TextBlock(text="初始目标")]),
            Message(role="assistant", blocks=[TextBlock(text="很长很长的第一轮回答" + "详" * 300)]),
        ]
        for index in range(1, 7):
            messages.append(Message(role="user", blocks=[TextBlock(text=f"第{index}轮提问")]))
            messages.append(Message(role="assistant", blocks=[TextBlock(text=f"第{index}轮回答")]))

        compactor = Compactor(window_capacity=10**9, keep_recent_turns=2)
        out = compactor.compact(messages, force=True).messages

        self.assertEqual(out[0].text, "初始目标", "提问仍逐字保留")
        self.assertEqual(out[1].role, "assistant")
        self.assertNotIn("详" * 100, out[1].text, "长回答必须被压掉（不再有锚点特例）")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
