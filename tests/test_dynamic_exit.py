"""Reverse-signal dynamic exit: exits when the node's net consensus flips (reverse) or at the
max-hold cap; return measured entry-open -> exit-open. Deterministic; no network."""
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
    # max_hold=2 so the 5-day price window satisfies the full-maturity gate (entry_i+max_hold+1 < n)
    dx.compute_dynamic_outcomes(conn, max_hold=2)
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


def test_selection_bias_gate_requires_full_maxhold_maturity(conn):
    # A clear day-2 reverse, but only 5 trading days and max_hold=10 -> not enough forward data for
    # the full holding window, so it must be IMMATURE. (Before the up-front gate this fast reverse
    # would have sneaked in near the data edge while slower long holds were dropped -> the
    # holding-length-dependent selection bias the review flagged.)
    _company(conn)
    _event(conn, "e1", "2026-10-05", "cik:0000000001", "positive", 0.3, 0, seed="cik:A")
    _event(conn, "e2", "2026-10-07", "cik:0000000001", "negative", 0.9, 0, seed="cik:B")
    _px(conn)
    summary = dx.compute_dynamic_outcomes(conn, max_hold=10)
    assert conn.execute("SELECT COUNT(*) c FROM dynamic_outcomes WHERE event_id='e1'").fetchone()["c"] == 0
    assert summary["immature"] >= 1


def test_entry_mag_is_bounded_by_tanh(conn):
    # Two strong same-day positives: the raw signed sum would exceed 1, but entry_mag must saturate
    # into (-1, 1) to live on the same scale as the live aggregate's tanh consensus + the threshold.
    _company(conn)
    _event(conn, "e1", "2026-10-05", "cik:0000000001", "positive", 1.0, 0, seed="cik:A")
    _event(conn, "e2", "2026-10-05", "cik:0000000001", "positive", 1.0, 0, seed="cik:B")
    _px(conn)
    dx.compute_dynamic_outcomes(conn, max_hold=2)
    r = conn.execute("SELECT entry_mag FROM dynamic_outcomes WHERE event_id='e1'").fetchone()
    assert r is not None
    assert -1.0 < r["entry_mag"] < 1.0
    assert r["entry_mag"] > 0.5          # strong bullish consensus, just saturated


def _dyn_row(eid):
    return {"event_id": eid, "node_id": "cik:0000000001", "ticker": "AAPL", "hop": 0,
            "direction": "positive", "magnitude": 0.3, "entry_date": "2026-10-06",
            "exit_date": "2026-10-08", "holding_days": 2, "exit_reason": "reverse",
            "stock_ret": 0.01, "benchmark": "XLK", "benchmark_ret": 0.0, "abn_ret": 0.01,
            "hit": 1, "net_cap": 0.008, "net_hit": 1, "entry_mag": 0.5, "cost_bps": 20.0,
            "max_hold": 10, "computed_at": "t"}


def test_upsert_preserves_pruned_event_rows_but_clears_present_regressions(conn):
    # One present event, one that will be pruned; a dynamic row for each.
    for eid in ("ev_present", "ev_pruned"):
        store.insert_event(conn, {"id": eid, "headline": "h", "source": "s", "url": "u",
                                  "category": "c", "published_at": "2026-10-05", "seed_entity": "E",
                                  "seed_node_id": "cik:X", "status": "traced"})
    store.upsert_dynamic_outcomes(conn, [_dyn_row("ev_present"), _dyn_row("ev_pruned")])
    assert conn.execute("SELECT COUNT(*) c FROM dynamic_outcomes").fetchone()["c"] == 2
    # Prune ev_pruned from events (its mature outcome is finalized history) and regress the present
    # signal (recompute yields no current rows). The next write must clear only the present-event
    # row and keep the pruned-event one.
    conn.execute("DELETE FROM events WHERE id='ev_pruned'")
    conn.commit()
    store.upsert_dynamic_outcomes(conn, [])
    ids = {r["event_id"] for r in conn.execute("SELECT event_id FROM dynamic_outcomes")}
    assert ids == {"ev_pruned"}
