# So What? V2 · Phase 1 — Broad News Ingestion — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Checkbox steps.

**Goal:** A 12h runnable that pulls broad multi-category news, maps each event to a graph node, dedupes, ranks, and records the top ~25 as `status='queued'` in a new `events` table (no tracing).

**Architecture:** New `pipeline/ingest_news.py` orchestrates source fetchers (8-K via `sec_8k`, Marketaux, Alpha Vantage — key-optional; broad RSS via one Claude extraction call) → resolve+graph-gate → dedupe → rank (source × centrality × recency) → persist top-N. New `events` table in `schema/store.py`. Reuses the existing engine/graph read-only.

**Tech Stack:** Python 3.11 / sqlite3 / requests / Claude CLI (RSS extraction) / pytest.

**Spec:** `docs/superpowers/specs/2026-06-17-sowhat-v2-p1-ingestion-design.md`. **Branch:** `feat/sowhat-v2`. Run Python with `python -B` (OneDrive stale-pycache).

---

## Task 1: `events` table + store helpers

**Files:** Modify `schema/store.py`; Test `tests/test_events_store.py` (create).

- [ ] **Step 1: Failing tests** — `tests/test_events_store.py`:

```python
from __future__ import annotations
from schema import store


def _mem():
    conn = store.connect(":memory:")
    store.init_db(conn)
    return conn


def test_events_table_created_and_insert_query():
    conn = _mem()
    store.insert_event(conn, {
        "id": "e1", "headline": "Acme acquires Beta", "source": "SEC 8-K",
        "url": "http://x/1", "category": "m&a", "published_at": "2026-06-17",
        "seed_entity": "Acme", "seed_node_id": "cik:0000000001", "status": "queued",
    })
    assert store.event_exists(conn, "e1") is True
    assert store.event_exists(conn, "nope") is False
    q = store.queued_events(conn)
    assert len(q) == 1 and q[0]["id"] == "e1" and q[0]["status"] == "queued"


def test_insert_event_is_idempotent_on_id():
    conn = _mem()
    row = {"id": "e1", "headline": "h", "source": "s", "url": "u", "category": "c",
           "published_at": None, "seed_entity": "E", "seed_node_id": "cik:1", "status": "queued"}
    store.insert_event(conn, row)
    store.insert_event(conn, {**row, "headline": "changed"})   # same id → ignored
    q = store.queued_events(conn)
    assert len(q) == 1 and q[0]["headline"] == "h"
```

- [ ] **Step 2: Run — fail** (`python -B -m pytest tests/test_events_store.py -v` → no `events` table / no `insert_event`).

- [ ] **Step 3: Add the `events` table to the `DDL` string** in `schema/store.py` (append inside the triple-quoted `DDL`, after the `aliases` block):

```sql
CREATE TABLE IF NOT EXISTS events (
    id           TEXT PRIMARY KEY,
    headline     TEXT NOT NULL,
    source       TEXT,
    url          TEXT,
    category     TEXT,
    published_at TEXT,
    ingested_at  TEXT NOT NULL DEFAULT (datetime('now')),
    seed_entity  TEXT,
    seed_node_id TEXT,
    status       TEXT NOT NULL DEFAULT 'queued'
);
CREATE INDEX IF NOT EXISTS idx_events_status ON events(status);
```

- [ ] **Step 4: Add store helpers** in `schema/store.py` (after `init_db`):

```python
def insert_event(conn: sqlite3.Connection, ev: dict[str, Any]) -> None:
    """Insert one event; INSERT OR IGNORE so a re-seen id is a no-op (idempotent)."""
    conn.execute(
        """
        INSERT OR IGNORE INTO events
          (id, headline, source, url, category, published_at, seed_entity, seed_node_id, status)
        VALUES (:id, :headline, :source, :url, :category, :published_at,
                :seed_entity, :seed_node_id, :status)
        """,
        {
            "id": ev["id"], "headline": ev["headline"], "source": ev.get("source"),
            "url": ev.get("url"), "category": ev.get("category"),
            "published_at": ev.get("published_at"), "seed_entity": ev.get("seed_entity"),
            "seed_node_id": ev.get("seed_node_id"), "status": ev.get("status", "queued"),
        },
    )
    conn.commit()


def event_exists(conn: sqlite3.Connection, event_id: str) -> bool:
    return conn.execute("SELECT 1 FROM events WHERE id = ? LIMIT 1", (event_id,)).fetchone() is not None


def queued_events(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM events WHERE status = 'queued' ORDER BY ingested_at").fetchall()
    return [dict(r) for r in rows]
```
(`Any` and `sqlite3` are already imported in `store.py`.)

- [ ] **Step 5: Run — pass.** `python -B -m pytest tests/test_events_store.py -v` (2 pass). Also `python -B -m pytest tests/test_schema.py -q` (existing schema tests unaffected).

- [ ] **Step 6: Commit** — `git add schema/store.py tests/test_events_store.py && git commit -m "feat(v2): events table + store helpers (insert/exists/queued)"`

---

## Task 2: Ingestion core (resolve · dedupe · rank · persist)

**Files:** Create `pipeline/ingest_news.py` (core only; fetchers in Task 3); Test `tests/test_ingest_news.py`.

- [ ] **Step 1: Failing tests** — `tests/test_ingest_news.py`:

```python
from __future__ import annotations
from schema import store
from pipeline import ingest_news as ing


def _graph():
    conn = store.connect(":memory:")
    store.init_db(conn)
    # Two company nodes (one a hub with many edges), one commodity.
    conn.execute("INSERT INTO nodes (id,type,name,tickers) VALUES ('cik:1','Company','Acme Corp','[\"ACME\"]')")
    conn.execute("INSERT INTO nodes (id,type,name,tickers) VALUES ('cik:2','Company','Beta Inc','[\"BETA\"]')")
    conn.execute("INSERT INTO nodes (id,type,name) VALUES ('commodity:wheat','Commodity','Wheat')")
    # Acme is a hub: 3 edges; Beta: 0.
    for i,t in enumerate(['cik:2','commodity:wheat','cik:2']):
        conn.execute("INSERT INTO edges (id,source,target,type,confidence,prov_filing,prov_url,prov_snippet,prov_extracted_by)"
                     f" VALUES ('x{i}','cik:1','{t}','supplies',0.9,'','','s','rule')")
    conn.commit()
    return conn


def test_resolve_to_node_id_all_types():
    conn = _graph()
    assert ing._resolve_to_node_id(conn, "Wheat") == "commodity:wheat"      # commodity by name
    assert ing._resolve_to_node_id(conn, "Acme Corp") == "cik:1"            # company by name
    assert ing._resolve_to_node_id(conn, "Nope") is None


def test_ticker_index_maps_symbol_to_node():
    conn = _graph()
    idx = ing._ticker_index(conn)
    assert idx["ACME"] == "cik:1" and idx["BETA"] == "cik:2"


def test_event_id_is_stable_and_url_based():
    a = ing._event_id({"url": "http://x/1", "source": "s", "headline": "h"})
    b = ing._event_id({"url": "http://x/1", "source": "other", "headline": "diff"})
    assert a == b   # same url → same id regardless of other fields


def test_rank_and_cap_selects_top_by_priority():
    conn = _graph()
    cands = [
        {"id": "hub",  "seed_node_id": "cik:1", "source": "SEC 8-K", "published_at": "2026-06-17"},  # hub + best source
        {"id": "leaf", "seed_node_id": "cik:2", "source": "RSS",     "published_at": "2026-06-17"},  # no edges + weak source
    ]
    ranked = ing.rank(cands, conn, today="2026-06-17")
    assert ranked[0]["id"] == "hub"   # higher centrality + source weight ranks first
    top = ing.cap(ranked, cap=1)
    assert [c["id"] for c in top if c["status"] == "queued"] == ["hub"]
    assert [c["id"] for c in top if c["status"] == "skipped"] == ["leaf"]


def test_dedupe_drops_ids_already_in_db():
    conn = _graph()
    store.insert_event(conn, {"id": "seen", "headline": "h", "source": "s", "url": "u",
                              "category": "c", "published_at": None, "seed_entity": "E",
                              "seed_node_id": "cik:1", "status": "traced"})
    out = ing.dedupe([{"id": "seen"}, {"id": "fresh"}, {"id": "fresh"}], conn)
    assert [c["id"] for c in out] == ["fresh"]   # prior-cycle 'seen' dropped; in-cycle dup collapsed
```

- [ ] **Step 2: Run — fail** (`ModuleNotFoundError: pipeline.ingest_news`).

- [ ] **Step 3: Implement the core** — create `pipeline/ingest_news.py`:

```python
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
```

- [ ] **Step 4: Run — pass.** `python -B -m pytest tests/test_ingest_news.py -v` (5 pass).

- [ ] **Step 5: Commit** — `git add pipeline/ingest_news.py tests/test_ingest_news.py && git commit -m "feat(v2): ingest core — resolve/ticker-index/centrality/rank/cap/dedupe"`

---

## Task 3: Source fetchers

**Files:** Modify `pipeline/ingest_news.py` (add fetchers); Test `tests/test_ingest_fetchers.py`.

**Read first:** `pipeline/sec_8k.py` (`fetch_recent_8k_meta(cik, since_days)` returns dicts with `accession/form/date/primary/items/url`) and `api/news.py` (`_RSS_FEEDS`, `_fetch_raw`, `_claude_call`, `_parse_llm_json`) — reuse these, don't reinvent.

- [ ] **Step 1: Failing tests** — `tests/test_ingest_fetchers.py`:

```python
from __future__ import annotations
import json
from schema import store
from pipeline import ingest_news as ing


def _graph():
    conn = store.connect(":memory:")
    store.init_db(conn)
    conn.execute("INSERT INTO nodes (id,type,name,tickers) VALUES ('cik:0000000001','Company','Acme','[\"ACME\"]')")
    conn.execute("INSERT INTO nodes (id,type,name) VALUES ('commodity:wheat','Commodity','Wheat')")
    conn.commit()
    return conn


def test_map_api_item_by_ticker():
    conn = _graph()
    idx = ing._ticker_index(conn)
    c = ing._candidate_from_ticker("ACME", "Acme wins contract", "Marketaux",
                                   "http://x/1", "company", "2026-06-17", idx)
    assert c and c["seed_node_id"] == "cik:0000000001"
    assert ing._candidate_from_ticker("ZZZ", "x", "Marketaux", "u", "company", None, idx) is None


def test_8k_category_from_item_code():
    assert ing._category_for_8k("2.01") == "m&a"
    assert ing._category_for_8k("1.01") == "agreement"
    assert ing._category_for_8k("5.02") == "exec"
    assert ing._category_for_8k("8.01") == "filing"


def test_extract_rss_events_parses_claude_json(monkeypatch):
    conn = _graph()
    raw = [{"title": "Acme buys Wheat supplier", "source": "RSS-World", "url": "http://x/9", "pub_date": "2026-06-17"}]
    monkeypatch.setattr(ing, "_claude_call",
        lambda p: json.dumps([{"index": 1, "headline": "Acme acquires wheat supplier",
                               "entity": "Acme", "category": "m&a"}]))
    out = ing.extract_rss_events(raw)
    assert len(out) == 1 and out[0]["seed_entity"] == "Acme" and out[0]["category"] == "m&a"
    assert out[0]["url"] == "http://x/9"


def test_extract_rss_events_empty_when_claude_unavailable(monkeypatch):
    monkeypatch.setattr(ing, "_claude_call", lambda p: "")   # CLI failure → fail-open
    assert ing.extract_rss_events([{"title": "t", "source": "s", "url": "u", "pub_date": None}]) == []


def test_marketaux_skipped_without_key(monkeypatch):
    monkeypatch.delenv("MARKETAUX_KEY", raising=False)
    assert ing.fetch_marketaux(ing._ticker_index(_graph())) == []
```

- [ ] **Step 2: Run — fail.**

- [ ] **Step 3: Implement fetchers** — append to `pipeline/ingest_news.py`:

```python
# Reuse the impact engine's Claude caller + tolerant parser, and the news RSS layer.
from api.impact import _claude_call, _parse_llm_json  # noqa: E402

_8K_ITEM_CATEGORY = {"2.01": "m&a", "1.01": "agreement", "5.02": "exec"}


def _category_for_8k(item_code: str) -> str:
    return _8K_ITEM_CATEGORY.get((item_code or "").strip(), "filing")


def _candidate_from_ticker(ticker, headline, source, url, category, published_at, idx) -> Optional[dict]:
    node_id = idx.get((ticker or "").upper())
    if not node_id:
        return None
    c = {"headline": headline[:200], "source": source, "url": url, "category": category,
         "published_at": published_at, "seed_entity": ticker, "seed_node_id": node_id}
    c["id"] = _event_id(c)
    return c


def fetch_8k(conn) -> list[dict]:
    """Recent 8-Ks across graph filers → candidates (seed = the filer node)."""
    from pipeline.sec_8k import fetch_recent_8k_meta
    out = []
    ciks = [r["id"] for r in conn.execute("SELECT id FROM nodes WHERE id LIKE 'cik:%'")]
    for node_id in ciks:
        cik = node_id.split(":", 1)[1]
        try:
            for m in fetch_recent_8k_meta(cik, since_days=INGEST_MAX_AGE_DAYS):
                item0 = (m.get("items", "") or "").split(",")[0].strip()
                c = {"headline": f"{node_id} 8-K item {item0}"[:200], "source": "SEC 8-K",
                     "url": m.get("url", ""), "category": _category_for_8k(item0),
                     "published_at": m.get("date"), "seed_entity": node_id, "seed_node_id": node_id}
                c["id"] = _event_id(c)
                out.append(c)
        except Exception as exc:
            log.debug("8k: %s failed: %s", node_id, exc)
    return out


def fetch_marketaux(idx: dict[str, str]) -> list[dict]:
    key = os.environ.get("MARKETAUX_KEY")
    if not key:
        log.info("marketaux: no key; skipping")
        return []
    import requests
    try:
        r = requests.get("https://api.marketaux.com/v1/news/all",
                         params={"api_token": key, "language": "en", "filter_entities": "true", "limit": 50},
                         timeout=10)
        r.raise_for_status()
        data = r.json().get("data", [])
    except Exception as exc:
        log.warning("marketaux: %s", exc)
        return []
    out = []
    for art in data:
        for ent in art.get("entities", []):
            c = _candidate_from_ticker(ent.get("symbol"), art.get("title", ""), "Marketaux",
                                       art.get("url", ""), "company", (art.get("published_at") or "")[:10], idx)
            if c:
                out.append(c)
                break   # one seed per article (top entity)
    return out


def fetch_alphavantage(idx: dict[str, str]) -> list[dict]:
    key = os.environ.get("ALPHAVANTAGE_KEY")
    if not key:
        log.info("alphavantage: no key; skipping")
        return []
    import requests
    try:
        r = requests.get("https://www.alphavantage.co/query",
                         params={"function": "NEWS_SENTIMENT", "apikey": key, "limit": 50}, timeout=10)
        r.raise_for_status()
        feed = r.json().get("feed", [])
    except Exception as exc:
        log.warning("alphavantage: %s", exc)
        return []
    out = []
    for art in feed:
        ts = art.get("time_published", "")            # YYYYMMDDTHHMMSS
        pub = f"{ts[0:4]}-{ts[4:6]}-{ts[6:8]}" if len(ts) >= 8 else None
        for ts_ent in sorted(art.get("ticker_sentiment", []),
                             key=lambda x: -abs(float(x.get("ticker_sentiment_score", 0) or 0))):
            c = _candidate_from_ticker(ts_ent.get("ticker"), art.get("title", ""), "Alpha Vantage",
                                       art.get("url", ""), "company", pub, idx)
            if c:
                out.append(c)
                break
    return out


_RSS_EXTRACT_PROMPT = """You are extracting market-moving EVENTS from raw news headlines for a
supply-chain impact graph. For EACH headline describing a concrete event that could move a
public company, commodity, or region (M&A, contract, output cut, approval, ban, tariff,
outbreak, policy action, etc.), output one object. SKIP opinion, analysis, "how/why"
explainers, listicles, price-move-only stories, and celebrity/sports.

For each kept item: rewrite the headline neutrally in <=15 words; name the SINGLE most
specific entity (company/commodity/region) it concerns; classify the category as one of
politics|commodity|health|filing|m&a|agreement|exec|macro|company|other.

RAW (numbered):
{items}

Return ONLY a JSON array (no prose):
[{{"index": <n>, "headline": "<rewrite>", "entity": "<name>", "category": "<cat>"}}]
"""


def extract_rss_events(raw: list[dict]) -> list[dict]:
    """One Claude call over pooled RSS items → candidates (unresolved seed_entity).
    Fail-open: empty/garbage from the CLI → [] (8-K + APIs still populate the cycle)."""
    if not raw:
        return []
    block = "\n".join(f"{i+1}. {r['title']}" for i, r in enumerate(raw))
    parsed = _parse_llm_json(_claude_call(_RSS_EXTRACT_PROMPT.format(items=block)))
    if not isinstance(parsed, list):
        return []
    out = []
    for h in parsed:
        if not isinstance(h, dict):
            continue
        i = h.get("index")
        if not isinstance(i, int) or not (1 <= i <= len(raw)):
            continue
        src = raw[i - 1]
        c = {"headline": str(h.get("headline") or src["title"])[:200], "source": src["source"],
             "url": src["url"], "category": str(h.get("category") or "other"),
             "published_at": src.get("pub_date"), "seed_entity": str(h.get("entity") or "").strip(),
             "seed_node_id": None}
        c["id"] = _event_id(c)
        if c["seed_entity"]:
            out.append(c)
    return out


def fetch_rss_broad() -> list[dict]:
    """Pull broad-category RSS via the news layer, then Claude-extract events."""
    from api.news import _fetch_raw
    return extract_rss_events(_fetch_raw())
```

- [ ] **Step 4: Run — pass.** `python -B -m pytest tests/test_ingest_fetchers.py -v` (5 pass).

- [ ] **Step 5: Commit** — `git add pipeline/ingest_news.py tests/test_ingest_fetchers.py && git commit -m "feat(v2): ingest fetchers — 8-K, Marketaux, Alpha Vantage, RSS+Claude"`

---

## Task 4: `main()` wiring + broadened feeds + `.env.example` + dry-run

**Files:** Modify `pipeline/ingest_news.py`, `api/news.py` (broaden feeds), `.env.example`.

- [ ] **Step 1: Broaden the RSS feed list** — in `api/news.py` `_RSS_FEEDS`, add these category feeds to the existing 7 (keep the tuple shape `(name, url, max_age_days)`). Verify each returns items with `python -B -c "import requests,xml.etree.ElementTree as ET; [print(u, len(list(ET.fromstring(requests.get(u,timeout=8,headers={'User-Agent':'EconGraph/0.1 kondaru.mk@gmail.com'}).content).iter('item')))) for u in [...]]"` and DROP any that error or return 0 (judgment call — same validation we used before):

```python
    ("CNBC World",       "https://www.cnbc.com/id/100727362/device/rss/rss.html", 2),
    ("CNBC Politics",    "https://www.cnbc.com/id/10000113/device/rss/rss.html", 2),
    ("CNBC Health Care", "https://www.cnbc.com/id/10000108/device/rss/rss.html", 3),
    ("CNBC Energy",      "https://www.cnbc.com/id/19836768/device/rss/rss.html", 2),
```

- [ ] **Step 2: Add `main()`** to `pipeline/ingest_news.py`:

```python
def run_ingest(db_path: Path = DB_PATH) -> dict[str, int]:
    """One ingestion cycle. Returns a summary counter dict."""
    from schema.store import connect, init_db, insert_event
    conn = connect(db_path)
    init_db(conn)
    try:
        idx = _ticker_index(conn)
        cands: list[dict] = []
        cands += fetch_8k(conn)
        cands += fetch_marketaux(idx)
        cands += fetch_alphavantage(idx)
        cands += fetch_rss_broad()

        # Resolve seeds for RSS (API/8-K already mapped); gate unresolvable.
        resolved = []
        for c in cands:
            if not c.get("seed_node_id"):
                c["seed_node_id"] = _resolve_to_node_id(conn, c.get("seed_entity", ""))
            if c["seed_node_id"]:
                resolved.append(c)

        fresh = dedupe(resolved, conn)
        ranked = cap(rank(fresh, conn))
        for c in ranked:
            insert_event(conn, {**c, "status": c["status"]})
        summary = {"fetched": len(cands), "resolved": len(resolved), "fresh": len(fresh),
                   "queued": sum(1 for c in ranked if c["status"] == "queued"),
                   "skipped": sum(1 for c in ranked if c["status"] == "skipped")}
        log.info("ingest: %s", summary)
        return summary
    finally:
        conn.close()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    s = run_ingest()
    print(f"ingest cycle: {s}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 3: Document keys** — add to `.env.example`:

```env
# So What? V2 news ingestion (all optional; source skipped if unset)
MARKETAUX_KEY=            # free tier: https://www.marketaux.com
ALPHAVANTAGE_KEY=         # free tier: https://www.alphavantage.co/support/#api-key
INGEST_CAP=25             # events queued per 12h cycle
INGEST_MAX_AGE_DAYS=3
```

- [ ] **Step 4: Add a `main` smoke test** to `tests/test_ingest_news.py` (uses a temp DB + monkeypatched fetchers so it's deterministic and offline):

```python
def test_run_ingest_end_to_end(monkeypatch, tmp_path):
    import pipeline.ingest_news as ing
    db = tmp_path / "g.db"
    conn = store.connect(db); store.init_db(conn)
    conn.execute("INSERT INTO nodes (id,type,name,tickers) VALUES ('cik:1','Company','Acme','[]')")
    conn.commit(); conn.close()
    monkeypatch.setattr(ing, "fetch_8k", lambda c: [
        {"id": "a", "headline": "h", "source": "SEC 8-K", "url": "u1", "category": "m&a",
         "published_at": "2026-06-17", "seed_entity": "cik:1", "seed_node_id": "cik:1"}])
    monkeypatch.setattr(ing, "fetch_marketaux", lambda idx: [])
    monkeypatch.setattr(ing, "fetch_alphavantage", lambda idx: [])
    monkeypatch.setattr(ing, "fetch_rss_broad", lambda: [])
    s = ing.run_ingest(db)
    assert s["queued"] == 1
    conn = store.connect(db)
    assert store.queued_events(conn)[0]["seed_node_id"] == "cik:1"
```

- [ ] **Step 5: Run all P1 tests.** `python -B -m pytest tests/test_events_store.py tests/test_ingest_news.py tests/test_ingest_fetchers.py -v` (all pass).

- [ ] **Step 6: Live dry-run (judgment call, offline-tolerant).** `python -B -m pipeline.ingest_news`. Expected: prints an `ingest cycle: {...}` summary. With Claude at 401 and no API keys, RSS/APIs contribute 0 (fail-open) but **8-K should populate** (network to SEC, no LLM). Confirm `events` has rows: `python -B -c "from schema.store import connect,queued_events; print(len(queued_events(connect('econgraph.db'))))"`. If SEC is slow/unreachable, note it — the deterministic tests are the real gate.

- [ ] **Step 7: Commit** — `git add pipeline/ingest_news.py api/news.py .env.example tests/test_ingest_news.py && git commit -m "feat(v2): ingest main() cycle + broadened category feeds + .env keys"`

---

## Self-Review

- **Spec coverage:** events table (T1) · resolve/dedupe/rank/cap core (T2) · 8-K/Marketaux/AlphaVantage/RSS fetchers, key-optional, fail-open (T3) · main cycle + broadened feeds + keys + dry-run (T4). ✓
- **Priority formula** (source × centrality × recency), centrality via hubs.jsonl→degree fallback → T2 `rank`/`_centrality`. ✓
- **Dedup across cycles** (skip ids in `events`) → T2 `dedupe`. ✓
- **Graph-gate** (drop unresolvable) → T4 `run_ingest` resolve loop. ✓
- **Placeholders:** none — full code each step; the feed-verification one-liner (T4 S1) is a real command with a judgment call to drop dead feeds, not a TBD. ✓
- **Naming consistency:** `_event_id`, `_resolve_to_node_id`, `_ticker_index`, `_centrality`, `rank`, `cap`, `dedupe`, `extract_rss_events`, `fetch_8k/marketaux/alphavantage/rss_broad`, `run_ingest`, `insert_event`/`event_exists`/`queued_events` — consistent across tasks and tests. ✓
- **Known follow-ups (out of scope, noted):** the all-types resolver now exists in 3 places (`impact._resolve_entity`, `news._entity_resolves`, `ingest._resolve_to_node_id`) — a shared util is a future cleanup; 8-K item coverage reuses `sec_8k`'s contract-item filter (broaden later). No tracing (P2).
