"""One-time backfill: enrich already-traced AMBIGUOUS in-window events (high-reach first)
and RE-TRACE them with the capsule, so the CURRENT map reflects article context instead of
just headline+gkg. Forward enrichment already handles NEW events, so this only accelerates
the current 7-day window.

Restartable + budgeted: enrich skips already-enriched rows; re-trace marks enrich_tier=9 and
skips already-backfilled rows; re-run with a bigger --limit to go further. Re-aggregates at
the end so the tints update.

    python -B scripts/backfill_enrich.py --limit 150 [--provider claude]

ROI is modest (only ~7% of ambiguous verdicts flip), so start small and widen if worth it.
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from schema import store                                    # noqa: E402
from pipeline import enrich, aggregate_impacts              # noqa: E402
from pipeline.ingest_news import _has_material_trigger      # noqa: E402
from pipeline.precompute_impacts import BATCH_MAX_HOPS, PRECOMPUTE_SEED_CAP  # noqa: E402
from api import impact as _impact                           # noqa: E402

log = logging.getLogger(__name__)
RETRACED_SENTINEL = 9   # events.enrich_tier value marking "backfill re-traced" (restart guard)


def _known(ev: dict) -> list[str]:
    ids = json.loads(ev.get("seed_ids") or "[]")
    if PRECOMPUTE_SEED_CAP > 0:
        ids = ids[:PRECOMPUTE_SEED_CAP]
    return ids or ([ev["seed_node_id"]] if ev.get("seed_node_id") else [])


def _candidates(conn, limit: int, window_days: int) -> list[dict]:
    rows = conn.execute(
        "SELECT id, headline, url, seed_entity, seed_ids, seed_node_id, enrich_status, enrich_tier "
        "FROM events WHERE status='traced' AND url LIKE 'http%' "
        "AND date(COALESCE(NULLIF(published_at,''), ingested_at)) >= date('now', ?)",
        (f"-{window_days} days",),
    ).fetchall()
    cands = [dict(r) for r in rows
             if not _has_material_trigger(r["headline"] or "")
             and (r["enrich_tier"] is None or r["enrich_tier"] != RETRACED_SENTINEL)]
    cands.sort(key=lambda c: -enrich._seed_reach(conn, c.get("seed_node_id")))
    return cands[:limit]


def backfill(conn, *, limit: int, provider: str, window_days: int = 7,
             wallclock_s: float = 3600.0) -> dict:
    cands = _candidates(conn, limit, window_days)
    deadline = time.monotonic() + wallclock_s
    summary = {"candidates": len(cands), "enriched": 0, "no_capsule": 0, "retraced": 0, "failed": 0}
    for ev in cands:
        if time.monotonic() > deadline:
            log.info("backfill: wallclock hit; %d left (re-run to continue)", summary["candidates"] - summary["retraced"] - summary["no_capsule"])
            break
        # 1) Enrich this event if not already attempted (fetch→reduce→summarize→ground→store).
        if ev.get("enrich_status") not in ("done", "no_content", "failed", "skipped"):
            html, _ = enrich.article.fetch_article(ev.get("url"))
            cap_rendered = None
            if html:
                reduced = enrich.article.reduce_html(html, enrich._seed_names(conn, ev))
                if len(reduced) >= enrich.article.ARTICLE_MIN_CHARS:
                    cap = enrich._summarize(ev.get("headline"), reduced, provider)
                    if cap is not None:
                        cap = enrich._ground(cap, reduced)
                        cap_rendered = cap.render() if cap.is_informative() else None
            store.set_event_enrichment(conn, ev["id"], enriched_context=cap_rendered,
                                       status=("done" if cap_rendered else "no_content"),
                                       version=enrich.ENRICH_VERSION)
            ev["enriched_context"] = cap_rendered
        else:
            row = conn.execute("SELECT enriched_context FROM events WHERE id=?", (ev["id"],)).fetchone()
            ev["enriched_context"] = row[0] if row else None

        if not ev.get("enriched_context"):
            summary["no_capsule"] += 1
            continue
        summary["enriched"] += 1

        # 2) Re-trace with gkg + capsule and overwrite this event's impacts.
        gk = conn.execute("SELECT gkg_context FROM events WHERE id=?", (ev["id"],)).fetchone()[0]
        context = "\n".join(x for x in (gk, ev["enriched_context"]) if x) or None
        try:
            res = _impact.run_impact(ev["headline"], conn=conn, provider=provider,
                                     max_hops=BATCH_MAX_HOPS, refine=False, verify=False,
                                     known_seed_ids=_known(ev), context=context)
            store.write_event_impacts(conn, ev["id"], res.get("impacts") or [])
            conn.execute("UPDATE events SET enrich_tier=? WHERE id=?", (RETRACED_SENTINEL, ev["id"]))
            conn.commit()
            summary["retraced"] += 1
        except Exception as exc:
            log.warning("backfill: re-trace %s failed: %s", ev["id"], exc)
            summary["failed"] += 1

    # 3) Re-aggregate so the tints reflect the re-traced verdicts.
    summary["aggregate"] = aggregate_impacts.aggregate(conn)
    return summary


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=150)
    ap.add_argument("--provider", choices=["claude", "ollama"], default=enrich.ENRICH_LLM_PROVIDER)
    ap.add_argument("--window-days", type=int, default=7)
    args = ap.parse_args()
    conn = store.connect(store.default_db_path())
    store.init_db(conn)
    conn.row_factory = sqlite3.Row
    try:
        s = backfill(conn, limit=args.limit, provider=args.provider, window_days=args.window_days)
    finally:
        conn.close()
    print(f"backfill: {s}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
