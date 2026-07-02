"""So What? V2 · Phase 2 — batch impact precompute.

For each queued event, trace it with a lighter Claude config (2 hops, no
refinement/verification) and write per-node verdicts to event_impacts. Budgeted,
idempotent (only 'queued' events), restartable (per-event transaction).

    python -B -m pipeline.precompute_impacts [--retry-failed]
"""
from __future__ import annotations

import argparse
import logging
import os
import time
from pathlib import Path
from typing import Optional

from api import impact as _impact   # patched in tests via pc._impact.run_impact
from schema import store

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = REPO_ROOT / "econgraph.db"

PRECOMPUTE_MAX_EVENTS = int(os.environ.get("PRECOMPUTE_MAX_EVENTS", "25"))
PRECOMPUTE_WALLCLOCK_S = int(os.environ.get("PRECOMPUTE_WALLCLOCK_S", str(6 * 3600)))
BATCH_MAX_HOPS = int(os.environ.get("PRECOMPUTE_MAX_HOPS", "2"))


def run_precompute(db_path: Path = DB_PATH, *, max_events: int = PRECOMPUTE_MAX_EVENTS,
                   wallclock_s: int = PRECOMPUTE_WALLCLOCK_S,
                   provider: Optional[str] = None, retry_failed: bool = False) -> dict:
    conn = store.connect(db_path)
    store.init_db(conn)
    prov = provider or os.environ.get("IMPACT_LLM_PROVIDER", "claude")
    summary = {"processed": 0, "traced": 0, "failed": 0, "impacts_written": 0, "elapsed_s": 0.0}
    t0 = time.time()

    def _mark_failed(eid: str) -> None:
        # Clear any impacts a prior trace may have written for this event, so a
        # re-traced event (e.g. via --retry-failed) that now fails never leaves
        # stale rows behind for aggregation. No-op when there were none.
        store.write_event_impacts(conn, eid, [])
        store.set_event_status(conn, eid, "failed")
        summary["failed"] += 1

    try:
        if retry_failed:
            conn.execute("UPDATE events SET status='queued' WHERE status='failed'")
            conn.commit()
        events = store.queued_events(conn)
        for ev in events:
            if summary["processed"] >= max_events or (time.time() - t0) >= wallclock_s:
                log.info("precompute: budget hit; deferring %d events", len(events) - summary["processed"])
                break
            summary["processed"] += 1
            try:
                r = _impact.run_impact(ev["headline"], conn=conn, provider=prov,
                                       max_hops=BATCH_MAX_HOPS, refine=False, verify=False,
                                       seed_hint_id=ev.get("seed_node_id"))
            except Exception as exc:
                log.warning("precompute: %s trace raised %s", ev["id"], exc)
                _mark_failed(ev["id"])
                continue
            has_seeds = bool(r.get("seed")) or bool(r.get("seeds"))
            impacts = r.get("impacts") or []
            # Real propagation gate: a seed-only / no_neighbors / all-no_effect trace
            # must NOT be written+marked 'traced' — doing so injects phantom seed-only
            # rows into node_impact. Require at least one downstream (hop>=1) impact with
            # a directional verdict, and reject explicit error/no_neighbors signals.
            propagated = any(
                (i.get("hop") or 0) >= 1 and i.get("direction") in ("positive", "negative")
                for i in impacts
            )
            if (not has_seeds or not impacts or r.get("error") or r.get("no_neighbors")
                    or not propagated):
                _mark_failed(ev["id"])
                continue
            store.write_event_impacts(conn, ev["id"], impacts)
            store.set_event_status(conn, ev["id"], "traced")
            summary["traced"] += 1
            summary["impacts_written"] += len(impacts)
        summary["elapsed_s"] = round(time.time() - t0, 1)
        log.info("precompute: %s", summary)
        return summary
    finally:
        conn.close()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description="Batch impact precompute for queued events.")
    ap.add_argument("--retry-failed", action="store_true", help="re-queue 'failed' events first")
    ap.add_argument("--max-events", type=int, default=PRECOMPUTE_MAX_EVENTS)
    args = ap.parse_args()
    s = run_precompute(max_events=args.max_events, retry_failed=args.retry_failed)
    print(f"precompute cycle: {s}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
