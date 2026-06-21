# "So What?" Scoring Coverage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** No node silently vanishes from a "So What?" run — recover transient LLM scoring failures with targeted retries, and surface anything still unscorable as an explicit, visible `unscored` state with reported counts.

**Architecture:** In `api/impact.py`, replace the per-chunk verdict-application (which drops whole failed chunks and omitted ids) with an ensure-coverage loop: collect verdicts into a `{node_id → verdict}` map, re-ask only the missing ids up to N rounds, then iterate the *candidates* (not the verdicts) so every candidate gets either a real verdict or an explicit `unscored` one. Surface a `scoring` summary + per-hop counts. The frontend gains an `unscored` direction rendered in a distinct color.

**Tech Stack:** Python 3.11 / FastAPI / sqlite3 / pytest; TypeScript / Sigma.js / 3d-force-graph / Vitest.

**Spec:** `docs/superpowers/specs/2026-06-17-impact-scoring-coverage-design.md`
**Branch:** `feat/impact-scoring-coverage` (stacked on `feat/impact-streaming`).

**Operational note (project memory):** This repo lives in OneDrive — stale `__pycache__` can serve old bytecode. Run Python with `python -B`. Start servers detached, not as session-bound background tasks.

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `api/impact.py` | BFS engine | Extract prompt helpers; `_collect_verdicts`; ensure-coverage retry loop; `unscored` verdicts; `scoring` summary; hop-event counts |
| `tests/test_impact_stream.py` | Backend tests | recovery, unscored-surfacing, coverage-invariant, terminal, summary |
| `web/src/api.ts` | API client types | `"unscored"` direction; `scoring?`; hop-event `recovered`/`unscored` |
| `web/src/impact.ts` | Tint logic | `UNSCORED` color; `tintColor`/`tintColorRGB` handle `unscored` first |
| `web/src/main.ts` | Status line | append unscored count (no size change needed — see Task 5) |
| `web/src/__tests__/impact-tint.test.ts` | Frontend tint tests | new |

No schema/DB/pipeline changes. `/impact`, `/impact/multi`, the streaming event contract, and the archive format change only by additive fields.

---

## Task 1: Extract prompt-building helpers (behavior-preserving refactor)

**Files:**
- Modify: `api/impact.py` — add two helpers after `_RING_PROMPT_TEMPLATE` (ends ~line 743); rewrite the inline chunk-prompt build (~lines 1064-1094) to call them.

- [ ] **Step 1: Add the helpers**

Insert after the `_RING_PROMPT_TEMPLATE` definition (after its closing `"""`, before the frontier-sampling section):

```python
def _format_candidate_line(nb: dict[str, Any], impacts: dict[str, Any]) -> str:
    """One ring-candidate line for the scoring prompt. Mirrors the columns the
    _RING_PROMPT_TEMPLATE documents: id | type | name | sector | country |
    edge_geo | weight | parent | edge | parent_dir | parent_mag."""
    parent_v = impacts.get(nb["via_parent"], {})
    country = nb.get("country") or "-"
    geo = nb.get("supply_geography") or "?"
    ew = nb.get("edge_weight")
    et = nb.get("edge_source_tier") or ""
    weight_str = f"{ew * 100:.0f}%|{et or 'sec_explicit'}" if ew is not None else "est"
    parent_mag = parent_v.get("magnitude")
    parent_mag_str = f"{parent_mag:.2f}" if parent_mag is not None else "?"
    return (
        f"  {nb['id']} | {nb['type']} | {nb['name']} | "
        f"{nb.get('sector') or '-'} | country={country} | edge_geo={geo} | "
        f"weight={weight_str} | "
        f"parent={nb['via_parent']} | "
        f"edge={nb['edge_type']} | "
        f"parent_dir={parent_v.get('direction', '?')} | "
        f"parent_mag={parent_mag_str}"
    )


def _build_ring_prompt(news: str, seeds_block: str, hop: int,
                       ring: list[dict[str, Any]], impacts: dict[str, Any]) -> str:
    """Full scoring prompt for one chunk of ring candidates."""
    cand_lines = [_format_candidate_line(nb, impacts) for nb in ring]
    return _RING_PROMPT_TEMPLATE.format(
        news=news, seeds_block=seeds_block, hop_num=hop,
        candidates="\n".join(cand_lines),
    )
```

- [ ] **Step 2: Rewrite the inline chunk-prompt build to use the helper**

Replace the block that currently builds `chunk_prompts` (the `chunk_prompts: list[str] = []` loop, ~lines 1064-1094) with:

```python
            chunk_prompts = [_build_ring_prompt(text, seeds_block, hop, ring, impacts)
                             for ring in chunks]
```

- [ ] **Step 3: Run the suite — behavior must be unchanged**

Run: `python -B -m pytest tests/test_impact_stream.py -q`
Expected: PASS (5 tests). The prompt strings are byte-identical to before, so the deterministic fakes produce identical results.

- [ ] **Step 4: Commit**

```bash
git add api/impact.py
git commit -m "refactor(impact): extract _format_candidate_line / _build_ring_prompt"
```

---

## Task 2: Ensure-coverage retry loop + unscored state + scoring summary

**Files:**
- Modify: `api/impact.py` — add `RING_SCORE_RETRIES` const + `_collect_verdicts`; init `total_recovered`; replace the scoring+apply block; add `scoring` to the `done` result and the no_neighbors result.
- Test: `tests/test_impact_stream.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_impact_stream.py`:

```python
def _seed_and_entity(prompt):
    """Shared first-stage routing for the coverage fakes."""
    if "Extract ONLY investable companies" in prompt:
        return "[]"
    if "Pick the ONE node" in prompt:
        m = re.search(r"^\s*(\S+)\s*\|", prompt, re.MULTILINE)
        return json.dumps({"node_id": m.group(1) if m else None,
                           "direction": "negative", "magnitude": 0.9, "reasoning": "t"})
    if "refining impact assessments" in prompt:
        ids = re.findall(r"^NODE:\s*(\S+)\s*\(", prompt, re.MULTILINE)
        return json.dumps([{"node_id": i, "direction": "negative",
                            "magnitude": 0.9, "reasoning": "t"} for i in ids])
    return None


def _ring_ids(prompt):
    cand = prompt.split("CANDIDATES at hop", 1)[-1]
    return re.findall(r"^\s{2}(\S+)\s*\|", cand, re.MULTILINE)


def _make_recovery_fake():
    """Omit the first candidate of each ring prompt on its FIRST sighting,
    then include it when re-asked (proves targeted retry recovers gaps)."""
    omitted_once: set[str] = set()

    def fake(prompt):
        pre = _seed_and_entity(prompt)
        if pre is not None:
            return pre
        if "propagating a news shock" in prompt:
            ids = _ring_ids(prompt)
            out = []
            for idx, i in enumerate(ids):
                if idx == 0 and i not in omitted_once:
                    omitted_once.add(i)
                    continue
                out.append({"node_id": i, "direction": "negative",
                            "magnitude": 0.5, "reasoning": "t"})
            return json.dumps(out)
        return ""
    return fake


def _always_omit_first_fake(prompt):
    """Always omit the first candidate of every ring/retry prompt → that id can
    never be recovered and must end up `unscored`."""
    pre = _seed_and_entity(prompt)
    if pre is not None:
        return pre
    if "propagating a news shock" in prompt:
        ids = _ring_ids(prompt)
        return json.dumps([{"node_id": i, "direction": "negative",
                            "magnitude": 0.5, "reasoning": "t"} for i in ids[1:]])
    return ""


def test_retry_recovers_missing_nodes(conn, monkeypatch):
    monkeypatch.setattr(impact_mod, "_llm_call", _make_recovery_fake())
    monkeypatch.setattr(impact_mod, "MAX_FRONTIER", 9999)
    monkeypatch.setattr(impact_mod, "RING_SCORE_RETRIES", 1)
    done = next(e for e in impact_mod.run_impact_stream(
        "global crude oil supply shock", conn=conn) if e["event"] == "done")
    scoring = done["result"]["scoring"]
    assert scoring["recovered"] >= 1
    # Nothing left unscored — everything was recovered on retry.
    assert scoring["unscored"] == 0
    assert all(v["direction"] != "unscored"
               for v in done["result"]["impacts"] if not v.get("is_seed"))


def test_unrecoverable_nodes_surface_as_unscored(conn, monkeypatch):
    monkeypatch.setattr(impact_mod, "_llm_call", _always_omit_first_fake)
    monkeypatch.setattr(impact_mod, "MAX_FRONTIER", 9999)
    monkeypatch.setattr(impact_mod, "RING_SCORE_RETRIES", 1)
    events = list(impact_mod.run_impact_stream("global crude oil supply shock", conn=conn))
    done = next(e for e in events if e["event"] == "done")
    scoring = done["result"]["scoring"]
    assert scoring["unscored"] >= 1
    unscored = [v for v in done["result"]["impacts"] if v["direction"] == "unscored"]
    assert len(unscored) == scoring["unscored"]
    assert {v["node_id"] for v in unscored} == set(scoring["unscored_node_ids"])
    # Surfaced, not dropped: each carries the standard fields.
    assert all(u["magnitude"] == 0.0 and u["hop"] >= 1 for u in unscored)


def test_every_ring_candidate_is_accounted_for(conn, monkeypatch):
    # Coverage invariant: with omissions forced, each hop still yields one
    # impact per ring candidate (scored or unscored) — nothing vanishes.
    monkeypatch.setattr(impact_mod, "_llm_call", _always_omit_first_fake)
    monkeypatch.setattr(impact_mod, "MAX_FRONTIER", 9999)
    monkeypatch.setattr(impact_mod, "RING_SCORE_RETRIES", 1)
    for ev in impact_mod.run_impact_stream("global crude oil supply shock", conn=conn):
        if ev["event"] == "hop":
            assert len(ev["new_impacts"]) == ev["ring_size"]


def test_unscored_nodes_do_not_propagate(conn, monkeypatch):
    monkeypatch.setattr(impact_mod, "_llm_call", _always_omit_first_fake)
    monkeypatch.setattr(impact_mod, "MAX_FRONTIER", 9999)
    monkeypatch.setattr(impact_mod, "RING_SCORE_RETRIES", 1)
    done = next(e for e in impact_mod.run_impact_stream(
        "global crude oil supply shock", conn=conn) if e["event"] == "done")
    impacts = done["result"]["impacts"]
    unscored_ids = {v["node_id"] for v in impacts if v["direction"] == "unscored"}
    # No node was discovered via an unscored parent.
    assert all(v.get("via_parent") not in unscored_ids for v in impacts)


def test_scoring_summary_is_consistent(conn, monkeypatch):
    monkeypatch.setattr(impact_mod, "_llm_call", _always_omit_first_fake)
    monkeypatch.setattr(impact_mod, "MAX_FRONTIER", 9999)
    monkeypatch.setattr(impact_mod, "RING_SCORE_RETRIES", 1)
    done = next(e for e in impact_mod.run_impact_stream(
        "global crude oil supply shock", conn=conn) if e["event"] == "done")
    r = done["result"]
    scoring = r["scoring"]
    assert set(scoring) == {"scored", "recovered", "unscored", "unscored_node_ids"}
    non_seed = [v for v in r["impacts"] if not v.get("is_seed")]
    assert scoring["scored"] + scoring["unscored"] == len(non_seed)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -B -m pytest tests/test_impact_stream.py -k "recover or unscored or accounted or propagate or scoring_summary" -v`
Expected: FAIL — `KeyError: 'scoring'` / missing `recovered`/`unscored` keys / `direction == "unscored"` never set.

- [ ] **Step 3: Add the config constant and `_collect_verdicts`**

Add near the other config constants (after `REFINEMENT_BATCH_SIZE`, ~line 75):

```python
# How many extra rounds to re-ask the LLM for candidates that came back with
# no parseable verdict (a failed chunk OR an omitted id). Each round re-asks
# ONLY the still-missing ids. After these rounds, any remaining id is surfaced
# as an explicit `unscored` node rather than silently dropped.
RING_SCORE_RETRIES = int(os.environ.get("IMPACT_SCORE_RETRIES", "1"))
```

Add `_collect_verdicts` next to `_build_ring_prompt` (from Task 1):

```python
def _collect_verdicts(prompts: list[str]) -> dict[str, dict[str, Any]]:
    """Run scoring prompts in parallel and return {node_id: verdict} for every
    dict verdict carrying a node_id. Unparseable chunks and malformed verdicts
    simply don't populate the map — which is how the caller detects what to
    retry. Last writer wins on duplicate ids."""
    if not prompts:
        return {}
    workers = min(RING_PARALLELISM, len(prompts))
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            raws = list(pool.map(_llm_call, prompts))
    except Exception as exc:  # _ollama_call can raise on network failure
        log.warning("_collect_verdicts: LLM pool raised %s", exc)
        raws = [""] * len(prompts)
    out: dict[str, dict[str, Any]] = {}
    for raw in raws:
        parsed = _parse_llm_json(raw)
        if isinstance(parsed, dict) and "results" in parsed:
            parsed = parsed.get("results")
        if not isinstance(parsed, list):
            continue
        for verdict in parsed:
            if isinstance(verdict, dict) and verdict.get("node_id"):
                out[verdict["node_id"]] = verdict
    return out
```

- [ ] **Step 4: Initialize the recovered accumulator before the hop loop**

Immediately before `for hop in range(1, MAX_HOPS + 1):`, add:

```python
        total_recovered = 0   # nodes filled by retry across all hops (for `scoring`)
```

- [ ] **Step 5: Replace the scoring + apply block**

Replace everything from the `chunk_prompts = [...]` build through the end of the verdict-application `for` loop and the `if chunk_failed ...` break logic (the region spanning roughly the old lines 1058-1187, i.e. from the chunk-prompt construction down to and including `if chunk_failed and not new_frontier: break`) with:

```python
            chunk_prompts = [_build_ring_prompt(text, seeds_block, hop, ring, impacts)
                             for ring in chunks]
            debug_log.append(
                f"hop {hop}: scoring {len(full_ring)} candidates in {len(chunks)} chunk(s)"
            )

            t_hop = time.time()
            verdict_by_id = _collect_verdicts(chunk_prompts)
            first_pass_ids = set(verdict_by_id)

            # Ensure-coverage: re-ask ONLY the still-missing ids, up to N rounds.
            attempts = 0
            while attempts < RING_SCORE_RETRIES:
                missing = [nb for nb in full_ring if nb["id"] not in verdict_by_id]
                if not missing:
                    break
                attempts += 1
                retry_prompts = [
                    _build_ring_prompt(text, seeds_block, hop,
                                       missing[i:i + MAX_RING_CANDIDATES], impacts)
                    for i in range(0, len(missing), MAX_RING_CANDIDATES)
                ]
                before = len(verdict_by_id)
                verdict_by_id.update(_collect_verdicts(retry_prompts))
                debug_log.append(
                    f"hop {hop}: retry {attempts} — {len(missing)} missing, "
                    f"{len(verdict_by_id) - before} recovered"
                )
            debug_log.append(f"hop {hop}: scoring done in {time.time() - t_hop:.1f}s")

            ring_ids = {nb["id"] for nb in full_ring}
            recovered_this_hop = len((set(verdict_by_id) & ring_ids) - first_pass_ids)
            total_recovered += recovered_this_hop

            # Apply by iterating CANDIDATES (not verdicts): every candidate gets a
            # verdict or an explicit `unscored` marker — nothing is dropped.
            new_frontier: list[str] = []
            hop_new_ids: list[str] = []
            unscored_this_hop = 0
            for nb in full_ring:
                nid = nb["id"]
                if nid in impacts:
                    continue
                verdict = verdict_by_id.get(nid)
                if not isinstance(verdict, dict):
                    impacts[nid] = {
                        "node_id": nid,
                        "name": nb["name"],
                        "type": nb["type"],
                        "direction": "unscored",
                        "magnitude": 0.0,
                        "hop": hop,
                        "reasoning": f"Could not be scored after {RING_SCORE_RETRIES + 1} attempts",
                        "via_parent": nb["via_parent"],
                        "edge_type": nb["edge_type"],
                        "country": nb.get("country"),
                        "edge_weight": nb.get("edge_weight"),
                        "edge_source_tier": nb.get("edge_source_tier"),
                        "is_estimated": nb.get("edge_weight") is None,
                    }
                    visited.add(nid)
                    hop_new_ids.append(nid)
                    unscored_this_hop += 1
                    continue
                direction = verdict.get("direction") or "no_effect"
                magnitude = float(verdict.get("magnitude") or 0.0)
                reasoning = verdict.get("reasoning") or ""
                _ew = nb.get("edge_weight")
                impacts[nid] = {
                    "node_id": nid,
                    "name": nb["name"],
                    "type": nb["type"],
                    "direction": direction,
                    "magnitude": magnitude,
                    "hop": hop,
                    "reasoning": reasoning,
                    "via_parent": nb["via_parent"],
                    "edge_type": nb["edge_type"],
                    "country": nb.get("country"),
                    "edge_weight": _ew,
                    "edge_source_tier": nb.get("edge_source_tier"),
                    "is_estimated": _ew is None,
                }
                hop_new_ids.append(nid)
                visited.add(nid)
                if direction in ("positive", "negative") and magnitude >= 0.15:
                    new_frontier.append(nid)

            # === emit the hop event ===
            # frontier_size = the input frontier scored into this hop.
            # ring_size = candidates actually scored (post-cap).
            # recovered = ids filled by retry; unscored = ids surfaced unscorable.
            yield {
                "event": "hop",
                "hop": hop,
                "new_impacts": [impacts[nid] for nid in hop_new_ids],
                "frontier_size": len(frontier),
                "ring_size": len(full_ring),
                "sampled": sampled_flag,
                "recovered": recovered_this_hop,
                "unscored": unscored_this_hop,
            }

            if not new_frontier:
                break
            frontier = new_frontier
```

Notes for the implementer:
- This DELETES the old `chunk_raws`/`pool.map` block, the `chunk_failed` flag, the per-chunk parse loop, the old hop-event yield, and the `if chunk_failed and not new_frontier: break`. The new code supersedes all of it. Keep the surrounding frontier-cap/`sampled_flag`/`chunks` code above it (Task 1 left it intact) and the refinement pass below it unchanged.
- `sampled_flag`, `frontier`, `visited`, `impacts`, `seeds_block`, `full_ring`, `chunks` all already exist in scope.

- [ ] **Step 6: Add the `scoring` summary to the `done` result**

Find where the final `result` dict is built (the success path, with keys `seed`, `seeds`, `impacts`, `provider`, `model`, `max_hops`, `debug`, `refinement`). Immediately before it, compute:

```python
        _non_seed = [v for v in impacts.values() if not v.get("is_seed")]
        _unscored_ids = [v["node_id"] for v in _non_seed if v.get("direction") == "unscored"]
        scoring_summary = {
            "scored": len(_non_seed) - len(_unscored_ids),
            "recovered": total_recovered,
            "unscored": len(_unscored_ids),
            "unscored_node_ids": _unscored_ids,
        }
```

and add `"scoring": scoring_summary,` to the `result` dict.

Also add a zero summary to the no_neighbors early-return result (the dict that carries `"no_neighbors": True`):

```python
                    "scoring": {"scored": 0, "recovered": 0, "unscored": 0, "unscored_node_ids": []},
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `python -B -m pytest tests/test_impact_stream.py -v`
Expected: PASS (all — the 5 original + 5 new). The original `test_stream_reconciles_with_done` and `test_wrapper_equals_done_payload` still hold (unscored nodes appear in both `hop` events and `done.impacts`; the `scoring` key rides along).

- [ ] **Step 8: Run the full backend suite (no new regressions)**

Run: `python -B -m pytest tests/ -q`
Expected: same as baseline plus the new tests — only the pre-existing `test_api.py::test_walmart_customer_of_derivation_matches_stored_supplies` fails (unrelated, fails on clean baseline).

- [ ] **Step 9: Commit**

```bash
git add api/impact.py tests/test_impact_stream.py
git commit -m "feat(impact): ensure-coverage retry loop; surface unscored nodes + scoring summary"
```

---

## Task 3: Frontend types (`web/src/api.ts`)

**Files:**
- Modify: `web/src/api.ts` — `ImpactVerdict.direction`, `ImpactResponse`, `ImpactStreamEvent` hop variant.

- [ ] **Step 1: Extend the direction union**

In `ImpactVerdict` (line ~231), change:
```ts
  direction: "positive" | "negative" | "no_effect";
```
to:
```ts
  direction: "positive" | "negative" | "no_effect" | "unscored";
```
Also update the `ImpactEventVerdict.direction` (line ~221) the same way for consistency:
```ts
  direction: "positive" | "negative" | "no_effect" | "unscored";
```

- [ ] **Step 2: Add `scoring` to `ImpactResponse`**

In `ImpactResponse` (ends ~line 260), add before the closing brace:
```ts
  /** Coverage accounting (stream 2): how many ring candidates were scored,
   *  recovered via retry, or left unscorable and surfaced as `unscored`. */
  scoring?: {
    scored: number;
    recovered: number;
    unscored: number;
    unscored_node_ids: string[];
  };
```

- [ ] **Step 3: Add counts to the hop stream event**

In `ImpactStreamEvent` (the `hop` variant, ~line 390), add the two fields:
```ts
  | { event: "hop"; hop: number; new_impacts: ImpactVerdict[]; frontier_size: number; ring_size: number; sampled: boolean; recovered: number; unscored: number }
```

- [ ] **Step 4: Type-check**

Run: `cd web && npx tsc --noEmit`
Expected: no errors.

- [ ] **Step 5: Commit**

```bash
git add web/src/api.ts
git commit -m "feat(web): unscored direction + scoring fields in impact types"
```

---

## Task 4: Frontend tint for `unscored` (`web/src/impact.ts`)

**Files:**
- Modify: `web/src/impact.ts` — add `UNSCORED` color; handle `unscored` first in `tintColor` and `tintColorRGB`.
- Test: `web/src/__tests__/impact-tint.test.ts` (create)

- [ ] **Step 1: Write the failing test**

Create `web/src/__tests__/impact-tint.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import { tintColor, tintColorRGB, buildImpactState } from "../impact";
import type { ImpactVerdict } from "../api";

function v(partial: Partial<ImpactVerdict>): ImpactVerdict {
  return {
    node_id: "x", name: "X", type: "Company",
    direction: "no_effect", magnitude: 0, hop: 1,
    reasoning: "", via_parent: null, edge_type: null, ...partial,
  } as ImpactVerdict;
}

describe("unscored tint", () => {
  it("tintColor returns a non-null, distinct colour for unscored (despite magnitude 0)", () => {
    const c = tintColor(v({ direction: "unscored", magnitude: 0 }));
    expect(c).not.toBeNull();
    // Not a positive (green) or negative (red) tier colour.
    expect(c).not.toContain("0, 224");   // #00e0.. positive high
    expect(c).not.toContain("255, 51");  // #ff33.. negative high
  });

  it("no_effect stays hidden (null)", () => {
    expect(tintColor(v({ direction: "no_effect", magnitude: 0 }))).toBeNull();
  });

  it("tintColorRGB returns rgb for unscored, null for no_effect", () => {
    expect(tintColorRGB(v({ direction: "unscored", magnitude: 0 }))).not.toBeNull();
    expect(tintColorRGB(v({ direction: "no_effect", magnitude: 0 }))).toBeNull();
  });
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd web && npm run test -- impact-tint`
Expected: FAIL — unscored currently hits the `no_effect`/low-magnitude branch and returns null.

- [ ] **Step 3: Add the UNSCORED color and handle it first**

In `web/src/impact.ts`, add near `COLOR_NEUTRAL` (line ~49):
```ts
// Unscored: the engine reached this node but could not get a verdict for it
// (stream 2). Distinct from both the red/green impact tiers and the near-black
// dim of non-impacted nodes — a muted slate that reads as "reached, unknown".
const COLOR_UNSCORED = { r: 124, g: 134, b: 150 };
```

In `tintColor` (line ~85), add as the FIRST check after the `if (!verdict) return null;` guard, before the magnitude/no_effect logic:
```ts
  if (verdict.direction === "unscored") {
    const c = COLOR_UNSCORED;
    return `rgb(${c.r}, ${c.g}, ${c.b})`;
  }
```

In `tintColorRGB` (line ~103), add the same first check after `if (!verdict) return null;`:
```ts
  if (verdict.direction === "unscored") {
    return { r: COLOR_UNSCORED.r / 255, g: COLOR_UNSCORED.g / 255, b: COLOR_UNSCORED.b / 255 };
  }
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd web && npm run test -- impact-tint`
Expected: PASS (3 tests).

- [ ] **Step 5: Type-check + full web tests**

Run: `cd web && npx tsc --noEmit && npm run test`
Expected: tsc clean; all web tests pass.

- [ ] **Step 6: Commit**

```bash
git add web/src/impact.ts web/src/__tests__/impact-tint.test.ts
git commit -m "feat(web): render unscored nodes in a distinct muted colour"
```

---

## Task 5: Status-line unscored count (`web/src/main.ts`)

**Files:**
- Modify: `web/src/main.ts` — append the unscored count to the finalize status line.

No node-size change is needed: the nodeReducer sets `isImpacted = tintColor(verdict) !== null` and `size = isImpacted ? baseSize * 1.8 : baseSize * 0.45` (size is NOT magnitude-scaled), so once Task 4 makes `tintColor` return non-null for `unscored`, those nodes are automatically visible at full impacted size. Verified at `web/src/main.ts:294-300`.

- [ ] **Step 1: Append the unscored count to the success status**

In `handleImpactRun`, find the single-event finalize status line:
```ts
      setImpactStatus(
        `[${niceProvider}] Seeds: ${seedNames} → ${resp.impacts.length} nodes across ${resp.max_hops || 3} hops`,
      );
```
Replace with:
```ts
      const unscoredNote = resp.scoring && resp.scoring.unscored > 0
        ? ` · ${resp.scoring.unscored} unscored`
        : "";
      setImpactStatus(
        `[${niceProvider}] Seeds: ${seedNames} → ${resp.impacts.length} nodes across ${resp.max_hops || 3} hops${unscoredNote}`,
      );
```

- [ ] **Step 2: Type-check + tests**

Run: `cd web && npx tsc --noEmit && npm run test`
Expected: tsc clean; all web tests pass.

- [ ] **Step 3: Commit**

```bash
git add web/src/main.ts
git commit -m "feat(web): show unscored count in impact status line"
```

---

## Task 6: End-to-end verification

**Files:** none (verification only).

- [ ] **Step 1: Start the backend detached**

```powershell
Start-Process -WindowStyle Hidden python -ArgumentList "-B","-m","uvicorn","api.main:app","--host","127.0.0.1","--port","8101"
```
Confirm `curl http://127.0.0.1:8101/health` returns `{"status":"ok",...}`.

- [ ] **Step 2: Run a live streaming trace and confirm the scoring summary**

```bash
curl -sN --no-buffer -X POST http://127.0.0.1:8101/impact/stream \
  -H "Content-Type: application/json" \
  -d '{"text":"Russia suspends Black Sea grain exports","provider":"claude"}' \
  | python -u -c "import sys,json
for ln in sys.stdin:
    ln=ln.strip()
    if not ln: continue
    ev=json.loads(ln)
    if ev['event']=='hop': print('hop',ev['hop'],'+%d'%len(ev['new_impacts']),'recovered',ev.get('recovered'),'unscored',ev.get('unscored'))
    if ev['event']=='done': print('SCORING', ev['result'].get('scoring'))"
```
Expected: per-hop lines show `recovered`/`unscored` counts; the final `SCORING` line shows `{scored: N, recovered: ..., unscored: ..., unscored_node_ids: [...]}`. On a healthy run `unscored` is typically 0; any unscored nodes are present in the trace, not missing.

- [ ] **Step 3: Confirm `/impact` (non-streaming) carries the same summary**

```bash
curl -s -X POST http://127.0.0.1:8101/impact -H "Content-Type: application/json" \
  -d '{"text":"Russia suspends Black Sea grain exports"}' \
  | python -c "import sys,json; print(json.load(sys.stdin).get('scoring'))"
```
Expected: a `scoring` dict (same shape), confirming the wrapper path carries it.

- [ ] **Step 4: Stop the backend**

```bash
PID=$(netstat -ano | grep ':8101' | grep LISTENING | awk '{print $5}' | head -1); taskkill //F //PID "$PID"
```

---

## Self-Review

**Spec coverage:**
- Backend ensure-coverage loop + unscored + retries → Task 2. ✓
- Prompt-helper extraction → Task 1. ✓
- `scoring` summary + hop-event counts → Task 2 (Steps 5-6). ✓
- `unscored` direction + `scoring?` + hop event fields (TS) → Task 3. ✓
- Distinct `unscored` tint (2D + globe via shared tintColor) → Task 4. ✓
- Node-size visibility caveat → resolved by Task 4 (documented in Task 5; no code change). ✓
- Status-line count → Task 5. ✓
- Backend tests (recovery, unscored, coverage, terminal, summary) → Task 2 Step 1. ✓
- Frontend tint tests → Task 4 Step 1. ✓
- Refinement pass out of scope → not touched. ✓
- E2E + non-streaming parity → Task 6. ✓

**Placeholder scan:** No TBD/TODO. All code shown in full; the one "replace region X-Y" (Task 2 Step 5) names exact anchors and shows the complete replacement. ✓

**Type/name consistency:** `_format_candidate_line`, `_build_ring_prompt`, `_collect_verdicts`, `RING_SCORE_RETRIES`, `total_recovered`, `recovered_this_hop`, `unscored_this_hop`, `scoring_summary`, the event keys `recovered`/`unscored`, the `"unscored"` direction, and `scoring`/`unscored_node_ids` are used consistently across backend tasks and mirrored in the TS types (Task 3) and consumers (Tasks 4-5). ✓
