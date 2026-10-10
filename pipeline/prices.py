"""Provider-agnostic daily adjusted-close prices for the signal-reliability layer.

Default provider is **yfinance** (free, no API key); **Tiingo** is used automatically when
TIINGO_API_KEY is set (cleaner split+dividend adjustment). Prices are cached in the `prices`
SQLite table — the cache IS the table, so we never re-fetch a ticker's already-stored history
(CLAUDE.md #6). Abnormal returns use SPDR sector ETFs as the benchmark (SPY fallback).

    from pipeline import prices
    prices.ensure_prices(conn, ["AAPL", "MSFT"], "2026-07-01", "2026-10-10")
    series = store.price_series(conn, "AAPL")   # [(date, adj_close), ...]
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta
from typing import Optional

import requests

from schema import store

log = logging.getLogger(__name__)

_UA = {"User-Agent": "econgraph-reliability/1.0 (+research; contact kondaru.mk@gmail.com)"}
_TIINGO_KEY = os.environ.get("TIINGO_API_KEY", "").strip()
# Politeness: small gap between provider requests so a 573-ticker sweep stays well-mannered.
_REQ_GAP_S = float(os.environ.get("PRICE_REQ_GAP_S", "0.3"))
_TIMEOUT_S = float(os.environ.get("PRICE_TIMEOUT_S", "15"))
_last_req = 0.0

# GICS sector -> SPDR sector ETF (the abnormal-return benchmark). SPY is the fallback.
SECTOR_ETF = {
    "Information Technology": "XLK", "Financials": "XLF", "Health Care": "XLV",
    "Consumer Discretionary": "XLY", "Consumer Staples": "XLP", "Energy": "XLE",
    "Industrials": "XLI", "Materials": "XLB", "Real Estate": "XLRE",
    "Utilities": "XLU", "Communication Services": "XLC",
}
MARKET_ETF = "SPY"


def provider() -> str:
    return "tiingo" if _TIINGO_KEY else "yfinance"


def benchmark_for_sector(sector: Optional[str]) -> str:
    return SECTOR_ETF.get(sector or "", MARKET_ETF)


def _throttle() -> None:
    global _last_req
    wait = _REQ_GAP_S - (time.monotonic() - _last_req)
    if wait > 0:
        time.sleep(wait)
    _last_req = time.monotonic()


def _yahoo_symbol(ticker: str) -> str:
    # Yahoo uses a dash for share-class dots: BRK.B -> BRK-B, BF.B -> BF-B.
    return ticker.replace(".", "-")


def _fetch_yfinance(ticker: str, start: str, end: str) -> list[tuple]:
    # Keyless default. auto_adjust=True -> Close is split+dividend adjusted. Yahoo's `end` is
    # exclusive, so we omit it and fetch start..latest (upsert dedupes), ensuring the newest day.
    import yfinance as yf
    _throttle()
    df = yf.Ticker(_yahoo_symbol(ticker)).history(start=start, auto_adjust=True, raise_errors=False)
    if df is None or df.empty or "Close" not in df.columns:
        return []
    has_open = "Open" in df.columns
    out: list[tuple] = []
    for ts, row in df.iterrows():
        d = ts.strftime("%Y-%m-%d")
        if d > end:
            continue
        try:
            c = float(row["Close"])
        except (TypeError, ValueError, KeyError):
            continue
        if c != c:          # NaN (yfinance emits it for untraded/pre-listing days)
            continue
        o = None
        if has_open:
            try:
                ov = float(row["Open"])
                o = ov if ov == ov else None     # drop NaN open
            except (TypeError, ValueError, KeyError):
                o = None
        out.append((d, c, o))   # (date, adj_close, adj_open) — auto_adjust scales OHLC together
    return out


def _fetch_tiingo(ticker: str, start: str, end: str) -> list[tuple]:
    url = (f"https://api.tiingo.com/tiingo/daily/{ticker}/prices"
           f"?startDate={start}&endDate={end}&token={_TIINGO_KEY}")
    _throttle()
    r = requests.get(url, headers={**_UA, "Content-Type": "application/json"}, timeout=_TIMEOUT_S)
    if r.status_code != 200:
        return []
    out: list[tuple] = []
    for row in r.json():
        d = (row.get("date") or "")[:10]
        c = row.get("adjClose", row.get("close"))
        o = row.get("adjOpen", row.get("open"))
        if d and c is not None:
            out.append((d, float(c), None if o is None else float(o)))
    return out


def _fetch(ticker: str, start: str, end: str) -> list[tuple]:
    try:
        return _fetch_tiingo(ticker, start, end) if _TIINGO_KEY else _fetch_yfinance(ticker, start, end)
    except Exception as exc:                       # per-ticker isolation: one bad fetch can't stop the sweep
        log.warning("prices: fetch %s failed: %s", ticker, exc)
        return []


def ensure_prices(conn, tickers, start: str, end: str, refresh_days: int = 0) -> dict:
    """Ensure each ticker's daily adj-close is cached and consistent over [start, end].

    Fetches from the EARLIEST date that still needs data, in ONE request per ticker (yfinance
    returns start..latest regardless of `end`), covering: a cold ticker (full window), a backfill
    of earlier history when `start` precedes the cache (else an old-dated signal's base lands on
    the wrong day and reads immature), and a forward extension to `end`.

    `refresh_days` > 0 ALSO re-fetches+overwrites the trailing `refresh_days` calendar window every
    run. Adjusted closes are NOT immutable the way EDGAR filings are (CLAUDE.md #6 was written for
    those): a provider back-adjusts the whole series on each split/dividend, so a base bar cached
    months ago sits on a STALE baseline while a later-fetched end bar is current — biasing the
    multi-day return a signal's base->end span measures. Refreshing a window at least as long as
    the longest horizon keeps base and end on ONE baseline at the moment an outcome matures."""
    summary = {"requested": 0, "fetched": 0, "cached": 0, "empty": 0, "rows": 0}
    refresh_from = None
    if refresh_days and refresh_days > 0:
        refresh_from = (datetime.strptime(end, "%Y-%m-%d") - timedelta(days=refresh_days)).strftime("%Y-%m-%d")
    seen: set[str] = set()
    for t in tickers:
        if not t or t in seen:
            continue
        seen.add(t)
        summary["requested"] += 1
        cmin, cmax = store.cached_price_range(conn, t)
        candidates: list[str] = []
        if cmin is None:
            candidates.append(start)                   # cold ticker: full window
        else:
            if start < cmin:
                candidates.append(start)               # backfill earlier history
            if end > cmax:
                candidates.append(cmax)                # forward extension
        if refresh_from is not None:
            candidates.append(refresh_from)            # re-adjust the trailing window every run
        if not candidates:
            summary["cached"] += 1
            continue
        rows = _fetch(t, min(candidates), end)         # one fetch from the earliest need
        if rows:
            summary["rows"] += store.upsert_prices(conn, t, rows)
            summary["fetched"] += 1
        else:
            summary["empty"] += 1
    log.info("prices(%s): %s", provider(), summary)
    return summary
