"""压缩阈值与工具保留规则（CHANGE-004 → CHANGE-005）。

水位线：从「窗口 × 0.75，但封顶 80k」→ 「**窗口 − reserve**」
================================================================

两代公式都推翻了旧默认行为，所以都要有看护。

**CHANGE-004** 拆掉了 80k 封顶 —— 它把 **1M 窗口的触发点压到 8%**，
也就是"用掉 8% 的上下文就开始忘事"。那不是能力限制，
而是**成本偏好被写成了能力上限**；更糟的是它**静默生效**：
写在旁边的 `high_watermark_ratio = 0.75` 会让人以为大模型能用 75%。

**CHANGE-005** 又把“比例”换成了“预留”：

```
reserve        = min(32_768, 窗口 // 4)   # 小窗口保护
high_watermark = 窗口 − reserve
low_watermark  = int(窗口 × 0.50)          # 兜底
```

为什么“比例”不够：`窗口 × 0.75` 在 1M 下留了 26 万 token 的空白，
而真正的约束是"**回答得下 + 估算误差装得下**"，那是个**加法**不是乘法。
预设的 32k 对 200k/1M 窗口刚好（84% / 97%），且对小窗口自动缩水。

对照（都实测读到源码或官方文档）：

| 实现 | 阈值 | 1M 窗口下的实际触发点 |
|---|---|---|
| pi | `ctx > 窗口 − reserveTokens(16_384)` | **98.4%** |
| Claude Code | 官方文档："about 967K tokens by default" | **≈97%** |
| Logox（CHANGE-004 前） | `min(窗口 × 0.75, 80_000)` | **8%** ← 坏 |
| **Logox（CHANGE-005）** | `窗口 − 32_768` | **96.9%** |

注：Logox 的 reserve **不需要**预留 Claude Code 的 "compaction headroom" ——
因为我们的压缩是**确定性**的、不调模型，没有"摘要流程自己中途失败"这回事。
**抄机制，不要抄数字。**

工具保留：从“最近 K 轮全保”收窄为“最后一轮 ∪ 最近 M 条”
----------------------------------------------------------------

旧范围下，"一轮里 10 次工具调用"那 10 条**全部免剪**，而那一轮
既折不动（轮次太少）也剪不动（全被保护）—— 只剩工具自身的输出上限在挡。
并集仍然保留（它解决的是反向问题：最后一轮恰好没工具时，
全局最近 M 条能当安全网）。

幂等性（CHANGE-005 裁定 2）
----------------------------

判定"这块归档过没有"必须靠 `ToolResultBlock.archived` **元数据**，
不能靠"内容长度 > 150"这类**解构内容**的做法 —— 后者在归档格式变化时
会**静默失效**（归档后的文本自己就超 150），于是同一块被反复归档，
每次都改写历史中前部的字节 → **前缀缓存从那里断开**。
旧代码没炸只是因为它恰好越压越小（**偶然收敛**，不是幂等）。
"""

from __future__ import annotations

import unittest

from logox.context.compaction import Compactor, context_tokens_of, _excerpt
from logox.kernel.events import Usage
from logox.kernel.messages import Message, TextBlock, ToolResultBlock, ToolUseBlock

__all__ = [
    "WatermarkTests",
    "ToolRetentionTests",
    "ExcerptTests",
    "RealUsageTriggerTests",
    "PruningIdempotenceTests",
]


class WatermarkTests(unittest.TestCase):
    def test_high_watermark_reserves_space_instead_of_taking_a_percentage(self) -> None:
        """**CHANGE-005 裁定 1**：`high = 窗口 − reserve`，低水位 = 窗口的 50%。"""
        for window in (32_000, 128_000, 200_000, 1_048_576):
            with self.subTest(window=window):
                compactor = Compactor(window_capacity=window)
                reserve = min(32_768, window // 4)  # 小窗口保护
                self.assertEqual(compactor.reserve_tokens, reserve)
                self.assertEqual(compactor.high_watermark, window - reserve)
                self.assertEqual(compactor.low_watermark, int(window * 0.50))

    def test_one_million_window_triggers_around_97_percent(self) -> None:
        """★ 1M 窗口**不再在 8% 就压缩**。

        对照：Claude Code 在 1M 窗口下约 967K（≈97%）才压缩；pi 用 `窗口 − 16,384`（98.4%）。
        """
        compactor = Compactor(window_capacity=1_048_576)
        ratio = compactor.high_watermark / 1_048_576
        self.assertGreater(ratio, 0.96, "1M 窗口应在 96% 以上才触发")
        self.assertLess(ratio, 0.99, "但是仍要留出 reserve（不能贴到 100%）")

    def test_small_windows_keep_the_old_behaviour(self) -> None:
        """小窗口行为**不变**（75%/50%）—— 这个改动只放开大窗口。"""
        for window in (32_000, 128_000):
            with self.subTest(window=window):
                compactor = Compactor(window_capacity=window)
                self.assertEqual(compactor.high_watermark, int(window * 0.75))
                self.assertEqual(compactor.low_watermark, int(window * 0.50))

    def test_optional_cost_cap_still_works_when_explicitly_set(self) -> None:
        """成本闸门**没有删掉**，只是默认关闭 —— 关心花费的用户仍可显式打开。"""
        compactor = Compactor(
            window_capacity=1_048_576,
            max_budget_tokens=80_000,
            target_budget_tokens=40_000,
        )
        self.assertEqual(compactor.high_watermark, 80_000)
        self.assertEqual(compactor.low_watermark, 40_000)

    def test_cap_never_raises_the_watermark(self) -> None:
        """闸门只能**压低**水位线，不能抬高（否则就变成“强制多塞”）。"""
        compactor = Compactor(window_capacity=32_000, max_budget_tokens=10_000_000)
        self.assertEqual(compactor.high_watermark, 32_000 - 8_000)


class ToolRetentionTests(unittest.TestCase):
    @staticmethod
    def _one_turn_with(tool_calls: int, size: int = 4000) -> list[Message]:
        messages = [Message(role="user", blocks=[TextBlock(text="目标")])]
        for index in range(1, tool_calls + 1):
            messages.append(
                Message(
                    role="assistant",
                    blocks=[ToolUseBlock(id=f"c{index}", name="read", input={})],
                )
            )
            messages.append(
                Message(
                    role="tool",
                    blocks=[ToolResultBlock(id=f"c{index}", content="X" * size, ok=True)],
                )
            )
        messages.append(Message(role="assistant", blocks=[TextBlock(text="结论")]))
        return messages

    def test_a_single_turn_with_many_tool_calls_keeps_only_the_hot_tail(self) -> None:
        """★ 当前轮有 5 次工具调用时，**前面的会被卸载，只留最近 M 条**。

        ⚠️ 这条用例断言的行为**与 CHANGE-004 的原意相反**，是 CHANGE-005 用实测推翻的。
        原意是“模型刚要用的那几条不能动”，于是保护了**整轮** —— 但“模型正要用的”
        和“保存整轮”是两件事：实测中保存整轮会让**回合内复检完全失效**
        （视图 4308 → 5455 → 6602 → 7749 → 8896，一路涨到 3 倍窗口，厂商必回 400）。

        真正对位的规则是 Claude Code 的 **hot tail**：只保护**最近的一小窗**，
        其余进 cold storage —— 而且**只有超过高水位时才会卸载**。
        卸载后的位置保留落盘路径与确定性节选，模型一次 `fs_read` 就能取回。
        """
        compactor = Compactor(
            window_capacity=10**9, keep_recent_tool_results=1, keep_recent_turns=2
        )
        pruned_messages, pruned = compactor._prune_tool_results(  # noqa: SLF001 - 刻意的白盒断言
            self._one_turn_with(5)
        )
        self.assertEqual(pruned, 4, "5 次调用、护住 1 条 → 卸载 4 条")
        blocks = [
            block
            for message in pruned_messages
            for block in message.blocks
            if isinstance(block, ToolResultBlock)
        ]
        self.assertEqual(
            [block.archived for block in blocks],
            [True, True, True, True, False],
            "只有最后那一条是完整的",
        )

    def test_tool_results_outside_the_recent_turns_are_pruned(self) -> None:
        """保留窗口**之外**的旧工具结果要被换掉。"""
        messages = [Message(role="user", blocks=[TextBlock(text="初始目标")])]
        for turn in range(1, 5):
            messages.append(Message(role="user", blocks=[TextBlock(text=f"第{turn}轮")]))
            messages.append(
                Message(
                    role="assistant",
                    blocks=[ToolUseBlock(id=f"c{turn}", name="read", input={})],
                )
            )
            messages.append(
                Message(
                    role="tool",
                    blocks=[ToolResultBlock(id=f"c{turn}", content="X" * 4000, ok=True)],
                )
            )
            messages.append(Message(role="assistant", blocks=[TextBlock(text="结论")]))

        compactor = Compactor(
            window_capacity=10**9, keep_recent_tool_results=1, keep_recent_turns=2
        )
        pruned_messages, pruned = compactor._prune_tool_results(messages)  # noqa: SLF001
        by_id = {
            block.id: block
            for message in pruned_messages
            for block in message.blocks
            if isinstance(block, ToolResultBlock)
        }
        self.assertGreater(pruned, 0)
        self.assertIn("工具输出已归档", by_id["c1"].content)
        self.assertEqual(by_id["c4"].content, "X" * 4000, "最近一条必须完整保留")

    def test_pruning_keeps_the_disk_index(self) -> None:
        """修剪后必须留下**落盘路径 + 行数**，否则模型无从回查。"""
        compactor = Compactor(window_capacity=10**9, keep_recent_turns=1)
        messages = [Message(role="user", blocks=[TextBlock(text="初始目标")])]
        for turn in range(1, 4):
            messages.append(Message(role="user", blocks=[TextBlock(text=f"第{turn}轮")]))
            messages.append(
                Message(
                    role="assistant",
                    blocks=[ToolUseBlock(id=f"c{turn}", name="read", input={})],
                )
            )
            messages.append(
                Message(
                    role="tool",
                    blocks=[
                        ToolResultBlock(
                            id=f"c{turn}", content="行一\n行二\n" + "Y" * 4000, ok=True
                        )
                    ],
                )
            )
            messages.append(Message(role="assistant", blocks=[TextBlock(text="结论")]))

        pruned_messages, _pruned = compactor._prune_tool_results(messages)  # noqa: SLF001
        block = next(
            b
            for m in pruned_messages
            for b in m.blocks
            if isinstance(b, ToolResultBlock) and b.id == "c1"
        )
        self.assertIn("共 3 行", block.content)
        self.assertIn("行 1~3", block.content)


class RealUsageTriggerTests(unittest.TestCase):
    """**触发判据取「真实用量」与「估算」的较大者**（CHANGE-005 裁定 1）。

    两个方向都要看护：
    * 真实值 > 估算 → 不能因“估算器乐观”就漏掉压缩（**会超窗口 400**）；
    * 估算 > 真实值 → 不能因“真实值是上一次请求的旧数字”就不压（**会越积越多**）。
    """

    @staticmethod
    def _messages(turns: int = 3, size: int = 2000) -> list[Message]:
        messages = [Message(role="user", blocks=[TextBlock(text="初始目标")])]
        for index in range(1, turns + 1):
            messages.append(Message(role="user", blocks=[TextBlock(text=f"第{index}轮")]))
            messages.append(
                Message(
                    role="assistant",
                    blocks=[ToolUseBlock(id=f"c{index}", name="read", input={})],
                )
            )
            messages.append(
                Message(
                    role="tool",
                    blocks=[ToolResultBlock(id=f"c{index}", content="X" * size, ok=True)],
                )
            )
            messages.append(Message(role="assistant", blocks=[TextBlock(text="结论")]))
        return messages

    @staticmethod
    def _usage(context_tokens: int | None) -> Usage:
        return Usage(input_tokens=0, output_tokens=0, context_tokens=context_tokens)

    def test_real_usage_triggers_what_estimation_misses(self) -> None:
        """★ 估算器**低估一半**时，只有真实用量能救回来。

        这不是假想：估算器的校准系数被夹在 ``[0.5, 2.0]`` 里，最坏就是低估一半。
        前置条件有意造得真实 —— 3 轮对话、估算才 1.9k，而窗口水位线是 7.5k：
        **纯估算会说"还很空"，而厂商已经报了 7.5k+**。
        """
        messages = self._messages(turns=3, size=2000)
        compactor = Compactor(window_capacity=10_000, reserve_tokens=2_500)
        estimated = compactor.estimator.estimate_messages(messages)
        self.assertLess(estimated, compactor.high_watermark, "前提：纯估算不应该触发")

        result = compactor.compact(
            messages, last_usage=self._usage(compactor.high_watermark + 1)
        )

        self.assertGreater(
            result.pruned_count, 0, "真实用量已超水位线 —— 不能因为估算器乐观就漏掉"
        )
        self.assertEqual(result.tokens_before, compactor.high_watermark + 1)

    def test_estimation_still_triggers_when_real_usage_lags(self) -> None:
        """真实用量是**上一次请求**的数字，比当前上下文少一段 → 取大保证不漏。"""
        messages = self._messages(turns=6, size=6000)
        compactor = Compactor(window_capacity=10_000, reserve_tokens=2_500)
        estimated = compactor.estimator.estimate_messages(messages)
        self.assertGreater(estimated, compactor.high_watermark, "前提：估算已超水位线")

        # 真实用量很小（比如上一次请求发生在很久以前、那时历史还短）
        result = compactor.compact(messages, last_usage=self._usage(100))

        self.assertGreater(result.pruned_count, 0, "估算超线也要触发（取大）")
        self.assertEqual(result.tokens_before, estimated)

    def test_missing_usage_falls_back_to_estimation(self) -> None:
        """未上报 → 退回纯估算：**不报错，也不当成 0**（当成 0 就永远不压缩）。"""
        messages = self._messages(turns=6, size=6000)
        compactor = Compactor(window_capacity=10_000, reserve_tokens=2_500)
        with self.subTest(case="usage=None"):
            result = compactor.compact(messages, last_usage=None)
            self.assertGreater(result.pruned_count, 0)
        with self.subTest(case="context_tokens=None"):
            result = compactor.compact(messages, last_usage=self._usage(None))
            self.assertGreater(result.pruned_count, 0, "缺失字段也只能退回估算，不能当成 0")

    def test_context_tokens_of_rejects_absent_and_zero(self) -> None:
        """``context_tokens_of``：只接受**正数**，其余一律 ``None``（不猜）。"""
        self.assertIsNone(context_tokens_of(None))
        self.assertIsNone(context_tokens_of(self._usage(None)))
        self.assertIsNone(context_tokens_of(self._usage(0)))
        self.assertEqual(context_tokens_of(self._usage(1234)), 1234)


class PruningIdempotenceTests(unittest.TestCase):
    """**幂等**：同一批历史裁两次，第二次必须是**空操作**（CHANGE-005 裁定 2）。

    改之前：第二次会把"归档说明"再归档一遍 —— 内容变了、报的字节数也变了
    （**实测 8000 → 525 → 643 → 763，一路涨**）。
    后果：**前缀缓存从那个位置断开**，而它之后的全部内容要按全价重算
    （DeepSeek 口径 cache miss 是 cache read 的 **50 倍**）。

    为什么旧代码没炸：旧格式恰好**越压越小**（缩到 150 字符以下就停了）——
    **偶然收敛**，不是幂等。
    """

    @staticmethod
    def _history(turns: int = 4, size: int = 4000) -> list[Message]:
        messages = [Message(role="user", blocks=[TextBlock(text="初始目标")])]
        for index in range(1, turns + 1):
            messages.append(Message(role="user", blocks=[TextBlock(text=f"第{index}轮")]))
            messages.append(
                Message(
                    role="assistant",
                    blocks=[ToolUseBlock(id=f"c{index}", name="read", input={})],
                )
            )
            messages.append(
                Message(
                    role="tool",
                    blocks=[ToolResultBlock(id=f"c{index}", content="X" * size, ok=True)],
                )
            )
            messages.append(Message(role="assistant", blocks=[TextBlock(text="结论")]))
        return messages

    @staticmethod
    def _fingerprint(messages: list[Message]) -> list[str]:
        """把整段历史压成可比对的形状（角色 + 每个块的文本）。"""
        out: list[str] = []
        for message in messages:
            for block in message.blocks:
                body = getattr(block, "content", None) or getattr(block, "text", "")
                out.append(f"{message.role}:{block.type}:{body}")
        return out

    def test_second_prune_changes_nothing(self) -> None:
        """同一批历史裁两次：第二次必须一条都不动，且结果**逐字符相同**。

        “逐字符相同”就是“历史中前部字节稳定” —— 也就是前缀缓存不被它打断。
        """
        compactor = Compactor(window_capacity=10**9, keep_recent_turns=1)
        first, count_first = compactor._prune_tool_results(self._history())  # noqa: SLF001
        second, count_second = compactor._prune_tool_results(first)  # noqa: SLF001

        self.assertGreater(count_first, 0, "第一次应当真的裁掉了一些")
        self.assertEqual(count_second, 0, "第二次必须是空操作（已归档的块不再被处理）")
        self.assertEqual(
            self._fingerprint(first),
            self._fingerprint(second),
            "两次结果必须逐字符相同，否则前缀缓存会在那个位置每轮失效",
        )

    def test_archived_blocks_carry_the_marker(self) -> None:
        """归档后必须打上 `archived` 标记（**靠元数据判断，不靠解构内容**）。"""
        compactor = Compactor(window_capacity=10**9, keep_recent_turns=1)
        pruned, _count = compactor._prune_tool_results(self._history())  # noqa: SLF001
        blocks = {
            block.id: block
            for message in pruned
            for block in message.blocks
            if isinstance(block, ToolResultBlock)
        }
        self.assertTrue(blocks["c1"].archived, "归档过的块必须打上标记")
        self.assertFalse(blocks["c4"].archived, "没归档的必须保持 False")

    def test_only_the_most_recent_tool_results_are_protected(self) -> None:
        """保护范围 = **全局最近 M 条**，**不按轮次**（CHANGE-005 实测修正）。

        原方案“最后一轮全部 ∪ 最近 M 条”会让**回合内复检无事可做**：
        正在进行的那一轮的工具结果正是把窗口撑破的那批，却全部免剪。
        """
        compactor = Compactor(
            window_capacity=10**9, keep_recent_turns=2, keep_recent_tool_results=2
        )
        messages = self._history(turns=4)
        protected = compactor._protected_tool_indices(messages)  # noqa: SLF001
        tool_at = [
            i for i, m in enumerate(messages) if any(isinstance(b, ToolResultBlock) for b in m.blocks)
        ]
        self.assertEqual(
            protected,
            set(tool_at[-2:]),
            "只保护全局最近 2 条 —— 哪怕最后两轮里的工具结果也不得多保护一条",
        )
        self.assertNotIn(tool_at[0], protected, "最早的那条必须可被卸载（否则回合内复检没得做）")

    def test_more_than_the_hot_tail_can_be_offloaded_within_one_turn(self) -> None:
        """★ 一格子里 6 次工具调用 → 只能护住 2 条，剩下 4 条可卸载。"""
        compactor = Compactor(window_capacity=10**9, keep_recent_tool_results=2)
        messages = [Message(role="user", blocks=[TextBlock(text="目标")])]
        for index in range(1, 7):
            messages.append(
                Message(
                    role="tool",
                    blocks=[ToolResultBlock(id=f"c{index}", content="X" * 500, ok=True)],
                )
            )
        pruned, count = compactor._prune_tool_results(messages)  # noqa: SLF001
        self.assertEqual(count, 4, f"6 次调用、护住 2 条 → 应卸载 4 条，实际 {count}")
        archived = [b.archived for m in pruned for b in m.blocks if isinstance(b, ToolResultBlock)]
        self.assertEqual(archived, [True, True, True, True, False, False])


class ExcerptTests(unittest.TestCase):
    def test_excerpt_keeps_head_and_tail(self) -> None:
        """确定性节选：首部 + 尾部（**不是**语义摘要）。"""
        text = "HEAD" + "中" * 1000 + "TAIL"
        excerpt = _excerpt(text, head=20, tail=20)
        self.assertTrue(excerpt.startswith("HEAD"))
        self.assertTrue(excerpt.endswith("TAIL"))
        self.assertIn("中略", excerpt)

    def test_short_text_is_returned_verbatim(self) -> None:
        self.assertEqual(_excerpt("短", head=20, tail=20), "短")

    def test_excerpt_flattens_newlines(self) -> None:
        """节选要压成单行 —— 否则索引会被换行撑得很高。"""
        self.assertNotIn("\n", _excerpt("a\nb\nc"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
