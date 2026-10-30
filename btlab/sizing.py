"""Position sizing of DIRECTION entries, and quantities in lots (SPEC.md §9.4, §10.4). Pure."""

import math
from dataclasses import dataclass

SIZING_MODES = ("FIXED_FRACTION", "FIXED_AMOUNT", "FIXED_SHARES", "VOL_RISK")


@dataclass(frozen=True)
class Sizing:
    """fraction None means 1/N, N being the run's symbols."""

    mode: str = "FIXED_FRACTION"
    fraction: float | None = None
    amount: float | None = None
    shares: float | None = None
    risk_pct: float = 1.0
    atr_n: int = 20
    atr_mult: float = 2.0


def entry_quantity(sizing: Sizing, equity: float, price: float, atr: float, symbols: int) -> float:
    """The unrounded quantity of an entry, at the signal bar's equity and close; atr is in the price's scale.

    0 when it is not a finite positive number, for example without a price or an ATR.
    """
    if sizing.mode == "FIXED_FRACTION":
        fraction = 1 / symbols if sizing.fraction is None else sizing.fraction
        quantity = fraction * equity / price
    elif sizing.mode == "FIXED_AMOUNT":
        quantity = sizing.amount / price
    elif sizing.mode == "FIXED_SHARES":
        quantity = sizing.shares
    elif sizing.mode == "VOL_RISK":
        quantity = sizing.risk_pct / 100 * equity / (sizing.atr_mult * atr)
    else:
        raise ValueError(f"unknown sizing mode {sizing.mode}")
    return quantity if math.isfinite(quantity) and quantity > 0 else 0.0


def cap(quantity: float, equity: float, price: float, max_position_pct: float) -> float:
    """An entry is at most max_position_pct of equity."""
    return min(quantity, max(max_position_pct / 100 * equity / price, 0.0))


def to_lot(quantity: float, lot: int) -> float:
    """Rounded toward zero to the lot; 0 for a quantity that is not finite."""
    return float(math.trunc(quantity / lot) * lot) if math.isfinite(quantity) else 0.0
