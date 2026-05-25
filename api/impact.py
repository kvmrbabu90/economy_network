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
import sqlite3
import time
from typing import Any, Optional

import urllib.request
import urllib.error

log = logging.getLogger(__name__)

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434/api/generate")
OLLAMA_MODEL = os.environ.get("ECONGRAPH_LLM_MODEL", "gemma4:26b")
MAX_HOPS = int(os.environ.get("IMPACT_MAX_HOPS", "3"))
# Per-ring cap. Gemma 4 26B takes ~30-60s for ~15-20 verdicts on local
# hardware; anything bigger blows past the per-request timeout. We
# still see the whole frontier, just sliced.
MAX_RING_CANDIDATES = int(os.environ.get("IMPACT_MAX_RING", "16"))
LLM_TIMEOUT_SECONDS = int(os.environ.get("IMPACT_LLM_TIMEOUT", "600"))


# ---------------------------------------------------------------------------
# Ollama client
# ---------------------------------------------------------------------------

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
    seed_raw = _ollama_call(seed_prompt)
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
        ring = _neighbors(conn, frontier, visited)
        log.info("hop %d: frontier=%d, ring=%d", hop, len(frontier), len(ring))
        debug_log.append(f"hop {hop}: frontier={len(frontier)}, raw_neighbors={len(ring)}")
        if not ring:
            debug_log.append(f"hop {hop}: no new neighbors -> stop")
            break
        # Cap per-ring candidates so prompts stay tractable.
        if len(ring) > MAX_RING_CANDIDATES:
            ring = ring[:MAX_RING_CANDIDATES]
        cand_lines = []
        for nb in ring:
            parent_v = impacts.get(nb["via_parent"], {})
            cand_lines.append(
                f"  {nb['id']} | {nb['type']} | {nb['name']} | "
                f"{nb.get('sector') or '-'} | parent={nb['via_parent']} | "
                f"edge={nb['edge_type']} | parent_dir={parent_v.get('direction', '?')}"
            )
        ring_prompt = _RING_PROMPT_TEMPLATE.format(
            news=text,
            seed_id=seed_id,
            seed_name=seed_summary["name"],
            seed_type=seed_summary["type"],
            seed_direction=seed_direction,
            seed_reasoning=seed_reasoning,
            hop_num=hop,
            candidates="\n".join(cand_lines),
        )
        log.info("scoring hop %d ring of %d candidates", hop, len(ring))
        ring_raw = _ollama_call(ring_prompt)
        debug_log.append(f"hop {hop}: LLM raw_len={len(ring_raw)} head={(ring_raw or '')[:120]!r}")
        ring_parsed = _parse_llm_json(ring_raw)
        # tolerate either {"results": [...]} or [...] directly
        if isinstance(ring_parsed, dict) and "results" in ring_parsed:
            ring_parsed = ring_parsed.get("results")
        if not isinstance(ring_parsed, list):
            log.warning("ring %d: LLM returned non-list, skipping", hop)
            debug_log.append(f"hop {hop}: parse FAIL type={type(ring_parsed).__name__}, tail={(ring_raw or '')[-300:]!r}")
            break
        debug_log.append(f"hop {hop}: LLM scored {len(ring_parsed)} verdicts")
        new_frontier: list[str] = []
        for verdict in ring_parsed:
            if not isinstance(verdict, dict):
                continue
            nid = verdict.get("node_id")
            if not nid or nid in impacts:
                continue
            # Find the matching ring entry to capture parent / edge
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
            # Only propagate through nodes the LLM considers meaningfully
            # affected -- "no_effect" nodes stop the chain there.
            if direction in ("positive", "negative") and magnitude >= 0.15:
                new_frontier.append(nid)
        if not new_frontier:
            break
        frontier = new_frontier

    return {
        "seed": impacts[seed_id],
        "impacts": list(impacts.values()),
        "model": OLLAMA_MODEL,
        "max_hops": MAX_HOPS,
        "debug": debug_log,
    }
