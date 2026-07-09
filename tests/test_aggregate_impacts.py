from __future__ import annotations
import json
from datetime import date, datetime, timezone
from schema import store
from pipeline import aggregate_impacts as agg


def _db(tmp_path):
    conn = store.connect(tmp_path / "g.db"); store.init_db(conn)
    return conn


def _event(conn, eid, published_at):
    store.insert_event(conn, {"id": eid, "headline": f"H {eid}", "source": "SEC 8-K",
                              "url": f"u/{eid}", "category": "m&a", "published_at": published_at,
                              "seed_entity": "E", "seed_node_id": "cik:1", "status": "traced"})


def test_nets_positive_and_negative_with_mixed_flag(tmp_path):
    conn = _db(tmp_path)
    _event(conn, "e1", "2026-06-17"); _event(conn, "e2", "2026-06-17")
    store.write_event_impacts(conn, "e1", [{"node_id": "cik:9", "direction": "positive", "magnitude": 0.8, "hop": 1}])
    store.write_event_impacts(conn, "e2", [{"node_id": "cik:9", "direction": "negative", "magnitude": 0.6, "hop": 1}])
    agg.aggregate(conn, today=date(2026, 6, 17))
    import math
    r = conn.execute("SELECT * FROM node_impact WHERE node_id='cik:9'").fetchone()
    assert r["direction"] == "positive"                  # 0.8 > 0.6
    assert abs(r["magnitude"] - math.tanh(0.2)) < 1e-3   # tanh(|0.8 - 0.6|); magnitude is round(,3)
    assert r["mixed_signals"] == 1 and r["event_count"] == 2   # ratio 0.6/0.8 = 0.75 >= 0.6 → mixed


def test_strong_net_with_minor_opposite_is_not_mixed(tmp_path):
    # 3 positive (0.9) vs 1 small negative (0.1): minority 0.1 / majority 2.7 = 0.037
    # < 0.35 ratio → reads POSITIVE (green), NOT mixed (amber). Fixes the amber flood.
    conn = _db(tmp_path)
    for i in range(3):
        _event(conn, f"p{i}", "2026-06-17")
        store.write_event_impacts(conn, f"p{i}", [{"node_id": "n", "direction": "positive", "magnitude": 0.9, "hop": 1}])
    _event(conn, "neg", "2026-06-17")
    store.write_event_impacts(conn, "neg", [{"node_id": "n", "direction": "negative", "magnitude": 0.1, "hop": 1}])
    agg.aggregate(conn, today=date(2026, 6, 17))
    r = conn.execute("SELECT direction, mixed_signals FROM node_impact WHERE node_id='n'").fetchone()
    assert r["direction"] == "positive" and r["mixed_signals"] == 0


def test_comparable_opposing_signals_still_mixed(tmp_path):
    # 1.0 positive vs 0.7 negative: minority 0.7 / majority 1.0 = 0.7 >= 0.6 → MIXED.
    conn = _db(tmp_path)
    _event(conn, "p1", "2026-06-17"); _event(conn, "p2", "2026-06-17"); _event(conn, "n", "2026-06-17")
    store.write_event_impacts(conn, "p1", [{"node_id": "z", "direction": "positive", "magnitude": 0.5, "hop": 1}])
    store.write_event_impacts(conn, "p2", [{"node_id": "z", "direction": "positive", "magnitude": 0.5, "hop": 1}])
    store.write_event_impacts(conn, "n", [{"node_id": "z", "direction": "negative", "magnitude": 0.7, "hop": 1}])
    agg.aggregate(conn, today=date(2026, 6, 17))
    r = conn.execute("SELECT mixed_signals FROM node_impact WHERE node_id='z'").fetchone()
    assert r["mixed_signals"] == 1


def test_magnitude_is_tanh_bounded_no_saturation(tmp_path):
    # Two nodes with net contributions above 1.0 must NOT both pin at 1.0 — tanh keeps
    # them strictly under 1.0 and ordered (the old min(1.0,.) clamp saturated both to 1.0).
    conn = _db(tmp_path)
    for i in range(2):        # net 1.8 → tanh 0.947 (old clamp would be 1.0)
        _event(conn, f"p{i}", "2026-06-17")
        store.write_event_impacts(conn, f"p{i}", [{"node_id": "big", "direction": "positive", "magnitude": 0.9, "hop": 1}])
    _event(conn, "q0", "2026-06-17")                     # net 0.9 → tanh 0.716
    store.write_event_impacts(conn, "q0", [{"node_id": "med", "direction": "positive", "magnitude": 0.9, "hop": 1}])
    agg.aggregate(conn, today=date(2026, 6, 17))
    big = conn.execute("SELECT magnitude FROM node_impact WHERE node_id='big'").fetchone()["magnitude"]
    med = conn.execute("SELECT magnitude FROM node_impact WHERE node_id='med'").fetchone()["magnitude"]
    assert big < 1.0 and med < 1.0 and big > med    # bounded, unsaturated, ordered


def test_recency_decay_favors_newer(tmp_path):
    conn = _db(tmp_path)
    _event(conn, "new", "2026-06-17"); _event(conn, "old", "2026-06-11")   # 6 days old
    store.write_event_impacts(conn, "new", [{"node_id": "a", "direction": "positive", "magnitude": 0.5, "hop": 1}])
    store.write_event_impacts(conn, "old", [{"node_id": "b", "direction": "positive", "magnitude": 0.5, "hop": 1}])
    agg.aggregate(conn, today=date(2026, 6, 17), halflife=3.0)
    a = conn.execute("SELECT magnitude FROM node_impact WHERE node_id='a'").fetchone()["magnitude"]
    b = conn.execute("SELECT magnitude FROM node_impact WHERE node_id='b'").fetchone()["magnitude"]
    assert a > b                                      # newer event weighted higher


def test_window_excludes_stale(tmp_path):
    conn = _db(tmp_path)
    _event(conn, "old", "2026-06-01")                 # 16 days old, outside 7-day window
    store.write_event_impacts(conn, "old", [{"node_id": "z", "direction": "negative", "magnitude": 0.9, "hop": 0}])
    agg.aggregate(conn, today=date(2026, 6, 17), window_days=7)
    assert conn.execute("SELECT COUNT(*) c FROM node_impact WHERE node_id='z'").fetchone()["c"] == 0


def test_inert_no_effect_node_not_emitted(tmp_path):
    # A node whose only touch is no_effect/magnitude-0 has no direction, no mass, and no
    # drivers — it is inert and must be dropped (no "N events, no drivers" ghost row).
    conn = _db(tmp_path)
    _event(conn, "e1", "2026-06-17")
    store.write_event_impacts(conn, "e1", [{"node_id": "n", "direction": "no_effect", "magnitude": 0.0, "hop": 2}])
    agg.aggregate(conn, today=date(2026, 6, 17))
    assert conn.execute("SELECT COUNT(*) c FROM node_impact WHERE node_id='n'").fetchone()["c"] == 0


def test_top_events_capped_and_ordered(tmp_path):
    conn = _db(tmp_path)
    for i in range(7):
        _event(conn, f"e{i}", "2026-06-17")
        store.write_event_impacts(conn, f"e{i}", [{"node_id": "hub", "direction": "negative",
                                                   "magnitude": (i + 1) / 10, "hop": 1}])
    agg.aggregate(conn, today=date(2026, 6, 17), top_n=5)
    top = json.loads(conn.execute("SELECT top_events FROM node_impact WHERE node_id='hub'").fetchone()["top_events"])
    assert len(top) == 5
    mags = [abs(t["weighted"]) for t in top]
    assert mags == sorted(mags, reverse=True)         # ordered by |weighted| desc


def test_rebuild_is_deterministic_and_drops_stale_nodes(tmp_path):
    conn = _db(tmp_path)
    _event(conn, "e1", "2026-06-17")
    store.write_event_impacts(conn, "e1", [{"node_id": "x", "direction": "positive", "magnitude": 0.5, "hop": 0}])
    agg.aggregate(conn, today=date(2026, 6, 17))
    first = conn.execute("SELECT node_id, direction, magnitude FROM node_impact ORDER BY node_id").fetchall()
    agg.aggregate(conn, today=date(2026, 6, 17))       # rerun
    second = conn.execute("SELECT node_id, direction, magnitude FROM node_impact ORDER BY node_id").fetchall()
    assert [tuple(r) for r in first] == [tuple(r) for r in second]


def test_mixed_signals_magnitude_floored(tmp_path):
    conn = _db(tmp_path)
    _event(conn, "p", "2026-06-17"); _event(conn, "n", "2026-06-17")
    store.write_event_impacts(conn, "p", [{"node_id": "m", "direction": "positive", "magnitude": 0.5, "hop": 1}])
    store.write_event_impacts(conn, "n", [{"node_id": "m", "direction": "negative", "magnitude": 0.45, "hop": 1}])
    agg.aggregate(conn, today=date(2026, 6, 17))
    r = conn.execute("SELECT * FROM node_impact WHERE node_id='m'").fetchone()
    assert r["mixed_signals"] == 1
    assert abs(r["magnitude"] - 0.15) < 1e-6              # |0.5 - 0.45| = 0.05 floored up to 0.15


def test_equal_opposing_masses_net_to_no_effect(tmp_path):
    conn = _db(tmp_path)
    _event(conn, "p", "2026-06-17"); _event(conn, "n", "2026-06-17")
    store.write_event_impacts(conn, "p", [{"node_id": "q", "direction": "positive", "magnitude": 0.6, "hop": 1}])
    store.write_event_impacts(conn, "n", [{"node_id": "q", "direction": "negative", "magnitude": 0.6, "hop": 1}])
    agg.aggregate(conn, today=date(2026, 6, 17))
    r = conn.execute("SELECT * FROM node_impact WHERE node_id='q'").fetchone()
    # Real opposing drivers exist (so the row is NOT inert and is still emitted), but the
    # net is zero mass — a magnitude-0 node must read neutral, never MIXED, on the map.
    assert r["direction"] == "no_effect" and r["magnitude"] == 0.0
    assert r["mixed_signals"] == 0 and r["event_count"] == 2


def test_unscored_and_no_effect_excluded_from_top_events(tmp_path):
    conn = _db(tmp_path)
    _event(conn, "pos", "2026-06-17"); _event(conn, "uns", "2026-06-17"); _event(conn, "noe", "2026-06-17")
    store.write_event_impacts(conn, "pos", [{"node_id": "k", "direction": "positive", "magnitude": 0.8, "hop": 1}])
    store.write_event_impacts(conn, "uns", [{"node_id": "k", "direction": "unscored", "magnitude": 0.0, "hop": 2}])
    store.write_event_impacts(conn, "noe", [{"node_id": "k", "direction": "no_effect", "magnitude": 0.0, "hop": 2}])
    agg.aggregate(conn, today=date(2026, 6, 17))
    r = conn.execute("SELECT * FROM node_impact WHERE node_id='k'").fetchone()
    top = json.loads(r["top_events"])
    assert r["event_count"] == 3                       # all touches counted for context
    assert len(top) == 1 and top[0]["direction"] == "positive"   # only the real contributor surfaces


def test_only_traced_events_aggregated(tmp_path):
    conn = _db(tmp_path)
    # an event whose status is not 'traced' must not contribute, even with impacts present
    store.insert_event(conn, {"id": "f1", "headline": "H f1", "source": "SEC 8-K", "url": "u/f1",
                              "category": "m&a", "published_at": "2026-06-17",
                              "seed_entity": "E", "seed_node_id": "cik:1", "status": "failed"})
    store.write_event_impacts(conn, "f1", [{"node_id": "ghost", "direction": "negative", "magnitude": 0.9, "hop": 0}])
    agg.aggregate(conn, today=date(2026, 6, 17))
    assert conn.execute("SELECT COUNT(*) c FROM node_impact WHERE node_id='ghost'").fetchone()["c"] == 0


def test_future_dated_event_rejected(tmp_path):
    # A future-dated event (age < 0) must not slip in and earn max recency weight.
    conn = _db(tmp_path)
    _event(conn, "future", "2026-06-20")               # 3 days AFTER the reference date
    store.write_event_impacts(conn, "future", [{"node_id": "fut", "direction": "positive", "magnitude": 0.9, "hop": 0}])
    agg.aggregate(conn, today=date(2026, 6, 17))
    assert conn.execute("SELECT COUNT(*) c FROM node_impact WHERE node_id='fut'").fetchone()["c"] == 0


def test_magnitude_clamped_to_unit_range(tmp_path):
    # A per-event magnitude >1 is clamped to 1.0 BEFORE it enters the net (defense in
    # depth), so net = 1.0 and the combined magnitude is tanh(1.0), not tanh(5.0).
    import math
    conn = _db(tmp_path)
    _event(conn, "e1", "2026-06-17")
    store.write_event_impacts(conn, "e1", [{"node_id": "c", "direction": "positive", "magnitude": 5.0, "hop": 1}])
    agg.aggregate(conn, today=date(2026, 6, 17))        # same-day weight = 1.0
    r = conn.execute("SELECT * FROM node_impact WHERE node_id='c'").fetchone()
    assert r["direction"] == "positive"
    assert abs(r["magnitude"] - math.tanh(1.0)) < 1e-3  # net 1.0 (5.0 per-event clamped) → tanh(1.0)
    top = json.loads(r["top_events"])
    assert abs(top[0]["magnitude"] - 1.0) < 1e-6        # per-event clamp still reflected in the driver


def test_mixed_not_set_when_net_is_zero(tmp_path):
    # Equal opposing masses net to magnitude 0; such a node reads neutral, never MIXED.
    conn = _db(tmp_path)
    _event(conn, "p", "2026-06-17"); _event(conn, "n", "2026-06-17")
    store.write_event_impacts(conn, "p", [{"node_id": "z0", "direction": "positive", "magnitude": 0.4, "hop": 1}])
    store.write_event_impacts(conn, "n", [{"node_id": "z0", "direction": "negative", "magnitude": 0.4, "hop": 1}])
    agg.aggregate(conn, today=date(2026, 6, 17))
    r = conn.execute("SELECT * FROM node_impact WHERE node_id='z0'").fetchone()
    assert r["magnitude"] == 0.0 and r["mixed_signals"] == 0


def test_computed_at_is_full_utc_timestamp(tmp_path):
    # computed_at must be a full UTC wall-clock stamp (real time-of-day, not the passed-in
    # `today` at T00:00:00) so 24 hourly cycles are distinguishable and a stall is detectable.
    conn = _db(tmp_path)
    _event(conn, "e1", "2026-06-17")
    store.write_event_impacts(conn, "e1", [{"node_id": "t", "direction": "positive", "magnitude": 0.5, "hop": 0}])
    agg.aggregate(conn, today=date(2026, 6, 17))
    stamp = conn.execute("SELECT computed_at FROM node_impact WHERE node_id='t'").fetchone()["computed_at"]
    parsed = datetime.fromisoformat(stamp)
    # Not the frozen `today` param at midnight; it is the actual UTC now to the second.
    assert parsed.date() != date(2026, 6, 17)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    assert abs((parsed.replace(tzinfo=None) - now).total_seconds()) < 120


def test_blank_published_at_still_in_window_via_ingested_at(tmp_path):
    # Marketaux stores published_at='' (empty string, not NULL) when an article
    # lacks a timestamp. The SQL window bound must fall back to ingested_at just
    # like the Python age math, or these traced events silently vanish.
    conn = _db(tmp_path)
    _event(conn, "mkt", "")                                   # blank publish date
    conn.execute("UPDATE events SET ingested_at='2026-07-01' WHERE id='mkt'")
    store.write_event_impacts(conn, "mkt", [{"node_id": "z", "direction": "positive", "magnitude": 0.8, "hop": 1}])
    agg.aggregate(conn, today=date(2026, 7, 1), window_days=7)
    r = conn.execute("SELECT * FROM node_impact WHERE node_id='z'").fetchone()
    assert r is not None and r["direction"] == "positive"     # included via ingested_at, not dropped
