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
# Per-ring cap. Both providers handle ~16 verdicts comfortably; Gemma
# tops out around 20, Claude can go higher but we keep it consistent.
MAX_RING_CANDIDATES = int(os.environ.get("IMPACT_MAX_RING", "16"))
LLM_TIMEOUT_SECONDS = int(os.environ.get("IMPACT_LLM_TIMEOUT", "600"))
# How many ring chunks to score in parallel. Each is an independent
# Claude CLI subprocess; 4-6 keeps a typical laptop happy without
# starving the CLI. Going wider helps wall time but spawns more
# concurrent claude-cli processes (each ~200-400 MB RSS).
RING_PARALLELISM = int(os.environ.get("IMPACT_RING_PARALLELISM", "6"))


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
        "SELECT id, type, name, sector, industry, metadata FROM nodes WHERE id = ?",
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
        SELECT DISTINCT source AS parent, target AS child, type AS edge_type
        FROM edges
        WHERE source IN ({placeholder}) AND below_threshold = 0
        UNION
        SELECT DISTINCT target AS parent, source AS child, type AS edge_type
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

SEED (already classified): {seed_id} ({seed_name}, {seed_type})
SEED DIRECTION: {seed_direction}  -- {seed_reasoning}

You are now at hop {hop_num}. Each candidate is connected to a parent
already classified. Score each candidate's likely impact GIVEN the news.

DIRECTION SEMANTICS:
  "negative" = this candidate is HURT (higher input cost, lost supply,
               reduced demand for its product, regulatory pain).
  "positive" = this candidate is HELPED (substitute benefits from rival's
               loss, demand shifts to it, sells more of an affected input).
  "no_effect" = the shock does not meaningfully reach this node.

Examples for the sugarcane-pest scenario:
  - Coca-Cola (uses sugar): negative (input cost up)
  - A beet-sugar producer: positive (substitute demand up)
  - A telecom tower REIT: no_effect (unrelated)

CANDIDATES at hop {hop_num}:
  Format: id | type | name | sector | parent | edge_type | parent_direction
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

def run_impact(text: str, *, conn: sqlite3.Connection) -> dict[str, Any]:
    text = (text or "").strip()
    if not text:
        return {"error": "empty news text", "seed": None, "impacts": []}

    # -- Step 1: identify seed -------------------------------------------
    candidates = _list_seed_candidates(conn)
    candidate_lines = []
    for c in candidates:
        cat = c.get("category") or c["type"].lower()
        candidate_lines.append(f"  {c['id']} | {c['type']} | {c['name']} | {cat}")
    seed_prompt = _SEED_PROMPT_TEMPLATE.format(
        news=text,
        candidates="\n".join(candidate_lines),
    )
    log.info("identifying seed (candidates: %d)", len(candidate_lines))
    seed_raw = _llm_call(seed_prompt)
    seed_obj = _parse_llm_json(seed_raw) or {}
    seed_id = seed_obj.get("node_id")
    if not seed_id:
        return {
            "error": "LLM did not pick a seed node",
            "seed": None,
            "impacts": [],
            "llm_raw": seed_raw[:500],
        }
    seed_summary = _node_summary(conn, seed_id)
    if not seed_summary:
        return {
            "error": f"LLM picked unknown node id: {seed_id}",
            "seed": None,
            "impacts": [],
        }
    seed_direction = seed_obj.get("direction") or "negative"
    seed_magnitude = float(seed_obj.get("magnitude") or 0.9)
    seed_reasoning = seed_obj.get("reasoning") or ""

    # impacts maps node_id -> verdict dict
    impacts: dict[str, dict[str, Any]] = {
        seed_id: {
            "node_id": seed_id,
            "name": seed_summary["name"],
            "type": seed_summary["type"],
            "direction": seed_direction,
            "magnitude": seed_magnitude,
            "hop": 0,
            "reasoning": seed_reasoning,
            "via_parent": None,
            "edge_type": None,
        }
    }
    visited = {seed_id}
    frontier = [seed_id]

    # -- Step 2-N: BFS ring by ring --------------------------------------
    debug_log: list[str] = []
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
                cand_lines.append(
                    f"  {nb['id']} | {nb['type']} | {nb['name']} | "
                    f"{nb.get('sector') or '-'} | parent={nb['via_parent']} | "
                    f"edge={nb['edge_type']} | parent_dir={parent_v.get('direction', '?')}"
                )
            chunk_prompts.append(_RING_PROMPT_TEMPLATE.format(
                news=text,
                seed_id=seed_id,
                seed_name=seed_summary["name"],
                seed_type=seed_summary["type"],
                seed_direction=seed_direction,
                seed_reasoning=seed_reasoning,
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
        seed_id=seed_id,
        seed_summary=seed_summary,
        seed_direction=seed_direction,
        seed_reasoning=seed_reasoning,
        conn=conn,
        debug_log=debug_log,
    )

    return {
        "seed": impacts[seed_id],
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

_REFINE_PROMPT_TEMPLATE = """You are an economist refining an impact assessment.

NEWS:
\"\"\"
{news}
\"\"\"

SEED (the original shock): {seed_id} ({seed_name}, {seed_type})
SEED DIRECTION: {seed_direction}  -- {seed_reasoning}

The node below was initially scored against ONE parent. It actually has
MULTIPLE impacted neighbours that may collectively affect it. Re-score
considering ALL the signals listed.

NODE: {node_id} ({node_name}, {node_type}, sector={node_sector})
INITIAL VERDICT: direction={direction}, magnitude={magnitude:.2f}
INITIAL REASONING: {reasoning}

ALL IMPACTED NEIGHBOURS (already classified earlier in the propagation):
{neighbour_lines}

Refine the verdict given the FULL picture. If multiple negative neighbours
collectively put pressure on this node, the verdict may flip from
no_effect to negative or strengthen an existing call. If signals offset
(positive + negative neighbours), explain how they net out.

Respond with STRICT JSON only, nothing else:
{{"direction": "positive" | "negative" | "no_effect", "magnitude": 0.0 to 1.0, "reasoning": "<short>"}}
"""


def _refinement_pass(
    *,
    text: str,
    impacts: dict[str, dict[str, Any]],
    seed_id: str,
    seed_summary: dict[str, Any],
    seed_direction: str,
    seed_reasoning: str,
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

    # Eligible: nodes with WEAK initial verdict + 2+ impacted neighbours
    eligible: list[tuple[str, int]] = []
    for nid, v in impacts.items():
        if nid == seed_id:
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

    # Build prompts and dispatch in parallel.
    prompts: list[str] = []
    eligible_meta: list[dict[str, Any]] = []
    for nid, _nb_count in eligible:
        v = impacts[nid]
        # Gather impacted neighbour lines, capped at 25 entries so the
        # prompt stays tractable. Sort by magnitude descending so the
        # strongest signals come first.
        nb_lines = []
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
                f"  {other} | {ov.get('name', '')[:40]} | {d} mag={m:.2f} | edge={etype} | {ov.get('reasoning', '')[:80]}",
            ))
        if len(nb_lines) < REFINEMENT_MIN_PARENTS:
            continue
        nb_lines.sort(key=lambda x: -x[0])
        nb_text = "\n".join(line for _m, line in nb_lines[:25])

        # Get node sector from db for context.
        node_row = conn.execute(
            "SELECT sector FROM nodes WHERE id = ?", (nid,)
        ).fetchone()
        node_sector = (node_row[0] if node_row else None) or v.get("type", "")

        prompt = _REFINE_PROMPT_TEMPLATE.format(
            news=text,
            seed_id=seed_id,
            seed_name=seed_summary.get("name", ""),
            seed_type=seed_summary.get("type", ""),
            seed_direction=seed_direction,
            seed_reasoning=seed_reasoning[:160],
            node_id=nid,
            node_name=v.get("name", ""),
            node_type=v.get("type", ""),
            node_sector=node_sector,
            direction=v.get("direction", "no_effect"),
            magnitude=float(v.get("magnitude", 0.0)),
            reasoning=v.get("reasoning", "")[:160],
            neighbour_lines=nb_text,
        )
        prompts.append(prompt)
        eligible_meta.append({"node_id": nid, "prev": v})

    if not prompts:
        return {"considered": len(eligible), "rescored": 0, "applied": 0}

    # Reuse RING_PARALLELISM since each refinement is the same shape of
    # call as a ring chunk.
    debug_log.append(
        f"refine: running {len(prompts)} LLM calls at parallelism={RING_PARALLELISM}"
    )
    workers = min(RING_PARALLELISM, len(prompts))
    t_refine = time.time()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(_llm_call, prompts))
    debug_log.append(f"refine: {len(results)} calls done in {time.time() - t_refine:.1f}s")

    applied = 0
    rescored = 0
    for meta, raw in zip(eligible_meta, results):
        nid = meta["node_id"]
        prev = meta["prev"]
        parsed = _parse_llm_json(raw)
        if not isinstance(parsed, dict):
            continue
        rescored += 1
        new_dir = parsed.get("direction") or prev.get("direction")
        new_mag = float(parsed.get("magnitude") or 0.0)
        new_reasoning = parsed.get("reasoning") or prev.get("reasoning", "")

        # Apply only if the new verdict is STRONGER:
        # - direction flips from no_effect to a definite call, OR
        # - magnitude rises by >= 0.15 with same direction, OR
        # - direction reverses with magnitude > 0.30 (clear flip).
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
        f"refine: rescored={rescored}/{len(prompts)} parsed; applied={applied}"
    )
    return {
        "considered": len(eligible),
        "rescored": rescored,
        "applied": applied,
        "candidates": [m["node_id"] for m in eligible_meta],
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
