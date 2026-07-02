"""So What? V2 · Phase 3 — impact aggregation.

Roll event_impacts into a per-node combined verdict (node_impact) over a recency-
decayed rolling window. Fully rebuildable from event_impacts.

    python -B -m pipeline.aggregate_impacts
"""
from __future__ import annotations

import json
import logging
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
               e.headline, e.published_at, e.ingested_at
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
        # Clamp to [0, 1] as defense-in-depth against any out-of-range stored magnitude.
        mag = max(0.0, min(1.0, float(r["magnitude"] or 0.0)))
        contrib = w * mag
        direction = r["direction"]
        a = acc.setdefault(r["node_id"], {"pos": 0.0, "neg": 0.0, "count": 0, "events": []})
        a["count"] += 1
        signed = 0.0
        if direction == "positive":
            a["pos"] += contrib; signed = contrib
        elif direction == "negative":
            a["neg"] += contrib; signed = -contrib
        a["events"].append({"event_id": r["event_id"], "headline": r["headline"],
                            "direction": direction, "magnitude": round(mag, 3),
                            "weighted": round(signed, 4), "hop": r["hop"],
                            "published_at": r["published_at"]})

    # Full UTC timestamp (with time) so 24 hourly cycles are distinguishable and a
    # stalled pipeline is detectable (a date-only stamp collapses them all to midnight).
    computed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    out = []
    for node_id, a in acc.items():
        pos, neg = a["pos"], a["neg"]
        if pos > neg:
            direction, magnitude = "positive", min(1.0, pos - neg)
        elif neg > pos:
            direction, magnitude = "negative", min(1.0, neg - pos)
        else:
            direction, magnitude = "no_effect", 0.0
        # Only flag MIXED for a node that actually carries mass: a net-zero (magnitude-0)
        # node must read neutral on the map, not MIXED. So require a non-zero net (or the
        # mixed floor to have kicked in) before setting the flag.
        mixed = 1 if (pos > 0 and neg > 0 and magnitude > 0.0) else 0
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
        out.append({"node_id": node_id, "direction": direction, "magnitude": round(magnitude, 3),
                    "mixed_signals": mixed, "event_count": a["count"],
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
