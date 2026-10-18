"""Pure statistics for the domino study (spec §5).

- clustered_mean: mean with a CR1 cluster-robust SE, se^2 = G/(G-1) * sum_g (sum_{i in g} (v_i - mean))^2 / n^2;
  t-based p-values with G-1 degrees of freedom. NaN values (and clusters left empty) are dropped.
- clustered_ols: y = a + b x with the CR1 sandwich (Stata convention: G/(G-1) * (n-1)/(n-k)); for k=1 this
  reduces to clustered_mean's SE.
- holm: Holm-Bonferroni step-down rejections in input order.
- block_bootstrap_ci: percentile CI of the pooled mean, resampling whole clusters with replacement.
- clustered_mean_2way: exploratory diagnostic (not pre-registered) — Cameron-Gelbach-Miller two-way CR1
  (V1 + V2 - V12), falling back to max(V1, V2) when that is not positive; df = min(G1, G2) - 1.

Cluster labels are event dates (or weeks / firms derived from them) and are never missing by construction, so a
missing label raises ValueError rather than silently changing the equal-weighted sample.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
from scipy import stats as sps


def _check_labels(*cluster_arrays) -> None:
    for c in cluster_arrays:
        bad = int(pd.isna(pd.Series(np.asarray(c, dtype=object))).sum())
        if bad:
            raise ValueError(f"{bad} missing cluster label(s): every observation needs a cluster")


def _clean(values, clusters):
    v = np.asarray(values, dtype=float)
    c = np.asarray(clusters)
    _check_labels(c)
    ok = np.isfinite(v)
    return v[ok], c[ok]


def _codes(c) -> tuple[np.ndarray, int]:
    codes, uniq = pd.factorize(pd.Series(c))
    return codes, len(uniq)


def _cr1_var(u: np.ndarray, codes: np.ndarray, G: int, n: int) -> float:
    """CR1 variance of a mean: G/(G-1) * sum_g (sum_{i in g} u_i)^2 / n^2 (u already demeaned)."""
    dev = np.bincount(codes, weights=u, minlength=G)
    return G / (G - 1) * float((dev ** 2).sum()) / n ** 2


def two_sided_p(t: float, df: int) -> float:
    return float(2 * sps.t.sf(abs(t), df)) if np.isfinite(t) and df > 0 else math.nan


def one_sided_p(t: float, df: int, direction: int) -> float:
    """P-value for H1: effect in `direction` (+1 positive, -1 negative)."""
    if not (np.isfinite(t) and df > 0):
        return math.nan
    return float(sps.t.sf(t, df) if direction >= 0 else sps.t.cdf(t, df))


def clustered_mean(values, clusters) -> dict:
    v, c = _clean(values, clusters)
    n = len(v)
    if n == 0:
        return {"mean": math.nan, "se": math.nan, "t": math.nan, "n": 0, "g": 0, "df": 0, "p_two": math.nan}
    codes, G = _codes(c)
    mean = float(v.mean())
    se = math.sqrt(_cr1_var(v - mean, codes, G, n)) if G >= 2 else math.nan
    t = mean / se if np.isfinite(se) and se > 0 else math.nan
    return {"mean": mean, "se": se, "t": t, "n": n, "g": G, "df": G - 1, "p_two": two_sided_p(t, G - 1)}


def clustered_mean_2way(values, clusters1, clusters2) -> dict:
    """Exploratory: mean with a two-way (e.g. event date x linked firm) CR1 SE. Returns mean, se, t, n, g1, g2,
    df, p_two, p_one_pos, p_one_neg, fallback (True when V1 + V2 - V12 <= 0 and max(V1, V2) was used)."""
    v = np.asarray(values, dtype=float)
    c1, c2 = np.asarray(clusters1), np.asarray(clusters2)
    _check_labels(c1, c2)
    ok = np.isfinite(v)
    v, c1, c2 = v[ok], c1[ok], c2[ok]
    n = len(v)
    out = {"mean": float(v.mean()) if n else math.nan, "se": math.nan, "t": math.nan, "n": n, "g1": 0, "g2": 0,
           "df": 0, "p_two": math.nan, "p_one_pos": math.nan, "p_one_neg": math.nan, "fallback": False}
    if n == 0:
        return out
    k1, G1 = _codes(c1)
    k2, G2 = _codes(c2)
    out.update({"g1": G1, "g2": G2})
    if G1 < 2 or G2 < 2:
        return out
    k12, G12 = _codes(k1.astype(np.int64) * G2 + k2)
    u = v - out["mean"]
    V1, V2 = _cr1_var(u, k1, G1, n), _cr1_var(u, k2, G2, n)
    V12 = _cr1_var(u, k12, G12, n) if G12 >= 2 else 0.0
    V = V1 + V2 - V12
    if not V > 0:
        V, out["fallback"] = max(V1, V2), True
    se = math.sqrt(V) if V > 0 else math.nan
    t = out["mean"] / se if np.isfinite(se) else math.nan
    df = min(G1, G2) - 1
    out.update({"se": se, "t": t, "df": df, "p_two": two_sided_p(t, df), "p_one_pos": one_sided_p(t, df, +1),
                "p_one_neg": one_sided_p(t, df, -1)})
    return out


def clustered_ols(y, x, clusters) -> dict:
    """y = a + b x by OLS on centred x, CR1 sandwich G/(G-1)*(n-1)/(n-2). NaN slope/SE when x has no variation;
    with one cluster the slope is kept but SE/t are NaN."""
    y = np.asarray(y, dtype=float)
    x = np.asarray(x, dtype=float)
    c = np.asarray(clusters)
    _check_labels(c)
    ok = np.isfinite(y) & np.isfinite(x)
    y, x, c = y[ok], x[ok], c[ok]
    n = len(y)
    out = {"slope": math.nan, "intercept": math.nan, "se": math.nan, "t": math.nan, "n": n, "g": 0,
           "df": 0, "p_two": math.nan}
    if n < 3:
        return out
    codes, G = _codes(c)
    out.update({"g": G, "df": G - 1})
    xc = x - x.mean()
    sxx = float(xc @ xc)
    if not sxx > 1e-12 * float(x @ x) or sxx == 0.0:
        return out
    slope = float(xc @ y) / sxx
    intercept = float(y.mean() - slope * x.mean())
    u = y - intercept - slope * x
    out.update({"slope": slope, "intercept": intercept})
    if G < 2:
        return out
    S = np.bincount(codes, weights=xc * u, minlength=G)
    var = float((S ** 2).sum()) / sxx ** 2 * (G / (G - 1)) * ((n - 1) / (n - 2))
    se = math.sqrt(var) if var > 0 else math.nan
    t = slope / se if np.isfinite(se) else math.nan
    out.update({"se": se, "t": t, "p_two": two_sided_p(t, G - 1)})
    return out


def holm(pvalues, alpha: float) -> list[bool]:
    p = [float(x) if x is not None else math.nan for x in pvalues]
    m = len(p)
    order = sorted(range(m), key=lambda i: (math.isnan(p[i]), p[i] if not math.isnan(p[i]) else 0.0))
    rejected = [False] * m
    for j, i in enumerate(order):
        if not math.isnan(p[i]) and p[i] <= alpha / (m - j):
            rejected[i] = True
        else:
            break
    return rejected


def block_bootstrap_ci(values, clusters, n_boot: int, seed: int, alpha: float = 0.05) -> tuple[float, float]:
    v, c = _clean(values, clusters)
    if len(v) == 0:
        return math.nan, math.nan
    codes, G = _codes(c)
    if G < 2:
        return math.nan, math.nan
    sums = np.bincount(codes, weights=v, minlength=G)
    cnts = np.bincount(codes, minlength=G).astype(float)
    rng = np.random.default_rng(seed)
    draws = rng.integers(0, G, size=(n_boot, G), dtype=np.int32)
    means = sums[draws].sum(axis=1) / cnts[draws].sum(axis=1)
    lo, hi = np.quantile(means, [alpha / 2, 1 - alpha / 2])
    return float(lo), float(hi)


def week_of(date) -> str:
    return pd.Timestamp(date).strftime("%G-W%V")
