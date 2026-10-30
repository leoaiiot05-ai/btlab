"""Costs and sizing (SPEC.md §9.4, §10.4, §10.6, AC-7)."""

from datetime import date
from pathlib import Path

import pytest

from btlab.costs import CostModel
from btlab.markets import TW, US, PerShareCommission
from btlab.records import to_day
from btlab.sizing import Sizing, cap, entry_quantity, to_lot
from btlab.taxes import load_taxes

TAXES = load_taxes(Path(__file__).resolve().parents[1] / "taxes.toml")


def us(**options) -> CostModel:
    return CostModel("US", US.commission, 5, 30, ("STOCK", "ETF"), borrow_fee_pct=(0.5, 2.0), **options)


def tw() -> CostModel:
    return CostModel("TW", TW.commission, 10, 0, ("STOCK", "ETF", "BOND_ETF", "TDR"), TAXES, short_fee_pct=0.08)


@pytest.mark.parametrize(
    "quantity, price, commission",
    [(500, 20.0, 2.5), (100, 50.0, 1.0), (1, 50.0, 0.5), (0, 50.0, 0.0), (100_000, 500.0, 500.0)],
    ids=["per share", "minimum", "1% cap below the minimum", "no fill", "per share above the minimum"],
)
def test_us_commissions(quantity, price, commission):
    assert us().commission(quantity, quantity * price) == pytest.approx(commission)


@pytest.mark.parametrize("value, commission", [(50_000.0, 80.0), (200_000.0, 160.0)], ids=["minimum", "rate"])
def test_tw_commissions(value, commission):
    assert tw().commission(1000, value) == pytest.approx(commission)


def test_slippage_on_buys_and_sells():
    assert us().fill_price(100.0, buy=True) == pytest.approx(100.05)
    assert us().fill_price(100.0, buy=False) == pytest.approx(99.95)
    assert tw().fill_price(100.0, buy=True) == pytest.approx(100.1)


@pytest.mark.parametrize(
    "symbol, iso, day_trade, rate",
    [(0, "2020-06-01", False, 0.3), (0, "2020-06-01", True, 0.15), (0, "2017-04-27", True, 0.3), (1, "2020-06-01", True, 0.1),
     (2, "2016-06-01", False, 0.1), (2, "2018-06-01", False, 0.0), (3, "2020-06-01", False, 0.1)],
)
def test_tw_taxes_by_category_and_date(symbol, iso, day_trade, rate):
    assert tw().tax_rate(symbol, to_day(date.fromisoformat(iso)), day_trade) == rate


def test_us_has_no_transaction_tax_and_tw_no_borrow_fee():
    assert us().tax_rate(0, to_day(date(2020, 6, 1))) == 0.0
    assert tw().borrow_fee(0, 1_000_000.0, 3) == 0.0


def test_borrow_fees_by_calendar_days_and_symbol():
    assert us().borrow_fee(0, 10_000.0, 3) == pytest.approx(10_000 * 0.005 * 3 / 365)
    assert us().borrow_fee(1, 10_000.0, 1) == pytest.approx(10_000 * 0.02 / 365)


def test_short_sale_fee_and_withholding():
    assert tw().short_sale_fee(100_000.0) == pytest.approx(80.0)
    assert us().net_dividend(100.0) == pytest.approx(70.0)
    assert us().short_sale_fee(100_000.0) == 0.0


@pytest.mark.parametrize(
    "sizing, quantity",
    [
        (Sizing(), 10_000 / 4 / 50),
        (Sizing(fraction=0.5), 10_000 * 0.5 / 50),
        (Sizing("FIXED_AMOUNT", amount=1_000), 20.0),
        (Sizing("FIXED_SHARES", shares=37), 37.0),
        (Sizing("VOL_RISK", risk_pct=1.0, atr_mult=2.0), 0.01 * 10_000 / (2 * 2.5)),
    ],
)
def test_entry_quantities_of_each_sizing_mode(sizing, quantity):
    assert entry_quantity(sizing, equity=10_000, price=50.0, atr=2.5, symbols=4) == pytest.approx(quantity)


def test_an_entry_without_a_price_or_atr_is_zero():
    assert entry_quantity(Sizing("VOL_RISK"), 10_000, 50.0, float("nan"), 1) == 0.0
    assert entry_quantity(Sizing(), 10_000, float("nan"), 1.0, 1) == 0.0
    with pytest.raises(ValueError):
        entry_quantity(Sizing("KELLY"), 10_000, 50.0, 1.0, 1)


def test_lots_and_the_position_cap():
    assert to_lot(1_499.9, 1_000) == 1_000 and to_lot(-1_500, 1_000) == -1_000 and to_lot(99.9, 1) == 99
    assert to_lot(float("nan"), 1) == 0
    assert cap(300.0, 10_000, 50.0, 100) == 200.0
    assert cap(150.0, 10_000, 50.0, 100) == 150.0
    assert cap(300.0, 10_000, 50.0, 50) == 100.0


def test_a_cost_model_holds_its_commission_rule():
    assert isinstance(us().commission_rule, PerShareCommission)
