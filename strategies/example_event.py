"""Example of an event-driven plugin: buy a breakout above the highest close of the last n bars, sell below the lowest."""

NAME_EN = "Example: breakout (events)"
NAME_ZH = "範例：突破（事件）"
TIMEFRAME = "1d"
PARAMS = {
    "n": {"type": "int", "default": 20, "min": 5, "max": 250, "step": 5, "label_en": "Channel", "label_zh": "通道"},
    "weight": {"type": "float", "default": 0.2, "min": 0.01, "max": 1.0, "step": 0.01, "label_en": "Weight",
               "label_zh": "權重"},
}


def on_bar(ctx):
    n = ctx.params["n"]
    closes = ctx.history("close", n + 1)
    if len(closes) <= n:
        return
    for j, symbol in enumerate(ctx.symbols):
        before, now = closes[:-1, j], closes[-1, j]
        if now > before.max() and ctx.position(symbol) == 0:
            ctx.order_target_weight(symbol, ctx.params["weight"])
        elif now < before.min() and ctx.position(symbol) > 0:
            ctx.order_target_qty(symbol, 0)
