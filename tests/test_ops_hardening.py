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


def test_claude_cli_runs_with_no_tools_and_no_mcp_servers(monkeypatch):
    seen: list = []
    _patch_cli(monkeypatch, {"is_error": False, "result": "ok"}, seen)
    assert impact._claude_call("hello") == "ok"
    cmd = seen[0]
    assert cmd[cmd.index("--tools") + 1] == ""             # no built-in tools (Bash, files, web)
    assert "--strict-mcp-config" in cmd                    # and none of the user's MCP servers (Gmail, Drive, codex)


def test_news_headline_filter_is_locked_down_too(monkeypatch):
    from api import news
    seen: list = []
    monkeypatch.setattr(news, "_resolve_claude_binary", lambda: "claude")
    monkeypatch.setattr(news.subprocess, "Popen", lambda cmd, **kw: seen.append(list(cmd)) or _FakeProc(
        json.dumps({"is_error": False, "result": "[]"}).encode()))
    news._claude_call("filter these headlines")
    assert seen and seen[0][seen[0].index("--tools") + 1] == "" and "--strict-mcp-config" in seen[0]


def test_every_claude_command_line_comes_from_the_locked_down_builder():
    from api import claude_cli
    argv = claude_cli.build_argv("claude", "PROMPT")
    assert argv[:3] == ["claude", "-p", "PROMPT"] and argv[argv.index("--tools") + 1] == ""
    assert "--strict-mcp-config" in argv and "--mcp-config" not in argv
    offenders = []
    for path in list((REPO / "api").rglob("*.py")) + list((REPO / "pipeline").rglob("*.py")) +             list((REPO / "scripts").rglob("*.py")):
        if path.name == "claude_cli.py":
            continue
        if re.search(r"""["']-p["']""", path.read_text(encoding="utf-8")):
            offenders.append(path.relative_to(REPO).as_posix())
    assert offenders == [], f"build Claude command lines with api.claude_cli.build_argv: {offenders}"


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


@pytest.mark.parametrize("path", ["scripts/ensure_servers.ps1", "dev.bat", "README.md", "start.sh"])
def test_backend_listens_on_loopback_only(path):
    src = (REPO / path).read_text(encoding="utf-8")
    assert re.search(r"--host'?,? '?::1\b|--host ::1\b", src), "backend must bind ::1"
    assert not re.search(r"--host'?,? '::'|--host :: ", src), "backend must not bind every interface"


def test_no_script_lowers_the_retention_window():
    for path in (REPO / "scripts").glob("*.ps1"):
        for days in re.findall(r"PRUNE_OLDER_THAN_DAYS\s*=\s*'(\d+)'", path.read_text(encoding="utf-8")):
            assert int(days) >= 3650, f"{path.name} sets PRUNE_OLDER_THAN_DAYS={days}"


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


def test_a_failed_backup_leaves_no_partial_file(tmp_path, monkeypatch):
    from pipeline import backup_db as bk
    src = tmp_path / "live.db"
    _make_db(src, 50)

    def boom(*a, **k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(bk.shutil, "copyfileobj", boom)
    with pytest.raises(OSError):
        bk.backup(src, tmp_path / "backups", today=date(2026, 10, 30))
    assert list((tmp_path / "backups").iterdir()) == []


def test_backup_only_creates_the_leaf_folder(tmp_path):
    from pipeline import backup_db as bk
    src = tmp_path / "live.db"
    _make_db(src, 5)
    with pytest.raises(FileNotFoundError, match="OneDrive"):
        bk.backup(src, tmp_path / "no-onedrive" / "econgraph_backups", today=date(2026, 10, 30))
    assert not (tmp_path / "no-onedrive").exists()


def test_default_destination_follows_the_onedrive_variable(monkeypatch, tmp_path):
    from pipeline import backup_db as bk
    monkeypatch.setenv("OneDrive", str(tmp_path))
    assert bk.default_dest() == tmp_path / "econgraph_backups"
    monkeypatch.delenv("OneDrive")
    with pytest.raises(FileNotFoundError, match="OneDrive"):
        bk.default_dest()


def test_rotation_ignores_impossible_dates_and_clears_stale_partials(tmp_path):
    import os
    import time
    from pipeline import backup_db as bk
    d = tmp_path / "b"
    d.mkdir()
    for day in ("2026-10-28", "2026-10-29", "2026-10-30"):
        (d / f"econgraph-{day}.db.gz").write_bytes(b"x")
    (d / "econgraph-2026-02-30.db.gz").write_bytes(b"x")          # matches the pattern, impossible date
    stale = d / "econgraph-2026-10-01.db.gz.part"
    stale.write_bytes(b"x")
    old = time.time() - 3 * 86400
    os.utime(stale, (old, old))
    fresh = d / "econgraph-2026-10-30.db.gz.part"
    fresh.write_bytes(b"x")
    bk.rotate(d, keep_daily=2, keep_monthly=0)
    names = sorted(p.name for p in d.iterdir())
    assert names == ["econgraph-2026-02-30.db.gz", "econgraph-2026-10-29.db.gz", "econgraph-2026-10-30.db.gz",
                     "econgraph-2026-10-30.db.gz.part"]


def test_main_reports_backup_and_rotation_failures_with_distinct_exit_codes(tmp_path, monkeypatch):
    from pipeline import backup_db as bk
    src = tmp_path / "live.db"
    _make_db(src, 5)
    (tmp_path / "od").mkdir()
    monkeypatch.setenv("ECONGRAPH_DB", str(src))
    monkeypatch.setenv("ECONGRAPH_BACKUP_DIR", str(tmp_path / "od" / "econgraph_backups"))
    assert bk.main() == 0
    monkeypatch.setattr(bk, "rotate", lambda *a, **k: (_ for _ in ()).throw(PermissionError("locked")))
    assert bk.main() == 2                                          # backup kept, rotation needs attention
    monkeypatch.setenv("ECONGRAPH_DB", str(tmp_path / "missing.db"))
    assert bk.main() == 1


# --- 2026-11-09: a user-level ANTHROPIC_API_KEY overrode the subscription login in the scheduled task ------------
def test_child_env_drops_api_keys_and_keeps_everything_else(monkeypatch):
    from api import claude_cli
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-invalid")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok")
    monkeypatch.setenv("ECONGRAPH_DB", r"C:\x\g.db")
    env = claude_cli.child_env()
    assert "ANTHROPIC_API_KEY" not in env and "ANTHROPIC_AUTH_TOKEN" not in env
    assert env["ECONGRAPH_DB"] == r"C:\x\g.db" and "PATH" in {k.upper() for k in env}


@pytest.mark.parametrize("module", ["impact", "news"])
def test_runtime_cli_calls_never_pass_an_api_key(monkeypatch, module):
    from api import impact as imp, news
    mod = {"impact": imp, "news": news}[module]
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-invalid")
    monkeypatch.setattr(mod, "_resolve_claude_binary", lambda: "claude")
    from schema import store
    monkeypatch.setattr(store, "record_llm_usage", lambda u, db_path=None: None)
    seen: list = []

    def fake_popen(cmd, **kw):
        seen.append(kw.get("env"))
        return _FakeProc(json.dumps({"is_error": False, "result": "ok"}).encode())

    monkeypatch.setattr(mod.subprocess, "Popen", fake_popen)
    mod._claude_call("hello")
    assert seen and seen[0] is not None and "ANTHROPIC_API_KEY" not in seen[0]


def test_every_cli_launch_site_uses_the_scrubbed_environment():
    offenders = []
    for path in list((REPO / "api").rglob("*.py")) + list((REPO / "pipeline").rglob("*.py")):
        src = path.read_text(encoding="utf-8")
        if "claude_cli.build_argv" in src and "env=claude_cli.child_env()" not in src:
            offenders.append(path.relative_to(REPO).as_posix())
    assert offenders == [], f"launch the Claude CLI with env=claude_cli.child_env(): {offenders}"
