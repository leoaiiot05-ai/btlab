"""Commissions, slippage, taxes, borrow fees, short-sale fees and withholding (SPEC.md §10.6). Pure."""

from dataclasses import dataclass

from btlab.markets import PerShareCommission, RateCommission
from btlab.taxes import TaxTable

DAYS_PER_YEAR = 365


@dataclass(frozen=True)
class CostModel:
    """A run's costs. categories and borrow_fee_pct hold one entry per symbol; a borrow fee is US only, with the
    symbol's override already resolved (§7). taxes is the loaded tax table, None in US."""

    market: str
    commission_rule: PerShareCommission | RateCommission
    slippage_bps: float
    dividend_withholding_pct: float
    categories: tuple[str, ...]
    taxes: TaxTable | None = None
    borrow_fee_pct: tuple[float, ...] = ()
    short_fee_pct: float = 0.0

    def commission(self, quantity: float, value: float) -> float:
        """Per fill: US min(max(per_share × q, min), max_pct% × value); TW max(rate_pct% × value, min)."""
        if quantity == 0:
            return 0.0
        rule = self.commission_rule
        if isinstance(rule, PerShareCommission):
            return min(max(rule.per_share * quantity, rule.min), rule.max_pct / 100 * value)
        return max(rule.rate_pct / 100 * value, rule.min)

    def fill_price(self, price: float, buy: bool) -> float:
        """Buys pay price × (1 + slippage); sells receive price × (1 − slippage)."""
        slippage = self.slippage_bps / 10_000
        return price * (1 + slippage) if buy else price * (1 - slippage)

    def tax_rate(self, symbol: int, day: int, day_trade: bool = False) -> float:
        """The transaction tax on a sell, in percent; the day-trade rate applies to STOCK while it is in force."""
        if self.taxes is None:
            return 0.0
        return self.taxes.rate_pct(self.market, self.categories[symbol], day, day_trade)

    def borrow_fee(self, symbol: int, short_value: float, days: int) -> float:
        """Short value at the previous close × borrow_fee_pct / 100 × calendar days / 365 (US)."""
        if not self.borrow_fee_pct:
            return 0.0
        return short_value * self.borrow_fee_pct[symbol] / 100 * days / DAYS_PER_YEAR

    def short_sale_fee(self, value: float) -> float:
        """TW's short-sale fee, a percentage of the short-sale value."""
        return value * self.short_fee_pct / 100

    def net_dividend(self, amount: float) -> float:
        """A long's dividend after withholding."""
        return amount * (1 - self.dividend_withholding_pct / 100)
