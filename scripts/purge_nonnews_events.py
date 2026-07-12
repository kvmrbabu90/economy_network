"""One-time cleanup: remove already-traced events that the non-news classifier now
flags (investment advice / analyst rating & price-target actions / dividend-earnings
logistics / opinion), so existing impact panels and the live map stop showing them.

For each matching traced event it deletes the event_impacts it produced and marks the
event status 'dropped_nonnews' (excluded from aggregate, and event_exists() keeps it
from being re-queued / re-traced). Then re-aggregates node_impact. Restartable and
idempotent — a second run finds nothing left to purge.

    python -B scripts/purge_nonnews_events.py            # dry-run: count only
    python -B scripts/purge_nonnews_events.py --apply    # delete + re-aggregate

Honors ECONGRAPH_DB. The non-news rule lives in pipeline.ingest_news._looks_like_non_news
(measured false-drop rate ~0 via scripts' validate-nonnews-drops workflow).
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from schema import store                       # noqa: E402
from pipeline.ingest_news import _looks_like_non_news   # noqa: E402
from pipeline import aggregate_impacts          # noqa: E402


def _chunks(seq, n=500):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="perform the delete + re-aggregate")
    args = ap.parse_args()

    conn = store.connect(store.default_db_path())
    store.init_db(conn)
    conn.row_factory = sqlite3.Row

    traced = conn.execute("SELECT id, headline FROM events WHERE status='traced'").fetchall()
    targets = [r["id"] for r in traced if _looks_like_non_news(r["headline"])]

    ei = 0
    for chunk in _chunks(targets):
        ph = ",".join("?" * len(chunk))
        ei += conn.execute(f"SELECT COUNT(*) FROM event_impacts WHERE event_id IN ({ph})", chunk).fetchone()[0]

    print(f"traced events: {len(traced)} | non-news to purge: {len(targets)} | "
          f"event_impacts they produced: {ei}")

    if not args.apply:
        print("dry-run; pass --apply to delete the impacts, mark the events, and re-aggregate.")
        return 0
    if not targets:
        print("nothing to purge.")
        return 0

    for chunk in _chunks(targets):
        ph = ",".join("?" * len(chunk))
        conn.execute(f"DELETE FROM event_impacts WHERE event_id IN ({ph})", chunk)
        conn.execute(f"UPDATE events SET status='dropped_nonnews' WHERE id IN ({ph})", chunk)
    conn.commit()
    print(f"deleted {ei} event_impacts; marked {len(targets)} events 'dropped_nonnews'.")

    summary = aggregate_impacts.aggregate(conn)
    print(f"re-aggregated node_impact: {summary}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
