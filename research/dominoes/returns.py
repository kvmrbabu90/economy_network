"""Returns panel on SPY's trading calendar, and the abnormal-return models (spec §4, §8, §13).

- build_panel: wide close / volume / simple returns aligned to SPY's dates, with the data-error rule
  (§13.3) applied to raw returns before anything is estimated.
- rolling_ols: OLS of y on [1, X] over rows [e-270, e-21] for every event row e, computed from
  cumulative cross-products (O(T) per series). Needs at least 200 valid rows, else NaN.
- AbnormalModel: 'market' (SPY market model; primary), 'sector_etf' (plain excess return over the firm's
  sector ETF; robustness 1) and 'ff6' (Fama-French 5 + momentum on excess returns; robustness 2).
  Window CARs come from cumulative sums in O(1) per (firm, event, window), and are NaN unless every day
  of the window has a valid return (§13.4) and the firm had a valid estimation window at the event.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from research.dominoes import config

FF6_COLUMNS = ("mkt_rf", "smb", "hml", "rmw", "cma", "mom")


@dataclass
class Panel:
    dates: pd.DatetimeIndex
    symbols: list[str]
    col: dict[str, int]
    close: np.ndarray        # (T, N) total-return-adjusted close
    volume: np.ndarray       # (T, N)
    r: np.ndarray            # (T, N) cleaned simple returns; NaN when unknown
    n_bad_prints: int
    dvx: np.ndarray          # (T, N) traded dollars per day: unadjusted close x volume (raw snapshot)


def clean_bad_prints(r: np.ndarray) -> tuple[np.ndarray, int]:
    """§13.3: |r_t| > 50% and the next day returns the price to within 5% of its pre-move level ->
    a bad print; both days' returns become NaN. Flags are judged on the raw series."""
    out = np.array(r, dtype=float, copy=True)
    if out.shape[0] < 2:
        return out, 0
    cur, nxt = out[:-1], out[1:]
    with np.errstate(invalid="ignore"):
        flag = ((np.abs(cur) > config.DATA_ERROR_ABS)
                & (np.abs((1.0 + cur) * (1.0 + nxt) - 1.0) < config.DATA_ERROR_REVERT)
                & np.isfinite(cur) & np.isfinite(nxt))
    t_idx, i_idx = np.nonzero(flag)
    out[t_idx, i_idx] = np.nan
    out[t_idx + 1, i_idx] = np.nan
    return out, int(flag.sum())


def _wide(p: pd.DataFrame, value: str, cal: pd.DatetimeIndex, symbols: list[str]) -> np.ndarray:
    # copy=True: pandas 3 copy-on-write hands back read-only views from to_numpy().
    return (p.pivot_table(index="date", columns="symbol", values=value, aggfunc="last")
            .reindex(index=cal, columns=symbols).to_numpy(dtype=float, copy=True))


def build_panel(prices: pd.DataFrame, market: str = config.MARKET_TICKER, raw: pd.DataFrame | None = None) -> Panel:
    """`raw` (symbol, date, close_raw, volume) is the unadjusted-close snapshot used for dollar volume; a symbol
    absent from it gets NaN dollar volume (never the adjusted close). Without `raw` (synthetic tests, where the
    adjusted and raw closes coincide) dollar volume is close x volume."""
    p = prices.copy()
    p["date"] = pd.to_datetime(p["date"]).dt.normalize()
    cal = pd.DatetimeIndex(sorted(p.loc[p["symbol"] == market, "date"].unique()))
    symbols = sorted(p["symbol"].unique())
    close = _wide(p, "close", cal, symbols)
    vol = _wide(p, "volume", cal, symbols)
    if raw is None:
        dvx = close * vol
    else:
        q = raw.copy()
        q["date"] = pd.to_datetime(q["date"]).dt.normalize()
        dvx = _wide(q, "close_raw", cal, symbols) * _wide(q, "volume", cal, symbols)
    r = np.full_like(close, np.nan)
    with np.errstate(divide="ignore", invalid="ignore"):
        r[1:] = close[1:] / close[:-1] - 1.0
    r[~np.isfinite(r)] = np.nan
    r, n_bad = clean_bad_prints(r)
    return Panel(dates=cal, symbols=symbols, col={s: i for i, s in enumerate(symbols)},
                 close=close, volume=vol, r=r, n_bad_prints=n_bad, dvx=dvx)


def _window_bounds(T: int, est: tuple[int, int]):
    e = np.arange(T)
    a, b = e + est[0], e + est[1]          # inclusive rows [e-270, e-21]
    ok = a >= 0
    return e[ok], a[ok], b[ok]


def rolling_ols(y: np.ndarray, X: np.ndarray, est: tuple[int, int] = config.EST_WINDOW,
                min_n: int = config.MIN_EST_DAYS):
    """Per event row e: OLS of y on [1, X] over rows [e+est0, e+est1]. Returns coef (T, 1+K),
    sigma (T,) = sqrt(SSR / (n - 1 - K)), n (T,) valid rows. NaN where n < min_n or the window
    starts before the data."""
    y = np.asarray(y, dtype=float)
    X = np.asarray(X, dtype=float)
    if X.ndim == 1:
        X = X[:, None]
    T, K = X.shape
    p = K + 1
    Z = np.column_stack([np.ones(T), X])
    valid = np.isfinite(y) & np.isfinite(X).all(axis=1)
    Zm = np.where(valid[:, None], Z, 0.0)
    ym = np.where(valid, y, 0.0)
    ZZ = np.zeros((T + 1, p, p))
    ZZ[1:] = np.cumsum(Zm[:, :, None] * Zm[:, None, :], axis=0)
    Zy = np.zeros((T + 1, p))
    Zy[1:] = np.cumsum(Zm * ym[:, None], axis=0)
    yy = np.zeros(T + 1)
    yy[1:] = np.cumsum(ym * ym)
    nn = np.zeros(T + 1, dtype=np.int64)
    nn[1:] = np.cumsum(valid)

    coef = np.full((T, p), np.nan)
    sigma = np.full(T, np.nan)
    n = np.zeros(T, dtype=np.int64)
    ei, ai, bi = _window_bounds(T, est)
    if len(ei) == 0:
        return coef, sigma, n
    cnt = nn[bi + 1] - nn[ai]
    n[ei] = cnt
    good = cnt >= min_n
    if not good.any():
        return coef, sigma, n
    A = ZZ[bi + 1][good] - ZZ[ai][good]
    rhs = Zy[bi + 1][good] - Zy[ai][good]
    try:
        b = np.linalg.solve(A, rhs[..., None])[..., 0]
    except np.linalg.LinAlgError:
        b = np.stack([np.linalg.lstsq(m, v, rcond=None)[0] for m, v in zip(A, rhs)])
    ssr = (yy[bi + 1][good] - yy[ai][good]) - np.einsum("ij,ij->i", b, rhs)
    idx = ei[good]
    coef[idx] = b
    sigma[idx] = np.sqrt(np.maximum(ssr, 0.0) / (cnt[good] - p))
    return coef, sigma, n


def _rolling_excess(d: np.ndarray, est: tuple[int, int] = config.EST_WINDOW,
                    min_n: int = config.MIN_EST_DAYS):
    """Excess-return 'model' (robustness 1): AR = d, no parameters. coef0 = 0 where the estimation
    window has >= min_n valid days (NaN otherwise), sigma = sample std of d over the window."""
    T = len(d)
    valid = np.isfinite(d)
    dm = np.where(valid, d, 0.0)
    c1 = np.concatenate([[0.0], np.cumsum(dm)])
    c2 = np.concatenate([[0.0], np.cumsum(dm * dm)])
    cn = np.concatenate([[0], np.cumsum(valid)])
    coef = np.full((T, 1), np.nan)
    sigma = np.full(T, np.nan)
    n = np.zeros(T, dtype=np.int64)
    ei, ai, bi = _window_bounds(T, est)
    cnt = cn[bi + 1] - cn[ai]
    n[ei] = cnt
    good = cnt >= min_n
    s1 = c1[bi + 1][good] - c1[ai][good]
    s2 = c2[bi + 1][good] - c2[ai][good]
    k = cnt[good]
    coef[ei[good], 0] = 0.0
    sigma[ei[good]] = np.sqrt(np.maximum(s2 - s1 * s1 / k, 0.0) / (k - 1))
    return coef, sigma, n


class AbnormalModel:
    """Event-specific abnormal returns: AR(i, t; e) = y(i, t) - (coef(i, e) . [1, X(t)])."""

    def __init__(self, panel: Panel, spec: str = "market", factors: pd.DataFrame | None = None,
                 etf_of: dict[str, str] | None = None):
        self.panel, self.spec = panel, spec
        T, N = panel.r.shape
        self.T, self.N = T, N
        if spec == "market":
            self.X = panel.r[:, panel.col[config.MARKET_TICKER]][:, None]
            self.Y = panel.r
        elif spec == "ff6":
            if factors is None:
                raise ValueError("ff6 needs factors")
            f = factors.copy()
            f["date"] = pd.to_datetime(f["date"]).dt.normalize()
            f = f.set_index("date").reindex(panel.dates)
            self.X = f[list(FF6_COLUMNS)].to_numpy(dtype=float)
            self.Y = panel.r - f["rf"].to_numpy(dtype=float)[:, None]
        elif spec == "sector_etf":
            self.X = None
            self.Y = np.full_like(panel.r, np.nan)
            for sym, etf in (etf_of or {}).items():
                if sym in panel.col and etf in panel.col:
                    self.Y[:, panel.col[sym]] = panel.r[:, panel.col[sym]] - panel.r[:, panel.col[etf]]
        else:
            raise ValueError(f"unknown model spec {spec!r}")

        p = 1 + (self.X.shape[1] if self.X is not None else 0)
        self.coef = np.full((N, T, p), np.nan)
        self.sigma = np.full((N, T), np.nan)
        self.nest = np.zeros((N, T), dtype=np.int64)
        market_col = panel.col.get(config.MARKET_TICKER)
        for i in range(N):
            y = self.Y[:, i]
            if not np.isfinite(y).any() or (spec != "sector_etf" and i == market_col):
                continue
            if spec == "sector_etf":
                c, s, n = _rolling_excess(y)
            else:
                c, s, n = rolling_ols(y, self.X)
            self.coef[i], self.sigma[i], self.nest[i] = c, s, n

        yv = np.isfinite(self.Y)
        self.Ycum = np.zeros((T + 1, N))
        self.Ycum[1:] = np.cumsum(np.where(yv, self.Y, 0.0), axis=0)
        self.Vcum = np.zeros((T + 1, N), dtype=np.int64)
        self.Vcum[1:] = np.cumsum(yv, axis=0)
        if self.X is not None:
            xv = np.isfinite(self.X).all(axis=1)
            self.Xcum = np.zeros((T + 1, self.X.shape[1]))
            self.Xcum[1:] = np.cumsum(np.where(xv[:, None], self.X, 0.0), axis=0)
            self.XVcum = np.concatenate([[0], np.cumsum(xv)])

    def car(self, cols, e, a: int, b: int) -> np.ndarray:
        """Sum of AR(i, t; e) over t in [e+a, e+b] for each (cols[k], e[k]); NaN unless complete."""
        cols = np.asarray(cols, dtype=np.int64)
        e = np.asarray(e, dtype=np.int64)
        out = np.full(len(cols), np.nan)
        lo, hi, L = e + a, e + b, b - a + 1
        inr = (lo >= 0) & (hi < self.T)
        if not inr.any():
            return out
        c, lo_, hi_, ee = cols[inr], lo[inr], hi[inr], e[inr]
        sy = self.Ycum[hi_ + 1, c] - self.Ycum[lo_, c]
        ok = (self.Vcum[hi_ + 1, c] - self.Vcum[lo_, c]) == L
        coef = self.coef[c, ee]
        expected = coef[:, 0] * L
        if self.X is not None:
            xs = self.Xcum[hi_ + 1] - self.Xcum[lo_]
            expected = expected + np.einsum("ij,ij->i", coef[:, 1:], xs)
            ok &= (self.XVcum[hi_ + 1] - self.XVcum[lo_]) == L
        val = sy - expected
        val[~ok] = np.nan
        out[inr] = val
        return out

    def path(self, cols, e, lo: int, hi: int) -> np.ndarray:
        """Daily AR(i, e+d; e) for d in [lo, hi]: shape (len(cols), hi-lo+1); NaN outside the data."""
        cols = np.asarray(cols, dtype=np.int64)
        e = np.asarray(e, dtype=np.int64)
        offs = np.arange(lo, hi + 1)
        tt = e[:, None] + offs[None, :]
        inr = (tt >= 0) & (tt < self.T)
        ttc = np.clip(tt, 0, self.T - 1)
        y = self.Y[ttc, cols[:, None]]
        coef = self.coef[cols, e]
        expected = np.repeat(coef[:, [0]], len(offs), axis=1)
        if self.X is not None:
            expected = expected + np.einsum("mdk,mk->md", self.X[ttc], coef[:, 1:])
        ar = y - expected
        ar[~inr] = np.nan
        return ar

    def ar0(self, cols, e) -> np.ndarray:
        return self.path(cols, e, 0, 0)[:, 0]


def _window_mean(values: np.ndarray, cols, e, win: tuple[int, int], min_n: int) -> np.ndarray:
    T = values.shape[0]
    cols = np.asarray(cols, dtype=np.int64)
    e = np.asarray(e, dtype=np.int64)
    v = np.isfinite(values)
    cs = np.zeros((T + 1, values.shape[1]))
    cs[1:] = np.cumsum(np.where(v, values, 0.0), axis=0)
    cn = np.zeros((T + 1, values.shape[1]), dtype=np.int64)
    cn[1:] = np.cumsum(v, axis=0)
    lo, hi = e + win[0], e + win[1]
    out = np.full(len(cols), np.nan)
    inr = (lo >= 0) & (hi < T)
    c, lo_, hi_ = cols[inr], lo[inr], hi[inr]
    n = cn[hi_ + 1, c] - cn[lo_, c]
    s = cs[hi_ + 1, c] - cs[lo_, c]
    with np.errstate(invalid="ignore", divide="ignore"):
        m = np.where(n >= min_n, s / np.maximum(n, 1), np.nan)
    out[inr] = m
    return out


def dollar_volume(panel: Panel, cols, e) -> np.ndarray:
    """Average traded dollars (unadjusted close x volume) over [e-80, e-21]; NaN with fewer than 20 valid
    days (§13.5)."""
    return _window_mean(panel.dvx, cols, e, config.DV_WINDOW, config.MIN_DV_DAYS)


def abnormal_volume(panel: Panel, cols, e) -> np.ndarray:
    """Day-0 volume over its [e-80, e-21] average (§13.7); NaN when either is unavailable."""
    base = _window_mean(panel.volume, cols, e, config.DV_WINDOW, config.MIN_DV_DAYS)
    cols = np.asarray(cols, dtype=np.int64)
    e = np.asarray(e, dtype=np.int64)
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(base > 0, panel.volume[e, cols] / base, np.nan)
