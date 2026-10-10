"""Intraday-entry outcomes: next-bar entry after news+latency, daily-close exits. No network."""
from __future__ import annotations

import pytest

from schema import store
from pipeline import intraday_outcomes as io


@pytest.fixture
def conn(monkeypatch):
    c = store.connect(":memory:")
    store.init_db(c)
    monkeypatch.setattr(io.intraday_prices, "ensure_intraday", lambda *a, **k: {})
    monkeypatch.setattr(io.prices, "ensure_prices", lambda *a, **k: {})
    monkeypatch.setattr(io, "LATENCY_MIN", 60)
    monkeypatch.setattr(io, "COST_BPS", 10.0)
    return c


def _seed(conn, direction="positive", published_ts="2026-08-11T14:30:00+00:00", hop=1):
    store.upsert_node(conn, {"id": "cik:0000000001", "type": "Company", "name": "A",
                             "tickers": ["AAPL"], "sector": "Information Technology"})  # bench XLK
    store.insert_event(conn, {"id": "e1", "headline": "h", "source": "GDELT-GKG", "url": "u",
                              "category": "c", "published_at": published_ts[:10], "published_ts": published_ts,
                              "seed_entity": "E", "seed_node_id": "cik:1", "status": "traced"})
    store.write_event_impacts(conn, "e1", [
        {"node_id": "cik:0000000001", "direction": direction, "magnitude": 0.5, "hop": hop, "reasoning": "r"}])
    # hourly bars 10:30 + 11:30 ET; 14:30 UTC = 10:30 ET, +60min latency -> entry at the 11:30 bar
    store.upsert_intraday(conn, "AAPL", [("2026-08-11T10:30:00-04:00", 100.0, 100.0),
                                         ("2026-08-11T11:30:00-04:00", 105.0, 106.0)])
    store.upsert_intraday(conn, "XLK", [("2026-08-11T10:30:00-04:00", 200.0, 200.0),
                                        ("2026-08-11T11:30:00-04:00", 201.0, 201.0)])
    store.upsert_prices(conn, "AAPL", [("2026-08-11", 110.0, 105.0), ("2026-08-12", 121.0, 112.0)])
    store.upsert_prices(conn, "XLK", [("2026-08-11", 203.0, 201.0), ("2026-08-12", 206.0, 204.0)])


def test_entry_is_next_bar_after_news_plus_latency(conn):
    _seed(conn)
    io.compute_intraday_outcomes(conn, horizons=(0,))
    r = conn.execute("SELECT * FROM intraday_outcomes").fetchone()
    assert r["entry_ts"] == "2026-08-11T11:30:00-04:00"      # 10:30 news + 60min -> 11:30 bar
    assert r["entry_date"] == "2026-08-11"
    assert r["latency_min"] == 60
    # capturable = same-session close / entry-bar open - 1, minus benchmark
    assert r["stock_ret"] == pytest.approx(110 / 105 - 1, abs=1e-6)
    assert r["benchmark_ret"] == pytest.approx(203 / 201 - 1, abs=1e-6)
    assert r["abn_ret"] == pytest.approx((110 / 105 - 1) - (203 / 201 - 1), abs=1e-6)
    assert r["hit"] == 1 and r["net_hit"] == 1


def test_daily_close_exits_from_entry_day(conn):
    _seed(conn)
    io.compute_intraday_outcomes(conn, horizons=(0, 1))
    rows = {r["horizon_days"]: r for r in conn.execute("SELECT * FROM intraday_outcomes")}
    assert set(rows) == {0, 1}
    assert rows[1]["stock_ret"] == pytest.approx(121 / 105 - 1, abs=1e-6)   # +1 trading day close


def test_only_events_with_published_ts_are_eligible(conn):
    _seed(conn)
    conn.execute("UPDATE events SET published_ts=NULL WHERE id='e1'")
    io.compute_intraday_outcomes(conn, horizons=(0,))
    assert conn.execute("SELECT COUNT(*) c FROM intraday_outcomes").fetchone()["c"] == 0


def test_immature_when_action_time_after_all_bars(conn):
    # news at 20:00 UTC (16:00 ET) + 60min -> 17:00 ET, after the last seeded bar (11:30) -> no entry
    _seed(conn, published_ts="2026-08-11T20:00:00+00:00")
    io.compute_intraday_outcomes(conn, horizons=(0,))
    assert conn.execute("SELECT COUNT(*) c FROM intraday_outcomes").fetchone()["c"] == 0
