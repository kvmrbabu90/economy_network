"""Stage: power check on EXPLORATION data only, run before any hypothesis test (spec §5).

For each role and slow window: demean the primary ripple (so the null holds exactly and the observed effect
cannot count as power), inject a synthetic slow ripple of size delta spread evenly over [+1, +20], and
bootstrap whole event dates. Adding a constant to every observation shifts a replicate's mean by that
constant and leaves its clustered SE unchanged, so one set of replicates serves every delta.
Detection = |t| > max(3, Holm's rank-1 threshold for the 9-test H2 family). The MDE is the smallest delta
(the size of the 20-day ripple) detected in >= 80% of replicates; a role whose w20 MDE exceeds 1.5% is
'underpowered / inconclusive'. The output reports SEs, thresholds and MDEs — never the observed means.
"""
from __future__ import annotations

import json
import math

import numpy as np
import pandas as pd
from scipy import stats as sps

from research.dominoes import build_events as be
from research.dominoes import config, io
from research.dominoes import stats as st

INJECT_SHARE = {"w5": 5 / 20, "w20": 1.0, "w60": 1.0}      # share of the [+1,+20] ripple inside each window
FAMILY_SIZE = len(config.ROLES) * len(config.SLOW_WINDOWS)   # the 9 H2 tests
Z80 = float(sps.norm.ppf(config.POWER_TARGET))


def t_threshold(df: int) -> float:
    holm_rank1 = float(sps.t.ppf(1 - config.FAMILY_ALPHA / (2 * FAMILY_SIZE), df)) if df > 0 else math.inf
    return max(config.T_BAR, holm_rank1)


def _replicates(v: np.ndarray, clusters: np.ndarray, n_boot: int, seed: int):
    codes, uniq = pd.factorize(pd.Series(clusters))
    G = len(uniq)
    dm = v - v.mean()
    sums = np.bincount(codes, weights=dm, minlength=G)
    cnts = np.bincount(codes, minlength=G).astype(float)
    draws = np.random.default_rng(seed).integers(0, G, size=(n_boot, G), dtype=np.int32)
    S, C = sums[draws], cnts[draws]
    N = C.sum(axis=1)
    m0 = S.sum(axis=1) / N
    dev = S - m0[:, None] * C
    se = np.sqrt(G / (G - 1) * (dev ** 2).sum(axis=1) / N ** 2)
    return m0, se


def _window_power(values: np.ndarray, clusters: np.ndarray, share: float, n_boot: int, seed: int) -> dict:
    ok = np.isfinite(values)
    v, cl = values[ok], np.asarray(clusters)[ok]
    g = int(pd.Series(cl).nunique()) if len(cl) else 0
    if len(v) < 3 or g < 2:
        return {"n": int(len(v)), "g": g, "se": math.nan, "t_threshold": math.nan,
                "mde_analytic": math.nan, "mde_bootstrap": math.nan, "curve": []}
    se = st.clustered_mean(v - v.mean(), cl)["se"]          # SE is invariant to demeaning
    thr = t_threshold(g - 1)
    m0, se_b = _replicates(v, cl, n_boot, seed)
    curve, mde_b = [], math.nan
    for delta in config.POWER_DELTAS:
        rate = float(np.mean(np.abs(m0 + delta * share) / se_b > thr))
        curve.append([float(delta), rate])
        if math.isnan(mde_b) and rate >= config.POWER_TARGET:
            mde_b = float(delta)
    return {"n": int(len(v)), "g": g, "se": float(se), "t_threshold": float(thr),
            "mde_analytic": float((thr + Z80) * se / share), "mde_bootstrap": mde_b, "curve": curve}


def power(events: pd.DataFrame, n_boot: int = config.POWER_BOOT_N, seed: int = config.SEED) -> dict:
    out = {"what": "spec §5 power check (exploration only; observed means deliberately not reported)",
           "detection_rule": f"|t| > max({config.T_BAR}, Holm rank-1 threshold, {FAMILY_SIZE} tests)",
           "injection": "delta spread evenly over [+1,+20]; MDEs are in units of that 20-day ripple",
           "mde_limit": config.MDE_LIMIT, "power_target": config.POWER_TARGET, "n_boot": n_boot,
           "roles": {}}
    for k, role in enumerate(config.ROLES):
        sub = events[events["role"] == role]
        res: dict = {}
        for j, w in enumerate(config.SLOW_WINDOWS):
            col = f"ripple_{w}_k{config.K_PRIMARY}"
            res[w] = _window_power(sub[col].to_numpy(dtype=float), sub["date"].to_numpy(),
                                   INJECT_SHARE[w], n_boot, seed + 100 * k + j)
        mde = res["w20"]["mde_bootstrap"]
        res["underpowered"] = bool(not np.isfinite(mde) or mde > config.MDE_LIMIT)
        res["n_events"] = int(len(sub))
        res["n_dates"] = int(sub["date"].nunique())
        out["roles"][role] = res
    return out


def run(period: str = "exploration", variant: str = "market", rule: str = "sigma3",
        n_boot: int = config.POWER_BOOT_N) -> dict:
    if period != "exploration":
        raise ValueError("the power check runs on exploration data only (spec §5)")
    sfx = config.variant_suffix(variant, rule)
    events = io.read_frame(f"events_exploration{sfx}", be.event_schema())
    res = power(events, n_boot=n_boot)
    res["variant"], res["rule"] = variant, rule
    path = io.store_dir() / f"power{sfx}.json"
    path.write_text(json.dumps(res, indent=2), encoding="utf-8")
    return res
