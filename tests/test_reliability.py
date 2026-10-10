"""Reliability aggregation: Wilson interval, Spearman IC, magnitude bands, and the
class/rollup/coverage scorecard built from signal_outcomes."""
from __future__ import annotations

import pytest

from schema import store
from pipeline import reliability


def _mem():
    c = store.connect(":memory:")
    store.init_db(c)
    return c


def test_mag_band_thresholds():
    assert reliability.mag_band(0.01) == "low"
    assert reliability.mag_band(0.05) == "med"
    assert reliability.mag_band(0.1) == "med"
    assert reliability.mag_band(0.15) == "high"
    assert reliability.mag_band(0.8) == "high"


def test_wilson_interval_brackets_point_estimate():
    lo, hi = reliability._wilson(7, 10)
    assert 0.0 <= lo < 0.7 < hi <= 1.0
    # A degenerate all-hits sample still yields a sane (<1) lower bound.
    lo2, hi2 = reliability._wilson(10, 10)
    assert lo2 > 0.6 and hi2 == pytest.approx(1.0, abs=1e-6)
    assert reliability._wilson(0, 0) == (0.0, 0.0)


def test_spearman_monotonic_and_degenerate():
    # Perfectly increasing -> +1; perfectly decreasing -> -1.
    assert reliability._spearman([1, 2, 3, 4, 5], [10, 20, 30, 40, 50]) == pytest.approx(1.0)
    assert reliability._spearman([1, 2, 3, 4, 5], [50, 40, 30, 20, 10]) == pytest.approx(-1.0)
    # Too few points for a meaningful IC -> None.
    assert reliability._spearman([1, 2], [1, 2]) is None
    # Zero variance -> undefined -> None.
    assert reliability._spearman([1, 1, 1, 1, 1], [1, 2, 3, 4, 5]) is None


def _outcome(**kw):
    base = {"event_id": "e", "node_id": "n", "ticker": "AAPL", "hop": 1,
            "direction": "positive", "magnitude": 0.5, "base_date": "2026-10-07",
            "horizon_days": 1, "stock_ret": 0.02, "benchmark": "XLK",
            "benchmark_ret": 0.01, "abn_ret": 0.01, "hit": 1, "clean": 1,
            "computed_at": "2026-10-10T00:00:00+00:00"}
    base.update(kw)
    return base


def test_compute_reliability_builds_classes_and_rollups(tmp_path, monkeypatch):
    conn = _mem()
    monkeypatch.setattr(reliability, "_SCORECARD", tmp_path / "scorecard.json")
    rows = []
    # 10 hop-1 positive high-magnitude signals: 7 hits, 3 misses.
    for i in range(10):
        rows.append(_outcome(event_id=f"e{i}", node_id=f"n{i}", magnitude=0.5,
                             hit=1 if i < 7 else 0, abn_ret=0.02 if i < 7 else -0.01))
    store.upsert_signal_outcomes(conn, rows)

    sc = reliability.compute_reliability(conn)
    # overall +1d: 7/10
    assert sc["overall"]["1"]["n"] == 10 and sc["overall"]["1"]["hit_rate"] == pytest.approx(0.7)
    # one class: hop1/positive/high/h1/clean
    cls = [c for c in sc["classes"] if c["hop"] == 1 and c["mag_band"] == "high"]
    assert len(cls) == 1 and cls[0]["n"] == 10 and cls[0]["hit_rate"] == pytest.approx(0.7)
    assert cls[0]["ci_low"] < 0.7 < cls[0]["ci_high"]
    # hop x horizon rollup present with an all + clean block
    hh = [c for c in sc["by_hop_horizon"] if c["hop"] == 1 and c["horizon_days"] == 1]
    assert hh and hh[0]["all"]["n"] == 10 and hh[0]["clean"]["n"] == 10
    # coverage + scorecard file written
    assert sc["coverage"]["outcome_rows"] == 10
    assert (tmp_path / "scorecard.json").exists()
    # table persisted for the /classes endpoint + Phase 2
    assert len(store.read_signal_reliability(conn)) == len(sc["classes"])


def test_clean_rollup_excludes_contaminated(tmp_path, monkeypatch):
    conn = _mem()
    monkeypatch.setattr(reliability, "_SCORECARD", tmp_path / "s.json")
    rows = [_outcome(event_id=f"e{i}", node_id=f"n{i}", clean=1) for i in range(5)]
    rows += [_outcome(event_id=f"d{i}", node_id=f"m{i}", clean=0) for i in range(3)]
    store.upsert_signal_outcomes(conn, rows)
    sc = reliability.compute_reliability(conn)
    hh = [c for c in sc["by_hop_horizon"] if c["hop"] == 1 and c["horizon_days"] == 1][0]
    assert hh["all"]["n"] == 8 and hh["clean"]["n"] == 5


def test_read_scorecard_none_when_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(reliability, "_SCORECARD", tmp_path / "missing.json")
    assert reliability.read_scorecard() is None
