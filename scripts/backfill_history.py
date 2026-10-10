"""Historical backfill: ingest + trace old GDELT-GKG news so the longer validation horizons
(+20d/+60d) mature immediately instead of over the coming weeks.

Why a dedicated path (not run_cycle): normal ingest (pipeline/ingest_news.fetch_gkg_bulk) only
walks back GKG_SLICES from NOW, and run_cycle's prune deletes events older than ~30 days — so
historical signals need their own, prune-aware route. This orchestrator:

  1. INGEST  (no LLM): sample GKG slices across [start, end], match GDELT orgs to graph nodes,
     dedupe, materiality-threshold, cap, and insert as status='queued'. Bypasses INGEST_CAP and
     the recency rank (which would bury old news). RULE-ONLY materiality (prior threshold) — it
     does NOT call the LLM materiality gate, so ingest costs ZERO quota.
  2. TRACE   (LLM/quota): precompute in bounded batches until the queue drains or --max-events
     is hit. This is the only quota-consuming stage (~5-10 Claude calls/event).
  3. VALIDATE(no LLM): run_validation after each batch, so signal_outcomes — which is NEVER
     pruned — is captured durably even if a later prune deletes the old events + event_impacts.

Prune safety: set PRUNE_OLDER_THAN_DAYS high on the hourly cycle (e.g. 120) while backfilling so
old events survive long enough to be traced; once validated the reliability record persists
regardless. Restartable: slice downloads are cache-first (#6), insert_event is INSERT OR IGNORE,
precompute only touches status='queued' — re-running resumes where it stopped.

    # tiny ingest-only pilot (no quota):
    python -B scripts/backfill_history.py --start 2026-08-11 --end 2026-08-13 --max-events 6 --no-trace
    # full armed run (post-reset), bounded:
    python -B scripts/backfill_history.py --start 2026-07-01 --end 2026-09-15 \
        --sample-hours 24 --max-events 250 --trace-batch 20 --db C:/Users/Public/econgraph/econgraph.db
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from schema import store                                          # noqa: E402
from pipeline import gkg, ingest_news, precompute_impacts, run_validation  # noqa: E402

log = logging.getLogger("backfill_history")


def _slice_timestamps(start: str, end: str, sample_hours: int) -> list[str]:
    """YYYYMMDDHHMMSS slice timestamps sampled across [start, end] inclusive. Anchored at
    13:00 and stepped by whole hours so every stamp lands on a real :00 GKG slice."""
    t = datetime.strptime(start, "%Y-%m-%d").replace(hour=13, minute=0, second=0)
    end_dt = datetime.strptime(end, "%Y-%m-%d").replace(hour=23, minute=59, second=0)
    step = timedelta(hours=max(1, sample_hours))
    out: list[str] = []
    while t <= end_dt:
        out.append(t.strftime("%Y%m%d%H%M%S"))
        t += step
    return out


def ingest_history(conn, start: str, end: str, sample_hours: int, min_score: float,
                   max_events: int) -> dict:
    """Match historical GKG slices to nodes and queue the top-materiality events. No LLM."""
    cache_dir = ingest_news.GKG_CACHE_DIR
    idx = ingest_news.build_gkg_node_index(conn)
    cen = ingest_news._centrality(conn)
    timestamps = _slice_timestamps(start, end, sample_hours)
    log.info("ingest: %d sampled slices %s..%s (every %dh)", len(timestamps), start, end, sample_hours)

    collected: list[tuple[float, dict]] = []
    slices_ok = slices_missing = 0
    for ts in timestamps:
        path = gkg.download_slice(gkg.gkg_slice_url(ts), cache_dir)
        if path is None:
            slices_missing += 1
            continue
        try:
            records = gkg.parse_gkg(gkg.read_gkg_lines(path))
        except Exception as exc:
            log.warning("ingest: slice %s parse failed: %s", ts, exc)
            slices_missing += 1
            continue
        slices_ok += 1
        for rec in records:
            res = ingest_news._gkg_candidate(rec, idx, cen)
            if res is not None and res[0] >= min_score:
                collected.append(res)

    # Highest-materiality first so the per-run cap keeps the best events, then dedupe
    # (url / story-signature / cross-feed, also against already-stored events).
    collected.sort(key=lambda t: -t[0])
    fresh = ingest_news.dedupe([c for _, c in collected], conn)
    to_insert = fresh[: max_events] if max_events and max_events > 0 else fresh

    inserted = 0
    for c in to_insert:
        try:
            store.insert_event(conn, {**c, "status": "queued"})
            inserted += 1
        except Exception as exc:
            log.warning("ingest: insert %s failed: %s", c.get("id"), exc)
    conn.commit()
    summary = {"slices_ok": slices_ok, "slices_missing": slices_missing,
               "matched": len(collected), "fresh": len(fresh), "queued": inserted}
    log.info("ingest: %s", summary)
    return summary


def trace_and_validate(conn, db_path, max_events: int, trace_batch: int, provider: str) -> dict:
    """Trace queued events in bounded batches; run_validation after each so outcomes persist."""
    totals = {"batches": 0, "traced": 0, "failed": 0, "impacts": 0}
    done = 0
    while True:
        remaining = len(store.queued_events(conn))
        if remaining == 0:
            break
        if max_events and done >= max_events:
            log.info("trace: hit --max-events cap (%d); %d still queued", max_events, remaining)
            break
        batch = trace_batch
        if max_events:
            batch = min(batch, max_events - done)
        res = precompute_impacts.run_precompute(db_path, max_events=batch, provider=provider)
        totals["batches"] += 1
        totals["traced"] += res.get("traced", 0)
        totals["failed"] += res.get("failed", 0)
        totals["impacts"] += res.get("impacts_written", 0)
        done += res.get("processed", 0)
        log.info("trace: batch %d -> %s", totals["batches"], res)
        # Capture outcomes durably right away (signal_outcomes is never pruned).
        val = run_validation.run_validation(conn)
        log.info("validate: %s", val["coverage"])
        if res.get("processed", 0) == 0:
            log.warning("trace: batch processed 0 (auth/budget?) — stopping")
            break
    return totals


def main() -> int:
    ap = argparse.ArgumentParser(description="Historical GKG backfill for signal-reliability horizons.")
    ap.add_argument("--start", required=True, help="window start YYYY-MM-DD")
    ap.add_argument("--end", required=True, help="window end YYYY-MM-DD")
    ap.add_argument("--sample-hours", type=int, default=24, help="hours between sampled slices (default 24 = 1/day)")
    ap.add_argument("--min-score", type=float, default=0.0, help="min GKG materiality prior to keep")
    ap.add_argument("--max-events", type=int, default=250, help="cap total events queued/traced (bounds quota)")
    ap.add_argument("--trace-batch", type=int, default=20, help="events per precompute batch")
    ap.add_argument("--no-trace", action="store_true", help="ingest only (no LLM); leaves events queued")
    ap.add_argument("--provider", default="claude", help="trace provider (claude|ollama)")
    ap.add_argument("--db", default=None, help="SQLite path (default ECONGRAPH_DB / canonical)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    conn = store.connect(args.db)
    store.init_db(conn)
    try:
        ing = ingest_history(conn, args.start, args.end, args.sample_hours, args.min_score, args.max_events)
        if args.no_trace:
            print(f"[backfill] ingest-only: {ing}  ({ing['queued']} events queued, not traced)")
            return 0
        tr = trace_and_validate(conn, args.db, args.max_events, args.trace_batch, args.provider)
        final = run_validation.run_validation(conn)
        print(f"[backfill] ingest={ing} trace={tr} coverage={final['coverage']}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
