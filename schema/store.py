"""SQLite source-of-truth for nodes, edges, and aliases.

Schema is deliberately narrow: three tables only. No `customer_of` edge type,
no reverse-edge table — that view is derived at query time by reversing
`supplies` (CLAUDE.md invariant #2).

Every write goes through the Pydantic models in `schema.models`, so validation
happens at the boundary between in-memory dicts and persisted rows.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import unicodedata
from pathlib import Path
from typing import Any, Iterable, Optional, Union

from .models import Edge, Node

log = logging.getLogger(__name__)
_REPO_ROOT = Path(__file__).resolve().parent.parent


def default_db_path() -> Path:
    """The graph DB path used by every runtime process. Honors the ECONGRAPH_DB
    env var so the DB can live on a LOCAL (non-synced) path; falls back to
    <repo>/econgraph.db. The DB must NOT live in a continuously-synced folder
    (OneDrive/Dropbox) — WAL sidecars corrupt under sync."""
    env = os.environ.get("ECONGRAPH_DB")
    return Path(env) if env else (_REPO_ROOT / "econgraph.db")

# Module-level DDL — applied by init_db(). Keeping it as a single block makes
# it cheap to diff and review against the data model in docs/PRD.md §4.
DDL = """
CREATE TABLE IF NOT EXISTS nodes (
    id           TEXT PRIMARY KEY,
    type         TEXT NOT NULL,
    name         TEXT NOT NULL,
    aliases      TEXT NOT NULL DEFAULT '[]',   -- JSON array
    tickers      TEXT NOT NULL DEFAULT '[]',   -- JSON array
    identifiers  TEXT NOT NULL DEFAULT '{}',   -- JSON object
    sector       TEXT,
    industry     TEXT,
    country      TEXT,
    metadata     TEXT NOT NULL DEFAULT '{}'    -- JSON object
);

CREATE INDEX IF NOT EXISTS idx_nodes_type ON nodes(type);
CREATE INDEX IF NOT EXISTS idx_nodes_sector ON nodes(sector);

CREATE TABLE IF NOT EXISTS edges (
    id           TEXT PRIMARY KEY,
    source       TEXT NOT NULL,
    target       TEXT NOT NULL,
    type         TEXT NOT NULL,
    directed     INTEGER NOT NULL DEFAULT 1,
    confidence   REAL NOT NULL,
    weight       REAL,
    -- Provenance is required on every edge (CLAUDE.md invariant #3).
    prov_filing      TEXT NOT NULL,    -- may be empty for rule-extracted edges
    prov_url         TEXT NOT NULL,    -- may be empty for rule-extracted edges
    prov_snippet     TEXT NOT NULL,    -- never empty (CLAUDE.md invariant #4)
    prov_extracted_by TEXT NOT NULL
        CHECK (prov_extracted_by IN (
            'llm','llm:claude-cli','llm:gemma','rule','manual',
            'inference:co-mention','inference:gics-peer','wikidata',
            'manual:curation'
        )),
    -- Phase 3 may merge multiple competes_with rows onto one edge. The
    -- additional provenances (full Provenance dicts, JSON-serialized) are
    -- stashed here so SQLite stays the source of truth.
    additional_provenance TEXT NOT NULL DEFAULT '[]',
    -- Phase 5 adds this flag so the API can serve a clean "core" graph
    -- (below_threshold=0, the high-confidence above-cutoff edges) AND a
    -- separate "audit" view (below_threshold=1, mostly provisional-slug
    -- competes_with edges capped low in Phase 3). One source of truth;
    -- the include_provisional API toggle reads this column.
    below_threshold INTEGER NOT NULL DEFAULT 0,
    -- Phase E: geographic scope of this supply edge. NULL = unknown / not
    -- applicable. "US" = extracted from a 10-K (implicitly US-domestic).
    -- "global" = Wikidata / Wikipedia source (no inherent geo-scope).
    supply_geography TEXT,
    -- Phase K: source quality tier — set by pipeline/score_confidence.py.
    source_tier TEXT CHECK(source_tier IN (
        'sec_explicit','sec_inferred','manual','wikidata','wikipedia'
    )),
    FOREIGN KEY (source) REFERENCES nodes(id),
    FOREIGN KEY (target) REFERENCES nodes(id)
);

CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source);
CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target);
CREATE INDEX IF NOT EXISTS idx_edges_type ON edges(type);
CREATE INDEX IF NOT EXISTS idx_edges_below_threshold ON edges(below_threshold);
-- De-dupe key for competes_with (treat as unordered pair, see PRD §4).
CREATE UNIQUE INDEX IF NOT EXISTS uq_edges_triple
    ON edges(source, target, type);

CREATE TABLE IF NOT EXISTS aliases (
    alias              TEXT NOT NULL,
    -- Phase 5 stores the resolver-normalized form so /search can look up
    -- "procter" or "p&g" without re-implementing normalize() in SQL.
    alias_normalized   TEXT NOT NULL DEFAULT '',
    node_id            TEXT NOT NULL,
    PRIMARY KEY (alias, node_id),
    FOREIGN KEY (node_id) REFERENCES nodes(id)
);

CREATE INDEX IF NOT EXISTS idx_aliases_alias ON aliases(alias);
CREATE INDEX IF NOT EXISTS idx_aliases_normalized ON aliases(alias_normalized);

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
    status       TEXT NOT NULL DEFAULT 'queued',
    -- Date-independent story signature (seed + first-6 headline words) for
    -- cross-time / cross-source dedup; see story_signature().
    story_sig    TEXT,
    -- Compact GKG grounding capsule (other orgs + money + tone) that grounds the
    -- trace's seed selection; NULL for non-GKG events. See build_gkg_context().
    gkg_context  TEXT,
    -- JSON array of known seed node-ids (salience-ranked, seed first) for the
    -- trusted-seed trace path; NULL for free-text events that need LLM extraction.
    seed_ids     TEXT,
    -- So What? V2 · article enrichment (pipeline/enrich.py). All NULL until enriched;
    -- precompute augments gkg_context with enriched_context. Never blocks a cycle.
    enriched_context TEXT,               -- rendered ArticleCapsule (compact bracket capsule)
    enrich_status    TEXT,               -- pending|done|skipped|no_content|failed
    enrich_tier      INTEGER,            -- router decision (0/1/2/2b); NULL before the router lands
    enrich_version   INTEGER             -- bump to re-summarize from cached HTML (no re-fetch)
);
-- Per-LLM-call token/cost usage (exact, from the Claude CLI JSON envelope) for the
-- Usage tab. Written by record_llm_usage() from _claude_call; aggregated by
-- usage_buckets(). Fail-safe: recording never blocks a trace.
CREATE TABLE IF NOT EXISTS llm_usage (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                TEXT NOT NULL DEFAULT (datetime('now')),
    model             TEXT,
    input_tokens      INTEGER NOT NULL DEFAULT 0,
    output_tokens     INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd          REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_llm_usage_ts ON llm_usage(ts);
CREATE INDEX IF NOT EXISTS idx_events_status ON events(status);
-- NOTE: idx_events_story_sig is created in _migrate_story_sig (not here) — on a
-- pre-existing DB the story_sig column is added by ALTER first, so an index in this
-- DDL would fail against the not-yet-migrated table.

CREATE TABLE IF NOT EXISTS event_impacts (
    event_id   TEXT NOT NULL,
    node_id    TEXT NOT NULL,
    direction  TEXT NOT NULL,
    magnitude  REAL NOT NULL,
    hop        INTEGER NOT NULL,
    reasoning  TEXT,
    PRIMARY KEY (event_id, node_id)
);
CREATE INDEX IF NOT EXISTS idx_event_impacts_node ON event_impacts(node_id);

CREATE TABLE IF NOT EXISTS node_impact (
    node_id       TEXT PRIMARY KEY,
    direction     TEXT NOT NULL,
    magnitude     REAL NOT NULL,
    mixed_signals INTEGER NOT NULL DEFAULT 0,
    event_count   INTEGER NOT NULL,   -- window rows SCANNED (incl. unscored/no_effect, for context)
    driver_count  INTEGER NOT NULL DEFAULT 0,   -- of those, the nonzero-weighted real drivers
    top_events    TEXT NOT NULL DEFAULT '[]',
    computed_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_node_impact_direction ON node_impact(direction);
"""


PathLike = Union[str, Path]


def connect(db_path: PathLike = "econgraph.db") -> sqlite3.Connection:
    """Open a SQLite connection with sensible defaults for this project."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # Wait up to 5s for a busy writer instead of raising 'database is locked'.
    conn.execute("PRAGMA busy_timeout=5000")
    # WAL lets the API read concurrently with the hourly cycle's writes without
    # blocking, and journal_mode is persistent (effectively set-once). Safe now
    # that the DB lives on a LOCAL path (see default_db_path) — it must NOT live
    # in a continuously-synced folder. Best-effort: :memory: has no WAL.
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        pass
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create tables and indexes if they do not already exist, then run migrations."""
    conn.executescript(DDL)
    conn.commit()
    _migrate_story_sig(conn)
    _migrate_gkg_context(conn)
    _migrate_seed_ids(conn)
    _migrate_llm_usage(conn)
    _migrate_node_impact_driver_count(conn)
    _migrate_enrich_columns(conn)


# ---------------------------------------------------------------------------
# Story signature (cross-time / cross-source dedup)
# ---------------------------------------------------------------------------

_SIG_TOKENS = 8          # first-N *distinctive* tokens kept in the signature
_SIG_MIN_TOKENS = 3      # need this many distinctive tokens, else too generic → None
_SIG_NONALNUM = re.compile(r"[^a-z0-9]+")
# Function words + news/finance FRAME words carry no story identity. Stripping them
# BEFORE taking the first-N tokens is what makes the signature specific: otherwise a
# boilerplate prefix ("<co> stock price target raised to …") is identical across
# genuinely different stories and the distinguishing number/name (at word 7+) never
# enters the key — collapsing distinct items. Numbers are kept (they differentiate).
_STORY_STOPWORDS = frozenset("""
a an and or but the to of in on at for with as by from is are was were be been being
it its this that these those will would has have had after over amid into out up down
off per than then about against s not no more most
stock stocks shares share price prices target says said say report reports reported
update updates breaking exclusive news amp
""".split())


def story_signature(seed_node_id: Optional[str], headline: Optional[str]) -> Optional[str]:
    """Date-independent story key: sha1('<seed>|<first-8 distinctive headline tokens>').

    Distinctive = headline words minus stopwords/frame boilerplate, numbers kept, in
    order. Returns None when there's no seed, no headline, or fewer than
    _SIG_MIN_TOKENS distinctive tokens (too generic to collapse across a multi-day
    window — those rely on URL-exact dedup instead). The SAME function is used at
    insert, lookup, and backfill so they always agree."""
    if not seed_node_id or not headline:
        return None
    # ASCII-fold accents FIRST (Nestlé→Nestle, Telefónica→Telefonica) so different
    # sources' spellings agree and no token is truncated ('nestlé'→'nestl'); an
    # ASCII-only strip would mangle or drop the identifying foreign token.
    folded = unicodedata.normalize("NFKD", headline).encode("ascii", "ignore").decode("ascii")
    tokens = [w for w in _SIG_NONALNUM.sub(" ", folded.lower()).split()
              if w and w not in _STORY_STOPWORDS]
    if len(tokens) < _SIG_MIN_TOKENS:
        return None
    basis = f"{seed_node_id}|{' '.join(tokens[:_SIG_TOKENS])}"
    return "sig:" + hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]


def _migrate_story_sig(conn: sqlite3.Connection) -> None:
    """Add events.story_sig to a pre-existing DB and backfill it once (idempotent).

    Fresh DBs already have the column from the DDL, so this is a no-op for them.
    An older DB gets the column + index added and every existing row's signature
    computed, so a story already in the window is immediately matchable."""
    if not conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='events'").fetchone():
        return
    cols = [r[1] for r in conn.execute("PRAGMA table_info(events)").fetchall()]
    if "story_sig" not in cols:
        conn.execute("ALTER TABLE events ADD COLUMN story_sig TEXT")
        for row in conn.execute("SELECT id, seed_node_id, headline FROM events").fetchall():
            sig = story_signature(row["seed_node_id"], row["headline"])
            if sig:
                conn.execute("UPDATE events SET story_sig = ? WHERE id = ?", (sig, row["id"]))
    # Create the index unconditionally (safe now that the column is guaranteed) so
    # BOTH fresh DBs (column from DDL) and migrated ones get it.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_story_sig ON events(story_sig)")
    conn.commit()


def story_sig_seen(conn: sqlite3.Connection, sig: Optional[str], within_days: int) -> bool:
    """True if an event with this signature exists whose age (published_at, else
    ingested_at) is within `within_days` days — i.e. the story is still active in
    the impact window, so a fresh copy would double-count. A fully-faded (older)
    match is ignored so the story can legitimately re-enter."""
    if not sig:
        return False
    cutoff = f"-{int(within_days)} days"
    # Fall back to ingested_at's date when published_at is absent OR unparseable
    # (a non-ISO 'YYYYMMDD'/localized string makes SQLite date() return NULL, which
    # would silently exclude the row from the window — a fail-open dedup miss).
    row = conn.execute(
        "SELECT 1 FROM events WHERE story_sig = ? AND "
        "COALESCE(date(NULLIF(published_at, '')), date(ingested_at)) >= date('now', ?) LIMIT 1",
        (sig, cutoff),
    ).fetchone()
    return row is not None


def _migrate_gkg_context(conn: sqlite3.Connection) -> None:
    """Add events.gkg_context to a pre-existing DB (idempotent). No backfill: the
    capsule is built from raw GKG slices, which are pruned — existing events gain
    a capsule only on re-ingest. Fresh DBs already have the column from the DDL."""
    if not conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='events'"
    ).fetchone():
        return
    cols = [r[1] for r in conn.execute("PRAGMA table_info(events)").fetchall()]
    if "gkg_context" not in cols:
        conn.execute("ALTER TABLE events ADD COLUMN gkg_context TEXT")
        conn.commit()


def _migrate_enrich_columns(conn: sqlite3.Connection) -> None:
    """Add the article-enrichment columns to a pre-existing DB (idempotent, no
    backfill — events gain enrichment only when pipeline/enrich.py runs). Forward-only."""
    if not conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='events'"
    ).fetchone():
        return
    cols = {r[1] for r in conn.execute("PRAGMA table_info(events)").fetchall()}
    for name, decl in (("enriched_context", "TEXT"), ("enrich_status", "TEXT"),
                       ("enrich_tier", "INTEGER"), ("enrich_version", "INTEGER")):
        if name not in cols:
            conn.execute(f"ALTER TABLE events ADD COLUMN {name} {decl}")
    conn.commit()


def set_event_enrichment(conn: sqlite3.Connection, event_id: str, *,
                         enriched_context: Optional[str] = None, status: str = "done",
                         tier: Optional[int] = None, version: Optional[int] = None) -> None:
    """Persist the article-enrichment result for one event. `enriched_context` is the
    rendered capsule (or None when nothing usable); `status` records the outcome."""
    conn.execute(
        "UPDATE events SET enriched_context = ?, enrich_status = ?, "
        "enrich_tier = COALESCE(?, enrich_tier), enrich_version = COALESCE(?, enrich_version) "
        "WHERE id = ?",
        (enriched_context, status, tier, version, event_id),
    )
    conn.commit()


def _migrate_seed_ids(conn: sqlite3.Connection) -> None:
    """Add events.seed_ids to a pre-existing DB (idempotent). Fresh DBs already have
    the column from the DDL. Forward-only — existing events get it on re-ingest."""
    if not conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='events'"
    ).fetchone():
        return
    cols = [r[1] for r in conn.execute("PRAGMA table_info(events)").fetchall()]
    if "seed_ids" not in cols:
        conn.execute("ALTER TABLE events ADD COLUMN seed_ids TEXT")
        conn.commit()


def _migrate_node_impact_driver_count(conn: sqlite3.Connection) -> None:
    """Add node_impact.driver_count to a pre-existing DB (idempotent). Existing rows
    default to 0 until the next aggregate cycle recomputes them. Forward-only."""
    if not conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='node_impact'"
    ).fetchone():
        return
    cols = [r[1] for r in conn.execute("PRAGMA table_info(node_impact)").fetchall()]
    if "driver_count" not in cols:
        conn.execute("ALTER TABLE node_impact ADD COLUMN driver_count INTEGER NOT NULL DEFAULT 0")
        conn.commit()


def _migrate_llm_usage(conn: sqlite3.Connection) -> None:
    """Create the llm_usage table on a pre-existing DB (idempotent)."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS llm_usage (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            ts                TEXT NOT NULL DEFAULT (datetime('now')),
            model             TEXT,
            input_tokens      INTEGER NOT NULL DEFAULT 0,
            output_tokens     INTEGER NOT NULL DEFAULT 0,
            cache_read_tokens INTEGER NOT NULL DEFAULT 0,
            cost_usd          REAL NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_llm_usage_ts ON llm_usage(ts);
        """
    )
    conn.commit()


def record_llm_usage(usage: dict[str, Any], db_path: Optional[Union[str, Path]] = None) -> None:
    """Insert one LLM-call usage row. Opens its own short-lived connection (default
    the runtime DB) so it can be called from _claude_call which holds no connection.
    FULLY FAIL-SAFE: any error is swallowed — usage recording must NEVER break a
    trace. `usage` keys: input_tokens, output_tokens, cache_read_tokens, cost_usd, model."""
    try:
        conn = connect(db_path or default_db_path())
        try:
            conn.execute(
                "INSERT INTO llm_usage (model, input_tokens, output_tokens, cache_read_tokens, cost_usd) "
                "VALUES (?,?,?,?,?)",
                (usage.get("model"), int(usage.get("input_tokens") or 0),
                 int(usage.get("output_tokens") or 0), int(usage.get("cache_read_tokens") or 0),
                 float(usage.get("cost_usd") or 0.0)))
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:                       # best-effort metric — never propagate
        log.debug("record_llm_usage skipped: %s", exc)


_USAGE_FMT = {"hour": "%Y-%m-%dT%H:00", "day": "%Y-%m-%d", "week": "%Y-W%W"}


def usage_buckets(conn: sqlite3.Connection, granularity: str, since_days: int = 30) -> list[dict[str, Any]]:
    """Aggregate llm_usage into time buckets. `granularity` ∈ {hour, day, week}.
    Returns per bucket (ascending): {bucket, input_tokens, output_tokens,
    cache_read_tokens, cost_usd, calls}."""
    fmt = _USAGE_FMT.get(granularity)
    if fmt is None:
        raise ValueError(f"granularity must be one of {sorted(_USAGE_FMT)}")
    # fmt comes from a fixed dict (not user input) → safe to interpolate; since_days is bound.
    rows = conn.execute(
        f"""
        SELECT strftime('{fmt}', ts) AS bucket,
               SUM(input_tokens)      AS input_tokens,
               SUM(output_tokens)     AS output_tokens,
               SUM(cache_read_tokens) AS cache_read_tokens,
               SUM(cost_usd)          AS cost_usd,
               COUNT(*)               AS calls
        FROM llm_usage
        WHERE ts >= datetime('now', ?)
        GROUP BY bucket
        ORDER BY bucket ASC
        """,
        (f"-{int(since_days)} days",),
    ).fetchall()
    return [dict(r) for r in rows]


def prune_llm_usage(conn: sqlite3.Connection, older_than_days: int = 180) -> int:
    """Delete usage rows older than the window. Returns rows deleted."""
    cur = conn.execute("DELETE FROM llm_usage WHERE ts < datetime('now', ?)",
                       (f"-{int(older_than_days)} days",))
    conn.commit()
    return cur.rowcount


_MISSING = object()   # distinguish "story_sig explicitly None" from "not provided"


def insert_event(conn: sqlite3.Connection, ev: dict[str, Any]) -> None:
    """Insert one event; INSERT OR IGNORE so a re-seen id is a no-op (idempotent)."""
    # story_sig: use what ingest computed (None for _no_collapse rows), else derive.
    sig = ev.get("story_sig", _MISSING)
    if sig is _MISSING:
        sig = story_signature(ev.get("seed_node_id"), ev.get("headline"))
    conn.execute(
        """
        INSERT OR IGNORE INTO events
          (id, headline, source, url, category, published_at, seed_entity, seed_node_id, status, story_sig, gkg_context, seed_ids)
        VALUES (:id, :headline, :source, :url, :category, :published_at,
                :seed_entity, :seed_node_id, :status, :story_sig, :gkg_context, :seed_ids)
        """,
        {
            "id": ev["id"], "headline": ev["headline"], "source": ev.get("source"),
            "url": ev.get("url"), "category": ev.get("category"),
            "published_at": ev.get("published_at"), "seed_entity": ev.get("seed_entity"),
            "seed_node_id": ev.get("seed_node_id"), "status": ev.get("status", "queued"),
            "story_sig": sig, "gkg_context": ev.get("gkg_context"),
            "seed_ids": ev.get("seed_ids"),
        },
    )
    conn.commit()


def event_exists(conn: sqlite3.Connection, event_id: str) -> bool:
    return conn.execute("SELECT 1 FROM events WHERE id = ? LIMIT 1", (event_id,)).fetchone() is not None


def queued_events(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM events WHERE status = 'queued' ORDER BY ingested_at").fetchall()
    return [dict(r) for r in rows]


def write_event_impacts(conn: sqlite3.Connection, event_id: str, impacts: list[dict[str, Any]]) -> None:
    """Replace all impact rows for an event (delete-then-insert, so a re-trace is clean)."""
    conn.execute("DELETE FROM event_impacts WHERE event_id = ?", (event_id,))
    conn.executemany(
        "INSERT INTO event_impacts (event_id, node_id, direction, magnitude, hop, reasoning) "
        "VALUES (?,?,?,?,?,?)",
        # magnitude is a NON-NEGATIVE weight in [0,1]; the sign lives in `direction`.
        # Store abs() so a tracer that emits a signed negative magnitude (e.g. -0.30
        # for a 'negative' row) doesn't get silently zeroed by the aggregate's
        # [0,1] clamp — which dropped the driver and could flip the node's tint.
        [(event_id, v["node_id"], v.get("direction", "no_effect"),
          min(1.0, abs(float(v.get("magnitude") or 0.0))), int(v.get("hop") or 0), v.get("reasoning"))
         for v in impacts],
    )
    conn.commit()


def set_event_status(conn: sqlite3.Connection, event_id: str, status: str) -> None:
    conn.execute("UPDATE events SET status = ? WHERE id = ?", (status, event_id))
    conn.commit()


def prune_old_events(conn: sqlite3.Connection, older_than_days: int = 30) -> int:
    """Delete events (and their event_impacts) older than the horizon.

    "Age" is COALESCE(published_at, ingested_at) — an event with no publish
    date falls back to when we ingested it. Both the event rows and their
    dependent event_impacts are removed in one transaction so the unattended
    hourly run doesn't grow the DB unbounded. Returns the number of events
    deleted."""
    cutoff = f"-{int(older_than_days)} days"
    conn.execute(
        """
        DELETE FROM event_impacts WHERE event_id IN (
            SELECT id FROM events
            WHERE date(COALESCE(published_at, ingested_at)) < date('now', ?)
        )
        """,
        (cutoff,),
    )
    cur = conn.execute(
        "DELETE FROM events "
        "WHERE date(COALESCE(published_at, ingested_at)) < date('now', ?)",
        (cutoff,),
    )
    deleted = cur.rowcount
    conn.commit()
    return deleted


def replace_node_impact(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> None:
    """Atomically swap the derived node_impact cache: wipe, then bulk-insert `rows`."""
    conn.execute("DELETE FROM node_impact")
    conn.executemany(
        "INSERT INTO node_impact (node_id, direction, magnitude, mixed_signals, "
        "event_count, driver_count, top_events, computed_at) VALUES (?,?,?,?,?,?,?,?)",
        [(r["node_id"], r["direction"], float(r["magnitude"]), int(r.get("mixed_signals", 0)),
          int(r["event_count"]), int(r.get("driver_count", 0)), r.get("top_events", "[]"),
          r["computed_at"]) for r in rows],
    )
    conn.commit()


def _is_missing_table(exc: sqlite3.OperationalError) -> bool:
    """True only for a genuinely-absent table (DB built before P4). A lock/busy
    error is NOT this — it must propagate so the API can surface a retryable 503
    instead of laundering a transient collision into an empty/None result."""
    return "no such table" in str(exc).lower()


def read_all_node_impact(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Compact node_impact rows for graph tinting, ordered by node_id. Returns []
    if the table doesn't exist (DB built before P4 / no cycle has run yet); any
    other OperationalError (notably 'database is locked') is re-raised.

    Inert (`direction = 'no_effect'`) rows are excluded: they never tint the
    graph and only bloat the /impact/live payload. A directly-queried
    no_effect node still resolves via read_node_impact().

    Region rows are ALSO excluded from the map payload: a market bucket
    (region:us-consumer, …) is a sink every consumer company points at, so it
    accumulates dozens of hop-1/2 verdicts and saturates to magnitude ~1.0 — the
    brightest, least-informative nodes on the map. Suppressing propagation THROUGH
    a region (IMPACT_SUPPRESS_REGION_EXPANSION) never stopped the region node
    itself from being scored, so filter it out at the tint boundary. A directly
    clicked region still shows its verdict via read_node_impact() (unfiltered)."""
    try:
        rows = conn.execute(
            "SELECT ni.node_id, ni.direction, ni.magnitude, ni.mixed_signals, ni.event_count "
            "FROM node_impact ni LEFT JOIN nodes n ON n.id = ni.node_id "
            "WHERE ni.direction != 'no_effect' AND COALESCE(n.type, '') != 'Region' "
            "ORDER BY ni.node_id"
        ).fetchall()
    except sqlite3.OperationalError as exc:
        if _is_missing_table(exc):
            return []
        raise
    return [dict(r) for r in rows]


def read_node_impact(conn: sqlite3.Connection, node_id: str) -> Optional[dict[str, Any]]:
    """Full node_impact row (top_events remains a JSON string), or None when the
    node has no row or the table doesn't exist yet. A lock error is re-raised."""
    try:
        row = conn.execute(
            "SELECT node_id, direction, magnitude, mixed_signals, event_count, "
            "driver_count, top_events, computed_at FROM node_impact WHERE node_id = ?", (node_id,)
        ).fetchone()
    except sqlite3.OperationalError as exc:
        if _is_missing_table(exc):
            return None
        raise
    return dict(row) if row else None


def latest_node_impact_computed_at(conn: sqlite3.Connection) -> Optional[str]:
    """MAX(computed_at) across node_impact, or None when empty / table absent.
    A lock error is re-raised (not swallowed to None)."""
    try:
        row = conn.execute("SELECT MAX(computed_at) AS c FROM node_impact").fetchone()
    except sqlite3.OperationalError as exc:
        if _is_missing_table(exc):
            return None
        raise
    return row["c"] if row and row["c"] is not None else None


def upsert_node(conn: sqlite3.Connection, node: Union[Node, dict[str, Any]]) -> Node:
    """Validate via Pydantic then upsert into `nodes`. Returns the validated Node."""
    n = node if isinstance(node, Node) else Node.model_validate(node)
    conn.execute(
        """
        INSERT INTO nodes (id, type, name, aliases, tickers, identifiers,
                           sector, industry, country, metadata)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            type=excluded.type,
            name=excluded.name,
            aliases=excluded.aliases,
            tickers=excluded.tickers,
            identifiers=excluded.identifiers,
            sector=excluded.sector,
            industry=excluded.industry,
            country=excluded.country,
            metadata=excluded.metadata
        """,
        (
            n.id,
            n.type if isinstance(n.type, str) else n.type.value,
            n.name,
            json.dumps(n.aliases),
            json.dumps(n.tickers),
            json.dumps(n.identifiers),
            n.sector,
            n.industry,
            n.country,
            json.dumps(n.metadata),
        ),
    )
    conn.commit()
    return n


def upsert_edge(
    conn: sqlite3.Connection,
    edge: Union[Edge, dict[str, Any]],
    *,
    below_threshold: bool = False,
) -> Edge:
    """Validate via Pydantic then upsert into `edges`. Returns the validated Edge.

    `below_threshold=True` flags the row as audit-layer (Phase 5 §A1). The
    column has DEFAULT 0 so existing callers stay correct without changes.
    Invariant: never insert a row whose `type == "customer_of"` -- that
    relationship is derived on read (CLAUDE.md invariant #2).
    """
    e = edge if isinstance(edge, Edge) else Edge.model_validate(edge)
    edge_type = e.type if isinstance(e.type, str) else e.type.value
    if edge_type == "customer_of":
        raise ValueError(
            "Refusing to store an Edge with type='customer_of'. The customer "
            "view is derived from `supplies` at query time (invariant #2)."
        )
    extra_prov = [p.model_dump() if hasattr(p, "model_dump") else p for p in e.additional_provenance]
    conn.execute(
        """
        INSERT INTO edges (id, source, target, type, directed, confidence, weight,
                           prov_filing, prov_url, prov_snippet, prov_extracted_by,
                           additional_provenance, below_threshold, supply_geography,
                           source_tier)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            source=excluded.source,
            target=excluded.target,
            type=excluded.type,
            directed=excluded.directed,
            confidence=excluded.confidence,
            weight=excluded.weight,
            prov_filing=excluded.prov_filing,
            prov_url=excluded.prov_url,
            prov_snippet=excluded.prov_snippet,
            prov_extracted_by=excluded.prov_extracted_by,
            additional_provenance=excluded.additional_provenance,
            below_threshold=excluded.below_threshold,
            supply_geography=excluded.supply_geography,
            source_tier=excluded.source_tier
        """,
        (
            e.id,
            e.source,
            e.target,
            edge_type,
            1 if e.directed else 0,
            e.confidence,
            e.weight,
            e.provenance.filing,
            e.provenance.url,
            e.provenance.snippet,
            e.provenance.extracted_by,
            json.dumps(extra_prov),
            1 if below_threshold else 0,
            e.supply_geography,
            e.source_tier,
        ),
    )
    conn.commit()
    return e


def get_node(conn: sqlite3.Connection, node_id: str) -> Optional[Node]:
    row = conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
    if row is None:
        return None
    return Node.model_validate(
        {
            "id": row["id"],
            "type": row["type"],
            "name": row["name"],
            "aliases": json.loads(row["aliases"]),
            "tickers": json.loads(row["tickers"]),
            "identifiers": json.loads(row["identifiers"]),
            "sector": row["sector"],
            "industry": row["industry"],
            "country": row["country"],
            "metadata": json.loads(row["metadata"]),
        }
    )


def get_edge(conn: sqlite3.Connection, edge_id: str) -> Optional[Edge]:
    row = conn.execute("SELECT * FROM edges WHERE id = ?", (edge_id,)).fetchone()
    if row is None:
        return None
    try:
        extra = json.loads(row["additional_provenance"]) if row["additional_provenance"] else []
    except (KeyError, IndexError, json.JSONDecodeError):
        extra = []
    return Edge.model_validate(
        {
            "id": row["id"],
            "source": row["source"],
            "target": row["target"],
            "type": row["type"],
            "directed": bool(row["directed"]),
            "confidence": row["confidence"],
            "weight": row["weight"],
            "provenance": {
                "filing": row["prov_filing"],
                "url": row["prov_url"],
                "snippet": row["prov_snippet"],
                "extracted_by": row["prov_extracted_by"],
            },
            "additional_provenance": extra,
            "supply_geography": row["supply_geography"] if "supply_geography" in row.keys() else None,
            "source_tier": row["source_tier"] if "source_tier" in row.keys() else None,
        }
    )


def add_aliases(conn: sqlite3.Connection, node_id: str, aliases: Iterable[str]) -> None:
    """Map raw alias strings -> canonical node id. Idempotent.

    The normalized column gets a best-effort lowercase fallback so this older
    entrypoint (used by Phase 0 fixtures) still works. Phase 3+ callers
    should prefer `add_alias_rows()` which carries the resolver's
    deterministic normalize() output.
    """
    rows = [(a, a.lower().strip(), node_id) for a in aliases if a]
    if not rows:
        return
    conn.executemany(
        "INSERT OR IGNORE INTO aliases (alias, alias_normalized, node_id) VALUES (?, ?, ?)",
        rows,
    )
    conn.commit()


def add_alias_rows(conn: sqlite3.Connection, rows: Iterable[dict]) -> int:
    """Bulk-load aliases.jsonl rows produced by `pipeline.resolve`.

    Each row must carry: alias, alias_normalized, canonical_id.
    Idempotent on (alias, node_id). Returns rows inserted (incl. ignored).
    """
    tuples = [
        (r["alias"], r.get("alias_normalized") or r["alias"].lower().strip(), r["canonical_id"])
        for r in rows
        if r.get("alias") and r.get("canonical_id")
    ]
    if not tuples:
        return 0
    conn.executemany(
        "INSERT OR IGNORE INTO aliases (alias, alias_normalized, node_id) VALUES (?, ?, ?)",
        tuples,
    )
    conn.commit()
    return len(tuples)
