"""So What? V2 · Phase 1 — broad news ingestion.

Pull multi-category news, map each event to a graph node, dedupe, rank, and record
the top-N as queued events for P2 to trace. No impact tracing here.

    python -B -m pipeline.ingest_news
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = REPO_ROOT / "econgraph.db"
HUBS_PATH = REPO_ROOT / "data" / "hubs.jsonl"

INGEST_CAP = int(os.environ.get("INGEST_CAP", "25"))
INGEST_MAX_AGE_DAYS = int(os.environ.get("INGEST_MAX_AGE_DAYS", "3"))
_SOURCE_WEIGHT = {"SEC 8-K": 1.0, "Marketaux": 1.0, "Alpha Vantage": 1.0}  # default 0.7 (RSS)


def _event_id(cand: dict[str, Any]) -> str:
    """Stable id: sha1 of the url if present, else source|headline."""
    basis = (cand.get("url") or f"{cand.get('source','')}|{cand.get('headline','')}").strip().lower()
    return "ev:" + hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]


def _resolve_to_node_id(conn, name: str) -> Optional[str]:
    """Resolve a name to a node id across ALL types (name/alias exact → starts-with →
    contains). Mirrors the news graph-gate resolver but returns the id."""
    q = (name or "").lower().strip()
    if not q:
        return None
    for sql, arg in (
        ("SELECT id FROM nodes WHERE LOWER(name) = ? LIMIT 1", q),
        ("SELECT n.id FROM aliases a JOIN nodes n ON n.id = a.node_id WHERE a.alias_normalized = ? LIMIT 1", q),
        ("SELECT id FROM nodes WHERE LOWER(name) LIKE ? LIMIT 1", q + "%"),
    ):
        row = conn.execute(sql, (arg,)).fetchone()
        if row:
            return row[0]
    if len(q) >= 5:
        row = conn.execute("SELECT id FROM nodes WHERE LOWER(name) LIKE ? LIMIT 1", (f"%{q}%",)).fetchone()
        if row:
            return row[0]
    return None


def _ticker_index(conn) -> dict[str, str]:
    """Uppercase ticker → node id, from nodes.tickers (JSON array)."""
    idx: dict[str, str] = {}
    for row in conn.execute("SELECT id, tickers FROM nodes WHERE tickers != '[]'"):
        try:
            for t in json.loads(row["tickers"] or "[]"):
                if t:
                    idx.setdefault(str(t).upper(), row["id"])
        except Exception:
            continue
    return idx


def _centrality(conn) -> dict[str, float]:
    """node_id → normalized centrality (0-1). Prefer Phase-K hub scores; else edge degree."""
    scores: dict[str, float] = {}
    if HUBS_PATH.exists():
        for line in HUBS_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                h = json.loads(line)
                if h.get("id") is not None and h.get("score") is not None:
                    scores[h["id"]] = float(h["score"])
            except Exception:
                continue
    if not scores:  # fallback: undirected degree over core edges
        for row in conn.execute(
            "SELECT id, (SELECT COUNT(*) FROM edges WHERE below_threshold=0 AND (source=n.id OR target=n.id)) AS deg "
            "FROM nodes n"
        ):
            scores[row["id"]] = float(row["deg"])
    top = max(scores.values()) if scores else 0.0
    return {k: (v / top if top else 0.0) for k, v in scores.items()}


def _recency_weight(published_at: Optional[str], today: str) -> float:
    if not published_at:
        return 0.7   # unknown date → mild penalty, benefit of the doubt
    try:
        age = (date.fromisoformat(today) - date.fromisoformat(published_at[:10])).days
    except Exception:
        return 0.7
    return 0.5 ** (max(0, age) / 1.5)


def rank(cands: list[dict[str, Any]], conn, *, today: Optional[str] = None) -> list[dict[str, Any]]:
    """Attach a priority score to each candidate and return sorted desc."""
    today = today or str(date.today())
    cen = _centrality(conn)
    for c in cands:
        sw = _SOURCE_WEIGHT.get(c.get("source", ""), 0.7)
        cn = cen.get(c.get("seed_node_id", ""), 0.0)
        rw = _recency_weight(c.get("published_at"), today)
        c["_priority"] = sw * (0.5 + 0.5 * cn) * rw
    return sorted(cands, key=lambda c: -c["_priority"])


def cap(ranked: list[dict[str, Any]], *, cap: int = INGEST_CAP) -> list[dict[str, Any]]:
    """Mark the top `cap` as queued, the rest skipped. Input must be rank()-sorted."""
    for i, c in enumerate(ranked):
        c["status"] = "queued" if i < cap else "skipped"
    return ranked


def dedupe(cands: list[dict[str, Any]], conn) -> list[dict[str, Any]]:
    """Drop candidates whose id already exists in `events` (any prior cycle), and
    collapse in-cycle duplicate ids (first wins)."""
    from schema.store import event_exists
    seen: set[str] = set()
    out = []
    for c in cands:
        cid = c["id"]
        if cid in seen or event_exists(conn, cid):
            continue
        seen.add(cid)
        out.append(c)
    return out
