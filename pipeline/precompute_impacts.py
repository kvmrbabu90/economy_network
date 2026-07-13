"""So What? V2 · Phase 2 — batch impact precompute.

For each queued event, trace it with a lighter Claude config (2 hops, no
refinement/verification) and write per-node verdicts to event_impacts. Budgeted,
idempotent (only 'queued' events), restartable (per-event transaction).

    python -B -m pipeline.precompute_impacts [--retry-failed]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

from api import impact as _impact   # patched in tests via pc._impact.run_impact
from schema import store

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = store.default_db_path()

PRECOMPUTE_MAX_EVENTS = int(os.environ.get("PRECOMPUTE_MAX_EVENTS", "25"))
# Default sized for the deployed HOURLY cadence (must finish well within the hour so
# the next trigger isn't dropped by the task's MultipleInstances=IgnoreNew). The env
# var overrides this; a 12h cadence can safely raise it.
PRECOMPUTE_WALLCLOCK_S = int(os.environ.get("PRECOMPUTE_WALLCLOCK_S", "3000"))
BATCH_MAX_HOPS = int(os.environ.get("PRECOMPUTE_MAX_HOPS", "2"))
# Cap on how many known seeds are handed to a trace. 1 = primary only (measured to
# best match the LLM reference's direction axis); 0 = no cap (full GKG org set).
PRECOMPUTE_SEED_CAP = int(os.environ.get("PRECOMPUTE_SEED_CAP", "1"))
# Augment the trace context with the article-enrichment capsule (events.enriched_context).
# On by default now that the A/B measured a real, never-harmful lift (+7pts, 2 recovered /
# 0 broke on a clean sample). ENRICH_ENABLED=0 restores headline+gkg-only.
ENRICH_ENABLED = os.environ.get("ENRICH_ENABLED", "1") == "1"


def _known_seed_ids(ev: dict) -> Optional[list[str]]:
    """The event's deterministically-resolved seed set (GKG/8-K carry `seed_ids`
    JSON), falling back to the single `seed_node_id`. None ⇒ the engine extracts
    seeds with the LLM (free-text events)."""
    raw = ev.get("seed_ids")
    if raw:
        try:
            ids = [x for x in json.loads(raw) if isinstance(x, str)]
            if ids:
                return ids
        except (ValueError, TypeError):
            pass
    sid = ev.get("seed_node_id")
    return [sid] if sid else None


def _any_commodity(conn, ids: Optional[list[str]]) -> bool:
    """True if any seed id is a Commodity/Region node — gates the commodity-seed
    LLM call so it fires only for commodity/macro-relevant stories."""
    if not ids:
        return False
    ph = ",".join("?" * len(ids))
    return conn.execute(
        f"SELECT 1 FROM nodes WHERE id IN ({ph}) AND type IN ('Commodity','Region') LIMIT 1", ids
    ).fetchone() is not None


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
                known = _known_seed_ids(ev)
                # MEASURED (ab_quality.py --e2e/--noisefloor, 2026-07-07): seeding the
                # FULL GKG org set changes hop directions vs the old primary-only path
                # (which the "LLM quality" target references) — below the LLM's own 0.94
                # self-consistency floor on ambiguous events. Cap the TRACE seed to the
                # primary; the secondary orgs still ground the trace via the gkg_context
                # capsule AND are reached by propagation (node set unchanged, jaccard=1.0).
                # PRECOMPUTE_SEED_CAP=0 restores full-set seeding.
                if known and PRECOMPUTE_SEED_CAP > 0:
                    known = known[:PRECOMPUTE_SEED_CAP]
                # AUGMENT (not replace) gkg_context with the article-enrichment capsule
                # when enabled: gkg_context carries secondary-org grounding the seed-cap
                # logic depends on, so we compose both. Off/absent → unchanged behavior.
                _enriched = ev.get("enriched_context") if ENRICH_ENABLED else None
                _context = "\n".join(x for x in (ev.get("gkg_context"), _enriched) if x) or None
                r = _impact.run_impact(ev["headline"], conn=conn, provider=prov,
                                       max_hops=BATCH_MAX_HOPS, refine=False, verify=False,
                                       known_seed_ids=known,
                                       commodity_hint=_any_commodity(conn, known) if known else None,
                                       context=_context)
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
