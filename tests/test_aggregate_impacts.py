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


import math


def test_write_event_impacts_clamps_magnitude_to_nonnegative_weight(tmp_path):
    # magnitude is a NON-NEGATIVE weight; sign lives in `direction`. A tracer that
    # emits a signed-negative magnitude must be stored as its abs weight, else the
    # aggregate's [0,1] clamp zeroes it and drops the driver.
    conn = _db(tmp_path)
    _event(conn, "e1", "2026-06-17")
    store.write_event_impacts(conn, "e1", [{"node_id": "n", "direction": "negative", "magnitude": -0.30, "hop": 1}])
    m = conn.execute("SELECT magnitude FROM event_impacts WHERE node_id='n'").fetchone()[0]
    assert m == 0.30


def test_legacy_signed_negative_magnitude_contributes_weight_not_zeroed(tmp_path):
    # Rows written before the clamp hold a signed-negative magnitude. The aggregate
    # must count the WEIGHT (abs) so the driver survives and the tint stays negative,
    # instead of max(0, .) zeroing it and (with enough positive mass) flipping green.
    conn = _db(tmp_path)
    _event(conn, "e1", "2026-06-17")
    # Insert a raw row bypassing write_event_impacts' abs-clamp (simulates a legacy row).
    conn.execute(
        "INSERT INTO event_impacts (event_id, node_id, direction, magnitude, hop, reasoning) "
        "VALUES (?,?,?,?,?,?)", ("e1", "cik:neg", "negative", -0.30, 1, None))
    conn.commit()
    agg.aggregate(conn, today=date(2026, 6, 17))
    r = conn.execute("SELECT direction, magnitude FROM node_impact WHERE node_id='cik:neg'").fetchone()
    assert r is not None, "a negative-magnitude driver must not be zeroed out of the aggregate"
    assert r["direction"] == "negative"
    assert abs(r["magnitude"] - math.tanh(0.30)) < 1e-3


def test_subfloor_directional_row_is_dropped(tmp_path):
    # A directional verdict at/below the tint floor never colours the map, so it must
    # not be emitted — else an untinted node shows a populated combined-impact panel.
    conn = _db(tmp_path)
    _event(conn, "e1", "2026-06-17")
    store.write_event_impacts(conn, "e1", [{"node_id": "tiny", "direction": "positive", "magnitude": 0.03, "hop": 1}])
    agg.aggregate(conn, today=date(2026, 6, 17))
    assert conn.execute("SELECT 1 FROM node_impact WHERE node_id='tiny'").fetchone() is None


def test_read_all_node_impact_excludes_regions_but_read_node_impact_keeps_them(tmp_path):
    # Region market-buckets saturate to magnitude ~1.0 and dominate the map. Exclude
    # them from the /impact/live tint payload, but keep them resolvable on a direct
    # click (read_node_impact stays unfiltered).
    conn = _db(tmp_path)
    conn.execute("INSERT INTO nodes (id, type, name) VALUES (?,?,?)", ("cik:5", "Company", "Co"))
    conn.execute("INSERT INTO nodes (id, type, name) VALUES (?,?,?)", ("region:x", "Region", "Reg"))
    conn.commit()
    store.replace_node_impact(conn, [
        {"node_id": "cik:5", "direction": "positive", "magnitude": 0.5, "mixed_signals": 0,
         "event_count": 3, "top_events": "[]", "computed_at": "t"},
        {"node_id": "region:x", "direction": "positive", "magnitude": 1.0, "mixed_signals": 0,
         "event_count": 9, "top_events": "[]", "computed_at": "t"},
    ])
    payload_ids = {r["node_id"] for r in store.read_all_node_impact(conn)}
    assert "cik:5" in payload_ids            # a Company still tints the map
    assert "region:x" not in payload_ids     # the Region bucket is filtered out of the tint payload
    assert store.read_node_impact(conn, "region:x") is not None   # but a direct click still resolves it


def test_driver_count_counts_only_nonzero_weighted_drivers(tmp_path):
    # event_count counts EVERY window row (incl. unscored/no_effect, kept for "scanned
    # N" context); driver_count counts only the real drivers (nonzero-weighted rows),
    # so the panel header stops claiming "30 events" when only a handful drove the tint.
    conn = _db(tmp_path)
    for eid in ("d1", "d2", "u1", "u2", "z1"):
        _event(conn, eid, "2026-06-17")
    store.write_event_impacts(conn, "d1", [{"node_id": "n", "direction": "positive", "magnitude": 0.7, "hop": 1}])
    store.write_event_impacts(conn, "d2", [{"node_id": "n", "direction": "negative", "magnitude": 0.3, "hop": 1}])
    store.write_event_impacts(conn, "u1", [{"node_id": "n", "direction": "unscored", "magnitude": 0.0, "hop": 1}])
    store.write_event_impacts(conn, "u2", [{"node_id": "n", "direction": "no_effect", "magnitude": 0.0, "hop": 1}])
    store.write_event_impacts(conn, "z1", [{"node_id": "n", "direction": "positive", "magnitude": 0.0, "hop": 1}])
    agg.aggregate(conn, today=date(2026, 6, 17))
    r = conn.execute("SELECT event_count, driver_count FROM node_impact WHERE node_id='n'").fetchone()
    assert r["event_count"] == 5      # all 5 window rows scanned
    assert r["driver_count"] == 2     # only the 2 nonzero-weighted directional drivers


def test_migrate_node_impact_driver_count_on_pre_existing_db(tmp_path):
    # A DB whose node_impact predates driver_count must gain the column (forward-only).
    import sqlite3
    p = tmp_path / "old.db"
    c = sqlite3.connect(p)
    c.executescript(
        "CREATE TABLE node_impact (node_id TEXT PRIMARY KEY, direction TEXT NOT NULL, "
        "magnitude REAL NOT NULL, mixed_signals INTEGER NOT NULL DEFAULT 0, "
        "event_count INTEGER NOT NULL, top_events TEXT NOT NULL DEFAULT '[]', "
        "computed_at TEXT NOT NULL);"
    )
    c.commit(); c.close()
    conn = store.connect(p); store.init_db(conn)   # runs migrations
    cols = [r[1] for r in conn.execute("PRAGMA table_info(node_impact)").fetchall()]
    assert "driver_count" in cols


def test_direct_count_counts_only_hop0_drivers(tmp_path):
    # direct_count = drivers that are DIRECT (hop 0 — news about the node itself).
    conn = _db(tmp_path)
    for eid in ("d0", "p1", "p2"):
        _event(conn, eid, "2026-06-17")
    store.write_event_impacts(conn, "d0", [{"node_id": "n", "direction": "positive", "magnitude": 0.5, "hop": 0}])
    store.write_event_impacts(conn, "p1", [{"node_id": "n", "direction": "positive", "magnitude": 0.4, "hop": 1}])
    store.write_event_impacts(conn, "p2", [{"node_id": "n", "direction": "positive", "magnitude": 0.3, "hop": 2}])
    agg.aggregate(conn, today=date(2026, 6, 17))
    r = conn.execute("SELECT driver_count, direct_count FROM node_impact WHERE node_id='n'").fetchone()
    assert r["driver_count"] == 3
    assert r["direct_count"] == 1   # only the hop-0 driver is direct


def test_direct_count_zero_for_pure_propagation(tmp_path):
    # A verdict driven ONLY by hop>=1 propagation (like HAL: aerospace sector spillover,
    # no direct HAL news) has direct_count 0 — the panel flags it "no direct news".
    conn = _db(tmp_path)
    for eid in ("p1", "p2"):
        _event(conn, eid, "2026-06-17")
    store.write_event_impacts(conn, "p1", [{"node_id": "hal", "direction": "positive", "magnitude": 0.6, "hop": 1}])
    store.write_event_impacts(conn, "p2", [{"node_id": "hal", "direction": "positive", "magnitude": 0.5, "hop": 2}])
    agg.aggregate(conn, today=date(2026, 6, 17))
    r = conn.execute("SELECT driver_count, direct_count FROM node_impact WHERE node_id='hal'").fetchone()
    assert r["driver_count"] == 2 and r["direct_count"] == 0
