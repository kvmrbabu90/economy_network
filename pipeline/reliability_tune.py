"""Phase-2 (opt-in) auto-tune: reweight impact contributions by each signal CLASS's MEASURED
reliability, from the signal_reliability table that pipeline/reliability.py produces.

Design (see docs/superpowers/specs/2026-10-10-signal-reliability-design.md, Phase 2):
  - Read-only + quota-free. Lives in the AGGREGATE layer and multiplies a per-contribution
    scalar in the derived, rebuildable node_impact cache. It NEVER touches event_impacts — which
    is the raw model prediction signal_outcomes.py measures against — so tuning can't feed back
    into its own measurement (no runaway loop).
  - Gated OFF by default: RELIABILITY_TUNE_ENABLED != '1' -> load_reliability_weights returns
    None and every multiplier is 1.0, so aggregation is byte-identical to the untuned pipeline.
  - A class (hop, direction, mag_band) earns a non-neutral multiplier only when it has
    DEMONSTRATED skill: n >= N_MIN AND its Wilson interval excludes 0.5 (hit-rate distinguishable
    from a coin flip). The adjustment is then shrunk toward neutral for small n and hard-clamped,
    so no single class can dominate or zero-out a signal. Everything else falls back to 1.0.

    from pipeline import reliability_tune
    weights = reliability_tune.load_reliability_weights(conn)        # None when disabled
    contrib *= reliability_tune.multiplier_for(weights, hop, direction, magnitude)
"""
from __future__ import annotations

import os

from schema import store
from pipeline.reliability import mag_band   # lockstep band cutpoints — do NOT reimplement

# ~matches the 7-day aggregate window to the +5d trading-day horizon; override per deployment.
_HORIZON = int(os.environ.get("RELIABILITY_TUNE_HORIZON", "5"))
_N_MIN = int(os.environ.get("RELIABILITY_TUNE_N_MIN", "20"))      # min samples to adjust at all
_K_PRIOR = float(os.environ.get("RELIABILITY_TUNE_K", "30"))       # empirical-Bayes shrink constant
_GAIN = float(os.environ.get("RELIABILITY_TUNE_GAIN", "0.5"))      # max fractional tilt at full skill
_MULT_MIN = float(os.environ.get("RELIABILITY_TUNE_MIN", "0.5"))
_MULT_MAX = float(os.environ.get("RELIABILITY_TUNE_MAX", "1.5"))


def enabled() -> bool:
    return os.environ.get("RELIABILITY_TUNE_ENABLED", "0") == "1"


def _multiplier(hit_rate: float, ci_low: float, ci_high: float, n: int) -> float:
    """Shrunk, clamped multiplier for a class. 1.0 unless the class has demonstrated skill."""
    if n < _N_MIN:
        return 1.0
    if ci_low <= 0.5 <= ci_high:          # CI straddles a coin flip -> no demonstrated skill
        return 1.0
    edge = 2.0 * (hit_rate - 0.5)         # [-1, 1]: >0 better than chance, <0 worse
    shrink = n / (n + _K_PRIOR)           # -> 0 for tiny n (stay neutral), -> 1 for large n
    mult = 1.0 + _GAIN * shrink * edge
    return max(_MULT_MIN, min(_MULT_MAX, mult))


def load_reliability_weights(conn, horizon: int | None = None, clean: int | None = 1):
    """(hop, direction, mag_band) -> multiplier for one horizon, or None when tuning is disabled
    (callers then use 1.0 everywhere). Defaults to the clean-subset (pure-propagation) classes."""
    if not enabled():
        return None
    horizon = _HORIZON if horizon is None else horizon
    try:
        rows = store.read_signal_reliability(conn)
    except Exception:
        return {}
    weights: dict[tuple, float] = {}
    for r in rows:
        if r["horizon_days"] != horizon:
            continue
        if clean is not None and r["clean"] != clean:
            continue
        # Tune on the NET-of-cost capturable hit-rate (the retail bar), falling back to the gross
        # capturable rate on a pre-migration DB that lacks the net columns.
        hr = r["net_hit_rate"] if r.get("net_hit_rate") is not None else r["hit_rate"]
        lo = r["net_ci_low"] if r.get("net_ci_low") is not None else r["ci_low"]
        hi = r["net_ci_high"] if r.get("net_ci_high") is not None else r["ci_high"]
        weights[(r["hop"], r["direction"], r["mag_band"])] = _multiplier(hr, lo, hi, r["n"])
    return weights


def multiplier_for(weights, hop: int, direction: str, magnitude: float) -> float:
    """1.0 (no change) when tuning is off, the row is non-directional, or the class is absent /
    lacks demonstrated skill. O(1) dict lookup — called once per contribution."""
    if not weights or direction not in ("positive", "negative"):
        return 1.0
    return weights.get((hop, direction, mag_band(magnitude)), 1.0)
