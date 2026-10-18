"""Stage: power check on EXPLORATION data only, run before any hypothesis test (spec §5, §13).

For each role and slow window: demean the primary ripple (so the null holds exactly and the observed effect
cannot count as power), inject a synthetic slow ripple of size delta spread evenly over [+1, +20], and
bootstrap whole event dates. Adding a constant to every observation shifts a replicate's mean by that
constant and leaves its clustered SE unchanged, so one set of replicates serves every delta. The demeaned date
sums carry the small-sample factor sqrt(G/(G-1)) (on both the replicate mean and its SE), and a replicate with
zero SE never counts as a detection.
Detection = |t| > max(3, Holm's rank-1 threshold for the 9-test H2 family). The MDE is the smallest delta
(the size of the 20-day ripple) detected in >= 80% of replicates; a role whose [+1,+20] bootstrap MDE exceeds
1.5% (or is never reached on the 0-3% grid) is 'underpowered / inconclusive'. That is the only verdict input.

Descriptive only (never verdict inputs), written here so they are fixed before any outcome is measured:
- mde_ucb80: the MDE using the 80th percentile of the replicate SEs (the bootstrap MDE is optimistic when a few
  dates dominate or tails are heavy);
- mde_twoway: analytic MDE with a two-way (event date x linked firm) clustered SE (overlapping windows);
- concentration: share of events / of clustered variance on the top-5 dates and the effective cluster counts;
- holdout_projection: from the SPY calendar only (no holdout data) — the holdout leg's floor-aware MDE, the
  joint exploration x holdout MDE, and the joint power at the 1.5% limit;
- borderline: a role that is not underpowered but where any of these exceeds 1.5%, with the reasons.
The output reports SEs, thresholds, MDEs and shares — never the observed means.
"""
from __future__ import annotations

import hashlib
import math
import subprocess

import numpy as np
import pandas as pd
from scipy import stats as sps

from research.dominoes import build_events as be
from research.dominoes import config, io
from research.dominoes import stats as st

INJECT_SHARE = {"w5": 5 / 20, "w20": 1.0, "w60": 1.0}      # share of the [+1,+20] ripple inside each window
FAMILY_SIZE = len(config.ROLES) * len(config.SLOW_WINDOWS)   # the 9 H2 tests
Z80 = float(sps.norm.ppf(config.POWER_TARGET))
GRID = 0.0005


def t_threshold(df: int) -> float:
    holm_rank1 = float(sps.t.ppf(1 - config.FAMILY_ALPHA / (2 * FAMILY_SIZE), df)) if df > 0 else math.inf
    return max(config.T_BAR, holm_rank1)


def _ceil_grid(x: float) -> float:
    return round(math.ceil(x / GRID - 1e-9) * GRID, 4) if np.isfinite(x) else math.nan


def _replicates(v: np.ndarray, clusters: np.ndarray, n_boot: int, seed: int, small_sample: bool = True):
    st._check_labels(clusters)
    codes, uniq = pd.factorize(pd.Series(clusters))
    G = len(uniq)
    dm = v - v.mean()
    sums = np.bincount(codes, weights=dm, minlength=G)
    if small_sample:
        sums = sums * math.sqrt(G / (G - 1))
    cnts = np.bincount(codes, minlength=G).astype(float)
    draws = np.random.default_rng(seed).integers(0, G, size=(n_boot, G), dtype=np.int32)
    S, C = sums[draws], cnts[draws]
    N = C.sum(axis=1)
    m0 = S.sum(axis=1) / N
    dev = S - m0[:, None] * C
    se = np.sqrt(G / (G - 1) * (dev ** 2).sum(axis=1) / N ** 2)
    return m0, se


def _concentration(v: np.ndarray, codes: np.ndarray, G: int) -> dict:
    nan = {"top5_share_events": math.nan, "g_eff_events": math.nan,
           "top5_share_variance": math.nan, "g_eff_variance": math.nan}
    n = len(v)
    if n == 0 or G < 2:
        return nan
    ng = np.bincount(codes, minlength=G).astype(float)
    D2 = np.bincount(codes, weights=v - v.mean(), minlength=G) ** 2
    s2 = float(D2.sum())
    out = {"top5_share_events": 1.0 if G < 5 else float(np.sort(ng)[-5:].sum() / n),
           "g_eff_events": float(n ** 2 / (ng ** 2).sum())}
    if s2 > 0:
        out.update({"top5_share_variance": 1.0 if G < 5 else float(np.sort(D2)[-5:].sum() / s2),
                    "g_eff_variance": float(s2 ** 2 / (D2 ** 2).sum())})
    else:
        out.update({"top5_share_variance": math.nan, "g_eff_variance": math.nan})
    return out


def calendar_days(calendar, window: str) -> dict:
    """Trading days that can host an event with a complete `window`: exploration days whose window ends by the
    last 2021 trading day (embargo), holdout days whose window ends by the calendar's last day."""
    dates = pd.DatetimeIndex(calendar)
    _, hi = config.WINDOWS[window]
    last_x = config.exploration_last_row(dates)
    idx = np.arange(len(dates))
    in_x = np.asarray(dates >= pd.Timestamp(config.EXPLORATION[0])) & (idx + hi <= last_x)
    in_h = np.asarray(dates >= pd.Timestamp(config.HOLDOUT_START)) & (idx + hi <= len(dates) - 1)
    return {"t_explore_days": int(in_x.sum()), "t_holdout_days": int(in_h.sum())}


def _projection(se: float, g: int, share: float, curve: list, days: dict) -> dict:
    t_e, t_h = days["t_explore_days"], days["t_holdout_days"]
    out = {**days, "se_holdout": math.nan, "g_holdout": math.nan, "mde_holdout_floor": math.nan,
           "mde_joint": math.nan, "joint_power_at_limit": math.nan}
    if not (np.isfinite(se) and t_e > 0 and t_h > 0 and g >= 2):
        return out
    se_h = se * math.sqrt(t_e / t_h)
    g_h = g * t_h / t_e
    thr_h = max(config.FLOOR, float(sps.t.ppf(1 - config.HOLDOUT_ALPHA, max(g_h - 1, 1))) * se_h)
    out.update({"se_holdout": se_h, "g_holdout": g_h, "mde_holdout_floor": (thr_h + Z80 * se_h) / share})
    p_hold = {d: float(sps.norm.sf((thr_h - d * share) / se_h)) for d, _ in curve}
    joint = [(d, r * p_hold[d]) for d, r in curve]
    out["mde_joint"] = next((d for d, j in joint if j >= config.POWER_TARGET), math.nan)
    out["joint_power_at_limit"] = next((j for d, j in joint if abs(d - config.MDE_LIMIT) < 1e-12), math.nan)
    return out


def _window_power(values: np.ndarray, clusters: np.ndarray, bids, share: float, n_boot: int, seed: int,
                  days: dict | None = None) -> dict:
    ok = np.isfinite(values)
    v, cl = values[ok], np.asarray(clusters)[ok]
    bb = np.asarray(bids)[ok] if bids is not None else None
    st._check_labels(cl)
    codes, uniq = pd.factorize(pd.Series(cl))
    g = len(uniq)
    base = {"n": int(len(v)), "g": g, "se": math.nan, "t_threshold": math.nan, "mde_analytic": math.nan,
            "mde_bootstrap": math.nan, "mde_ucb80": math.nan, "mde_twoway": math.nan, "curve": [],
            "concentration": _concentration(v, codes, g)}
    if len(v) < 3 or g < 2:
        if days is not None:
            base["holdout_projection"] = _projection(math.nan, g, share, [], days)
        return base
    se = st.clustered_mean(v - v.mean(), cl)["se"]          # SE is invariant to demeaning
    thr = t_threshold(g - 1)
    m0, se_b = _replicates(v, cl, n_boot, seed)
    pos = se_b > 1e-9 * se if np.isfinite(se) and se > 0 else se_b > 0     # same-date draws: SE is rounding noise
    curve, mde_b = [], math.nan
    for delta in config.POWER_DELTAS:
        with np.errstate(divide="ignore", invalid="ignore"):
            hit = pos & (np.abs(m0 + delta * share) / np.where(pos, se_b, 1.0) > thr)
        rate = float(np.mean(hit))
        curve.append([float(delta), rate])
        if math.isnan(mde_b) and rate >= config.POWER_TARGET:
            mde_b = float(delta)
    q_se = float(np.quantile(se_b[pos], config.POWER_UCB_Q)) if pos.any() else math.nan
    se2 = st.clustered_mean_2way(v - v.mean(), cl, bb)["se"] if bb is not None else math.nan
    base.update({"se": float(se), "t_threshold": float(thr), "mde_analytic": float((thr + Z80) * se / share),
                 "mde_bootstrap": mde_b, "mde_ucb80": _ceil_grid((thr + Z80) * q_se / share),
                 "mde_twoway": float((thr + Z80) * se2 / share) if np.isfinite(se2) else math.nan,
                 "curve": curve})
    if days is not None:
        base["holdout_projection"] = _projection(float(se), g, share, curve, days)
    return base


def borderline_reasons(w20: dict, underpowered: bool) -> list[str]:
    """Descriptive caveats for a role that passes the §5 power rule; never a verdict input."""
    if underpowered:
        return []
    lim = config.MDE_LIMIT
    checks = (("mde_ucb80", "80% upper-bound MDE"),
              ("mde_joint", "full-rule (exploration x holdout) MDE"),
              ("mde_twoway", "two-way (date x linked firm) MDE"))
    out = []
    for key, label in checks:
        if key == "mde_joint":
            proj = w20.get("holdout_projection")
            if not proj:
                out.append(f"{label} not computable (no trading calendar)")
                continue
            x = proj.get("mde_joint")
            if x is None or not np.isfinite(x):
                out.append(f"{label} not reached on the 0-{config.POWER_DELTAS[-1] * 100:.0f}% grid")
                continue
        else:
            x = w20.get(key)
            if x is None or not np.isfinite(x):
                out.append(f"{label} not computable")
                continue
        if x > lim + 1e-12:
            out.append(f"{label} {x * 100:.2f}% > {lim * 100:.1f}%")
    return out


def events_fingerprint(events: pd.DataFrame) -> str:
    """sha256 of the exploration ripples the power check reads (stale power files are refused by measure)."""
    keys = [c for c in ("event_id", "role", "date") if c in events.columns]
    cols = keys + [f"ripple_{w}_k{config.K_PRIMARY}" for w in config.SLOW_WINDOWS]
    df = events[cols].sort_values(keys, kind="mergesort")
    text = df.to_csv(index=False, float_format="%.17g", lineterminator="\n", date_format="%Y-%m-%d")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def power(events: pd.DataFrame, n_boot: int | None = None, seed: int | None = None, calendar=None) -> dict:
    n_boot = config.POWER_BOOT_N if n_boot is None else n_boot
    seed = config.SEED if seed is None else seed
    out = {"what": "spec §5 power check (exploration only; observed means deliberately not reported)",
           "detection_rule": f"|t| > max({config.T_BAR}, Holm rank-1 threshold, {FAMILY_SIZE} tests)",
           "injection": "delta spread evenly over [+1,+20]; MDEs are in units of that 20-day ripple",
           "mde_limit": config.MDE_LIMIT, "power_target": config.POWER_TARGET, "n_boot": n_boot,
           "meta": {"method_version": config.POWER_METHOD_VERSION, "n_boot": n_boot, "seed": seed,
                    "events_fingerprint": events_fingerprint(events)},
           "roles": {}}
    has_b = "b_id" in events.columns
    for k, role in enumerate(config.ROLES):
        sub = events[events["role"] == role]
        res: dict = {}
        for j, w in enumerate(config.SLOW_WINDOWS):
            col = f"ripple_{w}_k{config.K_PRIMARY}"
            res[w] = _window_power(sub[col].to_numpy(dtype=float), sub["date"].to_numpy(),
                                   sub["b_id"].to_numpy() if has_b else None, INJECT_SHARE[w], n_boot,
                                   seed + 100 * k + j, calendar_days(calendar, w) if calendar is not None else None)
        mde = res["w20"]["mde_bootstrap"]
        res["underpowered"] = bool(not np.isfinite(mde) or mde > config.MDE_LIMIT)
        res["borderline_reasons"] = borderline_reasons(res["w20"], res["underpowered"])
        res["borderline"] = bool(res["borderline_reasons"])
        res["n_events"] = int(len(sub))
        res["n_dates"] = int(sub["date"].nunique())
        out["roles"][role] = res
    return out


def _commit() -> str:
    r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=io.REPO_ROOT, capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else "unknown"


def run(period: str = "exploration", variant: str = "market", rule: str = "sigma3",
        n_boot: int | None = None) -> dict:
    if period != "exploration":
        raise ValueError("the power check runs on exploration data only (spec §5)")
    sfx = config.variant_suffix(variant, rule)
    store = io.store_dir()
    if (store / f"results_exploration{sfx}.json").exists():
        raise PermissionError(f"exploration already measured for {variant}/{rule}: the power check runs once, "
                              "before any hypothesis test, and is never regenerated afterwards (spec §5)")
    events = io.read_frame(f"events_exploration{sfx}", be.event_schema())
    calendar = None
    if io.frame_path("prices").exists():
        from research.dominoes import fetch_prices
        p = fetch_prices.load_prices()
        calendar = pd.DatetimeIndex(sorted(pd.to_datetime(p.loc[p["symbol"] == config.MARKET_TICKER, "date"])))
    res = power(events, n_boot=n_boot, calendar=calendar)
    res["variant"], res["rule"] = variant, rule
    res["meta"].update({"variant": variant, "rule": rule, "code_commit": _commit()})
    path = store / f"power{sfx}.json"
    io.write_json(path, res)
    return res
