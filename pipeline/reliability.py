"""Aggregate mature signal outcomes into class-level reliability + a scorecard JSON.

Reliability is a property of a signal CLASS, never a single signal — pooling is what gives
statistical power. A class is (order/hop x direction x magnitude-band x horizon x clean). Each
class gets: n, directional hit-rate with a Wilson confidence interval, mean/median abnormal
return, and a Spearman information-coefficient (does a bigger predicted magnitude go with a
bigger favorable move?). Rollups (hop x horizon, by magnitude band, overall) and a coverage/
maturity block are written to `data/signal_reliability.json` for the dashboard. Uses NO LLM.

    from pipeline import reliability
    reliability.compute_reliability(conn)
"""
from __future__ import annotations

import json
import logging
import math
import os
from datetime import datetime, timezone
from pathlib import Path

from schema import store

log = logging.getLogger(__name__)

# Magnitude-band cut points (strength is a 0-1 scale; median ~0.1). Override via env.
_B = [float(x) for x in os.environ.get("RELIABILITY_MAG_BANDS", "0.05,0.15").split(",")]
_Z = 1.96  # 95% Wilson interval
_IC_MIN_N = 5  # below this, an IC is too noisy to report
_SCORECARD = Path(os.environ.get("RELIABILITY_SCORECARD",
                  str(Path(__file__).resolve().parents[1] / "data" / "signal_reliability.json")))


def scorecard_path() -> Path:
    return _SCORECARD


def read_scorecard() -> dict | None:
    """The last-written scorecard, or None if validation hasn't run yet."""
    try:
        return json.loads(_SCORECARD.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def mag_band(m: float) -> str:
    return "low" if m < _B[0] else ("med" if m < _B[1] else "high")


def _wilson(hits: int, n: int) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = hits / n
    denom = 1 + _Z * _Z / n
    center = (p + _Z * _Z / (2 * n)) / denom
    half = _Z * math.sqrt(p * (1 - p) / n + _Z * _Z / (4 * n * n)) / denom
    return (round(max(0.0, center - half), 4), round(min(1.0, center + half), 4))


def _ranks(xs: list[float]) -> list[float]:
    """Average ranks (ties share the mean of their rank span)."""
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def _spearman(xs: list[float], ys: list[float]) -> float | None:
    """Spearman rank correlation, or None when undefined (too few / zero variance)."""
    if len(xs) < _IC_MIN_N:
        return None
    rx, ry = _ranks(xs), _ranks(ys)
    n = len(rx)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((rx[i] - mx) * (ry[i] - my) for i in range(n))
    dx = math.sqrt(sum((v - mx) ** 2 for v in rx))
    dy = math.sqrt(sum((v - my) ** 2 for v in ry))
    if dx == 0 or dy == 0:
        return None
    return round(num / (dx * dy), 4)


def _median(vs: list[float]) -> float:
    s = sorted(vs)
    n = len(s)
    if n == 0:
        return 0.0
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def _stat_block(rows: list[dict]) -> dict:
    """Summary stats for a group of outcome rows. All hit/abn fields are the CAPTURABLE (entry-open
    -> exit) abnormal return. `net_*` is that return direction-signed and net of round-trip cost —
    the retail bar. `dir_acc` is announcement-day direction accuracy (extraction quality, from the
    priced-in ann_abn); `mean_gap` is the missed overnight. `directional_abn` re-signs the abnormal
    into the predicted direction so IC>0 means stronger-predicted => more-favorable capturable move."""
    n = len(rows)
    hits = sum(r["hit"] for r in rows)
    abn = [r["abn_ret"] for r in rows]
    dir_abn = [r["abn_ret"] if r["direction"] == "positive" else -r["abn_ret"] for r in rows]
    mags = [r["magnitude"] for r in rows]
    lo, hi = _wilson(hits, n)
    # Net of round-trip cost — did you make money, in the predicted direction, after costs?
    net_hits = sum(1 for r in rows if r.get("net_hit"))
    nets = [r["net_cap"] for r in rows if r.get("net_cap") is not None]
    nlo, nhi = _wilson(net_hits, n)
    # Announcement-day direction accuracy (priced-in move went the predicted way) — extraction check.
    ann_rows = [r for r in rows if r.get("ann_abn") is not None]
    ann_ok = sum(1 for r in ann_rows if (r["direction"] == "positive") == (r["ann_abn"] > 0))
    gaps = [r["gap_abn"] for r in rows if r.get("gap_abn") is not None]
    return {
        "n": n, "hits": hits,
        "hit_rate": round(hits / n, 4) if n else 0.0,
        "ci_low": lo, "ci_high": hi,
        "mean_abn": round(sum(abn) / n, 6) if n else 0.0,
        "median_abn": round(_median(abn), 6),
        "ic": _spearman(mags, dir_abn),
        "net_hit_rate": round(net_hits / n, 4) if n else 0.0,
        "net_ci_low": nlo, "net_ci_high": nhi,
        "mean_net": round(sum(nets) / len(nets), 6) if nets else 0.0,
        "dir_acc": round(ann_ok / len(ann_rows), 4) if ann_rows else None,
        "mean_gap": round(sum(gaps) / len(gaps), 6) if gaps else None,
    }


def compute_reliability(conn, write_scorecard: bool = True) -> dict:
    outcomes = [dict(r) for r in conn.execute("SELECT * FROM signal_outcomes").fetchall()]
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # --- class rows (the durable, Phase-2-facing grain) ---
    classes: dict[tuple, list] = {}
    for r in outcomes:
        key = (r["hop"], r["direction"], mag_band(r["magnitude"]), r["horizon_days"], r["clean"])
        classes.setdefault(key, []).append(r)
    class_rows = []
    for (hop, direction, band, horizon, clean), rows in sorted(classes.items()):
        b = _stat_block(rows)
        class_rows.append({
            "hop": hop, "direction": direction, "mag_band": band, "horizon_days": horizon,
            "clean": clean, "n": b["n"], "hit_rate": b["hit_rate"], "ci_low": b["ci_low"],
            "ci_high": b["ci_high"], "mean_abn": b["mean_abn"], "median_abn": b["median_abn"],
            "ic": b["ic"], "net_hit_rate": b["net_hit_rate"], "net_ci_low": b["net_ci_low"],
            "net_ci_high": b["net_ci_high"], "mean_net": b["mean_net"], "dir_acc": b["dir_acc"],
            "mean_gap": b["mean_gap"], "computed_at": now})
    written = store.replace_signal_reliability(conn, class_rows)

    # --- rollups for the dashboard ---
    horizons = sorted({r["horizon_days"] for r in outcomes})
    hops = sorted({r["hop"] for r in outcomes})

    def cell(pred):
        sub = [r for r in outcomes if pred(r)]
        return _stat_block(sub) if sub else None

    by_hop_horizon = []
    for hop in hops:
        for h in horizons:
            allc = cell(lambda r, hop=hop, h=h: r["hop"] == hop and r["horizon_days"] == h)
            clnc = cell(lambda r, hop=hop, h=h: r["hop"] == hop and r["horizon_days"] == h and r["clean"] == 1)
            if allc:
                by_hop_horizon.append({"hop": hop, "horizon_days": h, "all": allc, "clean": clnc})

    by_magband = []
    for band in ("low", "med", "high"):
        for h in horizons:
            c = cell(lambda r, band=band, h=h: mag_band(r["magnitude"]) == band and r["horizon_days"] == h)
            if c:
                by_magband.append({"mag_band": band, "horizon_days": h, **c})

    overall = {str(h): cell(lambda r, h=h: r["horizon_days"] == h) for h in horizons}

    # --- coverage / maturity ---
    sig_total = conn.execute("""
        SELECT COUNT(*) FROM event_impacts ei JOIN events e ON e.id=ei.event_id
        JOIN nodes n ON n.id=ei.node_id
        WHERE ei.direction IN ('positive','negative') AND e.status='traced'
          AND n.tickers IS NOT NULL AND n.tickers!='[]' AND n.tickers!=''""").fetchone()[0]
    mature_by_h = {str(h): sum(1 for r in outcomes if r["horizon_days"] == h) for h in horizons}
    coverage = {
        "directional_signals": sig_total,
        "signals_with_mature_outcome": len({(r["event_id"], r["node_id"]) for r in outcomes}),
        "outcome_rows": len(outcomes),
        "mature_by_horizon": mature_by_h,
        "distinct_events": len({r["event_id"] for r in outcomes}),
        "distinct_nodes": len({r["node_id"] for r in outcomes}),
    }

    scorecard = {
        "generated_at": now,
        "meta": {"horizons": horizons, "mag_bands": {"low": f"<{_B[0]}",
                 "med": f"{_B[0]}-{_B[1]}", "high": f">={_B[1]}"},
                 "z": _Z, "ic_min_n": _IC_MIN_N, "benchmark": "sector-ETF excess (SPY fallback)",
                 "entry": "next session open after the announcement (retail-realistic, no look-ahead)",
                 "cost_bps": float(os.environ.get("RELIABILITY_COST_BPS", "10"))},
        "overall": overall,
        "by_hop_horizon": by_hop_horizon,
        "by_magband": by_magband,
        "coverage": coverage,
        "classes": class_rows,
    }
    if write_scorecard:
        _SCORECARD.parent.mkdir(parents=True, exist_ok=True)
        _SCORECARD.write_text(json.dumps(scorecard, indent=2), encoding="utf-8")
    log.info("reliability: %d classes, %d outcome rows, scorecard=%s",
             written, len(outcomes), _SCORECARD if write_scorecard else "(skipped)")
    return scorecard
