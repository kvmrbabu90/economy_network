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
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
# Honors ECONGRAPH_DB (matches schema.store.default_db_path) so the DB can live
# on a local, non-synced path; falls back to <repo>/econgraph.db.
DB_PATH = Path(os.environ.get("ECONGRAPH_DB") or (REPO_ROOT / "econgraph.db"))
HUBS_PATH = REPO_ROOT / "data" / "hubs.jsonl"

INGEST_CAP = int(os.environ.get("INGEST_CAP", "25"))
INGEST_MAX_AGE_DAYS = int(os.environ.get("INGEST_MAX_AGE_DAYS", "3"))
# Hard wall-clock ceiling for one ingestion cycle (seconds). A slow SEC crawl
# must not eat the whole hourly slot — fetch_8k stops enqueuing new CIKs once
# this budget is spent. Default 15 min.
INGEST_WALLCLOCK_S = float(os.environ.get("INGEST_WALLCLOCK_S", "900"))
# Cap 8-Ks pulled per filer per cycle, so a single filer with a long recent
# history can't dominate (and re-crawl) the window.
INGEST_8K_PER_CIK = int(os.environ.get("INGEST_8K_PER_CIK", "3"))
# GDELT is title-only provenance → default 0.7 weight (same as RSS), so it loses
# the first-wins cross-feed collapse to the authoritative 8-K / tickered APIs.
_SOURCE_WEIGHT = {"SEC 8-K": 1.0, "Marketaux": 1.0, "Alpha Vantage": 1.0}  # default 0.7 (RSS, GDELT)

# ── GDELT targeted per-node source ─────────────────────────────────────────
# Number of highest-centrality nodes to query GDELT for each cycle (one query
# per node; the seed is that node, so no fuzzy resolution). Small by design —
# GDELT is high-recall/low-precision, and each query costs an API call + the
# downstream materiality LLM budget.
GDELT_TOP_NODES = int(os.environ.get("GDELT_TOP_NODES", "15"))
GDELT_TIMESPAN = os.environ.get("GDELT_TIMESPAN", "1h")   # match hourly cadence
# Few per node keeps the downstream materiality LLM batch (all fresh candidates
# in one call) manageable: 15 nodes x 5 = 75 GDELT candidates max before dedupe.
GDELT_MAXRECORDS = int(os.environ.get("GDELT_MAXRECORDS", "5"))
# Self-budgeting wall clock so a slow upstream 8-K crawl can't starve GDELT.
# GDELT's 1-req/5s limit means 15 nodes need >=75s; 180 leaves headroom for the
# occasional 429 backoff-retry.
GDELT_WALLCLOCK_S = float(os.environ.get("GDELT_WALLCLOCK_S", "180"))
# Category assigned by seed node type (GDELT gives no taxonomy of its own).
_GDELT_CATEGORY_BY_TYPE = {
    "Company": "company", "Commodity": "commodity", "Material": "commodity",
    "Region": "macro", "Regulator": "politics",
}

# ── GDELT bulk GKG source (primary; supersedes the per-node DOC fetcher) ─────
# Pull the last GKG_SLICES 15-min Global-Knowledge-Graph files, match GDELT's
# pre-extracted organizations against ALL nodes at once, and pre-cap by a cheap
# materiality prior so the downstream single-call LLM gate stays small.
GKG_SLICES = int(os.environ.get("GKG_SLICES", "4"))          # 4 x 15min = 1 hour
GKG_PRECAP = int(os.environ.get("GKG_PRECAP", "40"))         # cap before the LLM gate
GKG_ENGLISH_ONLY = os.environ.get("GKG_ENGLISH_ONLY", "1") != "0"
GKG_PROXIMITY_CHARS = int(os.environ.get("GKG_PROXIMITY_CHARS", "400"))
# Salience: an article is "about" a matched org only if it appears in the title,
# near the top (lede), or is mentioned repeatedly — otherwise it's an incidental
# mention (e.g. "posted on Instagram", "NVIDIA (NASDAQ:NVDA)") and pure noise.
GKG_LEDE_CHARS = int(os.environ.get("GKG_LEDE_CHARS", "800"))
GKG_WALLCLOCK_S = float(os.environ.get("GKG_WALLCLOCK_S", "240"))
# Cache raw slice zips beside the (relocated, non-OneDrive) DB by default.
GKG_CACHE_DIR = Path(os.environ.get("GKG_CACHE_DIR") or (DB_PATH.parent / "gkg_cache"))


def _safe_float(value: Any) -> float:
    """Best-effort float; 0.0 on None / non-numeric (e.g. "N/A"). Never raises."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _event_id(cand: dict[str, Any]) -> str:
    """Stable id: sha1 of the url if present, else source|headline."""
    basis = (cand.get("url") or f"{cand.get('source','')}|{cand.get('headline','')}").strip().lower()
    return "ev:" + hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]


def _collapse_key(cand: dict[str, Any]) -> Optional[tuple[str, str, str]]:
    """Secondary cross-feed dedupe key: (seed_node_id, published-date, first-6
    normalized words of the headline). The same story arriving via 8-K +
    Marketaux + AV + RSS has distinct urls (so distinct _event_id) but collapses
    here to one queued event. Returns None when we lack a seed to key on."""
    seed = cand.get("seed_node_id")
    if not seed:
        return None
    day = (cand.get("published_at") or "")[:10]
    words = [w for w in "".join(
        ch if (ch.isalnum() or ch.isspace()) else " "
        for ch in (cand.get("headline") or "").lower()
    ).split()][:6]
    return (str(seed), day, " ".join(words))


def _like_escape(term: str) -> str:
    r"""Escape LIKE wildcards so a name containing % or _ (or the escape char
    itself) matches literally. Pair with an ESCAPE '\' clause."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _resolve_to_node_id(conn, name: str) -> Optional[str]:
    """Resolve a name to a node id across ALL types (name/alias exact → starts-with →
    contains). Mirrors the news graph-gate resolver but returns the id.

    LIKE fallbacks are made deterministic: wildcards in the query term are
    escaped, and ties broken by ORDER BY length(name), id so LIMIT 1 is stable
    across rebuilds (the shortest, then lexically-first, name wins)."""
    q = (name or "").lower().strip()
    if not q:
        return None
    esc = _like_escape(q)
    # Exact matches first (no LIKE ambiguity); then deterministic prefix/contains.
    for sql, arg in (
        ("SELECT id FROM nodes WHERE LOWER(name) = ? LIMIT 1", q),
        ("SELECT n.id FROM aliases a JOIN nodes n ON n.id = a.node_id WHERE a.alias_normalized = ? LIMIT 1", q),
        ("SELECT id FROM nodes WHERE LOWER(name) LIKE ? ESCAPE '\\' "
         "ORDER BY length(name), id LIMIT 1", esc + "%"),
    ):
        row = conn.execute(sql, (arg,)).fetchone()
        if row:
            return row[0]
    if len(q) >= 5:
        row = conn.execute(
            "SELECT id FROM nodes WHERE LOWER(name) LIKE ? ESCAPE '\\' "
            "ORDER BY length(name), id LIMIT 1", (f"%{esc}%",)
        ).fetchone()
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
                # identify_hubs.py writes the betweenness score under "centrality";
                # accept "score" too for any legacy hubs file. Reading only "score"
                # silently loaded NOTHING (every line has "centrality"), so this
                # whole function was falling through to the degree fallback.
                val = h.get("centrality", h.get("score"))
                if h.get("id") is not None and val is not None:
                    scores[h["id"]] = float(val)
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
    collapse in-cycle duplicates. Two collapse layers, first-wins:

      1. url-hash id (`_event_id`) — the primary key.
      2. cross-feed key (`_collapse_key`) — same (seed, publish-date,
         first-6-headline-words) via a different feed has a *different* url and
         thus a different id, so without this one real story becomes N queued
         events (8-K + Marketaux + AV + RSS)."""
    from schema.store import event_exists
    seen: set[str] = set()
    seen_keys: set[tuple[str, str, str]] = set()
    out = []
    for c in cands:
        cid = c["id"]
        if cid in seen or event_exists(conn, cid):
            continue
        key = _collapse_key(c)
        if key is not None and key in seen_keys:
            continue
        seen.add(cid)
        if key is not None:
            seen_keys.add(key)
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


def fetch_8k(conn, *, deadline: Optional[float] = None) -> list[dict]:
    """Recent 8-Ks across graph filers → candidates (seed = the filer node).

    Per-CIK cap (INGEST_8K_PER_CIK) keeps one prolific filer from dominating the
    window; the wall-clock `deadline` (monotonic seconds) stops enqueuing new
    CIKs so a slow SEC crawl can't blow the hourly slot — CIKs already scanned
    still count, we just stop starting new ones."""
    from pipeline.sec_8k import fetch_recent_8k_meta
    out = []
    ciks = [r["id"] for r in conn.execute("SELECT id FROM nodes WHERE id LIKE 'cik:%'")]
    for node_id in ciks:
        if deadline is not None and time.monotonic() >= deadline:
            log.warning("8k: wall-clock budget spent; stopping crawl (%d CIKs scanned)",
                        ciks.index(node_id))
            break
        cik = node_id.split(":", 1)[1]
        try:
            # fetch_recent_8k_meta returns EDGAR recent-first; keep the newest few.
            for m in fetch_recent_8k_meta(cik, since_days=INGEST_MAX_AGE_DAYS)[:INGEST_8K_PER_CIK]:
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
        # A null "entities" (or "title"/"url") must not raise — coerce to [].
        for ent in (art.get("entities") or []):
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
        ts = art.get("time_published", "") or ""      # YYYYMMDDTHHMMSS
        pub = f"{ts[0:4]}-{ts[4:6]}-{ts[6:8]}" if len(ts) >= 8 else None
        # A null "ticker_sentiment" must not raise; a non-numeric score like
        # "N/A" must not raise the sort — _safe_float returns 0.0 for both.
        for ts_ent in sorted((art.get("ticker_sentiment") or []),
                             key=lambda x: -abs(_safe_float(x.get("ticker_sentiment_score")))):
            c = _candidate_from_ticker(ts_ent.get("ticker"), art.get("title", ""), "Alpha Vantage",
                                       art.get("url", ""), "company", pub, idx)
            if c:
                out.append(c)
                break
    return out


def fetch_gdelt(conn, *, deadline: Optional[float] = None) -> list[dict]:
    """Targeted GDELT DOC 2.0 news for the top-centrality graph nodes.

    For each of the ``GDELT_TOP_NODES`` highest-centrality named nodes we run ONE
    DOC 2.0 artlist query scoped to that node's name; every returned article is
    seeded to that KNOWN node id — GDELT's entity noise never touches the seed,
    and we skip ``_resolve_to_node_id`` for this source entirely. Volume and
    precision are controlled downstream by the shared dedupe + materiality gate +
    per-cycle cap. Disabled with ``INGEST_GDELT='0'``.

    Self-budgeting: when ``deadline`` is None it computes its own wall-clock
    budget (``GDELT_WALLCLOCK_S``) *at call time*, so a slow upstream 8-K crawl
    can't starve it. Each per-node query is isolated — one failing entity is
    logged and skipped, never aborting the batch (mirrors ``fetch_8k``)."""
    # Off by default now that bulk GKG (fetch_gkg_bulk) is the primary GDELT
    # surface; opt in with INGEST_GDELT=1 for on-demand per-node queries.
    if os.environ.get("INGEST_GDELT", "0") == "0":
        return []
    from pipeline.gdelt import gdelt_search

    if deadline is None:
        deadline = time.monotonic() + GDELT_WALLCLOCK_S
    cen = _centrality(conn)
    rows = conn.execute(
        "SELECT id, name, type FROM nodes WHERE name IS NOT NULL AND name != ''"
    ).fetchall()
    # Highest centrality first; ties broken by id so the selection is stable.
    top = sorted(rows, key=lambda r: (-cen.get(r["id"], 0.0), r["id"]))[:GDELT_TOP_NODES]
    out: list[dict] = []
    for r in top:
        if time.monotonic() >= deadline:
            log.warning("gdelt: wall-clock budget spent; stopping crawl")
            break
        try:
            arts = gdelt_search(r["name"], timespan=GDELT_TIMESPAN, maxrecords=GDELT_MAXRECORDS)
        except Exception as exc:
            log.debug("gdelt: query for %r failed: %s", r["name"], exc)
            continue
        category = _GDELT_CATEGORY_BY_TYPE.get(r["type"], "other")
        for a in arts:
            c = {"headline": a["title"][:200], "source": "GDELT", "url": a["url"],
                 "category": category, "published_at": a.get("published_at"),
                 "seed_entity": r["name"], "seed_node_id": r["id"]}
            c["id"] = _event_id(c)
            out.append(c)
    return out


def build_gkg_node_index(conn) -> dict[str, tuple[str, str, str, bool]]:
    """Map a normalized name/alias → (node_id, node_name, node_type, ambiguous).

    Built from node NAMES + ALIASES only — never tickers-as-tokens (short tickers
    like ALL/CAR/IT collide with everyday words). A normalized key that maps to
    >1 node is DROPPED (we can't seed it confidently — precision over recall).
    `ambiguous` flags single-token common-English-word names (shell/gap/apple…)
    that require the offset-proximity gate before we trust an org match."""
    from pipeline import gkg
    keys: dict[str, set[str]] = {}
    meta: dict[str, tuple[str, str]] = {}
    for nid, name, ntype, aliases in conn.execute(
            "SELECT id, name, type, aliases FROM nodes WHERE name IS NOT NULL AND name != ''"):
        meta[nid] = (name, ntype)
        cands = [name]
        try:
            cands += [a for a in json.loads(aliases or "[]") if a]
        except Exception:
            pass
        for cand in cands:
            k = gkg.normalize_name(cand)
            if len(k) < 2:
                continue
            keys.setdefault(k, set()).add(nid)
    index: dict[str, tuple[str, str, str, bool]] = {}
    dropped = 0
    for k, ids in keys.items():
        if len(ids) != 1:
            dropped += 1
            continue
        nid = next(iter(ids))
        name, ntype = meta[nid]
        index[k] = (nid, name, ntype, gkg.is_common_word(k))
    log.info("gkg index: %d keys (%d ambiguous-collision keys dropped)", len(index), dropped)
    return index


def _proximity_ok(org_off: Optional[int], biz_offsets: list[int], amt_offsets: list[int]) -> bool:
    """Gate an ambiguous (common-word) org match: the org must sit near a business
    theme or a currency amount. With no offset for the org, fall back to requiring
    ANY business signal in the record."""
    if org_off is None:
        return bool(biz_offsets or amt_offsets)
    for off in biz_offsets:
        if abs(off - org_off) <= GKG_PROXIMITY_CHARS:
            return True
    for off in amt_offsets:
        if abs(off - org_off) <= GKG_PROXIMITY_CHARS:
            return True
    return False


def _gkg_materiality_prior(rec, centrality: float) -> float:
    """Cheap, LLM-free materiality score used only to PRE-CAP before the shared
    LLM gate. Business-theme count + hard-event + currency amount + tone/polarity
    + a centrality tiebreak."""
    from pipeline import gkg
    biz = sum(1 for code in rec.themes if gkg.is_business_theme(code))
    hard = any(gkg.is_hard_event_theme(code) for code in rec.themes)
    has_cur = any(gkg.amount_is_currency(obj) for _, obj, _ in rec.amounts)
    score = min(biz, 3) * 1.0
    score += 2.0 if has_cur else 0.0
    score += 2.0 if hard else 0.0
    score += min(abs(rec.tone) / 5.0, 2.0)
    score += 0.5 if rec.polarity > 6 else 0.0
    score += centrality
    return score


def _org_salience(k: str, offsets: Optional[list[int]], title_norm: str) -> Optional[bool]:
    """Is the article actually ABOUT this matched org, or just mentioning it?

    True if the org is in the (normalized) title, appears in the lede
    (min offset ≤ GKG_LEDE_CHARS), or is mentioned ≥2×. False if it appears once,
    deep in the body (incidental). None when we have no offsets to judge (only V1
    orgs) — treated as "unknown, allow" so we don't blindly drop title-less rows."""
    if title_norm and k in title_norm:
        return True
    if offsets:
        return min(offsets) <= GKG_LEDE_CHARS or len(offsets) >= 2
    return None


def _gkg_candidate(rec, index: dict, cen: dict) -> Optional[tuple[float, dict]]:
    """One GKG record → (materiality_prior, candidate dict) if it matches a node
    the article is plausibly ABOUT, else None. Picks the highest-centrality
    salient match as the single seed."""
    from pipeline import gkg
    if not rec.is_web or not rec.url:
        return None
    if GKG_ENGLISH_ONLY and rec.is_translingual:
        return None
    org_offsets: dict[str, list[int]] = {}
    for name, off in rec.v2orgs:
        org_offsets.setdefault(gkg.normalize_name(name), []).append(off)
    surfaces = rec.orgs or [n for n, _ in rec.v2orgs]
    title_norm = gkg.normalize_name(rec.title) if rec.title else ""
    biz_offsets = [off for code, off in rec.v2themes if gkg.is_business_theme(code)]
    amt_offsets = [off for _, obj, off in rec.amounts if gkg.amount_is_currency(obj)]
    best = None
    for surface in surfaces:
        k = gkg.normalize_name(surface)
        if not k or k in gkg.EXCHANGE_NAMES:
            continue
        hit = index.get(k)
        if not hit:
            continue
        nid, nname, ntype, ambiguous = hit
        offs = org_offsets.get(k)
        if ambiguous and not _proximity_ok(offs[0] if offs else None, biz_offsets, amt_offsets):
            continue
        if _org_salience(k, offs, title_norm) is False:
            continue   # incidental mention, not what the article is about
        c = cen.get(nid, 0.0)
        if best is None or c > best[0]:
            best = (c, nid, nname, ntype)
    if best is None:
        return None
    c, nid, nname, ntype = best
    headline = rec.title or f"{nname}: news from {rec.domain}"
    cand = {"headline": headline[:200], "source": "GDELT-GKG", "url": rec.url,
            "category": _GDELT_CATEGORY_BY_TYPE.get(ntype, "other"),
            "published_at": rec.published_at, "seed_entity": nname, "seed_node_id": nid}
    cand["id"] = _event_id(cand)
    return _gkg_materiality_prior(rec, c), cand


def fetch_gkg_bulk(conn) -> list[dict]:
    """Bulk GKG ingestion (primary GDELT surface). Download the last GKG_SLICES
    15-min GKG files, match GDELT's pre-extracted organizations against ALL nodes,
    apply the cheap LLM-free relevance/noise/dedup cascade, and return the top
    GKG_PRECAP candidates by materiality prior (so the shared single-call LLM gate
    stays small). Disabled with INGEST_GKG='0'. Each slice is isolated so one bad
    download can't lose the others."""
    if os.environ.get("INGEST_GKG", "1") == "0":
        return []
    from pipeline import gkg
    deadline = time.monotonic() + GKG_WALLCLOCK_S
    index = build_gkg_node_index(conn)
    cen = _centrality(conn)
    try:
        latest = gkg.latest_gkg_url()
    except Exception as exc:
        log.warning("gkg: lastupdate poll failed: %s", exc)
        return []
    if not latest:
        return []
    timestamps = gkg.recent_slice_timestamps(latest[0], GKG_SLICES)

    seen_urls: set[str] = set()
    seen_images: set[str] = set()
    seen_keys: set = set()
    scored: list[tuple[float, dict]] = []
    for ts in timestamps:
        if time.monotonic() >= deadline:
            log.warning("gkg: wall-clock budget spent; stopping at slice %s", ts)
            break
        try:
            path = gkg.download_slice(gkg.gkg_slice_url(ts), GKG_CACHE_DIR)
            if path is None:
                continue
            records = gkg.parse_gkg(gkg.read_gkg_lines(path))
        except Exception as exc:
            log.warning("gkg: slice %s failed, continuing: %s", ts, exc)
            continue
        for rec in records:
            res = _gkg_candidate(rec, index, cen)
            if res is None:
                continue
            score, cand = res
            # Three dedup layers, so syndication doesn't waste pre-cap slots:
            # exact URL, exact SharingImage, then the (seed,date,first-6-words)
            # collapse key that catches cross-outlet copies with distinct urls.
            if cand["url"] in seen_urls:
                continue
            if rec.sharing_image and rec.sharing_image in seen_images:
                continue
            key = _collapse_key(cand)
            if key is not None and key in seen_keys:
                continue
            seen_urls.add(cand["url"])
            if rec.sharing_image:
                seen_images.add(rec.sharing_image)
            if key is not None:
                seen_keys.add(key)
            scored.append((score, cand))
    scored.sort(key=lambda t: -t[0])
    kept = [c for _, c in scored[:GKG_PRECAP]]
    log.info("gkg: %d matched candidates → kept top %d", len(scored), len(kept))
    return kept


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
routine product launches, celebrity/sports/lifestyle, and law-firm "investigation" /
class-action-solicitation / "shareholder alert" notices (these are legal advertising, not
events), and stories where the company is only mentioned incidentally.

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
    usable = 0   # well-formed verdicts (in-range int index + boolean material)
    for h in parsed:
        if not isinstance(h, dict):
            continue
        i = h.get("index")
        mat = h.get("material")
        if isinstance(i, int) and 1 <= i <= len(cands) and isinstance(mat, bool):
            usable += 1
            if mat:
                keep_idx.add(i - 1)
    if usable == 0:
        # A list, but no well-formed verdicts — can't tell all-noise from garbage,
        # so fail open rather than silently zero the cycle.
        log.warning("materiality gate: no usable verdicts — keeping all %d", len(cands))
        return cands
    # Trust a well-formed verdict set even when it keeps zero: a valid "nothing
    # material this cycle" must NOT fall back to tracing noise.
    kept = [c for i, c in enumerate(cands) if i in keep_idx]
    log.info("materiality gate: kept %d/%d material", len(kept), len(cands))
    return kept


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
        deadline = time.monotonic() + INGEST_WALLCLOCK_S
        cands: list[dict] = []
        # PER-FETCHER ISOLATION: one flaky source must not discard the others'
        # candidates (esp. the completed 8-K crawl). Each fetch is wrapped so an
        # exception is logged and the cycle continues with whatever succeeded.
        for name, fetch in (
            ("8k", lambda: fetch_8k(conn, deadline=deadline)),
            ("marketaux", lambda: fetch_marketaux(idx)),
            ("alphavantage", lambda: fetch_alphavantage(idx)),
            # GKG (bulk GDELT) after the high-provenance sources: first-wins
            # cross-feed collapse must keep the authoritative 8-K / tickered story
            # over GKG's title-only one. Supersedes the per-node DOC fetch_gdelt,
            # which is now opt-in (INGEST_GDELT default off) for on-demand use.
            ("gkg", lambda: fetch_gkg_bulk(conn)),
            ("rss", fetch_rss_broad),
        ):
            try:
                cands += fetch()
            except Exception as exc:
                log.warning("ingest: fetcher %s failed, continuing: %s", name, exc)

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
        # Persist ONLY the queued (top-cap) events. Over-cap material candidates are
        # deliberately NOT written: recording them as 'skipped' made event_exists()
        # skip them forever (nothing re-queues 'skipped'), so a fresh material story
        # that merely lost the per-cycle cap race was dropped permanently. Leaving
        # them unpersisted keeps them re-eligible — the next cycle re-fetches (within
        # INGEST_MAX_AGE_DAYS), re-ranks, and queues them once the queue has room.
        queued = [c for c in ranked if c["status"] == "queued"]
        for c in queued:
            insert_event(conn, {**c, "status": "queued"})
        summary = {"fetched": len(cands), "resolved": len(resolved), "fresh": len(fresh),
                   "material": len(material), "queued": len(queued),
                   "deferred": len(ranked) - len(queued)}   # not dropped — re-eligible next cycle
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
