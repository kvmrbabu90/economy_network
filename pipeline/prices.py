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
            c = float(close)
        except (TypeError, ValueError):
            continue
        if c != c:          # NaN (yfinance emits it for untraded/pre-listing days)
            continue
        out.append((d, c))
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
    """Ensure each ticker's daily adj-close is cached over the WHOLE [start, end] window, only
    fetching the missing span(s) (CLAUDE.md #6). Covers BOTH ends: a historical backfill asks for
    an earlier `start` than a ticker whose cache begins later (e.g. first seen via recent news),
    so we must backfill the earlier history too — not just extend forward — or an old-dated
    signal's base date lands on the wrong (too-late) day and reads immature. Returns a summary."""
    summary = {"requested": 0, "fetched": 0, "cached": 0, "empty": 0, "rows": 0}
    seen: set[str] = set()
    for t in tickers:
        if not t or t in seen:
            continue
        seen.add(t)
        summary["requested"] += 1
        cmin, cmax = store.cached_price_range(conn, t)
        # Gaps needed so the cache fully spans [start, end]. yfinance ignores `end` (fetches
        # start..latest, filtered to <= end), so each gap fetch is precise after that filter.
        if cmin is None:
            gaps = [(start, end)]                       # cold ticker: full window
        else:
            gaps = []
            if start < cmin:
                gaps.append((start, cmin))              # earlier history (backfill)
            if end > cmax:
                gaps.append((cmax, end))                # newer bars (forward)
        if not gaps:
            summary["cached"] += 1
            continue
        got = 0
        any_rows = False
        for g_start, g_end in gaps:
            rows = _fetch(t, g_start, g_end)
            if rows:
                got += store.upsert_prices(conn, t, rows)
                any_rows = True
        if any_rows:
            summary["rows"] += got
            summary["fetched"] += 1
        else:
            summary["empty"] += 1
    log.info("prices(%s): %s", provider(), summary)
    return summary
