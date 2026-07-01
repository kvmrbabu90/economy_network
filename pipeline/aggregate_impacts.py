"""So What? V2 · Phase 3 — impact aggregation.

Roll event_impacts into a per-node combined verdict (node_impact) over a recency-
decayed rolling window. Fully rebuildable from event_impacts.

    python -B -m pipeline.aggregate_impacts
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date
from pathlib import Path
from typing import Optional

from schema import store

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = REPO_ROOT / "econgraph.db"

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
    today = today or date.today()
    rows = conn.execute(
        """
        SELECT ei.node_id, ei.event_id, ei.direction, ei.magnitude, ei.hop,
               e.headline, e.published_at, e.ingested_at
        FROM event_impacts ei JOIN events e ON e.id = ei.event_id
        WHERE e.status = 'traced'
        """
    ).fetchall()

    acc: dict[str, dict] = {}
    for r in rows:
        age = _age_days(r["published_at"] or r["ingested_at"], today)
        if age is None or age > window_days:
            continue
        w = 0.5 ** (max(0, age) / halflife)
        mag = float(r["magnitude"] or 0.0)
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

    computed_at = today.isoformat() + "T00:00:00"
    out = []
    for node_id, a in acc.items():
        pos, neg = a["pos"], a["neg"]
        mixed = 1 if (pos > 0 and neg > 0) else 0
        if pos > neg:
            direction, magnitude = "positive", min(1.0, pos - neg)
        elif neg > pos:
            direction, magnitude = "negative", min(1.0, neg - pos)
        else:
            direction, magnitude = "no_effect", 0.0
        if mixed and 0 < magnitude < 0.15:
            magnitude = 0.15
        top = sorted(a["events"], key=lambda e: -abs(e["weighted"]))[:top_n]
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
