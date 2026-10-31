"""Nightly backup of the EconGraph database (the irreplaceable signal-validation evidence base).

    python -B -m pipeline.backup_db

- Copies the live database with SQLite's online backup API, which takes a consistent snapshot even while the
  hourly cycle is writing (WAL mode); a plain file copy could catch it mid-write.
- Verifies the copy with `PRAGMA integrity_check` before keeping it, gzips it, and writes it atomically
  (`.part` then rename) as `econgraph-YYYY-MM-DD.db.gz`.
- Rotation keeps the newest 14 daily files plus the first backup of each of the last 12 months; anything that is
  not an `econgraph-*.db.gz` file is never touched.

Source: ECONGRAPH_DB (default C:/Users/Public/econgraph/econgraph.db). Destination: ECONGRAPH_BACKUP_DIR, else
`%OneDrive%/econgraph_backups` — outside the git repo, so the copies reach the cloud but never a commit. Only the
leaf folder is ever created: if the OneDrive folder itself is missing (unlinked, renamed) the backup fails loudly
instead of quietly writing to a local folder that never leaves the machine. The snapshot is assembled in the
system temp folder; a failed write leaves no partial file behind.

Exit codes (the scheduled task's Last Run Result): 0 ok, 1 backup failed, 2 backup kept but rotation failed.
"""
from __future__ import annotations

import gzip
import logging
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from datetime import date
from pathlib import Path

log = logging.getLogger("backup_db")

DEFAULT_SOURCE = Path("C:/Users/Public/econgraph/econgraph.db")
NAME_RE = re.compile(r"^econgraph-(\d{4}-\d{2}-\d{2})\.db\.gz$")
PART_RE = re.compile(r"^econgraph-\d{4}-\d{2}-\d{2}\.db\.gz\.part$")
STALE_PART_S = 86400


def default_dest() -> Path:
    """`%OneDrive%/econgraph_backups`; refuses to guess when OneDrive is not set up."""
    root = os.environ.get("OneDrive")
    if not root or not Path(root).is_dir():
        raise FileNotFoundError("OneDrive folder not found (%OneDrive% unset or missing): set ECONGRAPH_BACKUP_DIR")
    return Path(root) / "econgraph_backups"


def backup(source: Path, dest_dir: Path, today: date | None = None) -> Path:
    source, dest_dir = Path(source), Path(dest_dir)
    if not source.exists():
        raise FileNotFoundError(f"database not found: {source}")
    today = today or date.today()
    if not dest_dir.parent.is_dir():
        raise FileNotFoundError(f"backup parent folder missing (is OneDrive set up?): {dest_dir.parent}")
    dest_dir.mkdir(exist_ok=True)
    final = dest_dir / f"econgraph-{today.isoformat()}.db.gz"
    with tempfile.TemporaryDirectory() as tmp:
        snap = Path(tmp) / "snapshot.db"
        src = sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True)
        dst = sqlite3.connect(snap)
        try:
            src.backup(dst)
            ok = dst.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            dst.close()
            src.close()
        if ok != "ok":
            raise RuntimeError(f"backup snapshot failed integrity_check: {ok}")
        part = final.with_name(final.name + ".part")
        try:
            with open(snap, "rb") as fin, gzip.open(part, "wb", compresslevel=6) as fout:
                shutil.copyfileobj(fin, fout, length=1 << 20)
            os.replace(part, final)
        except BaseException:
            part.unlink(missing_ok=True)
            raise
    return final


def rotate(dest_dir: Path, keep_daily: int = 14, keep_monthly: int = 12) -> list[Path]:
    """Delete backups beyond the newest `keep_daily` days and the first-of-month copies of the last
    `keep_monthly` months. Returns the files removed."""
    import time
    removed: list[Path] = []
    dated = []
    for p in Path(dest_dir).iterdir():
        if PART_RE.match(p.name) and time.time() - p.stat().st_mtime > STALE_PART_S:
            p.unlink(missing_ok=True)                     # leftover of an interrupted run
            removed.append(p)
            continue
        m = NAME_RE.match(p.name)
        if m:
            try:
                dated.append((date.fromisoformat(m.group(1)), p))
            except ValueError:                            # e.g. a hand-made econgraph-2026-02-30: leave it
                continue
    dated.sort()
    if not dated:
        return removed
    keep = {p for _, p in dated[-keep_daily:]} if keep_daily > 0 else set()   # [-0:] would keep everything
    first_of_month: dict[tuple[int, int], Path] = {}
    for d, p in dated:
        first_of_month.setdefault((d.year, d.month), p)
    if keep_monthly > 0:
        for key in sorted(first_of_month)[-keep_monthly:]:
            keep.add(first_of_month[key])
    for _, p in dated:
        if p not in keep:
            p.unlink()
            removed.append(p)
    return removed


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    source = Path(os.environ.get("ECONGRAPH_DB") or DEFAULT_SOURCE)
    try:
        dest = Path(os.environ.get("ECONGRAPH_BACKUP_DIR") or default_dest())
        out = backup(source, dest)
    except Exception as exc:                       # the scheduled task's exit code is the alarm
        log.error("backup FAILED: %s", exc)
        return 1
    log.info("backup ok: %s (%.1f MB)", out, out.stat().st_size / 1e6)
    try:
        removed = rotate(dest)
    except Exception as exc:
        log.error("backup kept, but rotation FAILED: %s", exc)
        return 2
    log.info("rotated out %d old file(s)", len(removed))
    return 0


if __name__ == "__main__":
    sys.exit(main())
