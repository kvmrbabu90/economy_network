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


# Reuse the impact engine's Claude caller + tolerant parser, and the news RSS layer.
from api.impact import _claude_call, _parse_llm_json  # noqa: E402

_8K_ITEM_CATEGORY = {"2.01": "m&a", "1.01": "agreement", "5.02": "exec"}


def _category_for_8k(item_code: str) -> str:
    return _8K_ITEM_CATEGORY.get((item_code or "").strip(), "filing")


def _candidate_from_ticker(ticker, headline, source, url, category, published_at, idx) -> Optional[dict]:
    node_id = idx.get((ticker or "").upper())
    if not node_id:
        return None
    c = {"headline": (headline or "")[:200], "source": source, "url": url, "category": category,
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
                     "published_at": m.get("filing_date"), "seed_entity": node_id, "seed_node_id": node_id}
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


_MATERIALITY_PROMPT = """You are a markets analyst gatekeeping a supply-chain impact graph.
For EACH numbered item decide: is it a CONCRETE, DETERMINISTIC market-moving / business-impact
event with a clear directional effect on a company, commodity, or region?

KEEP (material=true): M&A, contracts won/lost, output/production cuts, regulatory
approval/ban/recall, tariffs/sanctions, earnings or guidance surprises, supply disruptions,
plant/mine closures, defaults, large capex/JV, major executive departures.

DROP (material=false): opinion / analysis / "how/why" explainers, price-move-only stories
("stock rises 3%"), analyst rating or price-target changes, rumor / "could/may/reportedly",
routine product launches, and celebrity/sports/lifestyle.

ITEMS (numbered):
{items}

Return ONLY a JSON array (no prose):
[{{"index": <n>, "material": true|false}}]
"""


def _materiality_filter(cands: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep only candidates the LLM judges to be concrete, deterministic market-moving /
    business-impact events. One batched call. Fail-open (empty/garbled → keep all).
    Disabled when INGEST_MATERIALITY_GATE='0'."""
    if not cands:
        return cands
    if os.environ.get("INGEST_MATERIALITY_GATE", "1") == "0":
        return cands
    block = "\n".join(f"{i+1}. {c.get('headline','')} — {c.get('seed_entity','')}"
                      for i, c in enumerate(cands))
    parsed = _parse_llm_json(_claude_call(_MATERIALITY_PROMPT.format(items=block)))
    if not isinstance(parsed, list):
        log.warning("materiality gate: unparseable LLM output — keeping all %d", len(cands))
        return cands
    keep_idx = set()
    for h in parsed:
        if isinstance(h, dict) and h.get("material") is True:
            i = h.get("index")
            if isinstance(i, int) and 1 <= i <= len(cands):
                keep_idx.add(i - 1)
    if not keep_idx:
        # A parsed-but-empty keep-set is ambiguous (all-noise vs. a bad response).
        # Fail-open to avoid silently zeroing a cycle.
        log.warning("materiality gate: LLM kept 0/%d — keeping all (fail-open)", len(cands))
        return cands
    return [c for i, c in enumerate(cands) if i in keep_idx]


def fetch_rss_broad() -> list[dict]:
    """Pull broad-category RSS via the news layer, then Claude-extract events."""
    from api.news import _fetch_raw
    return extract_rss_events(_fetch_raw())


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
        material = _materiality_filter(fresh)
        ranked = cap(rank(material, conn))
        for c in ranked:
            insert_event(conn, {**c, "status": c["status"]})
        summary = {"fetched": len(cands), "resolved": len(resolved), "fresh": len(fresh),
                   "material": len(material),
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
