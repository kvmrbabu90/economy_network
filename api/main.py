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

import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from fastapi import Body, Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from schema.store import connect

from . import impact as impact_mod
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

# Phase 6's Vite frontend will run on a different localhost port. Without
# CORS the browser silently blocks fetches and the failure looks like a
# frontend bug; allowing localhost saves an hour of debugging.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?",
    allow_credentials=False,
    allow_methods=["GET"],
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


@app.post("/impact")
def post_impact(
    payload: dict = Body(...),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """News-event impact propagation. Sends the supplied text to the
    local LLM (Ollama / gemma4:26b by default), picks a seed
    commodity-or-region node, then BFS-propagates verdicts ring-by-ring
    up to MAX_HOPS. Returns the full impact list for the frontend to
    tint."""
    text = (payload.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="`text` is required")
    try:
        return impact_mod.run_impact(text, conn=conn)
    except RuntimeError as e:
        # Most likely: Ollama isn't running. Surface clearly.
        raise HTTPException(status_code=502, detail=str(e)) from e
