"""Intraday hourly bars (yfinance 60m) for the next-bar entry model, cached in prices_intraday.

Only entry-day bars are needed (exits use daily closes), so the fetch window is small. yfinance
60m data is available ~730 days back; bars come back on the exchange calendar in US/Eastern, which
is exactly what the entry logic compares against.

    from pipeline import intraday_prices
    intraday_prices.ensure_intraday(conn, ["AAPL"], "2026-08-11", "2026-08-13")
    bars = store.intraday_bars(conn, "AAPL")   # [(ts_iso_ET, adj_open, adj_close), ...]
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta

from schema import store

log = logging.getLogger(__name__)

_REQ_GAP_S = float(os.environ.get("PRICE_REQ_GAP_S", "0.3"))
_last_req = 0.0


def _throttle() -> None:
    global _last_req
    wait = _REQ_GAP_S - (time.monotonic() - _last_req)
    if wait > 0:
        time.sleep(wait)
    _last_req = time.monotonic()


def _fetch_intraday_yf(ticker: str, start: str, end: str) -> list[tuple]:
    import yfinance as yf
    _throttle()
    # end is exclusive, so push it a day to include the end date's bars. auto_adjust scales OHLC.
    end_excl = (datetime.strptime(end, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    df = yf.Ticker(ticker.replace(".", "-")).history(
        start=start, end=end_excl, interval="60m", auto_adjust=True, raise_errors=False)
    if df is None or df.empty or "Open" not in df.columns or "Close" not in df.columns:
        return []
    out: list[tuple] = []
    for ts, o, c in zip(df.index, df["Open"], df["Close"]):
        try:
            o, c = float(o), float(c)
        except (TypeError, ValueError):
            continue
        if o != o or c != c:          # NaN
            continue
        out.append((ts.isoformat(), o, c))   # tz-aware ISO (US/Eastern) e.g. 2026-08-11T10:30:00-04:00
    return out


def ensure_intraday(conn, tickers, start: str, end: str) -> dict:
    """Cache hourly bars covering [start, end] (YYYY-MM-DD ET dates) for tickers lacking them.
    `end` is the last SIGNAL date we need; we fetch a few days past it so a late-day action that
    rolls entry to the next session is covered. The cache gate compares the last cached bar's date
    against `end` (a real signal date), not against a padded calendar day — else a pad landing on a
    weekend/holiday would make the gate permanently unsatisfiable and re-fetch everything each run."""
    summary = {"requested": 0, "fetched": 0, "cached": 0, "empty": 0, "rows": 0}
    fetch_end = (datetime.strptime(end, "%Y-%m-%d") + timedelta(days=4)).strftime("%Y-%m-%d")
    seen: set[str] = set()
    for t in tickers:
        if not t or t in seen:
            continue
        seen.add(t)
        summary["requested"] += 1
        cmin, cmax = store.cached_intraday_range(conn, t)
        if cmin and cmax and cmin[:10] <= start and cmax[:10] >= end:
            summary["cached"] += 1
            continue
        try:
            rows = _fetch_intraday_yf(t, start, fetch_end)
        except Exception as exc:
            log.warning("intraday: fetch %s failed: %s", t, exc)
            rows = []
        if not rows:
            summary["empty"] += 1
            continue
        summary["rows"] += store.upsert_intraday(conn, t, rows)
        summary["fetched"] += 1
    log.info("intraday-prices: %s", summary)
    return summary
