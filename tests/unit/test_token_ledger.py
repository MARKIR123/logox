"""锚点计量（anchor + delta）与 EMA 校准的用例（D158）。

对应设计文档 `MODULE_context_tokens.md` §7 的用例清单（实现其中 12 条）。
重点盯三类：
* **锚点语义**（尤其"锚点之后的新增段包含那条助手回复"这个 off-by-one 高发区）；
* **失效**（历史重写 ⇒ 代际 +1 ⇒ 退化为纯估算，且**绝不把估算值写回锚点**）；
* **校准目标对齐**（分母必须是"对实际发出那份列表的预测"）。
"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from logox.context.builder import HierarchicalContextBuilder
from logox.context.memory import ProjectMemory
from logox.context.storage import SessionTranscriptWriter
from logox.context.tokens import Anchor, TokenEstimator, TokenLedger
from logox.kernel import events as ev
from logox.kernel.messages import Message, MessageMeta, TextBlock
from tests.unit.support import make_temp_dir, remove_temp_dir

MODEL_A = "deepseek/deepseek-flash"
MODEL_B = "anthropic/claude-sonnet-4-5"
DIGEST = "digest-1"


def usage(tokens: int) -> ev.Usage:
    """构造一份"厂商上报"的用量：`context_tokens` 是**输入侧**总量。"""
    return ev.Usage(input_tokens=tokens, output_tokens=10, context_tokens=tokens)


def msg(text: str, role: str = "user") -> Message:
    return Message(role=role, blocks=[TextBlock(text=text)])


class LedgerAnchorTests(unittest.TestCase):
    """锚点本身。"""

    def setUp(self) -> None:
        self.ledger = TokenLedger()
        self.estimator = TokenEstimator()

    def _adopt(self, *, tokens: int, sent_count: int, model_key: str = MODEL_A) -> None:
        """走一遍真实流程：记 build → 用量回来 → 重建锚点。"""
        self.ledger.note_build(sent_count=sent_count, predicted_tokens=tokens)
        self.ledger.reconcile(
            usage=usage(tokens), estimator=self.estimator, model_key=model_key, prefix_digest=DIGEST
        )

    def test_t01_anchor_plus_delta(self) -> None:
        self._adopt(tokens=12_000, sent_count=10)
        messages = [msg("x" * 400) for _ in range(13)]  # 锚点之后多了 3 条
        prediction = self.ledger.predict(
            messages, system_prompt="sys", estimator=self.estimator,
            model_key=MODEL_A, prefix_digest=DIGEST,
        )
        trailing = self.estimator.estimate_messages(messages[10:], model_key=MODEL_A)
        self.assertTrue(prediction.used_anchor)
        self.assertEqual(prediction.reason, "anchor+delta")
        self.assertEqual(prediction.tokens, 12_000 + trailing)

    def test_t02_trailing_includes_the_assistant_reply_exactly_once(self) -> None:
        """★ off-by-one 高发区：`context_tokens` 是**输入侧**，所以锚点**不含**那条助手回复。

        发出去 2 条（user, assistant）→ 厂商报的 12,000 只覆盖这 2 条；
        下一次请求的上下文 = 锚点 + 那条**由上次请求产出的助手回复**（第 3 条）。
        断言：回复被计入**恰好一次**（既不能漏，也不能算两遍）。
        """
        self._adopt(tokens=12_000, sent_count=2)
        reply = msg("助手这次的回复" * 20, role="assistant")
        messages = [msg("问题"), msg("备用的第二条"), reply]
        prediction = self.ledger.predict(
            messages, system_prompt="sys", estimator=self.estimator,
            model_key=MODEL_A, prefix_digest=DIGEST,
        )
        only_reply = self.estimator.estimate_messages([reply], model_key=MODEL_A)
        self.assertEqual(prediction.tokens, 12_000 + only_reply)

    def test_t03_without_usage_it_falls_back_to_a_pure_estimate(self) -> None:
        messages = [msg("a" * 400)]
        prediction = self.ledger.predict(
            messages, system_prompt="sys", estimator=self.estimator,
            model_key=MODEL_A, prefix_digest=DIGEST,
        )
        self.assertFalse(prediction.used_anchor)
        self.assertEqual(prediction.reason, "no_usage_yet")
        self.assertEqual(
            prediction.tokens,
            self.estimator.estimate_messages(messages, system_prompt="sys", model_key=MODEL_A),
        )

    def test_t04_missing_context_tokens_is_not_guessed(self) -> None:
        """只有 `input_tokens`（老适配器）⇒ 不猜、不建锚点（口径不混用）。"""
        self.ledger.note_build(sent_count=3, predicted_tokens=1000)
        reason = self.ledger.reconcile(
            usage=ev.Usage(input_tokens=999, output_tokens=1),  # context_tokens 缺失
            estimator=self.estimator, model_key=MODEL_A, prefix_digest=DIGEST,
        )
        self.assertEqual(reason, "context_tokens_missing")
        self.assertIsNone(self.ledger.anchor)

    def test_t05_rewrite_invalidates_and_next_request_restores(self) -> None:
        self._adopt(tokens=12_000, sent_count=10)
        messages = [msg("x" * 400) for _ in range(12)]

        self.ledger.note_rewrite()
        after_rewrite = self.ledger.predict(
            messages, system_prompt="sys", estimator=self.estimator,
            model_key=MODEL_A, prefix_digest=DIGEST,
        )
        self.assertFalse(after_rewrite.used_anchor, "重写之后必须退化纯估算")
        self.assertEqual(self.ledger.generation, 1)
        self.assertIsNone(self.ledger.anchor, "作废就是清空 —— 绝不能把估算值写回锚点")

        # 下一次请求回来 ⇒ 锚点恢复（覆盖新的 sent_count）
        self.ledger.note_build(sent_count=12, predicted_tokens=5000)
        self.ledger.reconcile(
            usage=usage(5000), estimator=self.estimator, model_key=MODEL_A, prefix_digest=DIGEST
        )
        restored = self.ledger.predict(
            messages, system_prompt="sys", estimator=self.estimator,
            model_key=MODEL_A, prefix_digest=DIGEST,
        )
        self.assertTrue(restored.used_anchor)
        self.assertEqual(restored.tokens, 5000)

    def test_t06_the_same_usage_is_never_accounted_twice(self) -> None:
        first = usage(12_000)
        self.ledger.note_build(sent_count=10, predicted_tokens=11_000)
        self.ledger.reconcile(
            usage=first, estimator=self.estimator, model_key=MODEL_A, prefix_digest=DIGEST
        )
        self.assertEqual(self.ledger.adopted_anchors, 1)
        self.assertEqual(self.ledger.calibrated_samples, 1)

        again = self.ledger.reconcile(
            usage=first, estimator=self.estimator, model_key=MODEL_A, prefix_digest=DIGEST
        )
        self.assertEqual(again, "usage_already_consumed")
        self.assertEqual(self.ledger.adopted_anchors, 1, "同一份用量被重复记账了")
        self.assertEqual(self.ledger.calibrated_samples, 1, "同一份用量被重复校准了")

    def test_t07_model_switch_and_prefix_change_drop_the_anchor(self) -> None:
        messages = [msg("x" * 400) for _ in range(12)]
        self._adopt(tokens=12_000, sent_count=10)

        switched = self.ledger.predict(
            messages, system_prompt="sys", estimator=self.estimator,
            model_key=MODEL_B, prefix_digest=DIGEST,
        )
        self.assertEqual(switched.reason, "model_switched")
        self.assertFalse(switched.used_anchor)

        changed = self.ledger.predict(
            messages, system_prompt="sys", estimator=self.estimator,
            model_key=MODEL_A, prefix_digest="digest-2",
        )
        self.assertEqual(changed.reason, "prefix_changed")
        self.assertFalse(changed.used_anchor)

    def test_t08_shrunk_message_list_drops_the_anchor(self) -> None:
        self._adopt(tokens=12_000, sent_count=10)
        prediction = self.ledger.predict(
            [msg("only-one")], system_prompt="sys", estimator=self.estimator,
            model_key=MODEL_A, prefix_digest=DIGEST,
        )
        self.assertEqual(prediction.reason, "sent_count_shrank")
        self.assertFalse(prediction.used_anchor)

    def test_t09_reason_is_always_set(self) -> None:
        """可观测性：每条分支都必须给出原因（否则下次排查又是"看不出为什么"）。"""
        reasons = set()
        reasons.add(
            self.ledger.predict(
                [msg("a")], system_prompt="s", estimator=self.estimator,
                model_key=MODEL_A, prefix_digest=DIGEST,
            ).reason
        )
        self._adopt(tokens=1000, sent_count=1)
        reasons.add(
            self.ledger.predict(
                [msg("a"), msg("b")], system_prompt="s", estimator=self.estimator,
                model_key=MODEL_A, prefix_digest=DIGEST,
            ).reason
        )
        for reason in reasons:
            self.assertTrue(reason and reason.strip())


class EstimatorBucketTests(unittest.TestCase):
    """κ 分桶与校准目标。"""

    def test_t10_buckets_do_not_cross_contaminate(self) -> None:
        estimator = TokenEstimator()
        estimator.calibrate(1000, 1300, model_key=MODEL_A)
        # 一次 EMA：κ = 0.7×1.0 + 0.3×1.3 = 1.09（**不是**立刻变成 1.3 —— 那正是 EMA 的作用）
        self.assertAlmostEqual(estimator.factor_for(MODEL_A), 1.09, places=3)
        self.assertEqual(estimator.factor_for(MODEL_B), 1.0, "另一个模型不该受影响")
        self.assertEqual(estimator.samples_for(MODEL_A), 1)
        self.assertEqual(estimator.samples_for(MODEL_B), 0)

    def test_t11_calibration_converges_when_the_denominator_is_the_sent_list(self) -> None:
        """★ 校准目标对齐：分母 = 对**实际发出列表**的预测 ⇒ κ 向真实比值收敛。"""
        estimator = TokenEstimator()
        ledger = TokenLedger()
        ledger.note_build(sent_count=6, predicted_tokens=1000)
        ledger.reconcile(
            usage=usage(1300), estimator=estimator, model_key=MODEL_A, prefix_digest=DIGEST
        )
        # 一次 EMA（α=0.3）：κ = 0.7*1.0 + 0.3*1.3 = 1.09
        self.assertAlmostEqual(estimator.factor_for(MODEL_A), 1.09, places=3)
        self.assertEqual(ledger.calibrated_samples, 1)

    def test_t12_the_anchor_makes_the_prediction_insensitive_to_upstream_overestimate(self) -> None:
        """锚点的意义：把"已经测过的那一段"从估算误差里摘出去。

        造一段"估算偏大"的历史：纯估算给出一个很大的数，而带锚点的预测只估锚点之后的一小段
        ⇒ 前者会**过早触发压缩**，后者不会。这就是"取代 max(估算, 真实)"的收益。
        """
        estimator = TokenEstimator()
        ledger = TokenLedger()
        history = [msg("字" * 2000) for _ in range(20)]  # 纯估算会很大

        pure = ledger.predict(
            history, system_prompt="s", estimator=estimator, model_key=MODEL_A, prefix_digest=DIGEST
        )
        ledger.note_build(sent_count=19, predicted_tokens=1500)
        ledger.reconcile(
            usage=usage(1500), estimator=estimator, model_key=MODEL_A, prefix_digest=DIGEST
        )
        anchored = ledger.predict(
            history, system_prompt="s", estimator=estimator, model_key=MODEL_A, prefix_digest=DIGEST
        )

        self.assertLess(
            anchored.tokens,
            pure.tokens,
            "锚点式预测必须小于（或等于）全量纯估算 —— 它把已测部分摘出去了",
        )
        self.assertLess(anchored.tokens, pure.tokens * 0.5)


class BuilderLedgerIntegrationTests(unittest.TestCase):
    """接到 builder 上之后的整体行为。"""

    def _builder(self, tmp: Path) -> HierarchicalContextBuilder:
        tmp.mkdir(parents=True, exist_ok=True)
        with mock.patch(
            "logox.context.builder.find_project_memory",
            return_value=ProjectMemory(sources=[], total_tokens=0),
        ):
            return HierarchicalContextBuilder(
                system="你是 Logox",
                cwd=tmp,
                transcript_writer=SessionTranscriptWriter(base_dir=tmp / "s", session_id="ledger"),
                window_capacity=20_000,
                reserve_tokens=4_000,
                model_key=MODEL_A,
            )

    def test_t13_builder_records_the_sent_list_and_rebuilds_the_anchor(self) -> None:
        tmp = make_temp_dir("ledger-builder-")
        try:
            builder = self._builder(tmp)
            history = [msg("字" * 400, role="user") for _ in range(5)]
            bundle = builder.build(history)

            # 第一次：没有用量 ⇒ 纯估算
            self.assertEqual(builder.ledger.last_reason, "no_usage_yet")
            self.assertEqual(builder.ledger.anchor, None)

            # 模拟"厂商上报了这次请求的用量"
            sent = len(bundle.messages)
            builder.build(history, last_usage=usage(9_999))
            self.assertIsNotNone(builder.ledger.anchor, "有用量之后必须建立锚点")
            assert builder.ledger.anchor is not None
            self.assertEqual(builder.ledger.anchor.sent_count, sent)
            self.assertEqual(builder.ledger.anchor.tokens, 9_999)
            self.assertEqual(builder.ledger.anchor.model_key, MODEL_A)
        finally:
            remove_temp_dir(tmp)

    def test_t14_compaction_invalidates_the_anchor(self) -> None:
        """压缩折叠会重写历史 ⇒ 锚点必须作废（否则它描述的是不存在的上下文）。"""
        tmp = make_temp_dir("ledger-rewrite-")
        try:
            builder = self._builder(tmp)
            history = [msg("字" * 400, role="user") for _ in range(3)]
            # 第一次 build 只记下 pending；锚点要靠**下一次**带用量的 build 才能建立
            builder.build(history)
            builder.build(history, last_usage=usage(5_000))
            self.assertIsNotNone(builder.ledger.anchor)

            before = builder.ledger.generation
            builder._invalidate_cache()  # noqa: SLF001 - 唯一的失效入口
            self.assertEqual(builder.ledger.generation, before + 1)
            self.assertIsNone(builder.ledger.anchor)
        finally:
            remove_temp_dir(tmp)


if __name__ == "__main__":
    unittest.main()
