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
    out: list[tuple] = []
    for ts, close in df["Close"].items():
        d = ts.strftime("%Y-%m-%d")
        if d > end:
            continue
        try:
            out.append((d, float(close)))
        except (TypeError, ValueError):
            continue
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
        if d and c is not None:
            out.append((d, float(c)))
    return out


def _fetch(ticker: str, start: str, end: str) -> list[tuple]:
    try:
        return _fetch_tiingo(ticker, start, end) if _TIINGO_KEY else _fetch_yfinance(ticker, start, end)
    except Exception as exc:                       # per-ticker isolation: one bad fetch can't stop the sweep
        log.warning("prices: fetch %s failed: %s", ticker, exc)
        return []


def ensure_prices(conn, tickers, start: str, end: str) -> dict:
    """Ensure each ticker's daily adj-close is cached over [start, end]. Only fetches tickers
    whose cached data does not already reach `end` (CLAUDE.md #6). Returns a summary."""
    summary = {"requested": 0, "fetched": 0, "cached": 0, "empty": 0, "rows": 0}
    seen: set[str] = set()
    for t in tickers:
        if not t or t in seen:
            continue
        seen.add(t)
        summary["requested"] += 1
        _, cmax = store.cached_price_range(conn, t)
        if cmax is not None and cmax >= end:
            summary["cached"] += 1
            continue
        # Fetch from just after what we have (incremental) or the full window on a cold ticker.
        fetch_start = cmax if cmax else start
        rows = _fetch(t, fetch_start, end)
        if not rows:
            summary["empty"] += 1
            continue
        summary["rows"] += store.upsert_prices(conn, t, rows)
        summary["fetched"] += 1
    log.info("prices(%s): %s", provider(), summary)
    return summary
