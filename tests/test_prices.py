"""Price layer: caching/incremental fetch, dedupe, benchmark map, symbol normalization.
No network — the provider fetch is monkeypatched."""
from __future__ import annotations

from schema import store
from pipeline import prices


def _mem():
    conn = store.connect(":memory:")
    store.init_db(conn)
    return conn


def test_benchmark_for_sector_and_fallback():
    assert prices.benchmark_for_sector("Information Technology") == "XLK"
    assert prices.benchmark_for_sector("Energy") == "XLE"
    assert prices.benchmark_for_sector(None) == "SPY"
    assert prices.benchmark_for_sector("Nonsense Sector") == "SPY"


def test_yahoo_symbol_dot_to_dash():
    assert prices._yahoo_symbol("BRK.B") == "BRK-B"
    assert prices._yahoo_symbol("AAPL") == "AAPL"


def test_ensure_prices_caches_and_is_incremental(monkeypatch):
    conn = _mem()
    calls = []

    def fake_fetch(ticker, start, end):
        calls.append((ticker, start, end))
        # Two days of synthetic data regardless of window.
        return [("2026-10-07", 100.0), ("2026-10-08", 101.0)]

    monkeypatch.setattr(prices, "_fetch", fake_fetch)
    s1 = prices.ensure_prices(conn, ["AAPL"], "2026-10-07", "2026-10-08")
    assert s1["fetched"] == 1 and s1["rows"] == 2
    assert store.price_series(conn, "AAPL") == [("2026-10-07", 100.0), ("2026-10-08", 101.0)]

    # Second call to the same end date: cached (cmax >= end) -> no fetch.
    calls.clear()
    s2 = prices.ensure_prices(conn, ["AAPL"], "2026-10-07", "2026-10-08")
    assert s2["cached"] == 1 and s2["fetched"] == 0 and calls == []


def test_ensure_prices_incremental_fetch_starts_at_cached_max(monkeypatch):
    conn = _mem()
    store.upsert_prices(conn, "MSFT", [("2026-10-07", 50.0)])
    seen = {}

    def fake_fetch(ticker, start, end):
        seen["start"] = start
        return [("2026-10-08", 51.0)]

    monkeypatch.setattr(prices, "_fetch", fake_fetch)
    prices.ensure_prices(conn, ["MSFT"], "2026-10-01", "2026-10-08")
    # Incremental: fetch begins at the cached max, not the original start.
    assert seen["start"] == "2026-10-07"
    assert store.price_series(conn, "MSFT") == [("2026-10-07", 50.0), ("2026-10-08", 51.0)]


def test_ensure_prices_empty_fetch_is_counted_not_fatal(monkeypatch):
    conn = _mem()
    monkeypatch.setattr(prices, "_fetch", lambda t, s, e: [])
    summary = prices.ensure_prices(conn, ["DELISTED"], "2026-10-07", "2026-10-08")
    assert summary["empty"] == 1 and summary["fetched"] == 0
    assert store.price_series(conn, "DELISTED") == []


def test_ensure_prices_dedupes_requested_tickers(monkeypatch):
    conn = _mem()
    monkeypatch.setattr(prices, "_fetch", lambda t, s, e: [("2026-10-07", 1.0)])
    summary = prices.ensure_prices(conn, ["AAPL", "AAPL", ""], "2026-10-07", "2026-10-07")
    assert summary["requested"] == 1
