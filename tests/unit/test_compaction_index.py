"""归档索引的形态（D4）、真实行号（D6）与幂等落盘（F-17）。

这三条是同一个设计决策的三个面：**折叠之后留下来的东西，必须是可回查、可读懂、
且不重复劳动的。**

* **D4**：索引是**一条 `user` 消息**（不是 `system`），且第一轮提问逐字保留、
  第一轮回答压成摘要 —— 只压一次，再折多少次都不会越压越少。
* **D6**：索引里的行号是 `transcript.jsonl` 的**真实行号**，不是启发式估算。
  早先的写法取的是"当前最新行"，**模型照它去 `fs_read` 会读到错的内容，而且不会发现**。
* **F-17**：同一份工具输出只在**缺失时**落盘一次，不随折叠回合数反复覆盖写。
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from logox.context.compaction import Compactor, INDEX_MARKER, is_index_message, split_turn_spans
from logox.context.storage import SessionTranscriptWriter
from logox.kernel.messages import (
    Message,
    MessageMeta,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)

__all__ = ["IndexLineNumberTests", "IndexShapeTests", "BlobIdempotenceTests"]


def _turn(index: int, *, summary: str | None = None, tools: bool = False) -> list[Message]:
    """一个完整轮次：user →（可选 tool_use/tool_result）→ assistant。"""
    turn = [Message(role="user", blocks=[TextBlock(text=f"第{index}轮提问")])]
    if tools:
        turn.append(
            Message(
                role="assistant",
                blocks=[ToolUseBlock(id=f"c{index}", name="read", input={"path": "a"})],
            )
        )
        turn.append(
            Message(
                role="tool",
                blocks=[ToolResultBlock(id=f"c{index}", ok=True, content="y" * 4000)],
            )
        )
    meta = MessageMeta(turn_summary=summary) if summary else MessageMeta()
    turn.append(Message(role="assistant", blocks=[TextBlock(text=f"第{index}轮回答")], meta=meta))
    return turn


def build_history(turns: int, *, tools_from: int = 99) -> list[Message]:
    messages = [Message(role="user", blocks=[TextBlock(text="初始目标（要逐字保留）")])]
    for index in range(1, turns + 1):
        messages.extend(_turn(index, summary=f"第{index}轮的摘要", tools=index >= tools_from))
    return messages


class _TempWriter(unittest.TestCase):
    """给需要 transcript 的用例共用的临时目录夹具。"""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def make_writer(self, records_per_turn: tuple[int, ...] = (2, 3, 1)) -> SessionTranscriptWriter:
        writer = SessionTranscriptWriter(log_file=self.tmp / "transcript.jsonl")
        for turn, count in enumerate(records_per_turn, start=1):
            for step in range(count):
                writer.write_step(
                    turn=turn, step=step, role="user", event_type="user_prompt", content="x"
                )
        return writer


class IndexLineNumberTests(_TempWriter):
    def test_turn_lines_of_returns_real_ranges(self) -> None:
        """**D6**：行号区间是 writer 的真实记录（1-based，逐轮累计）。"""
        writer = self.make_writer((2, 3, 1))
        self.assertEqual(writer.current_line, 6)
        self.assertEqual(writer.turn_lines_of(1, 1), (1, 2))
        self.assertEqual(writer.turn_lines_of(2, 2), (3, 5))
        self.assertEqual(writer.turn_lines_of(1, 3), (1, 6))
        self.assertIsNone(writer.turn_lines_of(9, 9), "没有记录的轮次应返回 None")

    def test_turn_lines_are_rebuilt_when_reopening_an_existing_log(self) -> None:
        """**D6 的关键**：`/resume` 之后必须能**重建**映射，否则真实行号只在当次会话有效。"""
        path = self.tmp / "transcript.jsonl"
        first = SessionTranscriptWriter(log_file=path)
        for turn, count in ((1, 2), (2, 3)):
            for step in range(count):
                first.write_step(
                    turn=turn, step=step, role="user", event_type="user_prompt", content="x"
                )

        reopened = SessionTranscriptWriter(log_file=path)  # ≈ /resume
        self.assertEqual(reopened.current_line, 5)
        self.assertEqual(reopened.turn_lines_of(1, 2), (1, 5))

    def test_folded_turns_carry_the_real_line_numbers(self) -> None:
        """★ CHANGE-052 改写：行号从**索引**搬到**每轮的摘要开头**。

        用户裁定：「行表不应该放在索引处，应该放在**每轮的摘要开头**
        （本轮消息已折叠归档，日志行号 xx-xx）」。

        为什么搬：① **局部性** —— 某轮行号不可考时只影响那一轮，
        不会像旧实现那样"整个区间一起未知"（`turn_lines_of` 是一票否决）；
        ② 模型读到哪一轮就看到哪一轮的行号，不用回头去索引里对号。

        ⚠️ 行号的口径也变了：不再问 `writer.turn_lines_of()`（那依赖
        "轮次号 → 行号"的正则映射，而压缩器的轮次号是**视图相对**的 ⇒ 不同源），
        改用 **`MessageMeta.transcript_line`**（replay 从记录的 `"line"` 字段带来）。
        """
        history = build_history(6)
        # 给每条消息盖上"来自第几行"（生产里由 replay 盖，这里手工给）
        stamped = [
            message.model_copy(
                update={"meta": message.meta.model_copy(update={"transcript_line": index + 1})}
            )
            for index, message in enumerate(history)
        ]
        compactor = Compactor(window_capacity=10**9, keep_recent_turns=2)
        out = compactor.compact(stamped, force=True).messages

        folded = [m for m in out if m.role == "assistant" and m.meta.source == "compaction"]
        self.assertTrue(folded, "必须有折叠产物")
        for message in folded:
            self.assertIn("已归档 · 行 ", message.text, "每轮摘要开头必须带行号")
        self.assertNotIn("行号未知", "".join(m.text for m in folded))

        # 行号必须**单调不减**（append-only 日志的直接推论）
        numbers = [
            message.meta.archived_from_line
            for message in folded
            if message.meta.archived_from_line is not None
        ]
        self.assertEqual(numbers, sorted(numbers), "折叠轮的行号必须递增")

    def test_body_text_quoting_the_marker_is_not_an_index(self) -> None:
        """★★ **守卫**：正文里**引用** `[历史归档索引]` 的消息不得被当成索引。

        为什么值得一条专门的用例（实测驱动）：新结构**逐字保留用户提问**，
        而用户（或助手）引用文档时正文里**很容易出现这个词** ——
        本会话就有一轮把索引示例贴进来提问，于是那条 user 消息的正文带着该标记。

        误判的后果不是"少个数字"而是**静默失忆**：`_folded_head()` 会**提前**停在
        那条引用消息上 ⇒ 缓存前缀被截短 ⇒ 下一轮 `covered` 前进过多 ⇒
        中间那段折叠对被悄悄丢掉（与 F-53 同一类缺陷）。

        ⇒ 判据必须是"**文本以标记开头**"，不是"包含"。
        """
        # ① 引用形态：标记出现在正文中间或带前缀 ⇒ **不是**索引
        quoting = Message(
            role="user",
            blocks=[TextBlock(text="我看到的索引是这样的：\n▎ │ [历史归档索引]（完整原文见 …）")],
        )
        self.assertFalse(is_index_message(quoting), "正文引用不得被当成索引")
        inline = Message(
            role="assistant",
            blocks=[TextBlock(text="这段 `[历史归档索引]` 就是索引的样子")],
        )
        self.assertFalse(is_index_message(inline))

        # ② 真索引：文本**以标记开头** ⇒ 是索引
        real = Message(
            role="user",
            blocks=[TextBlock(text=f"{INDEX_MARKER}（折叠区到此结束）\n…")],
        )
        self.assertTrue(is_index_message(real))

    def test_index_no_longer_repeats_the_epoch_summaries(self) -> None:
        """★ CHANGE-052：索引里**不得**再出现 epoch 摘要行（双事实来源）。

        摘要已经按轮保留在各自的 assistant 消息上；两处都有就是本项目登记过的
        **F-03 类反模式**（同一事实两处存、约定"必须永远相等"、分叉时静默出错）。

        索引现在只剩：边界标记 + 元说明 + **会话目录绝对路径** + 工作集快照。
        """
        compactor = Compactor(window_capacity=10**9, keep_recent_turns=2)
        out = compactor.compact(build_history(6), force=True).messages
        index = next(
            m for m in out if is_index_message(m)
        )
        self.assertNotIn("· 第 ", index.text, "epoch 摘要行必须从索引里消失")
        self.assertIn("摘要", index.text, "仍须说明「以上是摘要不是原文」")

    def test_index_carries_an_absolute_log_path(self) -> None:
        """★ CHANGE-052：索引必须给出**可解析**的日志路径（修一个从未可用的承诺）。

        旧版只写 `transcript.jsonl` 这个**文件名**，而模型的 cwd 是仓库目录、
        会话文件在 `~/.logox/sessions/...`。实测**模型看到的全部内容里没有任何
        会话目录路径** ⇒ 即使给出行号它也无从 `fs_read`。
        """
        writer = SessionTranscriptWriter(log_file=self.tmp / "sess.jsonl")
        compactor = Compactor(window_capacity=10**9, keep_recent_turns=2, transcript_writer=writer)
        out = compactor.compact(build_history(6), force=True).messages
        index = next(
            m for m in out if is_index_message(m)
        )
        self.assertIn(str(writer.session_dir), index.text, "必须是**绝对**路径（可被 fs_read 解析）")


    def test_colliding_turn_numbers_are_reported_as_unknown_not_guessed(self) -> None:
        """**F-32 的历史遗留**：老文件里轮次号被复用过时，必须**拒给行号而不是猜**。

        证据来自真实会话 `tui-34248.jsonl`：同一个文件里 `turn=1` 出现了 3 次，
        于是 `turn_lines` 被合并成 `{1: (1, 238)}` —— “第 1 轮”覆盖了整个文件。
        那种区间比“行号未知”**危险得多**：模型会照它去 `fs_read`，读到错的内容
        而且不会发现自己错了。
        """
        writer = SessionTranscriptWriter(log_file=self.tmp / "legacy.jsonl")
        # 进程 A：turn 1、2
        for turn in (1, 2):
            writer.write_step(
                turn=turn, step=0, role="user", event_type="user_prompt", content="x"
            )
        # 进程 B 重新从 turn=1 开始（F-32 修复**之前**的行为）
        for turn in (1, 3):
            writer.write_step(
                turn=turn, step=0, role="user", event_type="user_prompt", content="x"
            )

        self.assertIn(1, writer.collided_turns)
        self.assertIsNone(writer.turn_lines_of(1, 1), "复用的轮次号不得给出行号")
        self.assertIsNone(writer.turn_lines_of(1, 2), "区间里含歧义号时应整体拒给")
        self.assertEqual(writer.turn_lines_of(2, 2), (2, 2), "没被复用的号仍要正常给出")


class IndexShapeTests(unittest.TestCase):
    def test_index_is_one_user_message(self) -> None:
        """**D4**：索引是**一条 `user` 消息** —— system 角色在 Anthropic 上会被拒。

        ★ CHANGE-052 调整断言的**判据**：旧版用 `source == "compaction"` 找索引，
        而新结构下**每个折叠轮的摘要也是 compaction 来源** ⇒ 那个判据不再唯一。
        改用 `INDEX_MARKER`（单一事实来源，与生产代码同口径）。
        """
        compactor = Compactor(window_capacity=10**9, keep_recent_turns=2)
        out = compactor.compact(build_history(6), force=True).messages

        indexes = [
            m for m in out if is_index_message(m)
        ]
        self.assertEqual(len(indexes), 1, "索引必须**恰好一条**（多出一条 = 旧索引没被替换）")
        self.assertEqual(indexes[0].role, "user", "索引必须是 user 角色")
        self.assertEqual([m.role for m in out].count("system"), 0, "不得产生 system 消息")

    def test_folded_turns_keep_user_verbatim_and_compress_only_the_answer(self) -> None:
        """★ CHANGE-052 核心结构：**每轮**都是 ``[user 逐字, assistant 摘要]``。

        第 1 轮与其余轮**形状完全相同** —— 用户裁定：
        「就没有第一轮锚点这个概念了，因为第一轮的处理逻辑和后面被压缩的轮次一模一样」。
        """
        history = [
            Message(role="user", blocks=[TextBlock(text="原始目标（要逐字保留）")]),
            Message(
                role="assistant",
                blocks=[TextBlock(text="很长很长的第一步回答" + "详" * 500)],
                meta=MessageMeta(turn_summary="给出整体方案：先改 A 再改 B"),
            ),
        ]
        history.extend(_turn(i, summary=f"第{i}轮摘要") for i in range(1, 7))
        flat = [m for turn in history for m in (turn if isinstance(turn, list) else [turn])]

        compactor = Compactor(window_capacity=10**9, keep_recent_turns=2)
        view = compactor.compact(flat, force=True).messages

        # 折叠区 = 成对的 [user 逐字, assistant 摘要]，直到索引为止
        index_at = next(
            i for i, m in enumerate(view) if is_index_message(m)
        )
        head = view[:index_at]
        self.assertEqual(len(head) % 2, 0, "折叠区必须是成对的 [user, assistant]")
        for position in range(0, len(head), 2):
            self.assertEqual(head[position].role, "user", "每对的第一条必须是 user")
            self.assertEqual(head[position + 1].role, "assistant", "每对的第二条必须是摘要")
            self.assertEqual(
                head[position + 1].meta.source, "compaction", "摘要必须是 compaction 来源"
            )
            self.assertNotIn("详" * 50, head[position + 1].text, "原始长回答不该原样留着")

        # 第 1 轮：提问逐字 + 回答压成摘要（与其余轮**同一套规则**）
        self.assertEqual(head[0].text, "原始目标（要逐字保留）")
        self.assertIn("给出整体方案", head[1].text)

        # 再折一次：提问仍逐字、摘要仍在（"只压一次"不能变成"每折一次就丢一点"）
        again = compactor.compact(view + _turn(9, summary="第9轮摘要"), force=True).messages
        self.assertEqual(again[0].text, "原始目标（要逐字保留）")
        self.assertIn("给出整体方案", again[1].text, "摘要不得在后续折叠中丢失")

    def test_index_is_never_counted_as_a_turn(self) -> None:
        """索引虽然是 `user` 角色，但**不得**被算成一个真实轮次（否则编号与行号全错）。

        ★ CHANGE-052 改写：旧版断言"索引里出现 `第 2~5 轮` / `第 6~6 轮`"——
        那依赖"摘要渲染进索引"。现在摘要按轮保留，账本在 `compactor.epochs` 里，
        所以**改用账本断言**，并直接验证 `split_turn_spans` 不把索引当轮起点。
        """
        compactor = Compactor(window_capacity=10**9, keep_recent_turns=2)
        first = compactor.compact(build_history(6), force=True).messages
        second = compactor.compact(first + _turn(7, summary="第7轮摘要"), force=True).messages

        # ① 账本：两次折叠必须覆盖**连续且不重叠**的区间
        ranges = [(e.from_turn, e.to_turn) for e in compactor.epochs]
        self.assertEqual(len(ranges), 2, f"应当有两段账本，实际 {ranges}")
        self.assertEqual(ranges[0][1] + 1, ranges[1][0], f"区间必须连续：{ranges}")

        # ② 索引不得被当成轮次起点（`split_turn_spans` 排除 `source=compaction` 的 user）
        spans = split_turn_spans(second)
        starts = [second[start] for start, _ in spans]
        for message in starts:
            self.assertFalse(
                is_index_message(message),
                "索引被算成了幽灵轮次 —— 会让轮次编号与行号全部错位",
            )


    def test_repeated_folding_never_accumulates_index_messages(self) -> None:
        """反复折叠后，视图里必须**只有一条**索引。

        ⚠️ 这是一条**真实的回归看护**：`_head_anchor` 在“锚点没有摘要”的路径上
        一度把 `messages[:first_turn_end]` 整段留下，而上一轮的**旧索引**就夹在
        这一段里 —— 于是视图里出现了**两个索引**，取第一个的调用方会读到
        **过期的账本**（行号与轮次区间都不对），而且不会报任何错。
        """
        compactor = Compactor(window_capacity=10**9, keep_recent_turns=2)
        view = compactor.compact(build_history(6), force=True).messages
        for turn in range(7, 13):
            view = compactor.compact(
                view + _turn(turn, summary=f"第{turn}轮摘要"), force=True
            ).messages
            indexes = [m for m in view if m.role == "user" and m.meta.source == "compaction"]
            self.assertEqual(
                len(indexes), 1, f"折叠到第 {turn} 轮时出现了 {len(indexes)} 条索引（旧索引必须被取代而非叠加）"
            )
        # 且账本本身必须完整（不能为了去重把历史丢了）
        self.assertGreaterEqual(len(compactor.epochs), 4)


class BlobIdempotenceTests(_TempWriter):
    def test_existing_blob_is_not_written_again(self) -> None:
        """**F-17**：压缩器只在 blob **缺失**时落盘，不随折叠回合数反复覆盖写。"""
        writer = SessionTranscriptWriter(log_file=self.tmp / "transcript.jsonl")
        compactor = Compactor(
            window_capacity=1000,
            max_budget_tokens=100,
            target_budget_tokens=50,
            transcript_writer=writer,
        )

        written: list[str] = []
        original = writer.save_tool_blob

        def counting(call_id: str, raw_output: str, **kwargs: object) -> str | None:
            written.append(call_id)
            return original(call_id, raw_output, **kwargs)  # type: ignore[arg-type]

        writer.save_tool_blob = counting  # type: ignore[method-assign]

        history = build_history(6, tools_from=1)  # 每个轮次都带工具输出
        compactor.compact(history, force=True)
        after_first = len(written)
        compactor.compact(history, force=True)
        compactor.compact(history, force=True)

        self.assertGreater(after_first, 0, "第一次折叠应当落盘过 blob")
        self.assertEqual(
            len(written),
            after_first,
            "后续折叠不得重复落盘同一批工具输出（写盘量不应随回合数增长）",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
