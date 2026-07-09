"""EconGraph read-only API (Phase 5).

FastAPI service over `econgraph.db`. The graph is NEVER mutated from here --
refreshes happen by re-running the pipeline (Phases 1-4). The one piece of
real logic is the `customer_of` derivation in :mod:`api.query`.

Run:
    uvicorn api.main:app --reload --port 8001

CORS is open for any localhost origin so Phase 6's Vite app (which runs on
another port) can call this without surprise CORS-blocked fetches.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from fastapi import Body, Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from schema import store
from schema.store import connect

from . import impact as impact_mod
from . import news as news_mod
from . import query as q
from .query import resolve_id


log = logging.getLogger("api.main")

# Module-level config -- replaced by tests via the dependency override hook.
_DB_PATH = store.default_db_path()
_ALIAS_INDEX: Optional[q.AliasIndex] = None


def set_db_path(path: Path) -> None:
    """Test/CLI hook to point the app at a different SQLite file."""
    global _DB_PATH, _ALIAS_INDEX
    _DB_PATH = Path(path)
    _ALIAS_INDEX = None  # rebuilt lazily on next /search call


# --- So What? V2 · P9 — on-demand "Refresh news" ---------------------------
# Run the full ingest -> materiality-gate -> trace -> aggregate cycle in a
# background thread so the UI can trigger it and poll for completion. Only one
# runs at a time (_refresh_lock). It shares the DB with the hourly scheduled
# cycle safely (WAL + per-event commits); worst case both run at once and a
# little work is duplicated.
_refresh_lock = threading.Lock()
_refresh_state: dict = {"running": False, "started_at": None, "finished_at": None,
                        "result": None, "error": None}


def _run_refresh() -> None:
    from pipeline.run_cycle import run_cycle
    try:
        _refresh_state["result"] = run_cycle(_DB_PATH)
        _refresh_state["error"] = None
    except Exception as exc:   # never let the daemon thread crash silently
        log.exception("impact refresh failed")
        _refresh_state["error"] = repr(exc)
    finally:
        _refresh_state["finished_at"] = time.time()
        _refresh_state["running"] = False


# ---------------------------------------------------------------------------
# Dependency: per-request SQLite connection.
# ---------------------------------------------------------------------------

def get_conn() -> sqlite3.Connection:
    """FastAPI dependency: open a fresh connection per request.

    SQLite handles small concurrent reads fine; we don't pool. The /search
    endpoint reuses a process-wide AliasIndex instead of rebuilding from
    the table every call.
    """
    conn = connect(_DB_PATH)
    try:
        yield conn
    finally:
        conn.close()


def get_alias_index(conn: sqlite3.Connection = Depends(get_conn)) -> q.AliasIndex:
    global _ALIAS_INDEX
    if _ALIAS_INDEX is None:
        _ALIAS_INDEX = q.AliasIndex.build(conn)
    return _ALIAS_INDEX


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="EconGraph API",
    version="0.5.0",
    description=(
        "Read-only graph over a single, queryable, typed economic graph. "
        "`customer_of` is derived on read from stored `supplies` edges "
        "(invariant #2)."
    ),
)


_warmup_lock = threading.Lock()   # prevents duplicate warmup threads within this process


@app.on_event("startup")
def _warmup_news_cache() -> None:
    """Pre-warm the daily headlines cache in a background thread.

    The first call to get_daily_headlines() hits RSS feeds + Claude CLI and
    takes 30-60 s. If the browser beats that with its 15 s fetch timeout,
    it shows 'API offline?' even though the API is healthy. By starting the
    warm-up immediately at server boot, the cache is ready (or nearly ready)
    by the time the user's browser loads the page.

    Guard: `_warmup_lock` prevents a second warmup thread from starting if
    _warmup_news_cache is somehow called more than once within the same process
    (e.g. tests that reset the ASGI app). Each uvicorn --reload spawns a fresh
    process so the lock is recreated unlocked on each restart.
    """
    def _warm() -> None:
        if not _warmup_lock.acquire(blocking=False):
            log.debug("news: warmup already running, skipping duplicate")
            return
        try:
            log.info("news: pre-warming headlines cache...")
            news_mod.get_daily_headlines()
            log.info("news: headlines cache ready")
        except Exception as exc:  # never crash the server
            log.warning("news: cache warmup failed: %s", exc)
        finally:
            _warmup_lock.release()

    threading.Thread(target=_warm, daemon=True, name="news-warmup").start()

# Phase 6's Vite frontend will run on a different localhost port. Without
# CORS the browser silently blocks fetches and the failure looks like a
# frontend bug; allowing localhost saves an hour of debugging.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?",
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
def get_health(conn: sqlite3.Connection = Depends(get_conn)):
    """Counts + the customer_of-rows invariant check (must be 0), plus
    node_impact cache freshness (So What? V2 · P4)."""
    result = q.health(conn)
    # Tolerate DBs built before the P4 node_impact table existed (e.g. a live
    # econgraph.db from an older build_graph): report 0 / None rather than 500.
    try:
        result["node_impact_rows"] = conn.execute(
            "SELECT COUNT(*) c FROM node_impact"
        ).fetchone()["c"]
        result["node_impact_computed_at"] = store.latest_node_impact_computed_at(conn)
    except sqlite3.OperationalError:
        result["node_impact_rows"] = 0
        result["node_impact_computed_at"] = None
    # Event pipeline counts (So What? V2 · P11) so the UI can show how many news
    # articles have been traced. Guarded: a pre-P1 DB has no events table.
    try:
        by_status = {r["status"]: r["c"] for r in conn.execute(
            "SELECT status, COUNT(*) c FROM events GROUP BY status"
        ).fetchall()}
    except sqlite3.OperationalError:
        by_status = {}
    result["events"] = by_status
    result["events_traced"] = by_status.get("traced", 0)
    return result


def _parse_types(types: Optional[str]) -> Optional[list[str]]:
    """`types` is a comma-separated string; None means "all"."""
    if not types:
        return None
    return [t.strip() for t in types.split(",") if t.strip()]


def _reraise_if_locked(exc: sqlite3.OperationalError) -> None:
    """Distinguish a transient lock from a genuinely-absent table.

    The store read helpers swallow `OperationalError` to empty so a DB built
    before the P4 `node_impact` table ('no such table') degrades gracefully.
    A lock ('database is locked' / 'database is busy') is a different beast:
    the cache is fine, we just collided with the hourly aggregate's commit.
    Surface that as a 503 so the frontend retries instead of rendering an
    (empty and therefore wrong) impact overlay.

    Missing-table errors are re-raised unchanged for the caller to swallow.
    """
    msg = str(exc).lower()
    if "database is locked" in msg or "database is busy" in msg:
        raise HTTPException(
            status_code=503, detail="impact cache temporarily locked, retry"
        ) from exc
    # 'no such table' (or anything else non-lock) — let the caller decide;
    # the impact endpoints treat it as the pre-P4 empty-cache case.
    raise exc


# IMPORTANT: declare the more-specific `.../ego` route BEFORE the catch-all
# `/node/{node_id:path}`. With path-style ids (colons in "cik:0000080424"),
# the :path converter would otherwise swallow "cik:0000080424/ego" into the
# generic node route and 404 the ego endpoint.
@app.get("/node/{node_id:path}/ego")
def get_ego_endpoint(
    node_id: str,
    types: Optional[str] = Query(
        None,
        description="Comma-separated edge types: "
                    "supplies,customer_of,competes_with,regulated_by,part_of. "
                    "Default = all.",
    ),
    include_provisional: bool = Query(
        False,
        description="If true, include the audit layer: below_threshold edges "
                    "to provisional slug-target nodes (Nestlé, Unilever, ...).",
    ),
    include_inferred: bool = Query(
        False,
        description="If true, include Tier-2 inference edges (co-mention "
                    "closure: pairs of competitors named together in the same "
                    "filing snippet). Capped at 0.65 confidence, so they only "
                    "show with this toggle on.",
    ),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """1-hop ego graph: center + neighbors along requested types."""
    result = q.ego(
        conn, node_id,
        types=_parse_types(types),
        include_provisional=include_provisional,
        include_inferred=include_inferred,
    )
    if result is None:
        raise HTTPException(status_code=404, detail=f"Node not found: {node_id!r}")
    return result


# So What? V2 · P4 — serve the precomputed node_impact cache. NO request-time LLM.
# NOTE: `/node/{node_id:path}/impact` MUST be declared BEFORE the catch-all
# `/node/{node_id:path}` (same greedy-:path hazard as the /ego route above),
# otherwise the catch-all swallows ".../impact" and this endpoint 404s.
@app.get("/impact/live")
def impact_live(conn: sqlite3.Connection = Depends(get_conn)):
    # Atomic snapshot: read the rows and their computed_at inside ONE deferred
    # transaction so `computed_at` describes exactly the payload returned. Read
    # as two bare statements, the hourly aggregate could commit its atomic swap
    # (replace_node_impact) between them and hand back a timestamp that belongs
    # to a different set of rows. BEGIN pins a read view for the whole call.
    try:
        conn.execute("BEGIN")
        try:
            rows = store.read_all_node_impact(conn)
            computed_at = store.latest_node_impact_computed_at(conn)
        finally:
            # Read-only: nothing to persist. Rollback releases the snapshot.
            conn.rollback()
    except sqlite3.OperationalError as exc:
        _reraise_if_locked(exc)   # 503 on lock, re-raises 'no such table'
        rows, computed_at = [], None   # unreachable for missing-table (helpers swallow it)
    return {"computed_at": computed_at, "count": len(rows), "impacts": rows}


@app.post("/impact/refresh")
def impact_refresh():
    """Kick off a full news→impact cycle (ingest → materiality gate → trace →
    aggregate) in the background. Idempotent while running: a second call just
    reports the in-progress state. Poll GET /impact/refresh/status for completion."""
    with _refresh_lock:
        if _refresh_state["running"]:
            return {"status": "already_running", "running": True}
        _refresh_state.update(running=True, started_at=time.time(),
                              finished_at=None, result=None, error=None)
    threading.Thread(target=_run_refresh, daemon=True, name="impact-refresh").start()
    return {"status": "started", "running": True}


@app.get("/impact/refresh/status")
def impact_refresh_status():
    """Current state of a background refresh: {running, started_at, finished_at,
    result, error}. When running flips false, the map is ready to re-poll."""
    return dict(_refresh_state)


@app.get("/node/{node_id:path}/impact")   # BEFORE the catch-all /node/{node_id:path}
def node_impact(node_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    # Wrap EVERY DB access (resolve_id, the nodes lookup, and the node_impact read)
    # in one guard: a lock during ANY of them must surface as a retryable 503, not
    # an uncaught 500. HTTPException (the 404 below) is not an OperationalError, so
    # it propagates unchanged. read_node_impact swallows only 'no such table'.
    try:
        cid = resolve_id(conn, node_id)
        if not cid:
            raise HTTPException(status_code=404, detail=f"unknown node: {node_id}")
        nrow = conn.execute("SELECT name, type FROM nodes WHERE id = ?", (cid,)).fetchone()
        name = nrow["name"] if nrow else cid
        ntype = nrow["type"] if nrow else None
        raw = store.read_node_impact(conn, cid)
    except sqlite3.OperationalError as exc:
        _reraise_if_locked(exc)   # 503 on lock; re-raises any other OperationalError
        raise                     # unreachable (helper always raises), keeps types happy
    if raw is None:
        return {"node_id": cid, "name": name, "type": ntype, "impact": None}
    top = json.loads(raw["top_events"] or "[]")
    ids = [e["event_id"] for e in top if e.get("event_id")]
    meta = {}
    if ids:
        q_sql = "SELECT id, url, source, gkg_context FROM events WHERE id IN (%s)" % ",".join("?" * len(ids))
        meta = {r["id"]: r for r in conn.execute(q_sql, ids).fetchall()}
    for e in top:
        m = meta.get(e.get("event_id"))
        e["url"] = m["url"] if m else None
        e["source"] = m["source"] if m else None
        # Grounding capsule (may be NULL for non-GKG events); the frontend forwards
        # it on "Sharpen with Claude" so the on-demand trace anchors like precompute.
        e["gkg_context"] = m["gkg_context"] if m else None
    return {"node_id": cid, "name": name, "type": ntype,
            "impact": {"direction": raw["direction"], "magnitude": raw["magnitude"],
                       "mixed_signals": raw["mixed_signals"], "event_count": raw["event_count"],
                       "computed_at": raw["computed_at"], "top_events": top}}


@app.get("/subgraph")
def get_subgraph_endpoint(
    seed: str = Query(..., description="Canonical id OR alias of the seed node."),
    hops: int = Query(2, ge=0, le=q.MAX_HOPS, description=f"BFS depth (max {q.MAX_HOPS})."),
    types: Optional[str] = Query(None, description="Comma-separated edge type filter."),
    include_provisional: bool = Query(False),
    include_inferred: bool = Query(False),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """N-hop BFS from `seed`. Node count is capped; `truncated=true` if hit."""
    if hops > q.MAX_HOPS:
        raise HTTPException(
            status_code=400, detail=f"hops capped at {q.MAX_HOPS}",
        )
    result = q.bfs_subgraph(
        conn, seed,
        hops=hops,
        types=_parse_types(types),
        include_provisional=include_provisional,
        include_inferred=include_inferred,
    )
    if result is None:
        raise HTTPException(status_code=404, detail=f"Seed not found: {seed!r}")
    return result


@app.get("/search")
def get_search_endpoint(
    q_: str = Query(..., alias="q", min_length=1, description="Search query."),
    limit: int = Query(10, ge=1, le=50),
    alias_index: q.AliasIndex = Depends(get_alias_index),
):
    """Resolve a name/ticker/alias to ranked canonical-node candidates."""
    return {"q": q_, "results": alias_index.search(q_, limit=limit)}


# Catch-all routes go LAST so the specific ones (e.g. .../ego) take precedence
# even though `node_id:path` is greedy.
@app.get("/node/{node_id:path}")
def get_node_endpoint(node_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    """Resolve `node_id` (canonical OR alias) -> node detail."""
    node = q.get_node(conn, node_id)
    if node is None:
        raise HTTPException(status_code=404, detail=f"Node not found: {node_id!r}")
    return node


@app.get("/edge/{edge_id:path}")
def get_edge_endpoint(edge_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    """Edge detail incl. provenance. Synthetic customer_of keys resolve to
    the underlying supplies row's provenance.
    """
    edge = q.get_edge(conn, edge_id)
    if edge is None:
        raise HTTPException(status_code=404, detail=f"Edge not found: {edge_id!r}")
    return edge


@app.post("/describe/{node_id:path}")
def post_describe(
    node_id: str,
    conn: sqlite3.Connection = Depends(get_conn),
):
    """LLM-generated 2-3 sentence description of a node aimed at
    someone who has never heard of it. Cached per-process by node_id
    so repeated inspector clicks are free.

    Picks fields off the node row to give the LLM something to ground
    on: sector / industry / country / tickers / wikidata HQ if known.
    """
    node = q.get_node(conn, node_id)
    if node is None:
        raise HTTPException(status_code=404, detail=f"Node not found: {node_id!r}")
    a = node.get("attributes", {})
    md = a.get("metadata") or {}
    wd = md.get("wikidata") or {}
    hq = wd.get("hq") if isinstance(wd, dict) else None
    try:
        description = impact_mod.describe_node(
            name=a.get("label") or node_id,
            node_type=a.get("type") or "Entity",
            sector=a.get("sector"),
            industry=a.get("industry"),
            country=a.get("country"),
            tickers=a.get("tickers") or [],
            hq=hq,
            cache_key=node_id,
        )
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    return {"node_id": node_id, "description": description}


@app.post("/impact")
def post_impact(
    payload: dict = Body(...),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """News-event impact propagation. Sends the supplied text to the
    selected LLM provider, picks a seed commodity-or-region node, then
    BFS-propagates verdicts ring-by-ring up to MAX_HOPS. Returns the
    full impact list for the frontend to tint.

    Optional `provider` in the body overrides the IMPACT_LLM_PROVIDER
    env default for this request: 'claude' (Claude Code CLI) or
    'ollama' (local Gemma). Lets the UI flip providers per-call.
    """
    text = (payload.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="`text` is required")
    provider_override = (payload.get("provider") or "").strip().lower() or None
    if provider_override and provider_override not in ("claude", "ollama"):
        raise HTTPException(
            status_code=400,
            detail=f"unknown provider {provider_override!r}; use 'claude' or 'ollama'",
        )
    try:
        return impact_mod.run_impact(text, conn=conn, provider=provider_override)
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e


@app.post("/impact/stream")
def post_impact_stream(payload: dict = Body(...)):
    """Streaming variant of /impact. Returns newline-delimited JSON (NDJSON):
    one event per line — seeds, then hop(s), then refinement, then done. The
    final `done` event's `result` is identical to /impact's response, so the
    frontend archive consumes it unchanged."""
    text = (payload.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="`text` is required")
    provider_override = (payload.get("provider") or "").strip().lower() or None
    if provider_override and provider_override not in ("claude", "ollama"):
        raise HTTPException(
            status_code=400,
            detail=f"unknown provider {provider_override!r}; use 'claude' or 'ollama'",
        )
    # Optional: ground the trace at a KNOWN node (e.g. "Sharpen with Claude" on a
    # commodity/region/regulator, which the Company-only text seed extractor can't
    # resolve). The engine injects it as a hop-0 seed after text extraction.
    seed_hint = (payload.get("seed_hint_id") or "").strip() or None
    # Optional: GKG grounding capsule appended to the seed-selection prompts.
    context = (payload.get("context") or "").strip() or None

    def gen():
        # Open the SQLite connection INSIDE the generator (not via
        # Depends(get_conn)): StreamingResponse runs gen() after this handler
        # returns, so a Depends-managed connection would already be closed.
        conn = connect(_DB_PATH)
        try:
            for ev in impact_mod.run_impact_stream(text, conn=conn, provider=provider_override,
                                                   seed_hint_id=seed_hint, context=context):
                yield json.dumps(ev, separators=(",", ":")) + "\n"
        finally:
            conn.close()

    return StreamingResponse(
        gen(),
        media_type="application/x-ndjson",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


@app.get("/usage")
def get_usage(
    granularity: str = Query("day", description="hour | day | week"),
    days: int = Query(30, description="lookback window (clamped 1..365)"),
):
    """LLM token/cost usage aggregated into time buckets for the Usage tab."""
    days = max(1, min(365, days))
    conn = connect(_DB_PATH)
    try:
        buckets = store.usage_buckets(conn, granularity, since_days=days)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        conn.close()
    return {"granularity": granularity, "days": days, "buckets": buckets}


@app.get("/news/headlines")
def get_headlines(force: bool = Query(False, description="Bypass the daily cache and re-fetch.")):
    """Return today's top-5 economic headlines, filtered by Claude for
    supply-chain / equity relevance. Results are cached per calendar day.
    Pass ?force=true to bypass the cache (e.g. manual refresh button).
    """
    try:
        return {"headlines": news_mod.get_daily_headlines(force=force)}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/impact/multi")
def post_impact_multi(payload: dict = Body(...)):
    """Multi-news impact propagation.

    Accepts a list of independent news texts. Each is propagated through
    its own isolated BFS (separate SQLite connection per thread), then
    verdicts are merged: same node appearing in multiple events gets a
    net direction (positive/negative) and a `mixed_signals` flag when
    opposing signals both fire.

    Body: {"texts": ["news 1", "news 2", ...], "provider": "claude"|"ollama"}
    """
    texts = payload.get("texts") or []
    if not isinstance(texts, list) or not texts:
        raise HTTPException(status_code=400, detail="`texts` must be a non-empty list")
    texts = [str(t).strip() for t in texts if str(t).strip()]
    if not texts:
        raise HTTPException(status_code=400, detail="all provided texts were empty")
    if len(texts) > 8:
        raise HTTPException(status_code=400, detail="max 8 news items per request")

    provider_override = (payload.get("provider") or "").strip().lower() or None
    if provider_override and provider_override not in ("claude", "ollama"):
        raise HTTPException(
            status_code=400,
            detail=f"unknown provider {provider_override!r}; use 'claude' or 'ollama'",
        )
    try:
        return impact_mod.run_multi_impact(texts, db_path=_DB_PATH, provider=provider_override)
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
