"""So What? V2 · Phase 3 — impact aggregation.

Roll event_impacts into a per-node combined verdict (node_impact) over a recency-
decayed rolling window. Fully rebuildable from event_impacts.

    python -B -m pipeline.aggregate_impacts
"""
from __future__ import annotations

import json
import logging
import math
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from schema import store

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = store.default_db_path()

IMPACT_WINDOW_DAYS = int(os.environ.get("IMPACT_WINDOW_DAYS", "7"))
IMPACT_HALFLIFE_DAYS = float(os.environ.get("IMPACT_HALFLIFE_DAYS", "3"))
TOP_EVENTS_PER_NODE = int(os.environ.get("TOP_EVENTS_PER_NODE", "5"))
# A node is only MIXED (amber) when the WEAKER signal is a meaningful fraction of the
# stronger one — genuinely conflicting news, not one small opposing event. Below this
# ratio the node reads as its dominant direction (green/red). Flagging any minority
# signal as mixed painted ~77% of the map amber, incl. strongly-net nodes.
IMPACT_MIXED_SIGNAL_RATIO = float(os.environ.get("IMPACT_MIXED_SIGNAL_RATIO", "0.6"))
# Below this combined magnitude a node is not tinted on the map. This MUST equal
# web/src/impact.ts IMPACT_TINT_FLOOR: sub-floor non-mixed rows are dropped from node_impact
# so the map tint, /impact/live, and the /node/{id}/impact panel all agree — a node the map
# won't tint must not present a combined-impact box. NOT env-overridable on purpose: the TS
# floor is a hardcoded constant, so an env override here would silently desync the two sides
# and re-create the untinted-node-with-populated-panel bug. Change both constants together.
IMPACT_TINT_FLOOR = 0.05
# Distance decay: a contribution's weight is multiplied by HOP_WEIGHT**hop, so news ABOUT a
# node (hop 0) counts full, a neighbor's news (hop 1) counts HOP_WEIGHT, a neighbor-of-neighbor
# (hop 2) HOP_WEIGHT**2. This replaces the old "damp only when direct_count==0" special case
# with one uniform rule: a company's own news should dominate news about its neighbors.
# MEASURED (2026-07-17): at 0.5, propagated news over-riding a node's OWN direction dropped
# from 92/416 nodes to 53, pure-spillover nodes softened (mean 0.28->0.19), and the direction
# split barely moved (Amgen's competitor-driven NEGATIVE 1.00 -> 0.76). 1.0 = disabled (flat).
IMPACT_HOP_WEIGHT = float(os.environ.get("IMPACT_HOP_WEIGHT", "0.5"))
# Consensus discount for MIXED nodes. magnitude = |tanh(pos-neg)| reflects the NET lean, so a
# node with heavy signal BOTH ways can read near-max even though it's contested (Nvidia: pos
# 8.0 / neg 6.2 → net 1.82 → 0.95, yet the minority is 77% of the majority). Scale a mixed
# node's magnitude by how one-sided it is — factor = 1 - DAMP*(minority/majority) — so a
# contested node tints SOFTER than a clean one of the same net. DAMP=1 → full consensus
# discount (a 77%-mixed node keeps 23% of its magnitude); 0 → disabled. The 0.15 mixed floor
# below still keeps it a visible dim amber rather than dropping it off the map.
IMPACT_MIXED_DAMP = float(os.environ.get("IMPACT_MIXED_DAMP", "1.0"))


def _age_days(row_date: Optional[str], today: date) -> Optional[int]:
    if not row_date:
        return None
    try:
        return (today - date.fromisoformat(str(row_date)[:10])).days
    except Exception:
        return None


def aggregate(conn, *, today: Optional[date] = None, window_days: int = IMPACT_WINDOW_DAYS,
              halflife: float = IMPACT_HALFLIFE_DAYS, top_n: int = TOP_EVENTS_PER_NODE) -> dict:
    # Default to UTC "today" to match events.ingested_at (SQLite datetime('now') is
    # UTC), so window/age math near the boundary is consistent for the fallback basis.
    today = today or datetime.now(timezone.utc).date()
    # Bound the scan to the window in SQL so cost tracks the window, not all history.
    # The cutoff is derived from `today` (not SQL's now()) so the scan bound stays
    # consistent with the Python age math below for any caller-supplied reference date.
    # date() on both sides makes the comparison chronological even when the stored value
    # carries a time component (ingested_at defaults to datetime('now') → 'YYYY-MM-DD HH:MM:SS').
    # The precise recency decay + age filter still happens in Python below.
    cutoff = (today - timedelta(days=window_days)).isoformat()
    rows = conn.execute(
        """
        SELECT ei.node_id, ei.event_id, ei.direction, ei.magnitude, ei.hop,
               e.headline, e.published_at, e.ingested_at, e.seed_node_id
        FROM event_impacts ei JOIN events e ON e.id = ei.event_id
        WHERE e.status = 'traced'
          AND date(COALESCE(NULLIF(e.published_at, ''), e.ingested_at)) >= date(?)
        """,
        (cutoff,),
    ).fetchall()

    acc: dict[str, dict] = {}
    for r in rows:
        age = _age_days(r["published_at"] or r["ingested_at"], today)
        # Reject future-dated events (age < 0) so they never earn max recency weight.
        if age is None or age < 0 or age > window_days:
            continue
        w = 0.5 ** (max(0, age) / halflife)
        # magnitude is a NON-NEGATIVE weight; sign lives in `direction`. Use abs() so a
        # stored signed-negative magnitude (a 'negative' row written as -0.30) contributes
        # its intended weight 0.30 instead of being zeroed by a [0,1] clamp — which
        # previously dropped real drivers and could flip a node's net tint. min(1.0, .)
        # still caps out-of-range values.
        mag = min(1.0, abs(float(r["magnitude"] or 0.0)))
        hop = r["hop"] or 0
        # Distance decay: news about a neighbor counts less than news about the node itself.
        contrib = w * mag * (IMPACT_HOP_WEIGHT ** hop)
        direction = r["direction"]
        a = acc.setdefault(r["node_id"],
                           {"pos": 0.0, "neg": 0.0, "count": 0, "events": [], "seen": {}})
        a["count"] += 1                              # every scanned row (incl. no_effect/dupes)
        if direction not in ("positive", "negative"):
            a["events"].append({"event_id": r["event_id"], "headline": r["headline"],
                                "direction": direction, "magnitude": round(mag, 3),
                                "weighted": 0.0, "hop": hop, "published_at": r["published_at"]})
            continue
        # Same-source dedup: one (source entity, day, direction) is ONE story — collapse the
        # duplicate press-release copies (e.g. a competitor's FDA approval carried by 4 wires,
        # each propagating negative to a peer). Keep only the STRONGEST copy so a single event
        # can't be counted N times. Applies at every hop (incl. a node's own repeated-headline
        # day). Genuinely distinct same-source same-day same-direction items collapse too — a
        # conservative anti-amplification tradeoff.
        day = (r["published_at"] or r["ingested_at"] or "")[:10]
        key = (r["seed_node_id"], day, direction)
        prev = a["seen"].get(key)
        if prev is not None:
            if contrib <= prev["contrib"]:
                continue                             # weaker duplicate — drop it
            # stronger duplicate — retract the previous copy's mass + driver, then re-add below
            a["pos" if direction == "positive" else "neg"] -= prev["contrib"]
            a["events"].remove(prev["event"])
        signed = contrib if direction == "positive" else -contrib
        a["pos" if direction == "positive" else "neg"] += contrib
        ev = {"event_id": r["event_id"], "headline": r["headline"], "direction": direction,
              "magnitude": round(mag, 3), "weighted": round(signed, 4), "hop": hop,
              "published_at": r["published_at"]}
        a["events"].append(ev)
        a["seen"][key] = {"contrib": contrib, "event": ev}

    # Full UTC timestamp (with time) so 24 hourly cycles are distinguishable and a
    # stalled pipeline is detectable (a date-only stamp collapses them all to midnight).
    computed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    out = []
    for node_id, a in acc.items():
        pos, neg = a["pos"], a["neg"]
        # tanh-normalize the net so the cumulative signal stays bounded in (-1, 1)
        # WITHOUT the old hard-clamp saturation (min(1.0, .) mapped both net=1.5 and
        # net=4 to a flat 1.0). tanh is smooth + monotonic, so relative strength above
        # the old ceiling is preserved. signed ∈ (-1,1); magnitude = |signed|.
        signed = math.tanh(pos - neg)
        if signed > 0:
            direction, magnitude = "positive", signed
        elif signed < 0:
            direction, magnitude = "negative", -signed
        else:
            direction, magnitude = "no_effect", 0.0
        # Contributing drivers + how many are DIRECT (hop 0 — news about this node itself).
        # direct_count still labels the panel ("propagated — no direct news"); the magnitude
        # attenuation for spillover now comes from IMPACT_HOP_WEIGHT applied per-contribution
        # above (a pure-spillover node has only hop>=1 rows, so all its mass is already
        # down-weighted) — no separate direct_count==0 damp needed.
        contributing = [e for e in a["events"] if e["weighted"] != 0.0]
        direct_count = sum(1 for e in contributing if e.get("hop") == 0)
        # Only flag MIXED when the signals genuinely conflict: the WEAKER side must be
        # at least IMPACT_MIXED_SIGNAL_RATIO of the stronger. A node that is overwhelmingly
        # one direction (a small opposing event) reads as that direction, not amber. Also
        # requires non-zero mass (a net-zero node reads neutral, never MIXED).
        minority, majority = min(pos, neg), max(pos, neg)
        mixed = 1 if (minority > 0 and majority > 0 and magnitude > 0.0
                      and minority >= IMPACT_MIXED_SIGNAL_RATIO * majority) else 0
        # Consensus discount (see IMPACT_MIXED_DAMP): a mixed node's magnitude reflected the
        # NET lean and could read near-max while heavily contested. Scale it down by how
        # balanced the two sides are, BEFORE the 0.15 floor so contested nodes stay a dim amber.
        if mixed and majority > 0:
            magnitude *= (1.0 - IMPACT_MIXED_DAMP * (minority / majority))
        if mixed and 0 < magnitude < 0.15:
            magnitude = 0.15
        # top_events shows real drivers only: drop zero-weighted rows (unscored /
        # no_effect) — they still count toward event_count but aren't "contributions".
        contributing = [e for e in a["events"] if e["weighted"] != 0.0]
        top = sorted(contributing, key=lambda e: -abs(e["weighted"]))[:top_n]
        # Skip inert nodes: no direction, no mass, no drivers. Emitting them bloats the
        # payload and produces the "N events, no drivers" contradiction on the map.
        if direction == "no_effect" and magnitude == 0.0 and not top:
            continue
        # Skip SUB-FLOOR directional rows: a positive/negative verdict whose magnitude
        # is at/below the tint floor never colours the map (impact.ts IMPACT_TINT_FLOOR),
        # so emitting it would produce an untinted node that nonetheless shows a
        # populated combined-impact panel. Mixed rows are exempt (the bump above lifts
        # them to 0.15 > floor); no_effect/net-zero rows are left as-is (they already
        # don't tint and render "No recent impact." in the panel).
        if not mixed and direction in ("positive", "negative") and magnitude <= IMPACT_TINT_FLOOR:
            continue
        # driver_count = the real drivers (nonzero-weighted rows), distinct from
        # event_count which counts EVERY scanned window row (incl. unscored/no_effect).
        # direct_count (computed above) = of those drivers, the DIRECT (hop-0) ones;
        # direct_count==0 means the verdict is pure PROPAGATED spillover — the panel flags
        # it and its magnitude was damped above so a 0.90 with no story reads honestly.
        out.append({"node_id": node_id, "direction": direction, "magnitude": round(magnitude, 3),
                    "mixed_signals": mixed, "event_count": a["count"], "driver_count": len(contributing),
                    "direct_count": direct_count,
                    "top_events": json.dumps(top), "computed_at": computed_at})

    store.replace_node_impact(conn, out)
    summary = {"nodes": len(out),
               "positive": sum(1 for r in out if r["direction"] == "positive"),
               "negative": sum(1 for r in out if r["direction"] == "negative"),
               "mixed": sum(1 for r in out if r["mixed_signals"]),
               "events_in_window": len({e["event_id"] for a in acc.values() for e in a["events"]})}
    log.info("aggregate: %s", summary)
    return summary


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    conn = store.connect(DB_PATH)
    store.init_db(conn)
    try:
        s = aggregate(conn)
    finally:
        conn.close()
    print(f"aggregate cycle: {s}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
