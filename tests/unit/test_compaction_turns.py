"""按轮次对齐的压缩切分（CHANGE-002 / D2）。

要防的缺陷
==========

原来的滑动窗口按**消息条数**取尾部：

    tail_window = messages[-keep_recent_turns:]

而真实会话里每个轮次的长度是**不均匀**的（带工具的轮次 4 条、纯对话 2 条）。
于是这个 ``-N`` 边界会切在某一轮的**中间**，把 ``tool_use`` 与它的 ``tool_result``
拆开——**少一条配对，下一轮请求就会被厂商 400**。
这和 E-3 是同一个失败模式，只是来源不同：E-3 来自"中断"，这里来自"切偏"。

**实测（改之前）**：``tptptp``（带工具/纯对话交替）形态在 ``keep_recent_turns=4``
下就会产出孤儿 ``tool_result``。

本文件盯住的不变量
==================

* **I-A**：折叠后的消息序列里**不存在**孤儿 ``tool_use`` / 孤儿 ``tool_result``。
* **I-B**：保留的尾部窗口必然从**某个轮次的起点**（一条 ``user`` 消息）开始。
* **I-C**：任何轮次构成下，I-A 都成立（不能只在"每轮刚好 4 条"这种巧合下成立）。
"""

from __future__ import annotations

import unittest

from logox.context.compaction import Compactor, INDEX_MARKER, is_index_message, split_turn_spans
from logox.kernel.messages import Message, TextBlock, ToolResultBlock, ToolUseBlock

__all__ = ["CompactionTurnAlignmentTests"]


def build_conversation(shape: str) -> list[Message]:
    """按形状构造一段会话。

    ``'t'`` = 带工具的轮次（user / assistant(tool_use) / tool / assistant，共 4 条）
    ``'p'`` = 纯对话的轮次（user / assistant，共 2 条）
    前面额外加一条 ``user`` 作为**锚点**（第一轮提问）。
    """
    messages = [Message(role="user", blocks=[TextBlock(text="初始目标")])]
    for index, kind in enumerate(shape, start=1):
        messages.append(Message(role="user", blocks=[TextBlock(text=f"第{index}问")]))
        if kind == "t":
            messages.append(
                Message(
                    role="assistant",
                    blocks=[ToolUseBlock(id=f"c{index}", name="read", input={"path": "a"})],
                )
            )
            messages.append(
                Message(role="tool", blocks=[ToolResultBlock(id=f"c{index}", ok=True, content="x")])
            )
        messages.append(Message(role="assistant", blocks=[TextBlock(text=f"第{index}答")]))
    return messages


def find_orphans(messages: list[Message]) -> tuple[list[str], list[str]]:
    """返回 (孤儿 tool_use 的 id, 孤儿 tool_result 的 id)。"""
    uses: list[str] = []
    results: list[str] = []
    for message in messages:
        for block in message.blocks:
            if isinstance(block, ToolUseBlock):
                uses.append(block.id)
            elif isinstance(block, ToolResultBlock):
                results.append(block.id)
    return sorted(set(uses) - set(results)), sorted(set(results) - set(uses))


#: 五种轮次构成——**故意做成不均匀**，因为"每轮刚好 4 条"是最容易骗过测试的巧合
SHAPES: dict[str, str] = {
    "全纯对话": "pppppp",
    "全带工具": "tttttt",
    "严格交替": "tptptp",
    "三工具+三纯": "tttppp",
    "前纯后工具": "pppttt",
}


class CompactionTurnAlignmentTests(unittest.TestCase):
    def test_split_turn_spans_marks_each_user_message_as_a_turn_start(self) -> None:
        messages = build_conversation("tp")
        spans = split_turn_spans(messages)
        # 1 条锚点 + 2 轮
        self.assertEqual(len(spans), 3)
        for start, _end in spans:
            self.assertEqual(messages[start].role, "user", f"轮次起点必须是 user：{start}")

    def test_t_turn_aligned_tail_starts_at_a_user_message(self) -> None:
        """I-B：保留的尾部窗口必须从某轮起点开始。

        ★ CHANGE-052 改写口径：旧断言是 `out[1].role in {user, system}`
        —— 它**依赖折叠头的固定形状**（`[锚点 user, 索引]` = 2 条）。
        新结构下头部是 `[user, 摘要] × N + [索引]`，长度可变，
        所以改成**按语义标记定位**：找到归档索引，断言**紧随其后的第一条是 user**。

        这与 `HierarchicalContextBuilder._folded_head()` 是同一套口径
        （按 `INDEX_MARKER` 认边界），因此**头部形状再变也不会失效**。
        """
        for name, shape in SHAPES.items():
            for keep in (1, 2, 3, 4):
                with self.subTest(shape=name, keep=keep):
                    compactor = Compactor(window_capacity=10**9, keep_recent_turns=keep)
                    out = compactor.compact(build_conversation(shape), force=True).messages
                    index_at = next(
                        (
                            i
                            for i, m in enumerate(out)
                            if is_index_message(m)
                        ),
                        None,
                    )
                    assert index_at is not None, "强制折叠后必须有归档索引（纯对话也要折）"
                    if index_at + 1 < len(out):
                        self.assertEqual(
                            out[index_at + 1].role,
                            "user",
                            "索引之后必须紧接着某轮起点（否则尾部窗口被切碎了）",
                        )

    def test_t_no_orphan_tool_calls_after_folding(self) -> None:
        """I-A + I-C：**任何**轮次构成、**任何**保留轮数下，都不得出现孤儿配对。"""
        for name, shape in SHAPES.items():
            for keep in (1, 2, 3, 4):
                with self.subTest(shape=name, keep=keep):
                    compactor = Compactor(window_capacity=10**9, keep_recent_turns=keep)
                    out = compactor.compact(build_conversation(shape), force=True).messages
                    orphan_uses, orphan_results = find_orphans(out)
                    self.assertEqual(
                        (orphan_uses, orphan_results),
                        ([], []),
                        f"{name} keep={keep} 产生了孤儿配对：uses={orphan_uses} results={orphan_results}",
                    )

    def test_only_whole_turns_are_folded_away(self) -> None:
        """折叠掉的部分必须由**完整的轮次**组成（不能切在轮次中间）。

        判定方式：把折叠前后的消息序列按轮次切分，被保留的消息必须恰好等于
        原来的**某几个完整轮次**（外加锚点与页表占位符）。
        """
        messages = build_conversation("tptptp")
        compactor = Compactor(window_capacity=10**9, keep_recent_turns=2)
        out = compactor.compact(messages, force=True).messages

        kept_spans = split_turn_spans(out)
        # 去掉锚点与可能的页表占位符后，剩下的每一轮都必须是完整的：
        # 一条带 tool_use 的轮次，其 tool_result 必须也在这一轮里
        for start, end in kept_spans:
            turn_uses = {
                b.id
                for m in out[start:end]
                for b in m.blocks
                if isinstance(b, ToolUseBlock)
            }
            turn_results = {
                b.id
                for m in out[start:end]
                for b in m.blocks
                if isinstance(b, ToolResultBlock)
            }
            self.assertEqual(
                turn_uses - turn_results,
                set(),
                f"轮次 [{start},{end}) 内部 tool_use 没有配对：{turn_uses - turn_results}",
            )
            self.assertEqual(
                turn_results - turn_uses,
                set(),
                f"轮次 [{start},{end}) 内部 tool_result 没有配对：{turn_results - turn_uses}",
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
