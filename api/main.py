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
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from fastapi import Body, Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from schema.store import connect

from . import impact as impact_mod
from . import news as news_mod
from . import query as q


log = logging.getLogger("api.main")

# Module-level config -- replaced by tests via the dependency override hook.
_DB_PATH = Path("econgraph.db")
_ALIAS_INDEX: Optional[q.AliasIndex] = None


def set_db_path(path: Path) -> None:
    """Test/CLI hook to point the app at a different SQLite file."""
    global _DB_PATH, _ALIAS_INDEX
    _DB_PATH = Path(path)
    _ALIAS_INDEX = None  # rebuilt lazily on next /search call


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
    """Counts + the customer_of-rows invariant check (must be 0)."""
    return q.health(conn)


def _parse_types(types: Optional[str]) -> Optional[list[str]]:
    """`types` is a comma-separated string; None means "all"."""
    if not types:
        return None
    return [t.strip() for t in types.split(",") if t.strip()]


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

    def gen():
        # Open the SQLite connection INSIDE the generator (not via
        # Depends(get_conn)): StreamingResponse runs gen() after this handler
        # returns, so a Depends-managed connection would already be closed.
        conn = connect(_DB_PATH)
        try:
            for ev in impact_mod.run_impact_stream(text, conn=conn, provider=provider_override):
                yield json.dumps(ev, separators=(",", ":")) + "\n"
        finally:
            conn.close()

    return StreamingResponse(
        gen(),
        media_type="application/x-ndjson",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


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
