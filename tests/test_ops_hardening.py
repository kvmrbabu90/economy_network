"""Operational hardening (2026-10-30): the Claude CLI runs with every tool disabled, usage rows record the model,
retention defaults can never delete history, the backend listens on loopback only, and the database has a
verified, rotated nightly backup."""
from __future__ import annotations

import gzip
import inspect
import json
import re
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pytest

from api import impact

REPO = Path(__file__).resolve().parents[1]


class _FakeProc:
    def __init__(self, out: bytes):
        self.stdout_b, self.returncode, self.pid = out, 0, 1

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def communicate(self, timeout=None):
        return self.stdout_b, b""


def _patch_cli(monkeypatch, envelope: dict, seen: list):
    monkeypatch.setattr(impact, "_resolve_claude_binary", lambda: "claude")
    from schema import store                       # never let a test write usage rows to a real DB file
    monkeypatch.setattr(store, "record_llm_usage", lambda u, db_path=None: None)

    def fake_popen(cmd, **kw):
        seen.append(list(cmd))
        return _FakeProc(json.dumps(envelope).encode())

    monkeypatch.setattr(impact.subprocess, "Popen", fake_popen)


def test_claude_cli_runs_with_every_tool_disabled(monkeypatch):
    seen: list = []
    _patch_cli(monkeypatch, {"is_error": False, "result": "ok"}, seen)
    assert impact._claude_call("hello") == "ok"
    cmd = seen[0]
    assert "--tools" in cmd and cmd[cmd.index("--tools") + 1] == ""


def test_usage_rows_record_the_model_from_model_usage(monkeypatch):
    seen: list = []
    _patch_cli(monkeypatch, {"is_error": False, "result": "ok", "total_cost_usd": 0.01,
                             "usage": {"input_tokens": 5, "output_tokens": 7},
                             "modelUsage": {"claude-haiku-5-5": {"outputTokens": 3},
                                            "claude-sonnet-5-5": {"outputTokens": 40}}}, seen)
    rows: list = []
    from schema import store
    monkeypatch.setattr(store, "record_llm_usage", lambda u, db_path=None: rows.append(u))
    impact._claude_call("hello")
    assert rows and rows[0]["model"] == "claude-sonnet-5-5"          # the model that did most of the work


@pytest.mark.parametrize("script", ["pipeline/extractor.py", "pipeline/extract_weights.py",
                                    "pipeline/extract_wikipedia.py"])
def test_graph_builder_scripts_also_disable_tools(script):
    src = (REPO / script).read_text(encoding="utf-8")
    for m in re.finditer(r"\[binary, \"-p\"[^\]]*\]", src):
        assert '"--tools", ""' in m.group(0), m.group(0)
    assert re.search(r"\[binary, \"-p\"", src)


def test_retention_defaults_can_never_delete_history(monkeypatch, tmp_path):
    from pipeline import run_cycle as rc
    from schema import store
    assert inspect.signature(store.prune_old_events).parameters["older_than_days"].default >= 3650
    assert inspect.signature(rc._run_prune).parameters["older_than_days"].default >= 3650
    monkeypatch.delenv("PRUNE_OLDER_THAN_DAYS", raising=False)
    seen = {}
    monkeypatch.setattr(rc, "_run_ingest", lambda db: {})
    monkeypatch.setattr(rc, "_run_precompute", lambda db, provider: {})
    monkeypatch.setattr(rc, "_run_aggregate", lambda db: {})
    monkeypatch.setattr(rc, "_run_prune", lambda db, older_than_days=0: seen.setdefault("d", older_than_days) or {})
    rc.run_cycle(tmp_path / "g.db")
    assert seen["d"] >= 3650


@pytest.mark.parametrize("path", ["scripts/ensure_servers.ps1", "dev.bat"])
def test_backend_listens_on_loopback_only(path):
    src = (REPO / path).read_text(encoding="utf-8")
    assert re.search(r"--host'?,? '?::1\b|--host ::1\b", src), "backend must bind ::1"
    assert not re.search(r"--host'?,? '::'|--host :: ", src), "backend must not bind every interface"


# --- nightly backup ----------------------------------------------------------------------------------------
def _make_db(path: Path, rows: int) -> None:
    c = sqlite3.connect(path)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("CREATE TABLE t (x INTEGER)")
    c.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(rows)])
    c.commit()
    c.close()


def test_backup_writes_a_verified_compressed_copy_while_the_db_is_open(tmp_path):
    from pipeline import backup_db as bk
    src = tmp_path / "live.db"
    _make_db(src, 500)
    writer = sqlite3.connect(src)                       # a live connection holding uncommitted WAL work
    writer.execute("INSERT INTO t VALUES (999)")
    out = bk.backup(src, tmp_path / "backups", today=date(2026, 10, 30))
    writer.close()
    assert out.name == "econgraph-2026-10-30.db.gz" and out.exists()
    restored = tmp_path / "restored.db"
    restored.write_bytes(gzip.decompress(out.read_bytes()))
    c = sqlite3.connect(restored)
    assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert c.execute("SELECT count(*) FROM t").fetchone()[0] == 500   # committed rows only
    c.close()
    assert not list((tmp_path / "backups").glob("*.part"))


def test_backup_refuses_a_missing_source(tmp_path):
    from pipeline import backup_db as bk
    with pytest.raises(FileNotFoundError):
        bk.backup(tmp_path / "nope.db", tmp_path / "backups", today=date(2026, 10, 30))


def test_rotation_keeps_recent_dailies_and_one_per_month(tmp_path):
    from pipeline import backup_db as bk
    d = tmp_path / "backups"
    d.mkdir()
    start = date(2025, 9, 1)
    for k in range(430):                                # ~14 months of nightly files
        day = start + timedelta(days=k)
        (d / f"econgraph-{day.isoformat()}.db.gz").write_bytes(b"x")
    (d / "notes.txt").write_text("keep me", encoding="utf-8")
    bk.rotate(d, keep_daily=14, keep_monthly=12)
    kept = sorted(p.name for p in d.glob("econgraph-*.db.gz"))
    last = start + timedelta(days=429)
    dailies = {f"econgraph-{(last - timedelta(days=i)).isoformat()}.db.gz" for i in range(14)}
    months = sorted({(day.year, day.month) for day in (start + timedelta(days=k) for k in range(430))})[-12:]
    monthly = {f"econgraph-{y}-{m:02d}-01.db.gz" for y, m in months}     # Dec 2025 .. Nov 2026
    assert set(kept) == dailies | monthly                                 # Nov 1 is both a daily and a monthly
    assert (d / "notes.txt").exists()
