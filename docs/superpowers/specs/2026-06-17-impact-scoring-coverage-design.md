# "So What?" Scoring Coverage — Design Spec

**Date:** 2026-06-17
**Status:** Approved (brainstorm), pending implementation
**Work stream:** 2 of 4 (Correctness & trust), sub-project 1 of 4
**Branch:** `feat/impact-scoring-coverage` (stacked on `feat/impact-streaming`)

---

## Context

The "So What?" impact engine (`api/impact.py`) scores each BFS ring by sending
batched candidates to the LLM and parsing a JSON array of verdicts. Two silent
failure modes drop real nodes from a run with no retry and no trace:

1. **Chunk-level (`api/impact.py:1119-1129`):** if an LLM batch returns
   unparseable/empty JSON, `chunk_failed = True; continue` drops **all** nodes in
   that batch (up to `MAX_RING_CANDIDATES`, currently 24).
2. **Missing-verdict (`api/impact.py:1133-1141`):** even in a parsed batch, any
   candidate the LLM omits from its array never enters `impacts` — silently
   dropped. The prompt says "cover every id," but the model doesn't always comply.

Both are invisible: the node simply isn't in the result, indistinguishable from a
node that was never reached. This is the first correctness sub-project of stream 2.

This sub-project's goal: **no node silently vanishes.** Recover transient failures
with targeted retries; surface anything still unscorable as an explicit, visible
state with reported counts.

### Non-goals (this sub-project)
- Verdict verification / anti-hallucination (stream 2, sub-project 2).
- Confidence/uncertainty scoring (sub-project 3).
- Backtesting/validation (sub-project 4).
- The **refinement pass** is out of scope: it re-scores already-present weak nodes,
  and a parse-fail there leaves them at their original verdict (no drop). Left
  unchanged to keep this focused on the actual dropping bug.
- Multi-event merge keeps its existing behavior (see Compatibility).

---

## Approach (chosen: A — unified ensure-coverage retry loop)

After the parallel chunk scoring, merge all verdicts into one `{node_id → verdict}`
map, then compute the candidate ids with **no valid verdict** — a single check that
covers *both* drop modes (a whole failed chunk and individual omitted ids). Re-ask
**only the missing ids** in fresh prompts, up to N retry rounds. Any id still
missing gets an explicit `unscored` verdict and is surfaced (not dropped, not
expanded into the next hop).

Rejected: **B. Per-chunk wholesale retry** (misses omitted-id drops; re-asks
already-scored nodes). **C. Retry inside `_llm_call`** (blind to missing-verdict
within a valid list; no surfacing). A is the complete fix and re-asks only the gaps.

Retries happen **within a hop**, before the streaming `hop` event fires — so each
hop arrives slightly later and **complete**; no new event types.

---

## Section 1 — Backend: ensure-coverage retry loop (`api/impact.py`)

**Refactor for reuse** — extract the inline candidate-line formatting into helpers
so the initial and retry passes share it:

```python
def _format_candidate_line(nb: dict, impacts: dict) -> str:
    """The existing per-candidate f-string: id | type | name | sector | country |
    edge_geo | weight | parent | edge | parent_dir | parent_mag."""

def _build_ring_prompt(news: str, seeds_block: str, hop: int, ring: list[dict],
                       impacts: dict) -> str:
    """Wrap _RING_PROMPT_TEMPLATE around _format_candidate_line for each candidate."""
```

**New helper + config:**

```python
RING_SCORE_RETRIES = int(os.environ.get("IMPACT_SCORE_RETRIES", "1"))  # retry rounds for gaps

def _collect_verdicts(prompts: list[str]) -> dict[str, dict]:
    """Run prompts in parallel (ThreadPoolExecutor, RING_PARALLELISM), parse each
    with _parse_llm_json (unwrapping {"results": [...]}), and return
    {node_id: verdict} for every dict verdict that has a node_id. Unparseable
    chunks and malformed verdicts simply don't populate the map — which is exactly
    how the caller detects what to retry."""
```

**Replace the hop scoring block (`api/impact.py` ~1044-1150)** with:

```python
# build first-pass chunk prompts via _build_ring_prompt(...)
verdict_by_id = _collect_verdicts(chunk_prompts)
first_pass_ids = set(verdict_by_id)          # for the recovered count
attempts = 0
while attempts < RING_SCORE_RETRIES:
    missing = [nb for nb in full_ring if nb["id"] not in verdict_by_id]
    if not missing:
        break
    attempts += 1
    retry_prompts = [_build_ring_prompt(text, seeds_block, hop,
                                        missing[i:i + MAX_RING_CANDIDATES], impacts)
                     for i in range(0, len(missing), MAX_RING_CANDIDATES)]
    before = len(verdict_by_id)
    verdict_by_id.update(_collect_verdicts(retry_prompts))   # only fills gaps
    debug_log.append(f"hop {hop}: retry {attempts} — {len(missing)} missing, "
                     f"{len(verdict_by_id) - before} recovered")

# recovered this hop = ring ids that were absent on the first pass but present now
ring_ids = {nb["id"] for nb in full_ring}
recovered_this_hop = len((set(verdict_by_id) & ring_ids) - first_pass_ids)
total_recovered += recovered_this_hop        # generator-scoped accumulator
unscored_ids: list[str] = []

# apply by iterating CANDIDATES (not verdicts) → coverage guaranteed by construction
for nb in full_ring:
    nid = nb["id"]
    verdict = verdict_by_id.get(nid)
    if not isinstance(verdict, dict):
        impacts[nid] = {
            "node_id": nid, "name": nb["name"], "type": nb["type"],
            "direction": "unscored", "magnitude": 0.0, "hop": hop,
            "reasoning": f"Could not be scored after {RING_SCORE_RETRIES + 1} attempts",
            "via_parent": nb["via_parent"], "edge_type": nb["edge_type"],
            "country": nb.get("country"),
            "edge_weight": nb.get("edge_weight"),
            "edge_source_tier": nb.get("edge_source_tier"),
            "is_estimated": nb.get("edge_weight") is None,
        }
        visited.add(nid); hop_new_ids.append(nid); unscored_ids.append(nid)
        continue
    # ... existing verdict-application logic (Phase E/K fields, new_frontier gate) ...
```

Key points:
- **Iterating `full_ring` instead of parsed verdicts is the structural fix** —
  every candidate is accounted for; both drop modes collapse into "verdict missing
  → mark unscored."
- The old `chunk_failed` bookkeeping and `if chunk_failed and not new_frontier:
  break` are removed. The BFS still stops when `new_frontier` is empty.
- **Unscored nodes are terminal:** `direction:"unscored"` has no propagating
  signal, so they never join `new_frontier` (consistent with `no_effect`/low-mag).

**Count aggregation for the `scoring` summary (Section 2):** `total_recovered` is a
generator-scoped accumulator summed per hop (above). `scored` and `unscored` are
**derived** from the final `impacts` at `done` time, not tracked:
```python
non_seed = [v for v in impacts.values() if not v.get("is_seed")]
unscored_ids_final = [v["node_id"] for v in non_seed if v["direction"] == "unscored"]
scoring = {
    "scored": len(non_seed) - len(unscored_ids_final),
    "recovered": total_recovered,
    "unscored": len(unscored_ids_final),
    "unscored_node_ids": unscored_ids_final,
}
```

---

## Section 2 — Schema / contract

**New `direction` value: `"unscored"`** (joining `positive | negative | no_effect`)
— the single source of truth, self-documenting in the data. An unscored verdict
carries the normal base fields, `magnitude: 0.0`, and a reasoning string. Distinct
from `no_effect` ("judged; no impact") because `unscored` means "could not judge" —
conflating them is the trust gap being closed.

**`done.result` gains a `scoring` summary** (aggregated across hops; `run_impact`
non-streaming returns it too):

```jsonc
"scoring": {
  "scored": 94,            // got a valid verdict (first pass or retry)
  "recovered": 7,          // missing on first pass, filled by retry
  "unscored": 2,           // still unscorable after retries — surfaced, not dropped
  "unscored_node_ids": ["cik:...", "wikidata:..."]
}
```

**`hop` event gains per-hop counts:** `{ "event":"hop", …, "recovered": N,
"unscored": M }`. Unscored nodes themselves ride in `new_impacts` with
`direction:"unscored"`.

**Compatibility:**
- `run_impact` (non-streaming) and the 24h archive carry the same `scoring`
  summary and unscored verdicts (same code path).
- Multi-event merge (`_merge_impact_results`) already coerces any direction not in
  `(positive, negative, no_effect)` to `no_effect`, so an `unscored` node folds to
  `no_effect` in multi-mode. Acceptable for now; left as-is and noted.
- TS `ImpactVerdict.direction` union adds `"unscored"`; `ImpactResponse` gains
  optional `scoring?` (so older archived runs without it still type-check); the
  `hop` stream event gains `recovered: number; unscored: number`.

---

## Section 3 — Frontend: render `unscored` distinctly

**`web/src/api.ts`:**
- `ImpactVerdict.direction` union → add `"unscored"`.
- `ImpactResponse` → add optional
  `scoring?: { scored: number; recovered: number; unscored: number; unscored_node_ids: string[] }`.
- `ImpactStreamEvent` `hop` variant → add `recovered: number; unscored: number`.

**`web/src/impact.ts`** (color single-source-of-truth):
- Add an `UNSCORED` color — a muted slate/grey-blue (proposal `rgb(124, 134, 150)`),
  clearly distinct from the vivid red/green/amber impact tiers *and* the near-black
  dim of non-impacted nodes ("reached, but unknown").
- In `tintColor()` **and** `tintColorRGB()`: check `verdict.direction === "unscored"`
  **first**, before the `no_effect`/low-magnitude early-return (an unscored verdict
  has `magnitude:0` and would otherwise be hidden). Return the `UNSCORED` color.
- `buildImpactState()` needs no change — unscored nodes are in `resp.impacts` and
  already enter `byNode`.

**`web/src/render3d.ts`:** no direct change — `nodeColor`/`nodeVisibility` closures
call `tintColor`, so unscored nodes appear on the globe automatically once
`impact.ts` returns a non-null `UNSCORED` color.

**Node sizing caveat:** the impacted-node size reducer must not shrink an unscored
node (magnitude 0) to invisibility. Verify the size logic (`main.ts` nodeReducer /
`render3d.ts`) and ensure unscored nodes render at standard impacted prominence.

**`web/src/main.ts`:** finalize status line appends the count when present, e.g.
`… → 94 nodes across 3 hops · 2 unscored`. The inspector already shows a verdict's
`reasoning`, so an unscored node shows its failure reason with no extra work. A
legend swatch for unscored is an optional nice-to-have.

---

## Section 4 — Testing

**Backend** (extend `tests/test_impact_stream.py`), deterministic graph-agnostic
fakes operating on whatever candidate ids are in the prompt:
- **Recovery:** stateful fake that omits the first candidate id of each ring prompt
  on first sighting, then includes it when re-asked. Assert that id ends up scored
  and `scoring["recovered"] >= 1`.
- **Unscored surfacing:** fake that always omits the first candidate id of every
  ring/retry prompt. Assert that id appears in `impacts` with `direction ==
  "unscored"`, is in `scoring["unscored_node_ids"]`, and `scoring["unscored"] >= 1`.
- **Coverage invariant:** with a fake that omits some ids, assert every candidate in
  a hop's ring gets *some* verdict — the set of hop-1 nodes in `impacts` equals the
  hop-1 ring candidate set; nothing vanishes.
- **Unscored is terminal:** an `unscored` node never appears as a `via_parent` of a
  later-hop node.
- **Scoring summary consistency:** `scored + unscored == ` total non-seed nodes;
  `scoring` has all four keys.
- **No regression:** `test_stream_reconciles_with_done` and
  `test_wrapper_equals_done_payload` still hold (unscored nodes appear in both `hop`
  events and `done.impacts`; the new `scoring` key rides along in both).

**Frontend** (new `web/src/__tests__/impact-tint.test.ts`):
- `tintColor({direction:"unscored", magnitude:0})` returns the `UNSCORED` color —
  **not `null`**, and not any positive/negative/mixed tier color.
- `tintColorRGB` likewise returns the unscored RGB, not null.
- `buildImpactState` includes an `unscored` node in `byNode`.

**Manual:** run a live trace, confirm the `scoring` summary surfaces (a clean run
shows `unscored: 0`). Forcing a real unscored node live is hard (needs the LLM to
fail twice), so the unscored path is covered by unit tests.

---

## Files touched

| File | Change |
|---|---|
| `api/impact.py` | `_format_candidate_line`, `_build_ring_prompt`, `_collect_verdicts` helpers; ensure-coverage retry loop; `unscored` verdicts; `scoring` summary; `recovered`/`unscored` on hop event |
| `tests/test_impact_stream.py` | recovery, unscored-surfacing, coverage-invariant, terminal, summary tests |
| `web/src/api.ts` | `"unscored"` direction; `scoring?` on `ImpactResponse`; `recovered`/`unscored` on hop event type |
| `web/src/impact.ts` | `UNSCORED` color; `tintColor`/`tintColorRGB` handle `"unscored"` first |
| `web/src/main.ts` | status-line unscored count; verify node-size reducer keeps unscored visible |
| `web/src/__tests__/impact-tint.test.ts` | tint tests for unscored |

No schema/DB/pipeline changes. `/impact`, `/impact/multi`, the streaming endpoint
contract (events), and the archive format are unchanged except for additive fields.
