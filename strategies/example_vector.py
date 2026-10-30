"""Example of a vectorized plugin: equal weights in every symbol whose fast average is above its slow average."""

import numpy as np

from btlab.indicators import sma

NAME_EN = "Example: MA cross (weights)"
NAME_ZH = "範例：均線交叉（權重）"
TIMEFRAME = "1d"
PARAMS = {
    "fast": {"type": "int", "default": 20, "min": 2, "max": 200, "step": 1, "label_en": "Fast", "label_zh": "快線"},
    "slow": {"type": "int", "default": 50, "min": 5, "max": 400, "step": 5, "label_en": "Slow", "label_zh": "慢線"},
}


def signals(bars, params):
    fast, slow = sma(bars.close, params["fast"]), sma(bars.close, params["slow"])
    above = fast > slow
    holding = np.maximum(above.sum(axis=1, keepdims=True), 1)
    weights = np.where(above, 1.0 / holding, 0.0)
    # NaN is "no instruction": until the slow average exists, nothing is decided.
    return np.where(np.isnan(slow), np.nan, weights)
