"""Outcome computation: abnormal return, directional hit, no-look-ahead base date,
maturity gating, and the clean-subset flag. Deterministic synthetic prices; no network."""
from __future__ import annotations

import pytest

from schema import store
from pipeline import signal_outcomes


@pytest.fixture
def conn(monkeypatch):
    c = store.connect(":memory:")
    store.init_db(c)
    # Prices are pre-seeded, so ensure_prices must do nothing (and hit no network).
    monkeypatch.setattr(signal_outcomes.prices, "ensure_prices", lambda *a, **k: {})
    return c


def _company(c, node_id, ticker, sector):
    store.upsert_node(c, {"id": node_id, "type": "Company", "name": node_id,
                          "tickers": [ticker], "sector": sector})


def _event(c, eid, published):
    store.insert_event(c, {"id": eid, "headline": "h", "source": "s", "url": "u",
                           "category": "c", "published_at": published,
                           "seed_entity": "E", "seed_node_id": "cik:0000000001", "status": "traced"})


def test_abnormal_return_hit_and_base_date(conn):
    _company(conn, "cik:0000000001", "AAPL", "Information Technology")  # benchmark XLK
    _event(conn, "e1", "2026-10-07")
    store.write_event_impacts(conn, "e1", [
        {"node_id": "cik:0000000001", "direction": "positive", "magnitude": 0.5, "hop": 1, "reasoning": "r"}])
    store.upsert_prices(conn, "AAPL", [("2026-10-07", 110.0), ("2026-10-08", 121.0)])  # +10%
    store.upsert_prices(conn, "XLK", [("2026-10-07", 200.0), ("2026-10-08", 206.0)])   # +3%

    signal_outcomes.compute_outcomes(conn, horizons=(1,))
    row = conn.execute("SELECT * FROM signal_outcomes").fetchone()
    assert row["base_date"] == "2026-10-07"
    assert row["stock_ret"] == pytest.approx(0.1, abs=1e-6)
    assert row["benchmark_ret"] == pytest.approx(0.03, abs=1e-6)
    assert row["abn_ret"] == pytest.approx(0.07, abs=1e-6)  # 10% - 3%
    assert row["hit"] == 1          # positive signal, positive abnormal return
    assert row["benchmark"] == "XLK"


def test_negative_signal_hit_logic(conn):
    _company(conn, "cik:0000000001", "AAPL", "Information Technology")
    _event(conn, "e1", "2026-10-07")
    store.write_event_impacts(conn, "e1", [
        {"node_id": "cik:0000000001", "direction": "negative", "magnitude": 0.5, "hop": 1, "reasoning": "r"}])
    store.upsert_prices(conn, "AAPL", [("2026-10-07", 110.0), ("2026-10-08", 121.0)])  # rose
    store.upsert_prices(conn, "XLK", [("2026-10-07", 200.0), ("2026-10-08", 206.0)])
    signal_outcomes.compute_outcomes(conn, horizons=(1,))
    row = conn.execute("SELECT hit FROM signal_outcomes").fetchone()
    assert row["hit"] == 0          # predicted down, stock beat its sector -> miss


def test_maturity_gating_skips_horizons_beyond_data(conn):
    _company(conn, "cik:0000000001", "AAPL", "Information Technology")
    _event(conn, "e1", "2026-10-07")
    store.write_event_impacts(conn, "e1", [
        {"node_id": "cik:0000000001", "direction": "positive", "magnitude": 0.5, "hop": 1, "reasoning": "r"}])
    store.upsert_prices(conn, "AAPL", [("2026-10-07", 110.0), ("2026-10-08", 121.0)])
    store.upsert_prices(conn, "XLK", [("2026-10-07", 200.0), ("2026-10-08", 206.0)])
    # +1 is mature (Oct-08 exists); +2 (would need Oct-09) is not.
    signal_outcomes.compute_outcomes(conn, horizons=(1, 2))
    horizons = [r["horizon_days"] for r in conn.execute("SELECT horizon_days FROM signal_outcomes")]
    assert horizons == [1]


def test_no_look_ahead_base_is_first_trading_day_on_or_after_pub(conn):
    _company(conn, "cik:0000000001", "AAPL", "Information Technology")
    _event(conn, "e1", "2026-10-06")   # a day with no price (e.g. weekend/holiday)
    store.write_event_impacts(conn, "e1", [
        {"node_id": "cik:0000000001", "direction": "positive", "magnitude": 0.5, "hop": 1, "reasoning": "r"}])
    store.upsert_prices(conn, "AAPL", [("2026-10-07", 110.0), ("2026-10-08", 121.0)])
    store.upsert_prices(conn, "XLK", [("2026-10-07", 200.0), ("2026-10-08", 206.0)])
    signal_outcomes.compute_outcomes(conn, horizons=(1,))
    row = conn.execute("SELECT base_date FROM signal_outcomes").fetchone()
    assert row["base_date"] == "2026-10-07"   # first trading day >= pub, never before


def test_clean_flag_false_when_node_has_own_direct_news_in_window(conn):
    _company(conn, "cik:0000000001", "AAPL", "Information Technology")
    _event(conn, "e1", "2026-10-07")   # a 2nd-order (hop 1) signal on AAPL
    store.write_event_impacts(conn, "e1", [
        {"node_id": "cik:0000000001", "direction": "positive", "magnitude": 0.5, "hop": 1, "reasoning": "r"}])
    # AAPL's OWN direct (hop 0) news, published inside the (base, end] window.
    _event(conn, "e2", "2026-10-08")
    store.write_event_impacts(conn, "e2", [
        {"node_id": "cik:0000000001", "direction": "negative", "magnitude": 0.2, "hop": 0, "reasoning": "own"}])
    store.upsert_prices(conn, "AAPL", [("2026-10-07", 110.0), ("2026-10-08", 121.0)])
    store.upsert_prices(conn, "XLK", [("2026-10-07", 200.0), ("2026-10-08", 206.0)])
    signal_outcomes.compute_outcomes(conn, horizons=(1,))
    hop1 = conn.execute("SELECT clean FROM signal_outcomes WHERE event_id='e1'").fetchone()
    assert hop1["clean"] == 0   # contaminated by the node's own first-order news


def test_clean_flag_false_when_own_direct_news_on_base_day(conn):
    # A propagated (hop>=1) signal whose node has its OWN direct (hop-0) news dated EXACTLY on the
    # base day must be contaminated — that news (breaking after the base close) drives the forward
    # return. The window is base-inclusive for hop>=1 (the signal itself is hop-1, not in the set).
    _company(conn, "cik:0000000001", "AAPL", "Information Technology")
    _event(conn, "e1", "2026-10-07")   # hop-1 signal, base_date = 2026-10-07
    store.write_event_impacts(conn, "e1", [
        {"node_id": "cik:0000000001", "direction": "positive", "magnitude": 0.5, "hop": 1, "reasoning": "r"}])
    _event(conn, "e2", "2026-10-07")   # AAPL's own hop-0 news, same day
    store.write_event_impacts(conn, "e2", [
        {"node_id": "cik:0000000001", "direction": "negative", "magnitude": 0.2, "hop": 0, "reasoning": "own"}])
    store.upsert_prices(conn, "AAPL", [("2026-10-07", 110.0), ("2026-10-08", 121.0)])
    store.upsert_prices(conn, "XLK", [("2026-10-07", 200.0), ("2026-10-08", 206.0)])
    signal_outcomes.compute_outcomes(conn, horizons=(1,))
    assert conn.execute("SELECT clean FROM signal_outcomes WHERE event_id='e1'").fetchone()["clean"] == 0
    # The hop-0 signal does NOT flag itself on its own base day.
    assert conn.execute("SELECT clean FROM signal_outcomes WHERE event_id='e2'").fetchone()["clean"] == 1


def test_untickered_and_no_effect_signals_are_skipped(conn):
    _company(conn, "cik:0000000001", "AAPL", "Information Technology")
    store.upsert_node(conn, {"id": "slug:x-corp", "type": "Company", "name": "X", "tickers": []})
    _event(conn, "e1", "2026-10-07")
    store.write_event_impacts(conn, "e1", [
        {"node_id": "cik:0000000001", "direction": "no_effect", "magnitude": 0.0, "hop": 1, "reasoning": "r"},
        {"node_id": "slug:x-corp", "direction": "positive", "magnitude": 0.5, "hop": 1, "reasoning": "r"}])
    store.upsert_prices(conn, "AAPL", [("2026-10-07", 110.0), ("2026-10-08", 121.0)])
    store.upsert_prices(conn, "XLK", [("2026-10-07", 200.0), ("2026-10-08", 206.0)])
    signal_outcomes.compute_outcomes(conn, horizons=(1,))
    assert conn.execute("SELECT COUNT(*) c FROM signal_outcomes").fetchone()["c"] == 0
