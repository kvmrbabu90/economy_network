"""One-off recovery for the Claude CLI login outage (2026-11-06 15:05 UTC to the user's re-login 2026-11-09).

During the outage every Claude call failed, the materiality gate failed open, and every queued event was marked
'failed' without a real trace. This replays the gate the way ingest would have applied it:
- SEC 8-K events are material by construction (ingest auto-keeps them) -> 'queued';
- every other event gets the real LLM materiality gate: material -> 'queued', the rest -> 'skipped' (kept, not
  deleted; nothing re-queues 'skipped', and aggregation only reads 'traced');
- re-queued events whose enrichment failed (an auth failure too) get enrich_status reset so enrichment retries;
- only events ingested in the window AND still 'failed' are touched.
Dry run by default. With --apply, every change (id, old status, old enrich_status, new status) is written to a JSON
manifest first, so the operation can be reversed. Refuses to run while the CLI is still unauthenticated.

    python -B -m scripts.recover_auth_outage [--since "2026-11-06 15:05"] [--until ...] [--apply]
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from schema import store

log = logging.getLogger("recover_auth_outage")
DEFAULT_SINCE = "2026-11-06 15:05"
AUTOKEEP_SOURCES = {"SEC 8-K"}


def _default_gate(cands: list[dict]) -> list[dict]:
    from pipeline.ingest_news import _materiality_filter
    return _materiality_filter(cands)


def _default_auth_check() -> Optional[str]:
    from api.impact import check_claude_auth
    return check_claude_auth("claude")


def run(db_path, *, since: str = DEFAULT_SINCE, until: Optional[str] = None, apply: bool = False,
        gate: Callable[[list[dict]], list[dict]] = _default_gate,
        auth_check: Callable[[], Optional[str]] = _default_auth_check,
        manifest_dir: Optional[Path] = None) -> dict:
    reason = auth_check()
    if reason:
        raise RuntimeError(f"Claude CLI not authenticated ({reason}); log in first")
    conn = store.connect(db_path)
    try:
        q = "SELECT id, source, headline, seed_entity, status, enrich_status FROM events " \
            "WHERE status = 'failed' AND ingested_at >= ?"
        args: list = [since]
        if until:
            q += " AND ingested_at < ?"
            args.append(until)
        rows = [dict(r) for r in conn.execute(q + " ORDER BY ingested_at", args).fetchall()]
        auto = [r for r in rows if r["source"] in AUTOKEEP_SOURCES]
        judge = [r for r in rows if r["source"] not in AUTOKEEP_SOURCES]
        kept = gate([{"id": r["id"], "headline": r["headline"], "seed_entity": r["seed_entity"]} for r in judge])
        material = {r["id"] for r in auto} | {c["id"] for c in kept}
        plan = {"window": [since, until], "considered": len(rows),
                "requeue": sorted(r["id"] for r in rows if r["id"] in material),
                "skip": sorted(r["id"] for r in rows if r["id"] not in material), "manifest": None}
        if not apply:
            return plan
        manifest_dir = Path(manifest_dir or (Path(db_path).parent))
        manifest = manifest_dir / f"recover_auth_outage_{datetime.now():%Y%m%d-%H%M%S}.json"
        changes = [{"id": r["id"], "old_status": r["status"], "old_enrich_status": r["enrich_status"],
                    "new_status": "queued" if r["id"] in material else "skipped"} for r in rows]
        manifest.write_text(json.dumps({"window": plan["window"], "changes": changes}, indent=2),
                            encoding="utf-8", newline="\n")
        with conn:
            for ch in changes:
                if ch["new_status"] == "queued":
                    conn.execute("UPDATE events SET status = 'queued', enrich_status = CASE WHEN enrich_status = "
                                 "'failed' THEN NULL ELSE enrich_status END WHERE id = ? AND status = 'failed'",
                                 (ch["id"],))
                else:
                    conn.execute("UPDATE events SET status = 'skipped' WHERE id = ? AND status = 'failed'",
                                 (ch["id"],))
        plan["manifest"] = manifest
        return plan
    finally:
        conn.close()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", default=DEFAULT_SINCE)
    ap.add_argument("--until")
    ap.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    args = ap.parse_args()
    plan = run(store.default_db_path(), since=args.since, until=args.until, apply=args.apply)
    log.info("considered %d failed events; re-queue %d, skip %d; manifest %s", plan["considered"],
             len(plan["requeue"]), len(plan["skip"]), plan["manifest"] or "(dry run)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
