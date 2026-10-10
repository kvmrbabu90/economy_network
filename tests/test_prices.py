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
    prices.ensure_prices(conn, ["MSFT"], "2026-10-07", "2026-10-08")   # start == cached min
    # Forward-only: one fetch beginning at the cached max (no earlier history needed).
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


def test_ensure_prices_backfills_earlier_history(monkeypatch):
    # A historical backfill asks for an earlier `start` than a ticker first seen via recent news:
    # the cache (Sep 30 on) must be extended BACKWARD to August, not just forward.
    conn = _mem()
    store.upsert_prices(conn, "NVDA", [("2026-09-30", 100.0), ("2026-10-01", 101.0)])
    calls = []

    def fake(t, s, e):
        calls.append((s, e))
        if s == "2026-08-01":                     # the earlier gap -> return August history
            return [("2026-08-03", 90.0), ("2026-08-04", 91.0)]
        return []

    monkeypatch.setattr(prices, "_fetch", fake)
    prices.ensure_prices(conn, ["NVDA"], "2026-08-01", "2026-10-01")
    assert calls[0][0] == "2026-08-01"             # single fetch starts at the earlier start
    ser = store.price_series(conn, "NVDA")
    assert ser[0][0] == "2026-08-03"               # August now present, ahead of Sep 30


def test_ensure_prices_no_gap_when_fully_covered(monkeypatch):
    conn = _mem()
    store.upsert_prices(conn, "AAPL", [("2026-09-01", 1.0), ("2026-10-09", 2.0)])
    monkeypatch.setattr(prices, "_fetch", lambda *a: (_ for _ in ()).throw(AssertionError("should not fetch")))
    summary = prices.ensure_prices(conn, ["AAPL"], "2026-09-15", "2026-10-09")   # inside cached span
    assert summary["cached"] == 1 and summary["fetched"] == 0


def test_ensure_prices_refresh_days_refetches_trailing_window(monkeypatch):
    # Adjusted closes are mutable (restated on each split/dividend). refresh_days must re-fetch
    # the trailing window EVEN when fully cached, so a signal's base + end share one baseline.
    conn = _mem()
    store.upsert_prices(conn, "AAPL", [("2026-09-01", 1.0), ("2026-10-09", 2.0)])
    calls = []

    def fake(t, s, e):
        calls.append(s)
        return [("2026-10-09", 2.5)]               # a re-stated (adjusted) close

    monkeypatch.setattr(prices, "_fetch", fake)
    summary = prices.ensure_prices(conn, ["AAPL"], "2026-10-05", "2026-10-09", refresh_days=30)
    assert summary["fetched"] == 1                 # NOT skipped despite full coverage
    assert calls and calls[0] == "2026-09-09"      # fetched from end - 30 days
    assert dict(store.price_series(conn, "AAPL"))["2026-10-09"] == 2.5   # overwritten baseline


def test_fetch_yfinance_skips_nan(monkeypatch):
    # yfinance emits NaN closes for untraded/pre-listing days; they must be dropped, not inserted
    # (the prices.adj_close column is NOT NULL). Mock yfinance with a real pandas frame.
    import sys
    import types
    import pandas as pd
    idx = pd.to_datetime(["2026-08-03", "2026-08-04", "2026-08-05"])
    df = pd.DataFrame({"Close": [90.0, float("nan"), 92.0]}, index=idx)

    class _FakeTicker:
        def __init__(self, _t):
            pass

        def history(self, **_k):
            return df

    monkeypatch.setitem(sys.modules, "yfinance", types.SimpleNamespace(Ticker=_FakeTicker))
    out = prices._fetch_yfinance("X", "2026-08-01", "2026-08-31")
    # 3-tuples (date, close, open); no Open column here -> open is None; NaN close row dropped
    assert out == [("2026-08-03", 90.0, None), ("2026-08-05", 92.0, None)]


def test_fetch_yfinance_reads_adjusted_open(monkeypatch):
    import sys
    import types
    import pandas as pd
    idx = pd.to_datetime(["2026-08-03", "2026-08-04"])
    df = pd.DataFrame({"Open": [89.0, float("nan")], "Close": [90.0, 91.0]}, index=idx)

    class _FakeTicker:
        def __init__(self, _t):
            pass

        def history(self, **_k):
            return df

    monkeypatch.setitem(sys.modules, "yfinance", types.SimpleNamespace(Ticker=_FakeTicker))
    out = prices._fetch_yfinance("X", "2026-08-01", "2026-08-31")
    assert out == [("2026-08-03", 90.0, 89.0), ("2026-08-04", 91.0, None)]   # NaN open -> None
