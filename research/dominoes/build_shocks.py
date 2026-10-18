"""Stage: detect first-domino shocks (spec §4, §13.2, robustness 3) and record each shock's own drift.

A firm shocks on day e when |AR(e; e)| >= 3 sigma, where AR and sigma come from that event's own
estimation window [e-270, e-21] (>= 200 valid days). Dedup (§13.2): a candidate is dropped if ANY earlier
candidate of the same firm — kept or dropped — lies within the previous 10 trading days; dedup runs over
all dates (pre-2015 included) before the period filter keeps exploration/holdout shocks only.
Rules: 'sigma3' (primary), 'sigma4' and 'top1pct' (each firm's 99th percentile of |AR| computed WITHIN each
period: dates up to 2021-12-31 — including pre-2015 dedup-only candidates — use the 2015-2021 pool, dates from
2022 use the 2022+ pool; a firm-period with fewer than 100 valid days gets no top-1% shocks; §13 amendment).

First-domino drift s*CAR(A) (H5) is written for exploration shocks only, and only for windows that end by
2021-12-31 (embargo). Holdout shocks carry their day-0 attributes (AR, sigma, z, s, abnormal volume, sector-ETF z)
but never outcome data before the holdout is opened; holdout H5 is computed in the gated holdout measure stage.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from research.dominoes import config
from research.dominoes import returns as rt

SHOCK_SCHEMA = {
    "shock_id": "str", "firm_id": "str", "ticker": "str", "symbol": "str", "date": "date", "e_idx": "int",
    "ar": "float", "sigma": "float", "z": "float", "s": "int", "period": "str", "abn_vol": "float",
    "sector_etf_z": "float", "cara_w5": "float", "cara_w20": "float", "cara_w60": "float",
}
RULES = ("sigma3", "sigma4", "top1pct")


def dedup(candidates: np.ndarray) -> list[int]:
    """Keep a candidate only if no earlier candidate (kept or not) is within DEDUP_DAYS before it."""
    keep, prev = [], None
    for t in candidates:
        if prev is None or t - prev > config.DEDUP_DAYS:
            keep.append(int(t))
        prev = int(t)
    return keep


def detect(panel: rt.Panel, model: rt.AbnormalModel, firms: pd.DataFrame,
           etf_model: rt.AbnormalModel | None = None, rule: str = "sigma3") -> pd.DataFrame:
    if rule not in RULES:
        raise ValueError(f"unknown shock rule {rule!r}")
    if etf_model is None and model.spec == "market":
        etf_model = model
    T = panel.r.shape[0]
    t_all = np.arange(T)
    last_x = config.exploration_last_row(panel.dates)
    early = t_all <= last_x                                          # up to 2021-12-31 (pre-2015 included)
    expl_pool = early & np.asarray(panel.dates >= pd.Timestamp(config.EXPLORATION[0]))
    hold_pool = ~early
    recs: list[dict] = []
    for f in firms.itertuples(index=False):
        if f.symbol not in panel.col:
            continue
        i = panel.col[f.symbol]
        ar = model.path(np.full(T, i), t_all, 0, 0)[:, 0]
        sig = model.sigma[i]
        with np.errstate(invalid="ignore", divide="ignore"):
            z = ar / sig
        valid = np.isfinite(ar) & np.isfinite(z)
        if rule == "top1pct":
            cand = np.zeros(T, dtype=bool)
            for pool_mask, applies in ((expl_pool, early), (hold_pool, ~early)):
                pool = np.abs(ar[valid & pool_mask])
                if pool.size >= config.TOP1PCT_MIN_POOL:
                    cand |= applies & valid & (np.abs(ar) >= np.percentile(pool, 99))
        else:
            cand = valid & (np.abs(z) >= (config.SHOCK_SIGMA if rule == "sigma3" else 4.0))
        for t in dedup(np.nonzero(cand)[0]):
            period = config.period_of(panel.dates[t].strftime("%Y-%m-%d"))
            if period is None:
                continue
            recs.append({"firm_id": f.firm_id, "ticker": f.ticker, "symbol": f.symbol, "sector": f.sector,
                         "col": i, "e_idx": t, "date": panel.dates[t], "ar": float(ar[t]),
                         "sigma": float(sig[t]), "z": float(z[t]), "s": 1 if ar[t] > 0 else -1,
                         "period": period})
    if not recs:
        return pd.DataFrame({c: pd.Series(dtype="float64") for c in SHOCK_SCHEMA})

    df = pd.DataFrame(recs)
    cols, e, s = df["col"].to_numpy(), df["e_idx"].to_numpy(), df["s"].to_numpy()
    expl = (df["period"] == "exploration").to_numpy()
    for name, (a, b) in (("cara_w5", config.WINDOWS["w5"]), ("cara_w20", config.WINDOWS["w20"]),
                         ("cara_w60", config.WINDOWS["w60"])):
        cara = s * model.car(cols, e, a, b)
        df[name] = np.where(expl & (e + b <= last_x), cara, np.nan)     # holdout: never; exploration: embargo
    df["abn_vol"] = rt.abnormal_volume(panel, cols, e)
    etf_z = np.full(len(df), np.nan)
    if etf_model is not None:
        for k, sector in enumerate(df["sector"]):
            etf = config.SECTOR_TO_ETF.get(sector)
            if etf in panel.col:
                kc = panel.col[etf]
                sd = etf_model.sigma[kc, e[k]]
                if np.isfinite(sd) and sd > 0:
                    etf_z[k] = etf_model.ar0(np.array([kc]), np.array([e[k]]))[0] / sd
    df["sector_etf_z"] = etf_z
    df["shock_id"] = df["firm_id"] + "|" + df["date"].dt.strftime("%Y-%m-%d")
    df["e_idx"] = df["e_idx"].astype("int64")
    df["s"] = df["s"].astype("int64")
    return df[list(SHOCK_SCHEMA)].sort_values(["date", "firm_id"]).reset_index(drop=True)
