"""News / hypothetical-event impact propagation.

A user submits a short news description (e.g. "a new pest infection
is destroying sugarcane crops worldwide"). We:

  1. Ask Gemma (local Ollama) to pick the single best-matching seed
     node from the catalogue of Commodity + Region nodes, plus a
     direction (positive | negative) and a 1-2 sentence rationale.

  2. BFS outward up to MAX_HOPS. At each ring boundary we batch every
     newly-discovered neighbour and ask Gemma to score the WHOLE ring
     at once with one prompt -- so the model sees the seed, the
     parent verdicts, and the candidate list together and can reason
     about substitutes / second-order effects rather than mechanically
     flipping signs along edges.

  3. Return the full propagation tree with per-node {direction,
     magnitude, hop, reasoning}. The frontend renders this as a
     red/green tint that fades with hop distance.

Stops on: max hop count, ring-empty (no new neighbours), or an LLM
ring response that classifies every candidate as "no_effect".
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Optional

import urllib.request
import urllib.error

log = logging.getLogger(__name__)

# LLM provider switch. "claude" routes through the local Claude Code CLI
# (`claude -p ... --output-format json`) so it bills against the user's
# Max plan instead of API credits. "ollama" keeps the local Gemma 4
# fallback for offline use.
LLM_PROVIDER = os.environ.get("IMPACT_LLM_PROVIDER", "claude").lower()

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434/api/generate")
OLLAMA_MODEL = os.environ.get("ECONGRAPH_LLM_MODEL", "gemma4:26b")
MAX_HOPS = int(os.environ.get("IMPACT_MAX_HOPS", "3"))
# Per-ring cap. Claude handles 24 candidates comfortably in one call
# (same prompt shape, just a longer JSON array); bumped from 16 to cut
# chunk count by ~33% on large hops.
MAX_RING_CANDIDATES = int(os.environ.get("IMPACT_MAX_RING", "24"))
LLM_TIMEOUT_SECONDS = int(os.environ.get("IMPACT_LLM_TIMEOUT", "600"))
# How many ring chunks / refinement batches to score in parallel. Each
# is an independent Claude CLI subprocess (~200-400 MB RSS). 8 cuts one
# serial round off large hops vs the old 6 without saturating a typical
# 16 GB laptop.
RING_PARALLELISM = int(os.environ.get("IMPACT_RING_PARALLELISM", "8"))
# How many nodes to pack into a single refinement LLM call. Old code did
# 1 per call (60 calls → 10 serial rounds at P=8). Batching 6 collapses
# that to 10 calls → 2 rounds -- ~5x faster with identical quality since
# Claude already multi-scores ring chunks of 24.
REFINEMENT_BATCH_SIZE = int(os.environ.get("IMPACT_REFINE_BATCH", "6"))


# ---------------------------------------------------------------------------
# Ollama client
# ---------------------------------------------------------------------------

_CLAUDE_BIN_CACHE: Optional[str] = None


def _resolve_claude_binary() -> str:
    """Find the Claude Code CLI. Same search order pipeline.extractor uses.
    Cached after first lookup."""
    global _CLAUDE_BIN_CACHE
    if _CLAUDE_BIN_CACHE:
        return _CLAUDE_BIN_CACHE
    candidates = [
        os.environ.get("CLAUDE_CLI"),
        str(Path.home() / ".local" / "bin" / "claude.exe"),
        shutil.which("claude.exe"),
        shutil.which("claude"),
    ]
    for c in candidates:
        if c and Path(c).exists():
            _CLAUDE_BIN_CACHE = c
            return c
    raise RuntimeError(
        "Could not find the `claude` CLI. Install via "
        "`irm https://claude.ai/install.ps1 | iex` and run `claude login`, "
        "then set CLAUDE_CLI to its full path or put it on PATH."
    )


def _claude_call(prompt: str) -> str:
    """Single Claude Code CLI call. Returns the model's text (the
    `result` field of the JSON envelope). Empty string on error -- the
    caller's tolerant JSON parser handles it the same way it handles
    Ollama failures."""
    binary = _resolve_claude_binary()
    cmd = [binary, "-p", prompt, "--output-format", "json"]
    t0 = time.time()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=LLM_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        log.warning("claude CLI timeout after %ds", LLM_TIMEOUT_SECONDS)
        return ""
    log.info("claude CLI call (%.1fs, %d bytes prompt, exit=%d)",
             time.time() - t0, len(prompt), proc.returncode)
    if proc.returncode != 0:
        log.warning("claude CLI non-zero exit: %s", (proc.stderr or "")[:300])
        return ""
    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        log.warning("claude CLI envelope parse failed: %s; head=%s",
                    exc, (proc.stdout or "")[:300])
        return ""
    if envelope.get("is_error"):
        log.warning("claude CLI is_error=true: %s", envelope.get("result", ""))
        return ""
    return envelope.get("result", "") or ""


def _llm_call(prompt: str, *, fmt_json: bool = False) -> str:
    """Dispatch to the configured LLM provider. Returns the raw text the
    provider emitted; callers run _parse_llm_json on it."""
    if LLM_PROVIDER == "claude":
        return _claude_call(prompt)
    if LLM_PROVIDER == "ollama":
        return _ollama_call(prompt, fmt_json=fmt_json)
    raise RuntimeError(
        f"Unknown IMPACT_LLM_PROVIDER={LLM_PROVIDER!r}; use 'claude' or 'ollama'."
    )


def _ollama_call(prompt: str, *, fmt_json: bool = False) -> str:
    """Single non-streaming call to Ollama. Returns the raw `response`
    string. We DO NOT set Ollama's format=json by default because Gemma
    4 leaks its `<channel|>` reasoning tokens when forced into json mode;
    instead we instruct it in the prompt to emit JSON only and run a
    tolerant parser on the response."""
    payload: dict[str, Any] = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        # Gemma 4 / Gemma 3.5 has reasoning mode on by default. With it
        # enabled, Ollama drops the thinking-channel tokens before
        # returning, so we see done_reason="length" + empty response
        # even after eval_count=200+. Force it off so the model
        # generates directly into the visible output channel.
        "think": False,
        "options": {
            "temperature": 0.1,
            "num_predict": 4000,
        },
    }
    if fmt_json:
        payload["format"] = "json"
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        OLLAMA_URL,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=LLM_TIMEOUT_SECONDS) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Ollama call failed: {e}") from e
    log.info("ollama call (%.1fs, %d bytes prompt)", time.time() - t0, len(prompt))
    data = json.loads(raw)
    return data.get("response", "")


def _parse_llm_json(text: str) -> Any:
    """Tolerant JSON parse. Gemma 4 sometimes emits internal special
    tokens (``<channel|>``, ``<|...|>``) and chain-of-thought prose
    before the JSON we asked for. Strip both, then scan for the first
    balanced { } or [ ] block."""
    text = (text or "").strip()
    if not text:
        return None
    # Strip Gemma-style special tokens like <channel|>, <|something|>, etc.
    text = re.sub(r"<\|?[a-zA-Z_]+\|?>", "", text)
    # Strip markdown fences if present.
    fence = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    # Try a clean parse first.
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Walk the string looking for the first { or [ that opens a
    # balanced block. Naive but tolerant of prose preamble / postscript.
    for opener, closer in (("[", "]"), ("{", "}")):
        i = text.find(opener)
        if i < 0:
            continue
        depth = 0
        for j in range(i, len(text)):
            c = text[j]
            if c == opener:
                depth += 1
            elif c == closer:
                depth -= 1
                if depth == 0:
                    candidate = text[i:j + 1]
                    try:
                        return json.loads(candidate)
                    except json.JSONDecodeError:
                        break
    return None


# ---------------------------------------------------------------------------
# Graph helpers (read against the live SQLite)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Multi-seed: named-entity extraction + graph resolution
# ---------------------------------------------------------------------------

_ENTITY_EXTRACT_PROMPT_TEMPLATE = """You are an economist analysing a news event.
Extract ONLY the companies or organizations that are DIRECTLY AND SPECIFICALLY NAMED
in the news text. Do not infer or guess companies; only include ones explicitly mentioned.

For each named company/organization, determine whether this event is economically
POSITIVE or NEGATIVE for that specific entity.

NEWS:
\"\"\"{news}\"\"\"

DIRECTION SEMANTICS:
  "positive" = the event DIRECTLY HELPS this entity (new market entry, major contract,
               regulatory approval, strong earnings, demand surge for its products)
  "negative" = the event DIRECTLY HURTS this entity (lawsuit, recall, market exit,
               competitor win, supply disruption affecting it specifically)

Return STRICT JSON only — an array. If no specific companies/organizations are named, return [].

[
  {{
    "company_name": "<exact name as stated in news>",
    "direction": "positive" | "negative",
    "magnitude": 0.0 to 1.0,
    "reasoning": "<under 15 words — why positive/negative for THIS company>"
  }}
]
"""


def _extract_named_entities(text: str) -> list[dict[str, Any]]:
    """Ask LLM to identify named companies in the news + their impact direction.
    Returns a list of dicts: company_name, direction, magnitude, reasoning.
    Returns [] if no companies found or LLM fails."""
    prompt = _ENTITY_EXTRACT_PROMPT_TEMPLATE.format(news=text)
    raw = _llm_call(prompt)
    parsed = _parse_llm_json(raw)
    if not isinstance(parsed, list):
        return []
    result: list[dict[str, Any]] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        name = (item.get("company_name") or "").strip()
        direction = item.get("direction", "")
        if name and direction in ("positive", "negative"):
            result.append({
                "company_name": name,
                "direction": direction,
                "magnitude": max(0.0, min(1.0, float(item.get("magnitude") or 0.7))),
                "reasoning": (item.get("reasoning") or "")[:200],
            })
    return result


def _resolve_entity(
    conn: sqlite3.Connection, company_name: str
) -> Optional[dict[str, Any]]:
    """Find a Company node whose name or alias best matches company_name.

    Search order (short-circuits on first hit):
    1. Exact name match on nodes table (case-insensitive)
    2. Exact alias_normalized match in aliases table
    3. name LIKE 'name%' (starts-with)
    4. alias_normalized LIKE 'name%'
    5. name LIKE '%name%' (contains, fallback)

    Returns a dict with id, type, name, sector, industry, country — or None.
    """
    q = company_name.lower().strip()
    if not q:
        return None

    # 1. Exact node name
    row = conn.execute(
        "SELECT id, type, name, sector, industry, country FROM nodes "
        "WHERE LOWER(name) = ? AND type = 'Company' LIMIT 1",
        (q,),
    ).fetchone()
    if row:
        return dict(row)

    # 2. Exact alias
    row = conn.execute(
        """
        SELECT n.id, n.type, n.name, n.sector, n.industry, n.country
        FROM aliases a JOIN nodes n ON n.id = a.node_id
        WHERE a.alias_normalized = ? AND n.type = 'Company'
        LIMIT 1
        """,
        (q,),
    ).fetchone()
    if row:
        return dict(row)

    # 3. Name starts-with ("Tata" → "Tata Consultancy Services")
    row = conn.execute(
        "SELECT id, type, name, sector, industry, country FROM nodes "
        "WHERE LOWER(name) LIKE ? AND type = 'Company' LIMIT 1",
        (q + "%",),
    ).fetchone()
    if row:
        return dict(row)

    # 4. Alias starts-with
    row = conn.execute(
        """
        SELECT n.id, n.type, n.name, n.sector, n.industry, n.country
        FROM aliases a JOIN nodes n ON n.id = a.node_id
        WHERE a.alias_normalized LIKE ? AND n.type = 'Company'
        LIMIT 1
        """,
        (q + "%",),
    ).fetchone()
    if row:
        return dict(row)

    # 5. Contains fallback (only for reasonably long names to avoid false positives)
    if len(q) >= 5:
        row = conn.execute(
            "SELECT id, type, name, sector, industry, country FROM nodes "
            "WHERE LOWER(name) LIKE ? AND type = 'Company' LIMIT 1",
            (f"%{q}%",),
        ).fetchone()
        if row:
            return dict(row)

    return None


def _list_seed_candidates(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Commodity + Region nodes are the typical seed targets for a
    news event. We include their type, name, and a one-liner category
    so the LLM has enough to discriminate."""
    rows = conn.execute(
        """
        SELECT id, type, name, metadata
        FROM nodes
        WHERE type IN ('Commodity', 'Region')
        ORDER BY type, name
        """
    ).fetchall()
    out = []
    for r in rows:
        try:
            md = json.loads(r["metadata"]) if r["metadata"] else {}
        except Exception:
            md = {}
        category = md.get("category") if isinstance(md, dict) else None
        top_producer = md.get("top_producer") if isinstance(md, dict) else None
        out.append({
            "id": r["id"],
            "type": r["type"],
            "name": r["name"],
            "category": category,
            "top_producer": top_producer,
        })
    return out


def _node_summary(conn: sqlite3.Connection, node_id: str) -> Optional[dict[str, Any]]:
    row = conn.execute(
        "SELECT id, type, name, sector, industry, country, metadata FROM nodes WHERE id = ?",
        (node_id,),
    ).fetchone()
    if not row:
        return None
    return {
        "id": row["id"],
        "type": row["type"],
        "name": row["name"],
        "sector": row["sector"],
        "industry": row["industry"],
        "country": row["country"],  # Phase E: needed for geography filter
    }


def _neighbors(conn: sqlite3.Connection, node_ids: list[str], visited: set[str]) -> list[dict[str, Any]]:
    """Return new neighbour summaries reachable from any of the given
    parent nodes via supplies / customer_of (derived) / competes_with /
    regulated_by edges. Skips below_threshold edges so we don't chase
    the audit layer through impact propagation."""
    if not node_ids:
        return []
    placeholder = ",".join("?" for _ in node_ids)
    rows = conn.execute(
        f"""
        SELECT DISTINCT source AS parent, target AS child, type AS edge_type,
               supply_geography
        FROM edges
        WHERE source IN ({placeholder}) AND below_threshold = 0
        UNION
        SELECT DISTINCT target AS parent, source AS child, type AS edge_type,
               supply_geography
        FROM edges
        WHERE target IN ({placeholder}) AND below_threshold = 0
        """,
        node_ids + node_ids,
    ).fetchall()
    out: list[dict[str, Any]] = []
    seen_local: set[str] = set()
    for r in rows:
        cid = r["child"]
        if cid in visited or cid in seen_local:
            continue
        seen_local.add(cid)
        summary = _node_summary(conn, cid)
        if not summary:
            continue
        summary["via_parent"] = r["parent"]
        summary["edge_type"] = r["edge_type"]
        # Phase E: supply_geography is nullable; may not exist on old DBs
        try:
            summary["supply_geography"] = r["supply_geography"]
        except IndexError:
            summary["supply_geography"] = None
        out.append(summary)
    return out


# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------

_SEED_PROMPT_TEMPLATE = """You are an economist analysing a news event. Pick the ONE node from the
list below that this event hits MOST DIRECTLY, and decide whether the event
HELPS or HURTS that node's economic position.

DIRECTION SEMANTICS (read carefully):
  "negative" = the event HURTS this node. Use for: supply shortage,
               disrupted production, lost demand, regulatory crackdown,
               input cost spike, contamination, ban.
  "positive" = the event HELPS this node. Use for: demand surge,
               favourable subsidy, competitor failure that this node
               benefits from, supply abundance that lowers this node's
               own input cost.

A pest destroying sugarcane = NEGATIVE for sugar (supply is being
destroyed, the value chain is disrupted). It would be positive only
for a SUBSTITUTE that benefits from sugar's loss.

NEWS:
\"\"\"
{news}
\"\"\"

CANDIDATE NODES (id | type | name | category):
{candidates}

Respond with STRICT JSON, nothing else. Keep reasoning under 25 words:
{{"node_id": "<one id from the list, or null>", "direction": "positive" or "negative", "magnitude": 0.0 to 1.0, "reasoning": "<one short clause>"}}
"""


_RING_PROMPT_TEMPLATE = """You are propagating a news shock through a value-chain graph.

NEWS:
\"\"\"
{news}
\"\"\"

SEEDS (hop 0 — directly affected by this event):
{seeds_block}

You are now at hop {hop_num}. Each candidate is connected to a parent
already classified. Score each candidate's likely impact GIVEN the news.

DIRECTION SEMANTICS:
  "negative" = this candidate is HURT (higher input cost, lost supply,
               reduced demand for its product, regulatory pain).
  "positive" = this candidate is HELPED (substitute benefits from rival's
               loss, demand shifts to it, sells more of an affected input).
  "no_effect" = the shock does not meaningfully reach this node.

GEOGRAPHY RULE (Phase E — read carefully):
  The "country" column is the company's HQ country (ISO-2 code).
  The "edge_geo" column is the supply edge's geographic scope:
    "US"     = relationship extracted from a US 10-K filing (implicitly domestic)
    "global" = Wikidata / Wikipedia source (international scope)
    "?"      = unknown

  Before scoring any Company node, ask: is this company plausibly exposed to
  the EVENT'S geography? Apply these guards:
  1. If the event is geography-specific (e.g. "enters India", "Ukraine drought",
     "California port strike") AND the candidate's country AND edge_geo are both
     clearly outside that geography, assign "no_effect" — cite the mismatch.
  2. If edge_geo="US" and the event is outside the US, do NOT assume the
     supply relationship reaches the event's geography unless the company name
     or sector strongly suggests it (e.g. a global commodity supplier).
  3. Commodity, Region, and Regulator nodes are exempt from the geography
     guard — they can be affected globally.

Examples for the sugarcane-pest scenario (global event):
  - Coca-Cola (uses sugar, country=US, edge_geo=US): negative (input cost up)
  - A beet-sugar producer: positive (substitute demand up)
  - A telecom tower REIT: no_effect (unrelated)

Examples for "Chic-fil-A enters India" (India-specific):
  - Tyson Foods (poultry supplier, country=US, edge_geo=US): no_effect
    (US domestic supply relationship; Tyson has no India supply presence)
  - Indian poultry companies (country=IN): positive (new B2B demand)
  - Consumer Market India (region): positive (new restaurant traffic)

CANDIDATES at hop {hop_num}:
  Format: id | type | name | sector | country | edge_geo | parent | edge_type | parent_direction
{candidates}

Respond with STRICT JSON only -- a single JSON array, one object per
candidate id. No prose before or after. Keep each reasoning under 20 words.

[
  {{"node_id": "<id>", "direction": "positive" | "negative" | "no_effect", "magnitude": 0.0 to 1.0, "reasoning": "<short>"}}
]

Cover every candidate id exactly once.
"""


# ---------------------------------------------------------------------------
# Top-level propagation
# ---------------------------------------------------------------------------

def _build_seeds_block(all_seeds: list[dict[str, Any]]) -> str:
    """Format all hop-0 seeds into a single readable block for ring/refinement prompts."""
    lines = []
    for s in all_seeds:
        lines.append(
            f"  {s['node_id']} | {s['type']} | {s['name']} | "
            f"{s['direction']} ({s['magnitude']:.2f}) — {s['reasoning']}"
        )
    return "\n".join(lines) if lines else "  (none)"


def run_impact(text: str, *, conn: sqlite3.Connection) -> dict[str, Any]:
    text = (text or "").strip()
    if not text:
        return {"error": "empty news text", "seed": None, "impacts": []}

    debug_log: list[str] = []

    # == Step 1: Build commodity/region seed prompt (no LLM yet) ==========
    candidates = _list_seed_candidates(conn)
    candidate_lines = []
    for c in candidates:
        cat = c.get("category") or c["type"].lower()
        candidate_lines.append(f"  {c['id']} | {c['type']} | {c['name']} | {cat}")
    seed_prompt = _SEED_PROMPT_TEMPLATE.format(
        news=text,
        candidates="\n".join(candidate_lines),
    )

    # == Step 2: Run entity extraction + commodity seed selection in parallel ==
    # Both are independent LLM calls; run them concurrently to cut latency.
    log.info("multi-seed: entity extraction + commodity seed in parallel")
    debug_log.append(f"seed_parallel: start (commodity candidates={len(candidate_lines)})")
    with ThreadPoolExecutor(max_workers=2) as pool:
        f_entities = pool.submit(_extract_named_entities, text)
        f_seed_raw = pool.submit(_llm_call, seed_prompt)
        named_entities = f_entities.result()
        seed_raw = f_seed_raw.result()
    debug_log.append(
        f"seed_parallel: done — entity_extract returned {len(named_entities)} entities: "
        f"{[e['company_name'] for e in named_entities]}"
    )

    # == Step 3: Resolve named entities to graph nodes (DB lookups, fast) ==
    resolved_seeds: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for entity in named_entities:
        node = _resolve_entity(conn, entity["company_name"])
        if node:
            nid = node["id"]
            if nid not in seen_ids:
                seen_ids.add(nid)
                resolved_seeds.append({
                    "node_id": nid,
                    "name": node["name"],
                    "type": node["type"],
                    "direction": entity["direction"],
                    "magnitude": entity["magnitude"],
                    "reasoning": entity["reasoning"],
                    "sector": node.get("sector"),
                    "country": node.get("country"),
                    "is_named_entity": True,
                })
                debug_log.append(
                    f"entity_resolve: '{entity['company_name']}' → {nid} ({node['name']})"
                )
        else:
            debug_log.append(
                f"entity_resolve: '{entity['company_name']}' → not found in graph"
            )

    # == Step 4: Parse commodity/region seed ==============================
    seed_obj = _parse_llm_json(seed_raw) or {}
    commodity_seed_id = seed_obj.get("node_id")
    commodity_seed: Optional[dict[str, Any]] = None
    if commodity_seed_id and commodity_seed_id not in seen_ids:
        commodity_summary = _node_summary(conn, commodity_seed_id)
        if commodity_summary:
            commodity_seed = {
                "node_id": commodity_seed_id,
                "name": commodity_summary["name"],
                "type": commodity_summary["type"],
                "direction": seed_obj.get("direction") or "negative",
                "magnitude": float(seed_obj.get("magnitude") or 0.9),
                "reasoning": seed_obj.get("reasoning") or "",
                "sector": commodity_summary.get("sector"),
                "country": commodity_summary.get("country"),
                "is_named_entity": False,
            }
            seen_ids.add(commodity_seed_id)
            debug_log.append(
                f"commodity_seed: {commodity_seed_id} ({commodity_summary['name']}) "
                f"{commodity_seed['direction']} ({commodity_seed['magnitude']:.2f})"
            )
        else:
            debug_log.append(f"commodity_seed: LLM picked unknown id {commodity_seed_id}")
    elif commodity_seed_id and commodity_seed_id in seen_ids:
        debug_log.append(
            f"commodity_seed: {commodity_seed_id} already in named seeds — skipping duplicate"
        )
    else:
        debug_log.append("commodity_seed: LLM returned no seed node")

    # == Step 5: Combine all seeds ========================================
    # Named entity seeds first (more specific); commodity/region seed appended.
    all_seeds: list[dict[str, Any]] = list(resolved_seeds)
    if commodity_seed:
        all_seeds.append(commodity_seed)

    if not all_seeds:
        return {
            "error": "Could not identify any seed nodes from the news text",
            "seed": None,
            "impacts": [],
            "debug": debug_log,
        }

    debug_log.append(
        f"seeds: {len(all_seeds)} total — "
        + ", ".join(f"{s['node_id']} ({s['name']}, {s['direction']})" for s in all_seeds)
    )

    # Build the seeds_block string used in all subsequent prompts.
    seeds_block = _build_seeds_block(all_seeds)

    # == Step 6: Initialize BFS from all seeds at hop 0 ===================
    impacts: dict[str, dict[str, Any]] = {}
    visited: set[str] = set()
    frontier: list[str] = []
    for s in all_seeds:
        nid = s["node_id"]
        impacts[nid] = {
            "node_id": nid,
            "name": s["name"],
            "type": s["type"],
            "direction": s["direction"],
            "magnitude": s["magnitude"],
            "hop": 0,
            "reasoning": s["reasoning"],
            "via_parent": None,
            "edge_type": None,
            "is_seed": True,
        }
        visited.add(nid)
        frontier.append(nid)

    # Primary seed: first named entity (if any); else commodity/region seed.
    # Used in the API response's `seed` field for backward compatibility.
    primary_seed_id = all_seeds[0]["node_id"]

    # -- Step 7: BFS ring by ring ----------------------------------------
    for hop in range(1, MAX_HOPS + 1):
        full_ring = _neighbors(conn, frontier, visited)
        log.info("hop %d: frontier=%d, ring=%d", hop, len(frontier), len(full_ring))
        debug_log.append(f"hop {hop}: frontier={len(frontier)}, raw_neighbors={len(full_ring)}")
        if not full_ring:
            debug_log.append(f"hop {hop}: no new neighbors -> stop")
            break
        # Chunk the ring into MAX_RING_CANDIDATES-sized batches so every
        # neighbour gets scored. Each chunk is one LLM call; a 28-node
        # hop-1 takes two calls with MAX_RING_CANDIDATES=16. Previous
        # behaviour silently dropped neighbours past the cap (that's why
        # Starbucks didn't appear in some runs).
        chunks = [
            full_ring[i:i + MAX_RING_CANDIDATES]
            for i in range(0, len(full_ring), MAX_RING_CANDIDATES)
        ]
        debug_log.append(
            f"hop {hop}: scoring in {len(chunks)} chunk(s) of <= "
            f"{MAX_RING_CANDIDATES} (parallelism={min(RING_PARALLELISM, len(chunks))})"
        )

        # Build all chunk prompts up front, then run them through a
        # bounded ThreadPoolExecutor. Each _llm_call is an independent
        # subprocess; parallelizing cuts wall time roughly linearly with
        # parallelism. On a typical war-shock scenario the worst ring
        # (hop 3, ~200 candidates / 13 chunks) drops from ~6 minutes
        # serial to ~1 minute at parallelism=6.
        chunk_prompts: list[str] = []
        for ring in chunks:
            cand_lines = []
            for nb in ring:
                parent_v = impacts.get(nb["via_parent"], {})
                country = nb.get("country") or "-"
                geo = nb.get("supply_geography") or "?"
                cand_lines.append(
                    f"  {nb['id']} | {nb['type']} | {nb['name']} | "
                    f"{nb.get('sector') or '-'} | country={country} | edge_geo={geo} | "
                    f"parent={nb['via_parent']} | "
                    f"edge={nb['edge_type']} | parent_dir={parent_v.get('direction', '?')}"
                )
            chunk_prompts.append(_RING_PROMPT_TEMPLATE.format(
                news=text,
                seeds_block=seeds_block,
                hop_num=hop,
                candidates="\n".join(cand_lines),
            ))

        t_hop = time.time()
        workers = min(RING_PARALLELISM, len(chunks))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            chunk_raws = list(pool.map(_llm_call, chunk_prompts))
        debug_log.append(
            f"hop {hop}: {len(chunks)} chunks done in {time.time() - t_hop:.1f}s"
        )

        new_frontier: list[str] = []
        chunk_failed = False
        for chunk_idx, (ring, ring_raw) in enumerate(zip(chunks, chunk_raws)):
            debug_log.append(
                f"hop {hop} chunk {chunk_idx + 1}/{len(chunks)}: "
                f"LLM raw_len={len(ring_raw)}"
            )
            ring_parsed = _parse_llm_json(ring_raw)
            if isinstance(ring_parsed, dict) and "results" in ring_parsed:
                ring_parsed = ring_parsed.get("results")
            if not isinstance(ring_parsed, list):
                log.warning("hop %d chunk %d: parse FAIL", hop, chunk_idx + 1)
                debug_log.append(
                    f"hop {hop} chunk {chunk_idx + 1}: parse FAIL "
                    f"type={type(ring_parsed).__name__}, tail={(ring_raw or '')[-200:]!r}"
                )
                chunk_failed = True
                continue
            debug_log.append(
                f"hop {hop} chunk {chunk_idx + 1}: scored {len(ring_parsed)} verdicts"
            )
            for verdict in ring_parsed:
                if not isinstance(verdict, dict):
                    continue
                nid = verdict.get("node_id")
                if not nid or nid in impacts:
                    continue
                matching = next((nb for nb in ring if nb["id"] == nid), None)
                if not matching:
                    continue
                direction = verdict.get("direction") or "no_effect"
                magnitude = float(verdict.get("magnitude") or 0.0)
                reasoning = verdict.get("reasoning") or ""
                impacts[nid] = {
                    "node_id": nid,
                    "name": matching["name"],
                    "type": matching["type"],
                    "direction": direction,
                    "magnitude": magnitude,
                    "hop": hop,
                    "reasoning": reasoning,
                    "via_parent": matching["via_parent"],
                    "edge_type": matching["edge_type"],
                    "country": matching.get("country"),  # Phase E
                }
                visited.add(nid)
                if direction in ("positive", "negative") and magnitude >= 0.15:
                    new_frontier.append(nid)
        if chunk_failed and not new_frontier:
            # If every chunk failed we bail out of the BFS to avoid an
            # infinite-looking wait on empty rings.
            break
        if not new_frontier:
            break
        frontier = new_frontier

    # -- Step N+1: refinement pass -----------------------------------------
    # The hop-by-hop BFS is myopic: each node is scored ONCE, against ONE
    # parent's verdict, even when many other impacted neighbours connect
    # to it. Canonical failure mode is "India under Ukraine war" -- India
    # gets scored at hop 2 via the wheat path ("self-sufficient in wheat;
    # no effect") and the model never sees that sunflower-oil (also at
    # hop 2, scored in parallel) is also negative and feeds the same
    # node via different companies.
    #
    # The fix: find every node currently no_effect / low-magnitude that
    # has TWO OR MORE impacted neighbours, then re-score those nodes
    # with the full set of neighbour verdicts shown to the LLM at once.
    # Only apply the new verdict if it strengthens (higher magnitude or
    # flipped direction from no_effect to a definite call) -- never
    # downgrade an already-strong verdict.
    refinement_summary = _refinement_pass(
        text=text,
        impacts=impacts,
        seeds_block=seeds_block,
        conn=conn,
        debug_log=debug_log,
    )

    # `seed` field: first named-entity seed, or commodity/region if no
    # named entities resolved. Kept for backward-compat with older frontends.
    seeds_response = [impacts[s["node_id"]] for s in all_seeds if s["node_id"] in impacts]
    return {
        "seed": impacts.get(primary_seed_id),
        "seeds": seeds_response,
        "impacts": list(impacts.values()),
        "provider": LLM_PROVIDER,
        "model": "claude-code-cli" if LLM_PROVIDER == "claude" else OLLAMA_MODEL,
        "max_hops": MAX_HOPS,
        "debug": debug_log,
        "refinement": refinement_summary,
    }


# ---------------------------------------------------------------------------
# Refinement pass (Fix A)
# ---------------------------------------------------------------------------

# How many candidates to refine per run. Caps wall time and credit usage.
REFINEMENT_MAX_NODES = int(os.environ.get("IMPACT_REFINE_MAX", "60"))
# Minimum impacted-neighbour count for a node to be eligible for refinement.
# 2 keeps the volume high but still meaningful; 1 would trigger on every
# weakly-connected node.
REFINEMENT_MIN_PARENTS = int(os.environ.get("IMPACT_REFINE_MIN_PARENTS", "2"))
# Only refine nodes currently below this magnitude (or "no_effect").
REFINEMENT_MAGNITUDE_THRESHOLD = float(
    os.environ.get("IMPACT_REFINE_MAG_THRESHOLD", "0.35")
)

_REFINE_BATCH_PROMPT_TEMPLATE = """You are an economist refining impact assessments for MULTIPLE nodes.

NEWS:
\"\"\"
{news}
\"\"\"

SEEDS (the original shocks, hop 0):
{seeds_block}

Each node below was initially scored against ONE parent only. It actually
has MULTIPLE impacted neighbours that may collectively affect it. Re-score
each node considering ALL the signals listed for that node.

DIRECTION SEMANTICS:
  "negative" = node is HURT (higher costs, lost supply, lost demand, regulatory pain)
  "positive" = node is HELPED (substitute demand, lower input cost, rival's loss is its gain)
  "no_effect" = shock does not meaningfully reach this node

GEOGRAPHY RULE (Phase E): Each NODE header shows "country=<ISO-2>" (HQ country).
  If the news event targets a specific geography and this node's country is clearly
  outside it AND the supply relationships shown are US-domestic (edge_geo=US),
  prefer "no_effect" citing the geographic mismatch — unless the node itself is
  a Commodity or Region node, or the sector strongly implies cross-border exposure.

{node_blocks}

Respond with STRICT JSON only -- a single array, one object per node_id
in the SAME ORDER as above. Keep each reasoning under 20 words.

[
  {{"node_id": "<id>", "direction": "positive" | "negative" | "no_effect", "magnitude": 0.0 to 1.0, "reasoning": "<short>"}}
]

Cover every node_id exactly once.
"""


def _build_refine_node_block(
    nid: str,
    v: dict,
    node_sector: str,
    node_country: str,
    nb_lines: list[tuple[float, str]],
) -> str:
    """Format one node's refinement context block for the batch prompt."""
    lines = [
        f"NODE: {nid} ({v.get('name', '')}, {v.get('type', '')}, "
        f"sector={node_sector}, country={node_country})",
        f"  Initial verdict: direction={v.get('direction', 'no_effect')}, "
        f"magnitude={float(v.get('magnitude', 0.0)):.2f}",
        f"  Initial reasoning: {v.get('reasoning', '')[:120]}",
        "  Impacted neighbours:",
    ]
    for _m, line in nb_lines[:20]:
        lines.append("    " + line)
    return "\n".join(lines)


def _refinement_pass(
    *,
    text: str,
    impacts: dict[str, dict[str, Any]],
    seeds_block: str,
    conn: sqlite3.Connection,
    debug_log: list[str],
) -> dict[str, Any]:
    """Re-score weakly-classified nodes with full multi-parent context.
    Returns a summary dict for the API response."""
    # Build: for every node in `impacts`, the set of OTHER nodes in
    # `impacts` that share an edge with it.
    if len(impacts) < 3:
        debug_log.append("refine: too few impacted nodes; skipping")
        return {"considered": 0, "rescored": 0, "applied": 0}

    impacted_ids = list(impacts.keys())
    placeholders = ",".join("?" for _ in impacted_ids)
    edge_rows = conn.execute(
        f"""
        SELECT source, target, type FROM edges
        WHERE below_threshold = 0
          AND source IN ({placeholders})
          AND target IN ({placeholders})
        """,
        impacted_ids + impacted_ids,
    ).fetchall()

    # neighbours[node_id] = list of (other_id, edge_type, direction-as-seen-from-node)
    # We don't differentiate source vs target direction -- impact flows
    # both ways through value-chain edges in practice.
    neighbours: dict[str, list[tuple[str, str]]] = {}
    for src, tgt, etype in edge_rows:
        neighbours.setdefault(src, []).append((tgt, etype))
        neighbours.setdefault(tgt, []).append((src, etype))

    # Eligible: nodes with WEAK initial verdict + 2+ impacted neighbours.
    # Skip hop-0 seeds — they already have their verdicts from the seed step.
    eligible: list[tuple[str, int]] = []
    for nid, v in impacts.items():
        if v.get("is_seed"):
            continue
        direction = v.get("direction", "no_effect")
        magnitude = float(v.get("magnitude", 0.0))
        is_weak = (
            direction == "no_effect"
            or magnitude < REFINEMENT_MAGNITUDE_THRESHOLD
        )
        if not is_weak:
            continue
        nb_count = sum(
            1 for (other, _et) in neighbours.get(nid, [])
            if other in impacts and impacts[other].get("direction") in ("positive", "negative")
            and float(impacts[other].get("magnitude", 0.0)) >= 0.20
        )
        if nb_count >= REFINEMENT_MIN_PARENTS:
            eligible.append((nid, nb_count))

    # Sort by neighbour count descending (most-connected first) and cap.
    eligible.sort(key=lambda x: -x[1])
    eligible = eligible[:REFINEMENT_MAX_NODES]
    debug_log.append(
        f"refine: {len(eligible)} eligible nodes (weak verdict + "
        f">= {REFINEMENT_MIN_PARENTS} impacted neighbours)"
    )
    if not eligible:
        return {"considered": 0, "rescored": 0, "applied": 0}

    # Build per-node context blocks, then pack them into REFINEMENT_BATCH_SIZE
    # batches. One LLM call per batch (same prompt shape as ring scoring --
    # multi-candidate array response). Old code: 60 calls, 10 serial rounds
    # at P=8. New code: 10 calls, 2 serial rounds at P=8 -- ~5x faster.
    node_blocks_list: list[tuple[str, str, dict]] = []   # (nid, block_text, prev_verdict)
    for nid, _nb_count in eligible:
        v = impacts[nid]
        nb_lines: list[tuple[float, str]] = []
        for other, etype in neighbours.get(nid, []):
            ov = impacts.get(other)
            if not ov:
                continue
            d = ov.get("direction", "no_effect")
            m = float(ov.get("magnitude", 0.0))
            if d == "no_effect" or m < 0.15:
                continue
            nb_lines.append((
                m,
                f"{other} | {ov.get('name', '')[:40]} | {d} mag={m:.2f} | "
                f"edge={etype} | country={ov.get('country') or '-'} | "
                f"{ov.get('reasoning', '')[:80]}",
            ))
        if len(nb_lines) < REFINEMENT_MIN_PARENTS:
            continue
        nb_lines.sort(key=lambda x: -x[0])

        node_row = conn.execute(
            "SELECT sector, country FROM nodes WHERE id = ?", (nid,)
        ).fetchone()
        node_sector = (node_row["sector"] if node_row else None) or v.get("type", "")
        node_country = (node_row["country"] if node_row else None) or v.get("country") or "-"

        block = _build_refine_node_block(nid, v, node_sector, node_country, nb_lines)
        node_blocks_list.append((nid, block, v))

    if not node_blocks_list:
        return {"considered": len(eligible), "rescored": 0, "applied": 0}

    # Pack into batches.
    batches: list[list[tuple[str, str, dict]]] = [
        node_blocks_list[i:i + REFINEMENT_BATCH_SIZE]
        for i in range(0, len(node_blocks_list), REFINEMENT_BATCH_SIZE)
    ]
    prompts: list[str] = []
    for batch in batches:
        node_blocks_text = "\n\n".join(block for _, block, _ in batch)
        prompts.append(_REFINE_BATCH_PROMPT_TEMPLATE.format(
            news=text,
            seeds_block=seeds_block,
            node_blocks=node_blocks_text,
        ))

    debug_log.append(
        f"refine: {len(node_blocks_list)} nodes -> {len(prompts)} batch calls "
        f"(batch_size={REFINEMENT_BATCH_SIZE}, parallelism={RING_PARALLELISM})"
    )
    workers = min(RING_PARALLELISM, len(prompts))
    t_refine = time.time()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        batch_raws = list(pool.map(_llm_call, prompts))
    debug_log.append(f"refine: {len(batch_raws)} batch calls done in {time.time() - t_refine:.1f}s")

    # Flatten verdicts: each raw is a JSON array for its batch.
    # Index by node_id for O(1) lookup.
    verdict_by_nid: dict[str, dict] = {}
    for batch, raw in zip(batches, batch_raws):
        parsed = _parse_llm_json(raw)
        if isinstance(parsed, dict) and "results" in parsed:
            parsed = parsed["results"]
        if not isinstance(parsed, list):
            debug_log.append(f"refine batch parse FAIL: type={type(parsed).__name__}")
            continue
        for verdict in parsed:
            if not isinstance(verdict, dict):
                continue
            nid = verdict.get("node_id")
            if nid:
                verdict_by_nid[nid] = verdict

    applied = 0
    rescored = 0
    for nid, _block, prev in node_blocks_list:
        verdict = verdict_by_nid.get(nid)
        if not verdict:
            continue
        rescored += 1
        new_dir = verdict.get("direction") or prev.get("direction")
        new_mag = float(verdict.get("magnitude") or 0.0)
        new_reasoning = verdict.get("reasoning") or prev.get("reasoning", "")

        # Apply only if the new verdict STRENGTHENS the existing one --
        # never downgrade a confident call to no_effect via refinement.
        prev_dir = prev.get("direction", "no_effect")
        prev_mag = float(prev.get("magnitude", 0.0))
        should_apply = False
        if new_dir != "no_effect" and prev_dir == "no_effect" and new_mag >= 0.20:
            should_apply = True
        elif new_dir == prev_dir and new_mag - prev_mag >= 0.15:
            should_apply = True
        elif new_dir != prev_dir and new_dir != "no_effect" and new_mag >= 0.30:
            should_apply = True

        if should_apply:
            impacts[nid] = {
                **prev,
                "direction": new_dir,
                "magnitude": new_mag,
                "reasoning": new_reasoning,
                "refined": True,
                "previous": {
                    "direction": prev_dir,
                    "magnitude": prev_mag,
                    "reasoning": prev.get("reasoning", ""),
                },
            }
            applied += 1

    debug_log.append(
        f"refine: rescored={rescored}/{len(node_blocks_list)} parsed; applied={applied}"
    )
    return {
        "considered": len(eligible),
        "rescored": rescored,
        "applied": applied,
        "candidates": [nid for nid, _, _ in node_blocks_list],
    }


# ---------------------------------------------------------------------------
# Node descriptions (LLM-generated, cached per-process)
# ---------------------------------------------------------------------------

_DESCRIBE_CACHE: dict[str, str] = {}

_DESCRIBE_PROMPT_TEMPLATE = """Write a 2-3 sentence description of the following entity for someone
who has never heard of it. Plain English, no jargon. Focus on:
  - what they actually do (make, sell, mine, grow, regulate, etc.)
  - who their customers are
  - why they matter in the value chain (scale, niche, market position)

ENTITY:
  name: {name}
  type: {type}
  sector: {sector}
  industry: {industry}
  country: {country}
  tickers: {tickers}
  hq: {hq}

Respond with ONLY the description text, no preamble, no quotes, no bullet points.
"""


def describe_node(
    *,
    name: str,
    node_type: str,
    sector: Optional[str] = None,
    industry: Optional[str] = None,
    country: Optional[str] = None,
    tickers: Optional[list[str]] = None,
    hq: Optional[str] = None,
    cache_key: Optional[str] = None,
) -> str:
    """One-shot business description via the configured LLM provider.
    Cached per-process by cache_key (typically the node id) so repeated
    inspector clicks don't re-hit the LLM."""
    if cache_key and cache_key in _DESCRIBE_CACHE:
        return _DESCRIBE_CACHE[cache_key]
    prompt = _DESCRIBE_PROMPT_TEMPLATE.format(
        name=name,
        type=node_type,
        sector=sector or "-",
        industry=industry or "-",
        country=country or "-",
        tickers=", ".join(tickers or []) or "-",
        hq=hq or "-",
    )
    raw = _llm_call(prompt)
    # Strip any leading/trailing whitespace + accidental quote-wrapping.
    text = (raw or "").strip().strip('"').strip("'").strip()
    # Trim Claude's occasional "Here is a description:" preamble.
    text = re.sub(r"^(here\s+is\s+(a\s+)?description[:\s\-]*)", "", text, flags=re.IGNORECASE)
    text = text.strip()
    if cache_key:
        _DESCRIBE_CACHE[cache_key] = text
    return text
