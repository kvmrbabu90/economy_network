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


def _event(c, eid, pub, node, direction, mag, hop):
    store.insert_event(c, {"id": eid, "headline": "h", "source": "s", "url": "u", "category": "c",
                           "published_at": pub, "seed_entity": "E", "seed_node_id": "cik:1", "status": "traced"})
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


def test_reverse_signal_triggers_exit(conn):
    _company(conn)
    _event(conn, "e1", "2026-10-05", "cik:0000000001", "positive", 0.3, 1)   # bullish entry signal
    _event(conn, "e2", "2026-10-07", "cik:0000000001", "negative", 0.6, 0)   # strong reverse on 10-07
    _px(conn)
    dx.compute_dynamic_outcomes(conn, max_hold=10)
    r = conn.execute("SELECT * FROM dynamic_outcomes WHERE event_id='e1'").fetchone()
    assert r is not None
    assert r["entry_date"] == "2026-10-06"       # next session open after the 10-05 announcement
    assert r["exit_date"] == "2026-10-07"        # the reverse flips net negative here
    assert r["exit_reason"] == "reverse"
    assert r["holding_days"] == 1
    # return = exit_close(95)/entry_open(102) - 1 minus benchmark, direction-signed (long)
    assert r["stock_ret"] == pytest.approx(95 / 102 - 1, abs=1e-6)
    assert r["hit"] == 0                          # the long lost; the reverse signal warned us out


def test_maxhold_caps_holding_without_a_reverse(conn):
    _company(conn)
    _event(conn, "e1", "2026-10-05", "cik:0000000001", "positive", 0.5, 1)   # no reverse follows
    _px(conn)
    dx.compute_dynamic_outcomes(conn, max_hold=2)
    r = conn.execute("SELECT * FROM dynamic_outcomes WHERE event_id='e1'").fetchone()
    assert r["exit_reason"] == "maxhold"
    assert r["holding_days"] == 2                 # entry 10-06, capped exit 10-08
    assert r["exit_date"] == "2026-10-08"


def test_immature_when_exit_beyond_price_data(conn):
    _company(conn)
    _event(conn, "e1", "2026-10-08", "cik:0000000001", "positive", 0.5, 1)   # entry 10-09, no fwd data
    _px(conn)
    dx.compute_dynamic_outcomes(conn, max_hold=10)
    assert conn.execute("SELECT COUNT(*) c FROM dynamic_outcomes WHERE event_id='e1'").fetchone()["c"] == 0
