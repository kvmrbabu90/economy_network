"""Synthetic markets for the domino-study tests (test utility only — production code never imports it).

- synth_prices: SPY plus named symbols on a business-day calendar, with optional controlled noise.
- synth_world: a many-firm market with random supplier / competitor links, from which a known ripple can be
  planted (e.g. every shock at A adds s * 0.8% to A's suppliers over days +1..+20) to check that the full
  pipeline recovers it — and recovers nothing when nothing is planted.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def synth_prices(n_days: int = 700, symbols=("AAA",), seed: int = 0, start: str = "2014-01-02",
                 betas: dict | None = None, idio: float = 0.015, mkt_vol: float = 0.01,
                 extra_returns: dict | None = None, idio_override: dict | None = None):
    """extra_returns: {symbol: ndarray(n_days)} added to that symbol's daily returns (for planting).
    idio_override: {symbol: ndarray(n_days)} replaces that symbol's idiosyncratic noise entirely, so a
    test controls every day (e.g. bounded uniform noise has no natural 3-sigma days)."""
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
        noise = rng.normal(0.0, idio, n_days)
        if idio_override and s in idio_override:
            noise = np.asarray(idio_override[s], dtype=float)
        r = 0.0001 + b * m + noise
        if extra_returns and s in extra_returns:
            r = r + extra_returns[s]
        add(s, r)
    return pd.concat(frames, ignore_index=True), m


def synth_world(n_firms: int = 80, n_sectors: int = 4, n_days: int = 2600, start: str = "2013-09-02",
                seed: int = 0, plant_w20: float = 0.0, plant_pre: float = 0.0, plant_role: str = "supplier",
                n_supplies: int = 140, n_competes: int = 140, df_t: int = 4, idio: float = 0.008):
    """Returns (prices, firms, links, any_edge_pairs). Fat-tailed (Student-t) idiosyncratic noise gives
    plenty of natural shocks. Planting is driven by the shocks detected in the un-planted world:
    for each shock (A, e, s) and each B with links row (A, B, plant_role), B's returns get s*plant_w20/20
    on days e+1..e+20 and s*plant_pre/5 on days e-5..e-1."""
    from research.dominoes import build_shocks as bs
    from research.dominoes import config
    from research.dominoes import returns as rt

    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(start, periods=n_days)
    m = rng.normal(0.0003, 0.01, n_days)
    syms = [f"F{i:03d}" for i in range(n_firms)]
    ids = [f"cik:{s}" for s in syms]
    sector_names = list(config.SECTOR_TO_ETF)[:n_sectors]
    sectors = [sector_names[i % n_sectors] for i in range(n_firms)]
    R = 0.0001 + m[:, None] + rng.standard_t(df_t, (n_days, n_firms)) * idio
    vols = rng.integers(1_000_000, 20_000_000, n_firms).astype(float)

    def rand_pairs(k: int) -> list[tuple[int, int]]:
        out: set = set()
        while len(out) < k:
            x, y = (int(v) for v in rng.integers(0, n_firms, 2))
            if x != y:
                out.add((x, y))
        return sorted(out)

    supplies = rand_pairs(n_supplies)                                   # (x supplies y)
    competes = sorted({(min(x, y), max(x, y)) for x, y in rand_pairs(n_competes)})
    rows = []
    for x, y in supplies:
        rows += [(ids[y], ids[x], "supplier"), (ids[x], ids[y], "customer")]
    for x, y in competes:
        rows += [(ids[x], ids[y], "competitor"), (ids[y], ids[x], "competitor")]
    links = (pd.DataFrame(rows, columns=["a_id", "b_id", "role"]).drop_duplicates()
             .assign(extractors="llm:claude-cli", weight=np.nan).reset_index(drop=True))
    pairs = pd.DataFrame(sorted({(ids[min(x, y)], ids[max(x, y)]) for x, y in supplies + competes}),
                         columns=["x_id", "y_id"])
    firms = pd.DataFrame({"firm_id": ids, "ticker": syms, "symbol": syms, "sector": sectors})

    def prices_of(rets: np.ndarray) -> pd.DataFrame:
        frames = [pd.DataFrame({"symbol": "SPY", "date": dates, "close": 100.0 * np.cumprod(1.0 + m),
                                "volume": 5e7})]
        close = 100.0 * np.cumprod(1.0 + rets, axis=0)
        for j, s in enumerate(syms):
            frames.append(pd.DataFrame({"symbol": s, "date": dates, "close": close[:, j], "volume": vols[j]}))
        return pd.concat(frames, ignore_index=True)

    if plant_w20 or plant_pre:
        panel = rt.build_panel(prices_of(R))
        shocks = bs.detect(panel, rt.AbnormalModel(panel, "market"), firms)
        targets = links[links.role == plant_role].groupby("a_id")["b_id"].apply(list).to_dict()
        col = {fid: j for j, fid in enumerate(ids)}
        R = R.copy()
        for sh in shocks.itertuples(index=False):
            for b in targets.get(sh.firm_id, []):
                j, e, s = col[b], int(sh.e_idx), int(sh.s)
                if plant_w20:
                    R[e + 1:min(e + 21, n_days), j] += s * plant_w20 / 20
                if plant_pre:
                    R[max(e - 5, 0):e, j] += s * plant_pre / 5
    return prices_of(R), firms, links, pairs
