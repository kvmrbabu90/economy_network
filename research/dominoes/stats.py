"""Pure statistics for the domino study (spec §5).

- clustered_mean: mean with a CR1 cluster-robust SE, se^2 = G/(G-1) * sum_g (sum_{i in g} (v_i - mean))^2 / n^2;
  t-based p-values with G-1 degrees of freedom. NaN values (and clusters left empty) are dropped.
- clustered_ols: y = a + b x with the CR1 sandwich (Stata convention: G/(G-1) * (n-1)/(n-k)); for k=1 this
  reduces to clustered_mean's SE.
- holm: Holm-Bonferroni step-down rejections in input order.
- block_bootstrap_ci: percentile CI of the pooled mean, resampling whole clusters with replacement.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
from scipy import stats as sps


def _clean(values, clusters):
    v = np.asarray(values, dtype=float)
    c = np.asarray(clusters)
    ok = np.isfinite(v)
    return v[ok], c[ok]


def _codes(c) -> tuple[np.ndarray, int]:
    codes, uniq = pd.factorize(pd.Series(c))
    return codes, len(uniq)


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
    se = math.nan
    if G >= 2:
        dev = np.bincount(codes, weights=v - mean, minlength=G)
        se = math.sqrt(G / (G - 1) * float((dev ** 2).sum()) / n ** 2)
    t = mean / se if np.isfinite(se) and se > 0 else math.nan
    return {"mean": mean, "se": se, "t": t, "n": n, "g": G, "df": G - 1, "p_two": two_sided_p(t, G - 1)}


def clustered_ols(y, x, clusters) -> dict:
    y = np.asarray(y, dtype=float)
    x = np.asarray(x, dtype=float)
    c = np.asarray(clusters)
    ok = np.isfinite(y) & np.isfinite(x)
    y, x, c = y[ok], x[ok], c[ok]
    n, k = len(y), 2
    out = {"slope": math.nan, "intercept": math.nan, "se": math.nan, "t": math.nan, "n": n, "g": 0,
           "df": 0, "p_two": math.nan}
    if n < 3:
        return out
    codes, G = _codes(c)
    X = np.column_stack([np.ones(n), x])
    bread = np.linalg.inv(X.T @ X)
    b = bread @ (X.T @ y)
    u = y - X @ b
    S = np.zeros((G, k))
    np.add.at(S, codes, X * u[:, None])
    V = bread @ (S.T @ S) @ bread * (G / (G - 1) if G > 1 else math.nan) * ((n - 1) / (n - k))
    se = math.sqrt(V[1, 1]) if np.isfinite(V[1, 1]) and V[1, 1] > 0 else math.nan
    t = b[1] / se if np.isfinite(se) else math.nan
    out.update({"slope": float(b[1]), "intercept": float(b[0]), "se": se, "t": t, "g": G, "df": G - 1,
                "p_two": two_sided_p(t, G - 1)})
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
    sums = np.bincount(codes, weights=v, minlength=G)
    cnts = np.bincount(codes, minlength=G).astype(float)
    rng = np.random.default_rng(seed)
    draws = rng.integers(0, G, size=(n_boot, G), dtype=np.int32)
    means = sums[draws].sum(axis=1) / cnts[draws].sum(axis=1)
    lo, hi = np.quantile(means, [alpha / 2, 1 - alpha / 2])
    return float(lo), float(hi)


def week_of(date) -> str:
    return pd.Timestamp(date).strftime("%G-W%V")
