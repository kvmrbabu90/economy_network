"""So What? V2 · Phase 4 — full-cycle orchestrator.

Runs the ingest -> precompute -> aggregate -> prune pipeline once, against one DB,
with per-stage error isolation. Idempotent + restartable. Scheduled hourly by the
deployed Windows Task (see docs); the cadence is just how often you invoke this.

    python -B -m pipeline.run_cycle
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Callable, Optional

from pipeline import ingest_news, precompute_impacts, aggregate_impacts
from schema import store

log = logging.getLogger(__name__)
REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = store.default_db_path()


def _run_ingest(db_path) -> dict:
    return ingest_news.run_ingest(db_path=db_path)          # P1 entrypoint: run_ingest(db_path=DB_PATH)


def _run_precompute(db_path, provider) -> dict:
    return precompute_impacts.run_precompute(db_path, provider=provider)


def _run_aggregate(db_path) -> dict:
    conn = store.connect(db_path); store.init_db(conn)
    try:
        return aggregate_impacts.aggregate(conn)
    finally:
        conn.close()


def _run_prune(db_path, older_than_days: int = 30) -> dict:
    conn = store.connect(db_path); store.init_db(conn)
    try:
        # prune_old_events returns a bare int; wrap it so the prune slot matches
        # the dict shape of the other stages (ingest/precompute/aggregate).
        return {"pruned_events": store.prune_old_events(conn, older_than_days=older_than_days)}
    finally:
        conn.close()


def _stage(name: str, fn: Callable[[], dict], summary: dict) -> None:
    try:
        summary[name] = fn()
    except Exception as exc:                                # isolate: log, record, continue
        log.exception("run_cycle: stage %s failed", name)
        summary[name] = {"error": repr(exc)}
        summary["ok"] = False


def run_cycle(db_path=DB_PATH, *, provider: Optional[str] = None) -> dict:
    t0 = time.time()
    summary: dict = {"ok": True}
    _stage("ingest", lambda: _run_ingest(db_path), summary)
    _stage("precompute", lambda: _run_precompute(db_path, provider), summary)
    _stage("aggregate", lambda: _run_aggregate(db_path), summary)
    # Bound table growth: drop events (and their impacts) older than the aggregation
    # window's reach. Runs after aggregate so we never prune a row still in-window.
    _stage("prune", lambda: _run_prune(db_path, older_than_days=30), summary)
    # DEFERRED — auto-retry of aged 'failed' events. run_precompute(retry_failed=True)
    # re-queues *every* failed event indiscriminately, so wiring it into the unattended
    # cycle would re-process permanent failures (bad seed, deleted node) on every run and
    # risk double-processing. A safe version needs a failed-at timestamp / attempt count in
    # the events table to bound retries to genuinely transient, recently-aged failures.
    # Until that column exists, retry stays a manual `--retry-failed` operator action.
    summary["elapsed_s"] = round(time.time() - t0, 1)
    log.info("run_cycle: %s", summary)
    return summary


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    s = run_cycle()
    print(f"cycle: {s}")
    return 0 if s["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
