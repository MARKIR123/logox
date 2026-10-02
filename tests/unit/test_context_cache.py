"""压缩缓存的不变量（CHANGE-002 / D1 + D3）。

要防的缺陷
==========

``_body`` 每一轮都调 ``builder.build(self.history)``，而 ``history`` 是**全量且只增**的。
在加缓存之前，这意味着**每一轮都把同一段历史重新折一遍**：

* 页表（``Compactor.epochs``）每轮追加一条**重叠**的 epoch；
* 实测（``window_capacity=2000``）：第 7 轮 1 条 / 746 tok；第 25 轮 **19 条 / 1501 tok**，
  其中 **907 tok（60%）是索引本身**——**压缩的产物反过来吃掉了压缩省下的预算**。

四条不变量
==========

* **T-1 缓存可丢弃**：增量 build 的结果 == 用全新 builder 从全量重算的结果。
  这是整个设计的**安全底线**——缓存只是加速，丢了只会变慢，**不会变错**。
* **T-2 幂等**：同一份 history 连续 ``build()`` 两次，结果相同且 ``epochs`` **不增长**。
* **T-3 缓存自校验**：``history`` 被整体替换（``/resume`` / ``/rewind`` / ``/new``）后，
  缓存必须**自己认出来并重建**，而不是拿旧的折叠前缀去配新的历史。
  刻意选「自校验」而不是「记得调 reset」：`/resume`、`/rewind`、`/new` 三条路
  （以及将来任何新路径）都要记得调 —— 而这个项目刚因为"忘了调第二次"踩过坑
  （`prepare_runtime` vs `build_runtime`）。
* **T-4 `history` 不被改写**：``build()` 前后 ``history`` 逐条不变。
  压缩结果是**视图**；源记录只增不改。
"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from logox.context.builder import HierarchicalContextBuilder
from logox.context.compaction import split_turn_spans
from logox.context.memory import ProjectMemory
from logox.context.storage import SessionTranscriptWriter
from logox.kernel.messages import Message, MessageMeta, TextBlock

__all__ = ["CompactionCacheTests"]

#: 小窗口，让"折叠"在少量轮次内就能触发（不必真造几十万 token）
_WINDOW = {
    "window_capacity": 2000,
    "max_budget_tokens": 1500,
    "target_budget_tokens": 1000,
}


def make_history(turns: int, *, chars: int = 200) -> list[Message]:
    """造一段**轮次清晰、长度均匀**的会话（1 条锚点 + N 轮）。"""
    messages = [Message(role="user", blocks=[TextBlock(text="初始目标" + "初" * chars)])]
    for index in range(1, turns + 1):
        messages.append(
            Message(role="user", blocks=[TextBlock(text=f"第{index}问" + "问" * chars)])
        )
        messages.append(
            Message(role="assistant", blocks=[TextBlock(text=f"第{index}答" + "答" * chars)])
        )
    return messages


INDEX_MARK = "[历史归档索引]"
"""归档索引消息的标记（F-53 守卫用）。"""


def make_history_with_summaries(turns: int, *, chars: int = 200) -> list[Message]:
    """★ **生产形态**的历史：每条助手消息都带 ``turn_summary``（D135 之后真实运行就是这样）。

    为什么必须单独一个 fixture：`make_history()` 造的助手消息**没有摘要**，
    于是 `Compactor._head_anchor()` 走 fallback 分支 ⇒ 折叠后的头部是 **2 条**；
    而生产形态下头部是 **3 条**（多出"锚点回答摘要"）。F-53（索引被 `[:2]` 切掉）
    **只在生产形态下复现** —— 这正是它躲过全部单测的原因（fixture 保真度问题）。
    """
    # ★ 锚点轮必须是**完整的一轮**：[user, assistant(带摘要)] ——
    #   这是 F-53 能否复现的关键：只有锚点轮带 `turn_summary` 时，
    #   `_head_anchor()` 才会返回 2 条（锚点 + 锚点回答摘要）⇒ 折叠后头部共 **3 条**。
    #   若锚点轮只有一条 user 消息（本文件另一个 fixture 就是那样），
    #   `_head_anchor()` 会走 fallback ⇒ 头部恰好 2 条 ⇒ 旧的 `[:2]` 会碰巧正确，
    #   于是 bug 藏了起来（实测：变异成 `[:2]` 也能全绿）。
    messages = [
        Message(role="user", blocks=[TextBlock(text="初始目标" + "初" * chars)]),
        Message(
            role="assistant",
            blocks=[TextBlock(text="初始答" + "答" * chars)],
            meta=MessageMeta(turn_summary="第0轮摘要"),
        ),
    ]
    for index in range(1, turns + 1):
        messages.append(
            Message(role="user", blocks=[TextBlock(text=f"第{index}问" + "问" * chars)])
        )
        messages.append(
            Message(
                role="assistant",
                blocks=[TextBlock(text=f"第{index}答" + "答" * chars)],
                meta=MessageMeta(turn_summary=f"第{index}轮摘要"),
            )
        )
    return messages


def make_builder(cwd: Path, writer=None) -> HierarchicalContextBuilder:
    """建一个 builder。

    ⚠️ 必须把 ``find_project_memory`` 打桩：它默认会从 ``cwd`` **向上逐级查找**，
    而测试临时目录就在仓库内（``.test-tmp/``），于是会读到仓库自己的 ``LOGOX.md``
    —— 那会让用例的结果**依赖机器状态**（F-21 就是这么来的）。这里固定成空记忆。
    """
    with mock.patch(
        "logox.context.builder.find_project_memory",
        return_value=ProjectMemory(sources=[], total_tokens=0),
    ):
        return HierarchicalContextBuilder(
            system="你是 Logox",
            cwd=cwd,
            # D153：writer 必填且必须显式落点（落在该用例自己的临时目录下）
            transcript_writer=writer
            or SessionTranscriptWriter(base_dir=cwd / "sessions", session_id="cache"),
            **_WINDOW,
        )


def describe(bundle: object) -> list[str]:
    """把 bundle 的消息压成可比对的形状（角色 + 文本）。"""
    messages = bundle.messages
    return [f"{m.role}:{m.text}" for m in messages]


def last_turn_texts(view: list[Message], turns: int) -> list[str]:
    """取视图里**最后 N 个真实轮次**的文本（模型直接依赖的那部分）。

    刻意跳过第 1 个 span（锚点那一轮）——它里面装着**归档索引**，
    而索引的粒度取决于"折过几次"：增量路径折 4 次、重算路径折 1 次。
    那是账本的差异，不是内容的差异，不应该拿来做等价断言。
    """
    spans = split_turn_spans(view)[1:]
    return [
        f"{m.role}:{m.text}" for start, end in spans[-turns:] for m in view[start:end]
    ]


class CompactionCacheTests(unittest.TestCase):
    def test_t2_build_is_idempotent_and_does_not_grow_the_page_table(self) -> None:
        """**T-2**：同一份 history 连续 build 两次，结果相同且 epochs 不增长。

        改之前：每次 ``build()`` 都重新折一遍 → 第二次 build 会追加第二条 epoch，
        页表跟着变长，两次结果**不同**。
        """
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            builder = make_builder(Path(tmp))
            history = make_history(turns=8)

            first = builder.build(history)
            epochs_after_first = len(builder.compactor.epochs)
            second = builder.build(history)

            self.assertEqual(
                describe(first),
                describe(second),
                "同一份 history 连续两次 build 必须得到完全相同的视图",
            )
            self.assertEqual(
                len(builder.compactor.epochs),
                epochs_after_first,
                "没有新轮次时不得追加 epoch（page table 不得增长）",
            )
            self.assertGreaterEqual(epochs_after_first, 1, "8 轮 + 小窗口应当已经触发折叠")

    def test_t1_dropping_the_cache_preserves_what_the_model_sees(self) -> None:
        """**T-1**：缓存只是加速——丢掉它重算，模型依赖的关键内容必须不变。

        ⚠️ 这里断言的是**语义等价**，不是字节相等。

        页表是"每次折叠追加一条"的**账本**：增量路径折叠了 4 次就有 4 条 epoch，
        而重算路径一次折完只有 1 条。**两份账本描述的是同一段归档内容**，
        拆成几条只是粒度不同——所以"字节相等"是个**错的断言**，
        它会把一个正确实现判成失败（本用例第一版就是这么写错的）。

        真正必须相等的是：
        1. **保留的最新轮次**（模型直接依赖的内容）逐字一致；
        2. **锚点**（第一轮提问）仍然在最前；
        3. 两条路径都**确实发生了折叠**——否则用例会退化成"什么都没折"也能过。
        """
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            incremental = make_builder(cwd)
            history = make_history(turns=4)
            for extra in range(5, 9):
                incremental.build(history)
                history = history + [
                    Message(role="user", blocks=[TextBlock(text=f"第{extra}问" + "问" * 200)]),
                    Message(role="assistant", blocks=[TextBlock(text=f"第{extra}答" + "答" * 200)]),
                ]
            incremental_view = incremental.build(history).messages

            fresh = make_builder(cwd)
            fresh_view = fresh.build(history).messages

            # ③ 两条路径都真的折过
            self.assertGreaterEqual(len(incremental.compactor.epochs), 1)
            self.assertGreaterEqual(len(fresh.compactor.epochs), 1)

            # ② 锚点仍在最前
            self.assertEqual(incremental_view[0].text, fresh_view[0].text)
            self.assertNotEqual(incremental_view[0].text, history[0].text)
            self.assertIn("初始目标", incremental_view[0].text)
            # In extreme-window mode the old original is represented by its summary.

            # ① 保留的最新轮次逐字一致
            self.assertEqual(
                last_turn_texts(incremental_view, 4),
                last_turn_texts(fresh_view, 4),
                "缓存丢失后重算，保留的最新轮次必须逐字不变",
            )

            # ④ ★ F-53：**归档索引也必须还在**（增量路径与重算路径都要有）。
            #   补这一条的原因：本用例原来只断言"最新轮次 + 锚点 + 确实折过"，
            #   恰好漏掉索引 —— 而两条路径的差异正好在这一点上：
            #   重算路径当场渲染索引，增量路径的前缀只留了 `[:2]`（索引被切掉）。
            #   没有它，被折叠的中间历史对模型完全不可见（也没有按行号 fs_read 的指引）。
            self.assertTrue(
                any(INDEX_MARK in (m.text or "") for m in incremental_view),
                "增量（缓存复用）路径必须仍带归档索引 —— 否则被折叠的历史对模型彻底失忆（F-53）",
            )
            self.assertTrue(
                any(INDEX_MARK in (m.text or "") for m in fresh_view),
                "重算路径必须带归档索引",
            )

    def test_t1b_archive_index_survives_the_second_build(self) -> None:
        """★ **F-53 回归守卫**：折叠之后**每一次** build 都必须带归档索引。

        为什么值得单独一条（而不是只靠 t1）：F-53 的表现是"折叠那一次有索引、
        下一次就没了"——两段行为完全不同，而 t1 只采样增量路径的最终视图。
        这里把**连续两次** build 都断言一遍，并额外要求两次的索引**逐字一致**
        （前缀稳定 ⇒ 不会因为头部抖动让 KV cache 在两次请求之间失效）。
        """
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            builder = make_builder(Path(tmp))
            # ★ 必须是**生产形态**（带 turn_summary）——F-53 只在这种形态下复现
            history = make_history_with_summaries(turns=30)

            first = builder.build(history).messages
            second = builder.build(history).messages

            first_index = [m.text for m in first if INDEX_MARK in (m.text or "")]
            second_index = [m.text for m in second if INDEX_MARK in (m.text or "")]

            self.assertEqual(len(first_index), 1, "折叠那次应当恰好带一条归档索引")
            self.assertEqual(len(second_index), 1, "第二次 build 也必须带归档索引（F-53）")
            self.assertEqual(
                first_index, second_index,
                "两次请求的归档索引必须逐字一致（否则头部前缀缓存会失效）",
            )
            self.assertEqual(len(first), len(second), "两次请求的消息条数应当一致（F-53 前是 9 vs 8）")

    def test_t1c_folded_history_is_still_reachable_by_line_number(self) -> None:
        """★ F-53 的另一半：索引里必须给出**可按行号 fs_read** 的指引（而不是"行号未知"）。

        为什么：索引的意义就在于"原文还在 transcript 里，按行号能读回来"。
        若索引丢了行号（`行号未知`），模型即便看到索引也无法取回内容 ——
        这属于"看起来修好了、其实还是失忆"。此处用真的写进 transcript 的历史来验。
        """
        import tempfile

        from logox.context.storage import SessionTranscriptWriter

        with tempfile.TemporaryDirectory() as tmp:
            log_file = Path(tmp) / "transcript.jsonl"
            writer = SessionTranscriptWriter(log_file=log_file)
            history = make_history_with_summaries(turns=30)
            for turn in range(1, 31):
                writer.write_step(
                    turn=turn, step=1, role="user", event_type="user_prompt", content=f"第{turn}问"
                )

            builder = make_builder(Path(tmp), writer=writer)
            view = builder.build(history).messages
            index = next(m.text for m in view if INDEX_MARK in (m.text or ""))
            self.assertNotIn("行号未知", index, "归档索引必须带真实行号（否则原文取不回来）")
            self.assertIn("行 ", index)

    def test_t3_cache_is_self_validating_when_history_is_replaced(self) -> None:
        """**T-3**：history 被整体替换后（模拟 `/resume` / `/rewind`），缓存必须重建。"""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            builder = make_builder(cwd)
            builder.build(make_history(turns=8))

            # 模拟 app.py::Runtime 的整体替换：clear() + extend(全新对象)
            replacement = make_history(turns=8)

            expected = describe(make_builder(cwd).build(replacement))
            actual = describe(builder.build(replacement))
            self.assertEqual(
                actual, expected, "history 被替换后，缓存必须失效并重建，而不是复用旧前缀"
            )

    def test_t4_build_never_modifies_the_history(self) -> None:
        """**T-4**：`history` 是源记录，`build()` 只读不改。"""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            builder = make_builder(Path(tmp))
            history = make_history(turns=8)
            before = [f"{m.role}:{m.text}" for m in history]

            builder.build(history)
            builder.build(history)

            self.assertEqual(
                [f"{m.role}:{m.text}" for m in history],
                before,
                "压缩结果是视图；history 必须逐条不变（否则 /rewind 与持久化的前提就破了）",
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
