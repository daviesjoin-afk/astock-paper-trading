"""Original offline momentum research strategy (synthetic fixture, not advice).

Rule, evaluated once per frozen observation in order:
    let `reference` be the price of the immediately preceding observation;
    the strategy needs at least `lookback` earlier observations before it acts.
    up   when price*10000 >= reference*(10000 + threshold_bps)  -> buy
    down when price*10000 <= reference*(10000 - threshold_bps)  -> sell
    otherwise no signal.

No floats, no clock, no randomness, no order quantity, price, fee or cash.
"""
STRATEGY_VERSION_ID = "research-momentum-v1"
PARAMETERS = {"lookback": 2, "threshold_bps": 25}


def signals(entries):
    """Return (entry, side) pairs in observation order."""
    prices = [entry["quote"]["price_cents"] for entry in entries]
    lookback = PARAMETERS["lookback"]
    threshold = PARAMETERS["threshold_bps"]
    emitted = []
    for index, entry in enumerate(entries):
        if index < lookback:
            continue
        price = prices[index]
        reference = prices[index - 1]
        if price * 10_000 >= reference * (10_000 + threshold):
            emitted.append((entry, "buy"))
        elif price * 10_000 <= reference * (10_000 - threshold):
            emitted.append((entry, "sell"))
    return emitted
