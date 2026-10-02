"""价格表与费用估算的契约（UI-SPEC §5.1 的 ``$`` 项就靠它）。

核心原则：**未知模型返回 ``None``，绝不猜。**
状态栏宁可显示 ``—``（``EMPTY``）也不要一个假数字——用户会据此做判断。
"""

from __future__ import annotations

import unittest

from logox.kernel.events import Usage
from logox.providers.pricing import Price, PriceTable, estimate_cost_usd, price_for


class PriceLookupTests(unittest.TestCase):
    def test_t01_exact_match(self) -> None:
        price = price_for("deepseek-flash")
        self.assertIsNotNone(price)
        assert price is not None
        # 断言"查到了并在表里"，而不是钉死某个数字——**厂商调价不该让一堆测试变红**
        # （这个文件测的是查表与算术，不是价格本身对不对）
        self.assertEqual(price, PriceTable().get("deepseek-flash"))
        self.assertGreater(price.input_per_mtok, 0)

    def test_t02_dated_variant_hits_by_prefix(self) -> None:
        """``claude-sonnet-4-5-20250929`` 这类带日期的版本号必须也能命中。"""
        price = price_for("claude-sonnet-4-5-20250929")
        self.assertIsNotNone(price)
        assert price is not None
        self.assertEqual(price.input_per_mtok, 3.00)

    def test_t03_longest_prefix_wins(self) -> None:
        """``o3`` 不得抢走 ``o4-mini``；这里用 ``gpt-4o`` vs ``gpt-4o-mini`` 验证。"""
        mini = price_for("gpt-4o-mini-2024-07-18")
        assert mini is not None
        self.assertEqual(mini.input_per_mtok, 0.15)

    def test_t04_unknown_model_returns_none(self) -> None:
        self.assertIsNone(price_for("some-local-llama"))
        self.assertIsNone(price_for(""))

    def test_t05_override_replaces_builtin(self) -> None:
        table = PriceTable({"my-model": Price(1.0, 2.0)})
        price = price_for("my-model", table)
        assert price is not None
        self.assertEqual(price.input_per_mtok, 1.0)

    def test_t06_with_price_returns_new_table_and_leaves_original_alone(self) -> None:
        base = PriceTable()
        extended = base.with_price("brand-new", Price(9.0, 9.0))
        self.assertIsNone(price_for("brand-new", base))
        self.assertIsNotNone(price_for("brand-new", extended))


class EstimateCostTests(unittest.TestCase):
    """费用算术。**断言与表里的单价对齐**，而不是钉死数字。

    理由：这个类要证明的是"100 万 token 乘以单价 = 费用"、"缓存命中走缓存价"、
    "输出单独计价"这些**算法性质**。把 0.27 / 0.07 / 1.10 写死之后，
    厂商一调价就要连带改一串测试，而真正想守的那条性质反而没人看。
    """

    MODEL = "deepseek-flash"

    def test_t07_plain_input_and_output(self) -> None:
        price = price_for(self.MODEL)
        assert price is not None
        usage = Usage(input_tokens=1_000_000, output_tokens=0)
        cost = estimate_cost_usd(usage, self.MODEL)
        self.assertAlmostEqual(cost or 0.0, price.input_per_mtok, places=6)

    def test_t08_cached_tokens_use_the_cached_rate(self) -> None:
        price = price_for(self.MODEL)
        assert price is not None
        usage = Usage(input_tokens=1_000_000, output_tokens=0, cached_input_tokens=1_000_000)
        cost = estimate_cost_usd(usage, self.MODEL)
        self.assertAlmostEqual(cost or 0.0, price.effective_cached, places=6)
        self.assertLessEqual(price.effective_cached, price.input_per_mtok, "缓存价应当不高于普通输入价")

    def test_t09_unreported_cache_is_charged_at_full_rate(self) -> None:
        """``cached_input_tokens is None`` → 全部按普通输入价，**保守不低估**。"""
        usage = Usage(input_tokens=1_000_000, output_tokens=0)
        with_declared_zero = Usage(input_tokens=1_000_000, output_tokens=0, cached_input_tokens=0)
        self.assertEqual(estimate_cost_usd(usage, self.MODEL), estimate_cost_usd(with_declared_zero, self.MODEL))

    def test_t10_output_tokens_priced_separately(self) -> None:
        price = price_for(self.MODEL)
        assert price is not None
        usage = Usage(input_tokens=0, output_tokens=1_000_000)
        cost = estimate_cost_usd(usage, self.MODEL)
        self.assertAlmostEqual(cost or 0.0, price.output_per_mtok, places=6)

    def test_t11_unknown_model_yields_none(self) -> None:
        usage = Usage(input_tokens=1000, output_tokens=1000)
        self.assertIsNone(estimate_cost_usd(usage, "unknown-model"))

    def test_t12_cache_larger_than_input_never_goes_negative(self) -> None:
        """厂商偶尔回报不一致的用量；宁可算成 0 也不能出现负费用。"""
        usage = Usage(input_tokens=100, output_tokens=0, cached_input_tokens=500)
        cost = estimate_cost_usd(usage, self.MODEL)
        self.assertIsNotNone(cost)
        assert cost is not None
        self.assertGreaterEqual(cost, 0.0)

    def test_t13_result_is_rounded(self) -> None:
        usage = Usage(input_tokens=1, output_tokens=1)
        cost = estimate_cost_usd(usage, self.MODEL)
        assert cost is not None
        self.assertEqual(cost, round(cost, 6))


class MissingCacheRateTests(unittest.TestCase):
    def test_t14_falls_back_to_plain_input_rate(self) -> None:
        price = Price(2.0, 4.0)
        self.assertEqual(price.effective_cached, 2.0)

    def test_t15_explicit_cache_rate_is_used(self) -> None:
        self.assertEqual(Price(2.0, 4.0, 0.5).effective_cached, 0.5)
