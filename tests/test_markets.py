"""Market constants (SPEC.md §4.1)."""

from dataclasses import FrozenInstanceError
from datetime import date, time
from zoneinfo import ZoneInfo

import pytest

from btlab.markets import (
    DATA_START_YEARS,
    MARKETS,
    TW,
    US,
    PerShareCommission,
    PriceLimit,
    RateCommission,
    Session,
)


def test_the_constants_of_section_4_1():
    assert MARKETS == {"US": US, "TW": TW}
    assert (US.currency, TW.currency) == ("USD", "TWD")
    assert (US.time_zone, TW.time_zone) == ("America/New_York", "Asia/Taipei")
    assert US.session == Session(time(9, 30), time(16, 0))
    assert TW.session == Session(time(9, 0), time(13, 30))
    assert US.categories == ("STOCK", "ETF")
    assert sorted(TW.categories) == ["BOND_ETF", "ETF", "STOCK", "TDR"]
    assert (US.benchmark, TW.benchmark) == ("SPY", "0050")
    assert (US.initial_capital, TW.initial_capital) == (100_000, 1_000_000)
    assert (US.lot, US.board_lot, TW.lot, TW.board_lot) == (1, None, 1, 1_000)
    assert US.price_limits == ()
    assert TW.price_limits == (PriceLimit(date.min, 7.0), PriceLimit(date(2015, 6, 1), 10.0))
    assert US.commission == PerShareCommission(per_share=0.005, min=1.0, max_pct=1.0)
    assert TW.commission == RateCommission(rate_pct=0.08, min=80.0)
    assert (US.transaction_tax, TW.transaction_tax) == (False, True)
    assert (US.slippage_bps, TW.slippage_bps) == (5, 10)
    assert (US.short_collateral, TW.short_collateral) == (0.5, 0.9)
    assert (US.borrow_fee_pct, US.short_fee_pct) == (0.5, None)
    assert (TW.borrow_fee_pct, TW.short_fee_pct) == (None, 0.08)
    assert (US.dividend_withholding_pct, TW.dividend_withholding_pct) == (30, 0)
    assert US.update_weekdays == (1, 2, 3, 4, 5)  # Tuesday to Saturday
    assert TW.update_weekdays == (0, 1, 2, 3, 4)  # Monday to Friday
    assert (US.order_kinds, TW.order_kinds) == (("MOO", "LIMIT"), ("LIMIT",))
    assert DATA_START_YEARS == 31


@pytest.mark.parametrize(
    "constant, attribute",
    [(US, "currency"), (TW.session, "open"), (TW.price_limits[1], "pct"), (US.commission, "min"), (TW.commission, "rate_pct")],
)
def test_the_constants_are_frozen(constant, attribute):
    with pytest.raises(FrozenInstanceError):
        setattr(constant, attribute, None)


@pytest.mark.parametrize("market", [US, TW])
def test_the_time_zones_resolve(market):
    # On Windows zoneinfo needs the tzdata package.
    assert ZoneInfo(market.time_zone).key == market.time_zone
