"""Reverse-signal dynamic exit: exits when the node's net consensus flips (reverse) or at the
max-hold cap; return measured entry-open -> exit-close. Deterministic; no network."""
from __future__ import annotations

import pytest

from schema import store
from pipeline import dynamic_exit as dx


@pytest.fixture
def conn(monkeypatch):
    c = store.connect(":memory:")
    store.init_db(c)
    monkeypatch.setattr(dx.prices, "ensure_prices", lambda *a, **k: {})
    monkeypatch.setattr(dx, "COST_BPS", 10.0)
    monkeypatch.setattr(dx, "WIN", 7)
    monkeypatch.setattr(dx, "HL", 3.0)
    return c


def _company(c):
    store.upsert_node(c, {"id": "cik:0000000001", "type": "Company", "name": "A",
                          "tickers": ["AAPL"], "sector": "Information Technology"})  # bench XLK


def _event(c, eid, pub, node, direction, mag, hop, seed="cik:0000000002"):
    store.insert_event(c, {"id": eid, "headline": "h", "source": "s", "url": "u", "category": "c",
                           "published_at": pub, "seed_entity": "E", "seed_node_id": seed, "status": "traced"})
    store.write_event_impacts(c, eid, [
        {"node_id": node, "direction": direction, "magnitude": mag, "hop": hop, "reasoning": "r"}])


def _px(c):
    # trading days Oct 5-9 for AAPL + XLK
    store.upsert_prices(c, "AAPL", [("2026-10-05", 100.0, 100.0), ("2026-10-06", 105.0, 102.0),
                                    ("2026-10-07", 95.0, 104.0), ("2026-10-08", 96.0, 95.0),
                                    ("2026-10-09", 97.0, 96.0)])
    store.upsert_prices(c, "XLK", [("2026-10-05", 100.0, 100.0), ("2026-10-06", 101.0, 100.0),
                                   ("2026-10-07", 102.0, 101.0), ("2026-10-08", 103.0, 102.0),
                                   ("2026-10-09", 104.0, 103.0)])


def test_reverse_signal_triggers_exit_at_next_open(conn):
    _company(conn)
    _event(conn, "e1", "2026-10-05", "cik:0000000001", "positive", 0.3, 0, seed="cik:A")  # bullish entry
    _event(conn, "e2", "2026-10-07", "cik:0000000001", "negative", 0.6, 0, seed="cik:B")  # strong reverse 10-07
    _px(conn)
    dx.compute_dynamic_outcomes(conn, max_hold=10)
    r = conn.execute("SELECT * FROM dynamic_outcomes WHERE event_id='e1'").fetchone()
    assert r is not None
    assert r["entry_date"] == "2026-10-06"       # next session open after the 10-05 announcement
    assert r["exit_reason"] == "reverse"         # net flips negative on the 10-07 trigger day
    assert r["exit_date"] == "2026-10-08"        # exit at the NEXT session open after the trigger
    assert r["holding_days"] == 2
    # entry open 10-06 (102) -> exit OPEN 10-08 (95), minus benchmark (XLK open 100 -> 102)
    assert r["stock_ret"] == pytest.approx(95 / 102 - 1, abs=1e-6)
    assert r["benchmark_ret"] == pytest.approx(102 / 100 - 1, abs=1e-6)
    assert r["hit"] == 0


def test_hop_weighting_keeps_a_weak_high_hop_reverse_from_flipping(conn):
    # hop-0 positive (0.3) vs a hop-2 negative (0.5): raw net would flip negative, but with the
    # aggregate's hop attenuation (0.25x at hop 2) the thesis stays positive -> NOT a reverse exit.
    _company(conn)
    _event(conn, "e1", "2026-10-05", "cik:0000000001", "positive", 0.3, 0, seed="cik:A")
    _event(conn, "e2", "2026-10-07", "cik:0000000001", "negative", 0.5, 2, seed="cik:B")
    _px(conn)
    dx.compute_dynamic_outcomes(conn, max_hold=2)
    r = conn.execute("SELECT * FROM dynamic_outcomes WHERE event_id='e1'").fetchone()
    assert r["exit_reason"] == "maxhold"          # held through 10-07 (would be 'reverse' without hop-weight)


def test_maxhold_caps_holding_without_a_reverse(conn):
    _company(conn)
    _event(conn, "e1", "2026-10-05", "cik:0000000001", "positive", 0.5, 0, seed="cik:A")  # no reverse
    _px(conn)
    dx.compute_dynamic_outcomes(conn, max_hold=2)
    r = conn.execute("SELECT * FROM dynamic_outcomes WHERE event_id='e1'").fetchone()
    assert r["exit_reason"] == "maxhold"
    assert r["exit_date"] == "2026-10-09"         # trigger capped at 10-08, exit next open 10-09
    assert r["holding_days"] == 3


def test_immature_when_exit_beyond_price_data(conn):
    _company(conn)
    _event(conn, "e1", "2026-10-08", "cik:0000000001", "positive", 0.5, 1)   # entry 10-09, no fwd data
    _px(conn)
    dx.compute_dynamic_outcomes(conn, max_hold=10)
    assert conn.execute("SELECT COUNT(*) c FROM dynamic_outcomes WHERE event_id='e1'").fetchone()["c"] == 0
