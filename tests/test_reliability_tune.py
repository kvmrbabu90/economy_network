"""Phase-2 auto-tune: gating math, enable flag, weight loading, and the off-by-default no-op."""
from __future__ import annotations

import pytest

from schema import store
from pipeline import reliability_tune as rt


def _mem():
    c = store.connect(":memory:")
    store.init_db(c)
    return c


def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("RELIABILITY_TUNE_ENABLED", raising=False)
    assert rt.enabled() is False
    conn = _mem()
    assert rt.load_reliability_weights(conn) is None
    # With no weights, every multiplier is a byte-identical 1.0.
    assert rt.multiplier_for(None, 1, "positive", 0.5) == 1.0
    assert rt.multiplier_for(None, 2, "negative", 0.01) == 1.0


def test_multiplier_gates_low_n_and_straddling_ci():
    assert rt._multiplier(0.9, 0.8, 1.0, n=5) == 1.0          # n < N_MIN -> neutral
    assert rt._multiplier(0.6, 0.48, 0.72, n=100) == 1.0      # CI straddles 0.5 -> neutral


def test_multiplier_boosts_and_penalizes_skilled_classes():
    up = rt._multiplier(0.7, 0.55, 0.82, n=50)               # clearly > chance
    assert up == pytest.approx(1.0 + 0.5 * (50 / 80) * 0.4, abs=1e-6) and up > 1.0
    down = rt._multiplier(0.3, 0.18, 0.44, n=50)             # clearly < chance
    assert down == pytest.approx(1.0 - 0.5 * (50 / 80) * 0.4, abs=1e-6) and down < 1.0


def test_multiplier_is_clamped(monkeypatch):
    # Defaults (GAIN=0.5) never reach the clamp; force a large gain to prove the bound binds.
    monkeypatch.setattr(rt, "_GAIN", 5.0)
    assert rt._multiplier(1.0, 0.95, 1.0, n=100000) == pytest.approx(1.5)   # capped high
    assert rt._multiplier(0.0, 0.0, 0.05, n=100000) == pytest.approx(0.5)   # capped low


def test_multiplier_stays_in_bounds_with_defaults():
    # Even at the extremes, the default-gain multiplier never leaves [MIN, MAX].
    for hr, lo, hi in [(1.0, 0.95, 1.0), (0.0, 0.0, 0.05)]:
        m = rt._multiplier(hr, lo, hi, n=100000)
        assert rt._MULT_MIN <= m <= rt._MULT_MAX


def test_multiplier_shrinks_toward_neutral_for_small_n():
    big = rt._multiplier(0.8, 0.6, 0.92, n=1000)
    small = rt._multiplier(0.8, 0.6, 0.92, n=25)
    assert 1.0 < small < big                                  # less evidence -> closer to 1.0


def test_load_weights_filters_horizon_and_clean(monkeypatch):
    monkeypatch.setenv("RELIABILITY_TUNE_ENABLED", "1")
    conn = _mem()
    store.replace_signal_reliability(conn, [
        # skilled, clean, horizon 5 -> should produce a boost
        {"hop": 1, "direction": "positive", "mag_band": "high", "horizon_days": 5, "clean": 1,
         "n": 50, "hit_rate": 0.7, "ci_low": 0.56, "ci_high": 0.81, "mean_abn": 0.01,
         "median_abn": 0.01, "ic": 0.2, "computed_at": "t"},
        # wrong horizon -> excluded
        {"hop": 1, "direction": "positive", "mag_band": "high", "horizon_days": 20, "clean": 1,
         "n": 50, "hit_rate": 0.9, "ci_low": 0.8, "ci_high": 0.97, "mean_abn": 0.01,
         "median_abn": 0.01, "ic": 0.2, "computed_at": "t"},
        # clean=0 -> excluded by default clean filter
        {"hop": 2, "direction": "negative", "mag_band": "med", "horizon_days": 5, "clean": 0,
         "n": 50, "hit_rate": 0.7, "ci_low": 0.56, "ci_high": 0.81, "mean_abn": 0.01,
         "median_abn": 0.01, "ic": 0.2, "computed_at": "t"},
    ])
    w = rt.load_reliability_weights(conn)
    assert set(w.keys()) == {(1, "positive", "high")}         # only horizon 5, clean 1
    assert w[(1, "positive", "high")] > 1.0


def test_multiplier_for_lookup_and_fallbacks(monkeypatch):
    weights = {(1, "positive", "high"): 1.2}
    assert rt.multiplier_for(weights, 1, "positive", 0.3) == 1.2     # mag 0.3 -> high
    assert rt.multiplier_for(weights, 1, "positive", 0.01) == 1.0    # mag 0.01 -> low (absent)
    assert rt.multiplier_for(weights, 1, "no_effect", 0.3) == 1.0    # non-directional
    assert rt.multiplier_for({}, 1, "positive", 0.3) == 1.0          # empty weights
