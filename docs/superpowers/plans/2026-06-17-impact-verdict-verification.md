# Verdict Verification Implementation Plan (stream 2.2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Add an adversarial verification pass that tries to refute high-impact verdicts and downgrades the unsupported ones (`refuted → no_effect`, `weakened → magnitude × 0.5`), attaching a verifier `confidence`.

**Architecture:** A new `_verification_pass` in `api/impact.py`, mirroring `_refinement_pass` (reuses `_build_refine_node_block` + the impacted-neighbours query), runs after refinement and before `done`. It only downgrades/annotates, never upgrades. Surfaced via a new `verification` stream event + `done.result.verification` summary; frontend merges the event like `refinement`.

**Tech Stack:** Python 3.11 / sqlite3 / pytest; TypeScript / Vitest.

**Spec:** `docs/superpowers/specs/2026-06-17-impact-verdict-verification-design.md`. **Branch:** `feat/impact-verdict-verification`.

**Operational note:** OneDrive → run Python with `python -B`; servers detached.

---

## Task 1: Backend verification pass

**Files:** Modify `api/impact.py`; test `tests/test_impact_stream.py`.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_impact_stream.py`:

```python
def _verify_fake(decision: str):
    """Wrap _fake_llm: answer the verify prompt with `decision` for the first
    NODE and 'upheld' for the rest; delegate seeds/ring/refine to _fake_llm."""
    def fake(prompt: str) -> str:
        if "TRY TO REFUTE" in prompt:
            ids = re.findall(r"^NODE:\s*(\S+)\s*\(", prompt, re.MULTILINE)
            out = [{"node_id": i, "verdict": (decision if idx == 0 else "upheld"),
                    "confidence": 0.8, "reasoning": "t"} for idx, i in enumerate(ids)]
            return json.dumps(out)
        return _fake_llm(prompt)
    return fake


def _run_done(conn):
    return next(e for e in impact_mod.run_impact_stream(
        "global crude oil supply shock", conn=conn) if e["event"] == "done")["result"]


def test_refute_downgrades_to_no_effect(conn, monkeypatch):
    monkeypatch.setattr(impact_mod, "_llm_call", _verify_fake("refuted"))
    monkeypatch.setattr(impact_mod, "MAX_FRONTIER", 9999)
    monkeypatch.setattr(impact_mod, "VERIFY_MAG_THRESHOLD", 0.45)
    r = _run_done(conn)
    refuted = [v for v in r["impacts"] if v.get("verification", {}).get("verdict") == "refuted"]
    assert refuted, "expected at least one refuted node"
    for v in refuted:
        assert v["direction"] == "no_effect" and v["magnitude"] == 0.0
        assert v["verified"] is True
    assert r["verification"]["refuted"] >= 1


def test_weaken_halves_magnitude(conn, monkeypatch):
    monkeypatch.setattr(impact_mod, "_llm_call", _verify_fake("weakened"))
    monkeypatch.setattr(impact_mod, "MAX_FRONTIER", 9999)
    monkeypatch.setattr(impact_mod, "VERIFY_MAG_THRESHOLD", 0.45)
    r = _run_done(conn)
    weakened = [v for v in r["impacts"] if v.get("verification", {}).get("verdict") == "weakened"]
    assert weakened
    # Ring fake scores candidates at magnitude 0.5 → weakened halves to 0.25.
    for v in weakened:
        assert abs(v["magnitude"] - 0.25) < 1e-9
    assert r["verification"]["weakened"] >= 1


def test_upheld_unchanged_but_annotated(conn, monkeypatch):
    monkeypatch.setattr(impact_mod, "_llm_call", _verify_fake("upheld"))
    monkeypatch.setattr(impact_mod, "MAX_FRONTIER", 9999)
    monkeypatch.setattr(impact_mod, "VERIFY_MAG_THRESHOLD", 0.45)
    r = _run_done(conn)
    upheld = [v for v in r["impacts"] if v.get("verification", {}).get("verdict") == "upheld"]
    assert upheld
    for v in upheld:
        assert v["direction"] in ("positive", "negative")  # unchanged
        assert v["verified"] is True and 0.0 <= v["confidence"] <= 1.0


def test_only_strong_verdicts_checked(conn, monkeypatch):
    monkeypatch.setattr(impact_mod, "_llm_call", _verify_fake("refuted"))
    monkeypatch.setattr(impact_mod, "MAX_FRONTIER", 9999)
    monkeypatch.setattr(impact_mod, "VERIFY_MAG_THRESHOLD", 0.99)  # nothing qualifies
    r = _run_done(conn)
    assert r["verification"] == {"checked": 0, "upheld": 0, "weakened": 0, "refuted": 0}
    assert not any(v.get("verified") for v in r["impacts"])


def test_verify_fail_open_leaves_verdicts_unchanged(conn, monkeypatch):
    # autouse _fake_llm returns "" for the verify prompt → no adjudication.
    monkeypatch.setattr(impact_mod, "MAX_FRONTIER", 9999)
    monkeypatch.setattr(impact_mod, "VERIFY_MAG_THRESHOLD", 0.45)
    r = _run_done(conn)
    assert r["verification"]["checked"] == 0
    assert not any(v.get("verified") for v in r["impacts"])
    # Strong verdicts retained (not downgraded by an unparseable verifier).
    assert any(v["direction"] in ("positive", "negative") and v["magnitude"] >= 0.45
               for v in r["impacts"] if not v.get("is_seed"))


def test_verification_disabled(conn, monkeypatch):
    monkeypatch.setattr(impact_mod, "_llm_call", _verify_fake("refuted"))
    monkeypatch.setattr(impact_mod, "MAX_FRONTIER", 9999)
    monkeypatch.setattr(impact_mod, "VERIFY_ENABLED", False)
    events = list(impact_mod.run_impact_stream("global crude oil supply shock", conn=conn))
    vevent = next(e for e in events if e["event"] == "verification")
    assert vevent["summary"] == {"checked": 0, "upheld": 0, "weakened": 0, "refuted": 0}
    done = next(e for e in events if e["event"] == "done")["result"]
    assert not any(v.get("verified") for v in done["impacts"])


def test_event_ordering_includes_verification(conn):
    kinds = [e["event"] for e in impact_mod.run_impact_stream(
        "global crude oil supply shock", conn=conn)]
    assert kinds[0] == "seeds" and kinds[-1] == "done"
    assert "verification" in kinds
    # verification comes after the (single) refinement event, before done.
    assert kinds.index("verification") > kinds.index("refinement")
    assert kinds.index("verification") < kinds.index("done")
```

- [ ] **Step 2: Run — verify they fail**

Run: `python -B -m pytest tests/test_impact_stream.py -k "refute or weaken or upheld or strong or fail_open or disabled or ordering_includes" -v`
Expected: FAIL (no `verification` event/summary; `VERIFY_*` attrs missing).

- [ ] **Step 3: Add config + the adversarial prompt template**

In `api/impact.py`, after the `REFINEMENT_*`/`_REFINE_BATCH_PROMPT_TEMPLATE` block, add:

```python
# --- Verdict verification (stream 2.2): adversarial refutation of strong verdicts ---
VERIFY_ENABLED = os.environ.get("IMPACT_VERIFY", "1") not in ("0", "false", "False", "")
VERIFY_MAG_THRESHOLD = float(os.environ.get("IMPACT_VERIFY_MAG", "0.45"))
VERIFY_MAX_NODES = int(os.environ.get("IMPACT_VERIFY_MAX", "24"))
VERIFY_BATCH_SIZE = int(os.environ.get("IMPACT_VERIFY_BATCH", "6"))

_VERIFY_BATCH_PROMPT_TEMPLATE = """You are a SKEPTICAL economist stress-testing impact claims. For EACH node below,
TRY TO REFUTE the claimed impact of this news event on that node.

NEWS:
\"\"\"
{news}
\"\"\"

SEEDS (the original shocks, hop 0):
{seeds_block}

Each node shows its claimed verdict and the impacted neighbours linking it to the
shock. Judge ONLY whether the claimed direction is defensible:
  "upheld"   = a concrete, plausible causal path exists from the shock to this node
               in the claimed direction.
  "weakened" = a real but OVERSTATED effect — the path is indirect, partial, or small
               relative to the claimed magnitude.
  "refuted"  = speculative, geographically implausible, double-counted, or no concrete
               mechanism. Default to this when in doubt.

Be adversarial — do not rubber-stamp. A confident-sounding verdict with no concrete
mechanism is "refuted".

{node_blocks}

Respond with STRICT JSON only -- a single array, one object per node_id in the SAME
ORDER as above. Keep each reasoning under 20 words.

[
  {{"node_id": "<id>", "verdict": "upheld" | "weakened" | "refuted", "confidence": 0.0 to 1.0, "reasoning": "<short>"}}
]

Cover every node_id exactly once.
"""
```

- [ ] **Step 4: Add `_verification_pass`** — after `_refinement_pass` (end of that function), add:

```python
def _verification_pass(
    *,
    text: str,
    impacts: dict[str, dict[str, Any]],
    seeds_block: str,
    conn: sqlite3.Connection,
    debug_log: list[str],
) -> dict[str, Any]:
    """Adversarially refute high-impact verdicts; downgrade those that don't hold.
    Mirrors _refinement_pass. Only downgrades/annotates — never upgrades. Fail-open:
    a verdict the verifier didn't adjudicate is left unchanged."""
    eligible = [
        (nid, float(v.get("magnitude", 0.0)))
        for nid, v in impacts.items()
        if not v.get("is_seed")
        and v.get("direction") in ("positive", "negative")
        and float(v.get("magnitude", 0.0)) >= VERIFY_MAG_THRESHOLD
    ]
    eligible.sort(key=lambda x: -x[1])
    eligible = eligible[:VERIFY_MAX_NODES]
    debug_log.append(f"verify: {len(eligible)} eligible (mag >= {VERIFY_MAG_THRESHOLD})")
    if not eligible:
        return {"checked": 0, "upheld": 0, "weakened": 0, "refuted": 0}

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
    neighbours: dict[str, list[tuple[str, str]]] = {}
    for src, tgt, etype in edge_rows:
        neighbours.setdefault(src, []).append((tgt, etype))
        neighbours.setdefault(tgt, []).append((src, etype))

    node_blocks_list: list[tuple[str, str, dict]] = []
    for nid, _mag in eligible:
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
        nb_lines.sort(key=lambda x: -x[0])
        node_row = conn.execute(
            "SELECT sector, country FROM nodes WHERE id = ?", (nid,)
        ).fetchone()
        node_sector = (node_row["sector"] if node_row else None) or v.get("type", "")
        node_country = (node_row["country"] if node_row else None) or v.get("country") or "-"
        block = _build_refine_node_block(nid, v, node_sector, node_country, nb_lines)
        node_blocks_list.append((nid, block, v))

    batches = [
        node_blocks_list[i:i + VERIFY_BATCH_SIZE]
        for i in range(0, len(node_blocks_list), VERIFY_BATCH_SIZE)
    ]
    prompts = [
        _VERIFY_BATCH_PROMPT_TEMPLATE.format(
            news=text, seeds_block=seeds_block,
            node_blocks="\n\n".join(b for _, b, _ in batch),
        )
        for batch in batches
    ]
    workers = min(RING_PARALLELISM, len(prompts))
    t0 = time.time()
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            raws = list(pool.map(_llm_call, prompts))
    except Exception as exc:
        log.warning("verify: LLM pool raised %s — skipping verification", exc)
        debug_log.append(f"verify: LLM pool error: {exc}")
        return {"checked": 0, "upheld": 0, "weakened": 0, "refuted": 0, "error": str(exc)}
    debug_log.append(f"verify: {len(prompts)} batch calls in {time.time() - t0:.1f}s")

    verdict_by_nid: dict[str, dict] = {}
    for raw in raws:
        parsed = _parse_llm_json(raw)
        if isinstance(parsed, dict) and "results" in parsed:
            parsed = parsed["results"]
        if not isinstance(parsed, list):
            continue
        for vd in parsed:
            if isinstance(vd, dict) and vd.get("node_id"):
                verdict_by_nid[vd["node_id"]] = vd

    counts = {"checked": 0, "upheld": 0, "weakened": 0, "refuted": 0}
    for nid, _block, _prev in node_blocks_list:
        vd = verdict_by_nid.get(nid)
        if not isinstance(vd, dict):
            continue  # fail-open: leave an unadjudicated verdict unchanged
        verdict = vd.get("verdict")
        if verdict not in ("upheld", "weakened", "refuted"):
            continue
        try:
            conf = max(0.0, min(1.0, float(vd.get("confidence", 0.5))))
        except (TypeError, ValueError):
            conf = 0.5
        reasoning = (vd.get("reasoning") or "")[:200]
        counts["checked"] += 1
        counts[verdict] += 1
        impacts[nid]["verified"] = True
        impacts[nid]["confidence"] = conf
        impacts[nid]["verification"] = {"verdict": verdict, "confidence": conf, "reasoning": reasoning}
        if verdict == "refuted":
            impacts[nid]["direction"] = "no_effect"
            impacts[nid]["magnitude"] = 0.0
        elif verdict == "weakened":
            impacts[nid]["magnitude"] = float(impacts[nid].get("magnitude", 0.0)) * 0.5
    debug_log.append(
        f"verify: checked={counts['checked']} upheld={counts['upheld']} "
        f"weakened={counts['weakened']} refuted={counts['refuted']}"
    )
    return counts
```

- [ ] **Step 5: Integrate into `run_impact_stream`** — after the `refinement` event yield (the block ending `"summary": refinement_summary,` + `}`), and before the `# === Done ===` section, insert:

```python
        verification_summary = (
            _verification_pass(text=text, impacts=impacts, seeds_block=seeds_block,
                               conn=conn, debug_log=debug_log)
            if VERIFY_ENABLED
            else {"checked": 0, "upheld": 0, "weakened": 0, "refuted": 0}
        )
        yield {
            "event": "verification",
            "updated": [v for v in impacts.values() if v.get("verified")],
            "summary": verification_summary,
        }
```

And add `"verification": verification_summary,` to the `done` `result` dict (next to `"scoring": scoring_summary,`).

- [ ] **Step 6: Run — verify pass**

Run: `python -B -m pytest tests/test_impact_stream.py -v`
Expected: PASS (all — prior + 7 new). Then full suite: `python -B -m pytest tests/ -q` (only the pre-existing Walmart failure remains).

- [ ] **Step 7: Commit**

```bash
git add api/impact.py tests/test_impact_stream.py
git commit -m "feat(impact): adversarial verdict-verification pass (refute/weaken/uphold)"
```

---

## Task 2: Frontend types + event handling

**Files:** Modify `web/src/api.ts`, `web/src/main.ts`; test `web/src/__tests__/impact-stream.test.ts`.

- [ ] **Step 1: Write the failing frontend test** — append to `web/src/__tests__/impact-stream.test.ts` a test using the existing `readerFrom` helper:

```ts
  it("applies a verification event that downgrades a node, then resolves on done", async () => {
    const chunks = [
      '{"event":"seeds","seeds":[{"node_id":"a"}],"primary_seed_id":"a"}\n',
      '{"event":"hop","hop":1,"new_impacts":[{"node_id":"b","direction":"positive","magnitude":0.8}],"frontier_size":1,"ring_size":1,"sampled":false,"recovered":0,"unscored":0}\n',
      '{"event":"refinement","updated":[],"summary":{}}\n',
      '{"event":"verification","updated":[{"node_id":"b","direction":"no_effect","magnitude":0,"verified":true,"verification":{"verdict":"refuted","confidence":0.8,"reasoning":"x"}}],"summary":{"checked":1,"upheld":0,"weakened":0,"refuted":1}}\n',
      '{"event":"done","result":{"impacts":[{"node_id":"a"},{"node_id":"b"}],"verification":{"checked":1,"upheld":0,"weakened":0,"refuted":1}}}\n',
    ];
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(readerFrom(chunks)));
    const seen: string[] = [];
    const result = await runImpactStream("oil", { onEvent: (e) => seen.push(e.event) });
    expect(seen).toEqual(["seeds", "hop", "refinement", "verification", "done"]);
    expect(result.verification?.refuted).toBe(1);
  });
```

- [ ] **Step 2: Run — fails** (`verification` not in the `ImpactStreamEvent` union → type error / `result.verification` unknown).
Run: `cd web && npm run test -- impact-stream`

- [ ] **Step 3: Extend the types in `web/src/api.ts`**

Add to `ImpactVerdict` (after `is_estimated?`):
```ts
  /** Stream 2.2: present once the adversarial verifier has adjudicated this verdict. */
  verified?: boolean;
  confidence?: number;
  verification?: { verdict: "upheld" | "weakened" | "refuted"; confidence: number; reasoning: string };
```
Add to `ImpactResponse`:
```ts
  verification?: { checked: number; upheld: number; weakened: number; refuted: number };
```
Add a `verification` variant to `ImpactStreamEvent`:
```ts
  | { event: "verification"; updated: ImpactVerdict[]; summary: { checked: number; upheld: number; weakened: number; refuted: number } }
```

- [ ] **Step 4: Handle the event in `web/src/main.ts`**

In `handleImpactRun`'s streaming `onEvent`, add a branch alongside `refinement`:
```ts
            } else if (ev.event === "verification") {
              ev.updated.forEach((v) => acc.set(v.node_id, v));
              reapply(false);
```
And in the finalize status line, extend the note (next to `unscoredNote`):
```ts
      const refutedNote = resp.verification && resp.verification.refuted > 0
        ? ` · ${resp.verification.refuted} refuted`
        : "";
```
and append `${refutedNote}` to the `setImpactStatus(...)` template.

- [ ] **Step 5: Run — pass + type-check**

Run: `cd web && npm run test && npx tsc --noEmit`
Expected: all green; tsc clean.

- [ ] **Step 6: Commit**

```bash
git add web/src/api.ts web/src/main.ts web/src/__tests__/impact-stream.test.ts
git commit -m "feat(web): handle verification stream event + refuted count in status"
```

---

## Task 3: End-to-end verification

**Files:** none.

- [ ] **Step 1:** Start backend detached on port 8103 (`python -B -m uvicorn api.main:app --host 127.0.0.1 --port 8103`); confirm `/health`.
- [ ] **Step 2:** Run a live `/impact/stream` trace for "Russia suspends Black Sea grain exports"; confirm a `verification` event arrives after `refinement`, the `done.result.verification` summary is present with sane counts (checked > 0 on a real run), and any refuted node has flipped to `no_effect`. Print the per-event timeline + the verification summary.
- [ ] **Step 3:** Stop the backend.

---

## Self-Review

- Verification pass (adversarial, downgrade-only, fail-open, bounded) → Task 1. ✓
- Eligibility = strong non-seed verdicts only → Task 1 eligible filter. ✓
- `refuted→no_effect`, `weakened→×0.5`, `upheld→annotate`, `confidence`/`verification`/`verified` set → Task 1 apply loop + tests. ✓
- `verification` event + `done.result.verification` → Task 1 Step 5. ✓
- Disabled / fail-open / threshold / ordering → Task 1 tests. ✓
- TS types + main.ts event handling + status → Task 2. ✓
- Frontend event-application test → Task 2 Step 1. ✓
- E2E → Task 3. ✓
- No-regression (reconcile/wrapper-equivalence) — verification is additive; both still pass (Task 1 Step 6 full run). ✓
- Placeholder scan: full code in every code step; no TBD. ✓
- Naming consistency: `_verification_pass`, `VERIFY_*`, `verification`/`verified`/`confidence`, summary keys `checked/upheld/weakened/refuted` consistent across backend, TS types, tests, and consumers. ✓
