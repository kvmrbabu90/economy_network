"""Synthetic market generator for the domino-study tests (test utility only — never imported by
production code). Produces a long price frame (symbol, date, close, volume) on a business-day calendar
with SPY plus any symbols, and returns the market return path used to build it."""
from __future__ import annotations

import numpy as np
import pandas as pd


def synth_prices(n_days: int = 700, symbols=("AAA",), seed: int = 0, start: str = "2014-01-02",
                 betas: dict | None = None, idio: float = 0.015, mkt_vol: float = 0.01,
                 extra_returns: dict | None = None):
    """extra_returns: {symbol: ndarray(n_days)} added to that symbol's daily returns (for planting)."""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(start, periods=n_days)
    m = rng.normal(0.0003, mkt_vol, n_days)
    frames = []

    def add(sym: str, r: np.ndarray) -> None:
        close = 100.0 * np.cumprod(1.0 + r)
        vol = rng.integers(1_000_000, 2_000_000, n_days).astype(float)
        frames.append(pd.DataFrame({"symbol": sym, "date": dates, "close": close, "volume": vol}))

    add("SPY", m)
    for s in symbols:
        b = (betas or {}).get(s, 1.0)
        r = 0.0001 + b * m + rng.normal(0.0, idio, n_days)
        if extra_returns and s in extra_returns:
            r = r + extra_returns[s]
        add(s, r)
    return pd.concat(frames, ignore_index=True), m
