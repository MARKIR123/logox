"""价格表与费用估算。

原则
----
**未知模型返回 ``None``，绝不猜测。** 猜出来的费用会污染状态栏的 ``$`` 显示，
而用户会据此做判断——宁可显示 ``—``（UI-SPEC §5.1 的 ``EMPTY``）也不要假数字。

价格单位统一为 **美元 / 百万 token**。表中数值为公开标价，可能随时变动，
因此**它们只是估算**：真实费用应由厂商返回的 ``usage`` 为准，本表用于流式过程中
"还没拿到 usage 时的即时估算"。
"""

from __future__ import annotations

from dataclasses import dataclass

from logox.kernel.events import Usage

__all__ = ["PriceTable", "estimate_cost_usd", "price_for"]

DEFAULT_TABLE_NAME = "builtin"


@dataclass(frozen=True)
class Price:
    input_per_mtok: float
    output_per_mtok: float
    cached_input_per_mtok: float | None = None
    """缓存命中的输入单价（通常远低于普通输入）。``None`` = 按普通输入价计。"""

    @property
    def effective_cached(self) -> float:
        return self.input_per_mtok if self.cached_input_per_mtok is None else self.cached_input_per_mtok


#: 内置价格表：``模型名前缀 → 价格``。用前缀匹配是为了让
#: ``claude-sonnet-4-5-20250929`` 这类带日期的版本号也能命中。
_BUILTIN: dict[str, Price] = {
    # DeepSeek（2026-09 按官方《Models & Pricing》核对）
    #
    # ⚠️ 官方是**峰谷双价**：低谷价 = 峰时价的一半（峰时 = UTC 周一至周五
    # 01:00–04:00 与 06:00–10:00）。Logox 的价格表一个模型只有一个价，
    # 因此这里取**峰时价**——宁可高估（显示 $0.0031 而实际花了 $0.0015），
    # 也不要低估（那会让用户以为很便宜）。**这是刻意的保守选择，不是抄错。**
    #
    # 旧名 `deepseek-chat` / `deepseek-reasoner` 已退役，条目删除。
    "deepseek-flash": Price(0.30, 1.20, 0.006),
    "deepseek-v4-pro": Price(1.32, 3.96, 0.044),
    # OpenAI
    "gpt-4o": Price(2.50, 10.00, 1.25),
    "gpt-4o-mini": Price(0.15, 0.60, 0.075),
    "o3": Price(2.00, 8.00, 0.50),
    "o4-mini": Price(1.10, 4.40, 0.275),
    # Anthropic
    "claude-opus-4": Price(15.00, 75.00, 1.50),
    "claude-sonnet-4": Price(3.00, 15.00, 0.30),
    "claude-haiku-4": Price(1.00, 5.00, 0.10),
    "claude-3-5-haiku": Price(0.80, 4.00, 0.08),
}


class PriceTable:
    """可按需覆盖的价格表（用户可在配置里补自己的模型）。"""

    def __init__(self, overrides: dict[str, Price] | None = None) -> None:
        self._prices = dict(_BUILTIN)
        if overrides:
            self._prices.update(overrides)

    def get(self, model: str) -> Price | None:
        """按**最长前缀**匹配，避免 ``o3`` 抢走 ``o3-mini`` 的价。"""
        if model in self._prices:
            return self._prices[model]
        candidates = [key for key in self._prices if model.startswith(key)]
        if not candidates:
            return None
        return self._prices[max(candidates, key=len)]

    def with_price(self, model: str, price: Price) -> PriceTable:
        table = PriceTable()
        table._prices = {**self._prices, model: price}
        return table


_DEFAULT_TABLE = PriceTable()


def price_for(model: str, table: PriceTable | None = None) -> Price | None:
    """查价；未知模型返回 ``None``。"""
    return (table or _DEFAULT_TABLE).get(model)


def estimate_cost_usd(usage: Usage, model: str, table: PriceTable | None = None) -> float | None:
    """估算费用（美元）。

    * 未命中缓存的输入 = ``input_tokens - cached_input_tokens``
    * ``cached_input_tokens is None`` → 全部按普通输入价计（保守，不低估）
    * 模型未知 → ``None``（**不猜**）
    """
    if usage.input_tokens < 0 or usage.output_tokens < 0:
        return None
    price = price_for(model, table)
    if price is None:
        return None

    cached = usage.cached_input_tokens or 0
    plain_input = max(0, usage.input_tokens - cached)
    cost = (
        plain_input / 1_000_000 * price.input_per_mtok
        + cached / 1_000_000 * price.effective_cached
        + usage.output_tokens / 1_000_000 * price.output_per_mtok
    )
    return round(cost, 6)
