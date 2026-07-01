"""So What? V2 · Phase 4 — unattended scheduler.

Loops run_cycle every SCHEDULER_INTERVAL_S (default 12h). Use --once for a single
cycle. RECOMMENDED on a workstation: instead of this long-lived loop, register an OS
scheduler (Windows Task Scheduler / cron) to run `python -B -m pipeline.run_cycle`
every 12h — more robust across sleep/restart.

    python -B -m pipeline.scheduler --once
    python -B -m pipeline.scheduler            # loop forever
"""
from __future__ import annotations

import argparse
import logging
import os
import time
from typing import Optional, Sequence

from pipeline.run_cycle import run_cycle

log = logging.getLogger(__name__)
INTERVAL_S = int(os.environ.get("SCHEDULER_INTERVAL_S", str(12 * 3600)))


def main(argv: Optional[Sequence[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description="So What? V2 12h cycle scheduler.")
    ap.add_argument("--once", action="store_true", help="run a single cycle and exit")
    ap.add_argument("--interval", type=int, default=INTERVAL_S, help="seconds between cycles")
    args = ap.parse_args(argv)
    if args.once:
        s = run_cycle()
        return 0 if s.get("ok") else 1
    log.info("scheduler: looping every %ds; Ctrl-C to stop", args.interval)
    while True:
        run_cycle()
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
