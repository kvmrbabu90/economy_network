"""Outcome computation, retail framing: next-open entry, capturable vs priced-in decomposition,
net-of-cost, no-look-ahead, maturity gating, clean-subset. Deterministic OHLC; no network."""
from __future__ import annotations

import pytest

from schema import store
from pipeline import signal_outcomes


@pytest.fixture
def conn(monkeypatch):
    c = store.connect(":memory:")
    store.init_db(c)
    monkeypatch.setattr(signal_outcomes.prices, "ensure_prices", lambda *a, **k: {})
    monkeypatch.setattr(signal_outcomes, "COST_BPS", 10.0)   # 10 bps round-trip
    return c


def _company(c, node_id, ticker, sector):
    store.upsert_node(c, {"id": node_id, "type": "Company", "name": node_id,
                          "tickers": [ticker], "sector": sector})


def _event(c, eid, published):
    store.insert_event(c, {"id": eid, "headline": "h", "source": "s", "url": "u",
                           "category": "c", "published_at": published,
                           "seed_entity": "E", "seed_node_id": "cik:1", "status": "traced"})


def _impact(c, eid, node_id, direction, hop, mag=0.5):
    store.write_event_impacts(c, eid, [
        {"node_id": node_id, "direction": direction, "magnitude": mag, "hop": hop, "reasoning": "r"}])


def _px(c, ticker, rows):
    store.upsert_prices(c, ticker, rows)   # rows = [(date, adj_close, adj_open)]


# A clean scenario: pub on a trading day => announcement = 10-07, ENTER next open 10-08.
#   AAPL:  10-06 c100 o100 | 10-07 c110 o105 | 10-08 c121 o115 | 10-09 c133 o120
#   XLK :  10-06 c100 o100 | 10-07 c103 o101 | 10-08 c106 o104 | 10-09 c109 o107
def _seed_clean(conn, direction="positive"):
    _company(conn, "cik:0000000001", "AAPL", "Information Technology")   # benchmark XLK
    _event(conn, "e1", "2026-10-07")
    _impact(conn, "e1", "cik:0000000001", direction, hop=1)
    _px(conn, "AAPL", [("2026-10-06", 100.0, 100.0), ("2026-10-07", 110.0, 105.0),
                       ("2026-10-08", 121.0, 115.0), ("2026-10-09", 133.0, 120.0)])
    _px(conn, "XLK", [("2026-10-06", 100.0, 100.0), ("2026-10-07", 103.0, 101.0),
                      ("2026-10-08", 106.0, 104.0), ("2026-10-09", 109.0, 107.0)])


def test_entry_is_next_open_and_capturable_is_open_to_close(conn):
    _seed_clean(conn, "positive")
    signal_outcomes.compute_outcomes(conn, horizons=(1,))
    r = conn.execute("SELECT * FROM signal_outcomes").fetchone()
    assert r["base_date"] == "2026-10-08"                       # ENTRY = next session open, not 10-07
    # capturable = exit_close(10-09)/entry_open(10-08) - 1 minus benchmark same window
    assert r["stock_ret"] == pytest.approx(133 / 115 - 1, abs=1e-6)
    assert r["benchmark_ret"] == pytest.approx(109 / 104 - 1, abs=1e-6)
    assert r["abn_ret"] == pytest.approx((133 / 115 - 1) - (109 / 104 - 1), abs=1e-6)
    assert r["hit"] == 1
    # priced-in (announcement day 10-07 vs 10-06) and overnight gap are recorded separately
    assert r["ann_abn"] == pytest.approx((110 / 100 - 1) - (103 / 100 - 1), abs=1e-6)   # 0.07
    assert r["gap_abn"] == pytest.approx((115 / 110 - 1) - (104 / 103 - 1), abs=1e-6)


def test_net_of_cost_recorded_and_flags(conn):
    _seed_clean(conn, "positive")
    signal_outcomes.compute_outcomes(conn, horizons=(1,))
    r = conn.execute("SELECT * FROM signal_outcomes").fetchone()
    cap_abn = (133 / 115 - 1) - (109 / 104 - 1)                 # ~+0.108, well above 10bps
    assert r["cost_bps"] == 10.0
    assert r["net_cap"] == pytest.approx(cap_abn - 0.001, abs=1e-6)
    assert r["net_hit"] == 1


def test_net_of_cost_flips_a_marginal_hit(conn):
    # Capturable abnormal of ~+5bps is a directional HIT but LOSES money after a 10bps round-trip.
    _company(conn, "cik:0000000001", "AAPL", "Information Technology")
    _event(conn, "e1", "2026-10-07")
    _impact(conn, "e1", "cik:0000000001", "positive", hop=1)
    # entry 10-08 open=100; exit 10-09 close=100.05 -> stock +5bps; benchmark flat -> abn +5bps
    _px(conn, "AAPL", [("2026-10-06", 100.0, 100.0), ("2026-10-07", 100.0, 100.0),
                       ("2026-10-08", 100.0, 100.0), ("2026-10-09", 100.05, 100.0)])
    _px(conn, "XLK", [("2026-10-06", 100.0, 100.0), ("2026-10-07", 100.0, 100.0),
                      ("2026-10-08", 100.0, 100.0), ("2026-10-09", 100.0, 100.0)])
    signal_outcomes.compute_outcomes(conn, horizons=(1,))
    r = conn.execute("SELECT * FROM signal_outcomes").fetchone()
    assert r["hit"] == 1          # directionally correct...
    assert r["net_hit"] == 0      # ...but underwater after the 10bps round-trip


def test_negative_signal_hit_logic(conn):
    _seed_clean(conn, "negative")   # stock rose vs sector -> a 'negative' call misses
    signal_outcomes.compute_outcomes(conn, horizons=(1,))
    r = conn.execute("SELECT hit, net_hit FROM signal_outcomes").fetchone()
    assert r["hit"] == 0 and r["net_hit"] == 0


def test_weekend_news_enters_first_session_not_skipped(conn):
    # pub on a non-trading day -> the first session (10-07) IS the first open after the news.
    _company(conn, "cik:0000000001", "AAPL", "Information Technology")
    _event(conn, "e1", "2026-10-06")   # not in the price series (weekend/holiday)
    _impact(conn, "e1", "cik:0000000001", "positive", hop=1)
    _px(conn, "AAPL", [("2026-10-05", 100.0, 100.0), ("2026-10-07", 110.0, 105.0),
                       ("2026-10-08", 121.0, 115.0)])
    _px(conn, "XLK", [("2026-10-05", 100.0, 100.0), ("2026-10-07", 103.0, 101.0),
                      ("2026-10-08", 106.0, 104.0)])
    signal_outcomes.compute_outcomes(conn, horizons=(1,))
    r = conn.execute("SELECT base_date, gap_abn FROM signal_outcomes").fetchone()
    assert r["base_date"] == "2026-10-07"   # entered the first session, not skipped to 10-08
    assert r["gap_abn"] is None             # entry == announcement session -> no overnight leg


def test_maturity_gating_needs_entry_plus_horizon(conn):
    _seed_clean(conn, "positive")   # dates through 10-09; entry 10-08
    signal_outcomes.compute_outcomes(conn, horizons=(1, 2))
    horizons = [r["horizon_days"] for r in conn.execute("SELECT horizon_days FROM signal_outcomes")]
    assert horizons == [1]          # +1 exit=10-09 mature; +2 exit=10-12 missing


def test_missing_open_is_no_price(conn):
    _seed_clean(conn, "positive")
    # wipe the entry-day open -> cannot model a next-open entry
    conn.execute("UPDATE prices SET adj_open=NULL WHERE ticker='AAPL' AND date='2026-10-08'")
    signal_outcomes.compute_outcomes(conn, horizons=(1,))
    assert conn.execute("SELECT COUNT(*) c FROM signal_outcomes").fetchone()["c"] == 0


def test_clean_flag_false_when_node_has_own_direct_news_in_window(conn):
    _seed_clean(conn, "positive")   # hop-1 signal on AAPL, announcement 10-07, entry 10-08
    _event(conn, "e2", "2026-10-08")
    _impact(conn, "e2", "cik:0000000001", "negative", hop=0)   # AAPL's own direct news in-window
    signal_outcomes.compute_outcomes(conn, horizons=(1,))
    assert conn.execute("SELECT clean FROM signal_outcomes WHERE event_id='e1'").fetchone()["clean"] == 0


def test_untickered_and_no_effect_signals_are_skipped(conn):
    _company(conn, "cik:0000000001", "AAPL", "Information Technology")
    store.upsert_node(conn, {"id": "slug:x-corp", "type": "Company", "name": "X", "tickers": []})
    _event(conn, "e1", "2026-10-07")
    store.write_event_impacts(conn, "e1", [
        {"node_id": "cik:0000000001", "direction": "no_effect", "magnitude": 0.0, "hop": 1, "reasoning": "r"},
        {"node_id": "slug:x-corp", "direction": "positive", "magnitude": 0.5, "hop": 1, "reasoning": "r"}])
    _px(conn, "AAPL", [("2026-10-07", 110.0, 105.0), ("2026-10-08", 121.0, 115.0), ("2026-10-09", 133.0, 120.0)])
    _px(conn, "XLK", [("2026-10-07", 103.0, 101.0), ("2026-10-08", 106.0, 104.0), ("2026-10-09", 109.0, 107.0)])
    signal_outcomes.compute_outcomes(conn, horizons=(1,))
    assert conn.execute("SELECT COUNT(*) c FROM signal_outcomes").fetchone()["c"] == 0
