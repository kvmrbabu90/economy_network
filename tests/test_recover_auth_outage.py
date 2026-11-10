"""Recovery for the 2026-11-06..09 Claude-login outage: events queued only because the materiality gate failed
open get the real gate again — 8-K filings (material by construction) and gate-approved stories go back to
'queued' (with their auth-failed enrichment reset), the rest become 'skipped'. Nothing outside the window or
already traced is touched, a dry run changes nothing, every change is recorded for reversal, and the script
refuses to run while the CLI is still unauthenticated."""
from __future__ import annotations

import json
import sqlite3

import pytest

from schema import store
from scripts import recover_auth_outage as rec

SINCE = "2026-11-06 15:05"


def _db(tmp_path):
    db = tmp_path / "g.db"
    conn = store.connect(db)
    store.init_db(conn)
    rows = [
        # id, source, headline, status, enrich_status, ingested_at
        ("k1", "SEC 8-K", "ACME 8-K: CEO resigns", "failed", None, "2026-11-07 10:00:00"),
        ("g1", "GDELT-GKG", "ACME wins $2bn contract", "failed", "failed", "2026-11-07 11:00:00"),
        ("g2", "GDELT-GKG", "ACME sponsors local fun run", "failed", "failed", "2026-11-08 09:00:00"),
        ("g3", "GDELT-GKG", "Supplier fire halts ACME line", "failed", "no_content", "2026-11-09 12:00:00"),
        ("old", "GDELT-GKG", "Earlier real no-ripple story", "failed", None, "2026-11-05 10:00:00"),
        ("ok", "GDELT-GKG", "Traced during the window somehow", "traced", None, "2026-11-07 12:00:00"),
    ]
    for i, src, head, status, enr, ts in rows:
        conn.execute("INSERT INTO events (id, headline, source, url, status, enrich_status, ingested_at, "
                     "seed_entity, seed_node_id) VALUES (?, ?, ?, 'http://x', ?, ?, ?, 'ACME', 'cik:1')",
                     (i, head, src, status, enr, ts))
    conn.commit()
    conn.close()
    return db


def _state(db):
    c = sqlite3.connect(db)
    out = {r[0]: (r[1], r[2]) for r in c.execute("SELECT id, status, enrich_status FROM events")}
    c.close()
    return out


def _gate(cands):
    seen.extend(c["id"] for c in cands)
    return [c for c in cands if c["id"] in ("g1", "g3")]


seen: list = []


@pytest.fixture(autouse=True)
def _reset():
    seen.clear()


def test_dry_run_changes_nothing(tmp_path):
    db = _db(tmp_path)
    before = _state(db)
    plan = rec.run(db, since=SINCE, apply=False, gate=_gate, auth_check=lambda: None,
                   manifest_dir=tmp_path)
    assert _state(db) == before
    assert plan["requeue"] == ["g1", "g3", "k1"] and plan["skip"] == ["g2"]
    assert not list(tmp_path.glob("recover_auth_outage_*.json"))


def test_apply_requeues_material_skips_the_rest_and_records_every_change(tmp_path):
    db = _db(tmp_path)
    plan = rec.run(db, since=SINCE, apply=True, gate=_gate, auth_check=lambda: None, manifest_dir=tmp_path)
    s = _state(db)
    assert s["k1"] == ("queued", None)                    # 8-K: kept by construction, never sent to the gate
    assert "k1" not in seen
    assert s["g1"] == ("queued", None)                    # auth-failed enrichment reset so it retries
    assert s["g3"] == ("queued", "no_content")            # a genuine 'no_content' stays
    assert s["g2"] == ("skipped", "failed")
    assert s["old"] == ("failed", None) and s["ok"] == ("traced", None)
    manifest = json.loads(plan["manifest"].read_text(encoding="utf-8"))
    assert {m["id"] for m in manifest["changes"]} == {"k1", "g1", "g2", "g3"}
    assert all(m["old_status"] == "failed" for m in manifest["changes"])
    assert {m["id"]: m["old_enrich_status"] for m in manifest["changes"]}["g1"] == "failed"


def test_refuses_to_run_while_the_cli_is_not_authenticated(tmp_path):
    db = _db(tmp_path)
    with pytest.raises(RuntimeError, match="not authenticated"):
        rec.run(db, since=SINCE, apply=True, gate=_gate, auth_check=lambda: "OAuth session expired",
                manifest_dir=tmp_path)
    assert _state(db)["g1"] == ("failed", "failed")
