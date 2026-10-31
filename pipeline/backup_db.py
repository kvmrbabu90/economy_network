"""Nightly backup of the EconGraph database (the irreplaceable signal-validation evidence base).

    python -B -m pipeline.backup_db

- Copies the live database with SQLite's online backup API, which takes a consistent snapshot even while the
  hourly cycle is writing (WAL mode); a plain file copy could catch it mid-write.
- Verifies the copy with `PRAGMA integrity_check` before keeping it, gzips it, and writes it atomically
  (`.part` then rename) as `econgraph-YYYY-MM-DD.db.gz`.
- Rotation keeps the newest 14 daily files plus the first backup of each of the last 12 months; anything that is
  not an `econgraph-*.db.gz` file is never touched.

Source: ECONGRAPH_DB (default C:/Users/Public/econgraph/econgraph.db). Destination: ECONGRAPH_BACKUP_DIR (default a
folder in the user's OneDrive, outside the git repo, so the copies reach the cloud but never a commit). The
snapshot is assembled in the system temp folder and only the finished .gz is placed in the synced folder.
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
DEFAULT_DEST = Path.home() / "OneDrive" / "econgraph_backups"
NAME_RE = re.compile(r"^econgraph-(\d{4}-\d{2}-\d{2})\.db\.gz$")


def backup(source: Path, dest_dir: Path, today: date | None = None) -> Path:
    source, dest_dir = Path(source), Path(dest_dir)
    if not source.exists():
        raise FileNotFoundError(f"database not found: {source}")
    today = today or date.today()
    dest_dir.mkdir(parents=True, exist_ok=True)
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
        with open(snap, "rb") as fin, gzip.open(part, "wb", compresslevel=6) as fout:
            shutil.copyfileobj(fin, fout, length=1 << 20)
        os.replace(part, final)
    return final


def rotate(dest_dir: Path, keep_daily: int = 14, keep_monthly: int = 12) -> list[Path]:
    """Delete backups beyond the newest `keep_daily` days and the first-of-month copies of the last
    `keep_monthly` months. Returns the files removed."""
    dated = sorted((date.fromisoformat(m.group(1)), p) for p in Path(dest_dir).iterdir()
                   if (m := NAME_RE.match(p.name)))
    if not dated:
        return []
    keep = {p for _, p in dated[-keep_daily:]}
    first_of_month: dict[tuple[int, int], Path] = {}
    for d, p in dated:
        first_of_month.setdefault((d.year, d.month), p)
    for key in sorted(first_of_month)[-keep_monthly:]:
        keep.add(first_of_month[key])
    removed = [p for _, p in dated if p not in keep]
    for p in removed:
        p.unlink()
    return removed


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    source = Path(os.environ.get("ECONGRAPH_DB") or DEFAULT_SOURCE)
    dest = Path(os.environ.get("ECONGRAPH_BACKUP_DIR") or DEFAULT_DEST)
    try:
        out = backup(source, dest)
        removed = rotate(dest)
    except Exception as exc:                       # the scheduled task's exit code is the alarm
        log.error("backup FAILED: %s", exc)
        return 1
    log.info("backup ok: %s (%.1f MB); rotated out %d old file(s)", out, out.stat().st_size / 1e6, len(removed))
    return 0


if __name__ == "__main__":
    sys.exit(main())
