# "So What?" Streaming — Design Spec

**Date:** 2026-06-17
**Status:** Approved (brainstorm), pending implementation plan
**Work stream:** 1 of 4 in the next "So What?" iteration

---

## Context

The "So What?" impact engine (`api/impact.py`) answers a news headline by seeding
the graph, running a capped BFS with per-ring LLM scoring, and a refinement pass.
A full run takes ~2 minutes. Today `POST /impact` is a single blocking request:
the engine builds an internal `debug_log` of progress, but the user gets **nothing
on screen until the entire run finishes** — a dead spinner. The only caching is the
frontend's 24h localStorage archive of *completed* runs.

This is the first of four agreed work streams, in priority order:
**1. Speed & UX → 2. Correctness & trust → 3. Quantify the impact → 4. Reach & depth.**

Within Speed & UX, the chosen first fix is **"make the wait feel alive"** via a
**per-hop progressive reveal**: seeds paint instantly, then each hop ring lights up,
then the refinement pass re-tints. ~5–6 progressive updates that map directly onto
boundaries the engine already has.

### Non-goals (explicitly deferred)
- Server-side run caching and LLM call-count reduction (later in stream 1).
- Per-chunk reveal and live textual narration (richer than per-hop; deferred).
- Streaming the multi-event path (`/impact/multi` stays non-streaming).
- The unrelated open bug where a failed ring chunk silently drops nodes with no
  retry (HANDOFF §13) — noted, not addressed here.

---

## Approach

**Chosen: POST + streamed NDJSON body, read via the `fetch` `ReadableStream`.**

A new `POST /impact/stream` returns a `StreamingResponse` that yields
newline-delimited JSON events as the BFS progresses. The frontend swaps its single
`await fetch().json()` for a streaming reader that applies each event to the graph.

Rejected alternatives:
- **Job-start POST + SSE GET** — needs an in-memory job registry, queue, lifecycle,
  and two endpoints; its only wins (reconnect, multi-viewer) are irrelevant to a
  local single-user app. Overkill.
- **WebSocket** — full duplex unjustified; cancel is just closing the stream. Heaviest.

### Safety principle
Streamed events are **purely additive**, and the final `done` event carries a payload
**byte-for-byte equivalent to today's `/impact` response**. This keeps every existing
consumer (`/impact`, `/impact/multi`, the 24h archive, all rendering) working
unchanged, and lets a test assert streaming never changes the answer.

---

## Section 1 — Engine: BFS as a generator (`api/impact.py`)

Extract the body of `run_impact()` into a generator that yields at the hop boundaries
that already exist (after seed resolution ~L951, after each hop's frontier update
~L1150, after `_refinement_pass` ~L1168):

```python
def run_impact_stream(text, *, conn, provider=None) -> Iterator[dict]:
    # thread-local provider set/restore wraps the whole generator in try/finally
    yield {"event": "seeds", "seeds": [...], "primary_seed_id": ...}
    for hop in range(1, MAX_HOPS + 1):
        ...                                   # existing ring logic, unchanged
        yield {"event": "hop", "hop": hop, "new_impacts": [...],
               "frontier_size": ..., "ring_size": ..., "sampled": bool}
    refinement_summary = _refinement_pass(...)
    yield {"event": "refinement", "updated": [...], "summary": refinement_summary}
    yield {"event": "done", "result": {<<exact dict run_impact returns today>>}}
```

`run_impact()` becomes a thin wrapper that drains the generator and returns the
final `done` payload:

```python
def run_impact(text, *, conn, provider=None) -> dict:
    final = {}
    for ev in run_impact_stream(text, conn=conn, provider=provider):
        if ev["event"] == "done":
            final = ev["result"]
    return final
```

Notes:
- `/impact` and `/impact/multi` keep calling `run_impact()` → **zero behavior change**.
  `run_multi_impact` (isolated full run per text, then merge) is untouched.
- Early-return cases (empty text; `no_neighbors` at hop 1) emit an `error` event
  followed by a closing `done`, so the stream always terminates cleanly.
- Thread-local provider set/restore must wrap the entire generator (rings spawn
  pooled threads) — same correctness as today.

---

## Section 2 — API endpoint + event schema (`api/main.py`)

New endpoint alongside the existing `@app.post("/impact")` (which stays as-is):

```python
@app.post("/impact/stream")
async def impact_stream(req: ImpactRequest, request: Request):
    async def gen():
        conn = connect(DB_PATH)
        try:
            for ev in run_impact_stream(req.text, conn=conn, provider=req.provider):
                if await request.is_disconnected():
                    break                      # user cancelled → stop, save credits
                yield json.dumps(ev, separators=(",", ":")) + "\n"
        finally:
            conn.close()
    return StreamingResponse(
        gen(), media_type="application/x-ndjson",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )
```

The generator is synchronous; FastAPI iterates it inside the async wrapper. Each hop
is a blocking stretch (existing `ThreadPoolExecutor` parallelism unchanged); one NDJSON
line flushes at each boundary. `X-Accel-Buffering: no` defends against proxy buffering.
`ImpactRequest` (text + optional provider) is reused unchanged.

### Event contract (one JSON object per line)

| `event` | Payload | When |
|---|---|---|
| `seeds` | `seeds[]` (hop-0 verdicts), `primary_seed_id` | immediately after seed resolution — first paint |
| `hop` | `hop`, `new_impacts[]`, `frontier_size`, `ring_size`, `sampled` | after each hop's ring is scored |
| `refinement` | `updated[]`, `summary` | after the refinement pass |
| `error` | `message`, plus existing fields (`no_neighbors`, `seed`, …) | empty text / stranded seeds |
| `done` | `result` = the exact dict `/impact` returns today | always last; closes the stream |

Each node object in `seeds` / `new_impacts` / `updated` uses the **same shape** the
frontend already renders from `impacts[]` today (`node_id, name, type, direction,
magnitude, hop, reasoning, via_parent, edge_type, edge_weight, edge_source_tier,
is_estimated`) — so the frontend consumes streamed nodes with no shape translation.

---

## Section 3 — Frontend: consume the stream, tint per hop

**New streaming client in `web/src/api.ts`** (alongside `runImpact`, kept for the
multi path and as a fallback):

```ts
export async function runImpactStream(
  text: string,
  opts: { provider; signal; onEvent: (ev: ImpactStreamEvent) => void }
): Promise<ImpactResponse> {        // resolves to the `done` result
  const res = await fetch(`${API}/impact/stream`, {
    method: "POST", signal: opts.signal,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text, provider: opts.provider }),
  });
  const reader = res.body!.getReader();
  // decode UTF-8, split on "\n", JSON.parse each complete line, buffer the
  // partial trailing line across reads; call onEvent per parsed object;
  // capture and return the `done` event's `result`.
}
```

**`handleImpactRun` single-event path** drives tint incrementally. The multi-event
path (`/impact/multi`) is **untouched**.

```ts
// Ensure the graph exists BEFORE the first tint (today this happens after the
// await — for streaming it must happen up front so seed nodes are present).
if (g.order === 0) { hideImpactOverlay(); await loadFullCore(); }

const acc = new Map<string, ImpactVerdict>();           // node_id → verdict
const reapply = () => {
  impactState = buildImpactState(g, { impacts: [...acc.values()] } as ImpactResponse);
  refreshEdgeVisibility();
  applyImpact3D(g, impactState, filters);
};

const resp = await runImpactStream(texts[0], { provider, signal, onEvent: (ev) => {
  if (ev.event === "seeds")           { ev.seeds.forEach(v => acc.set(v.node_id, v)); reapply(); }
  else if (ev.event === "hop")        { ev.new_impacts.forEach(v => acc.set(v.node_id, v)); reapply();
                                        setImpactStatus(`hop ${ev.hop} → ${acc.size} nodes…`); }
  else if (ev.event === "refinement") { ev.updated.forEach(v => acc.set(v.node_id, v)); reapply(); }
  else if (ev.event === "error")      { /* set status; same no_neighbors handling as today */ }
}});

// done: finalize exactly as today — renderTop5(resp.impacts), saveToArchive, final status.
```

Key points:
- **`buildImpactState` reused unchanged** — fed a growing `{impacts: [...]}`. It already
  runs on every archive restore, so ~6 calls/run is cheap in 2D Sigma.
- **Globe perf guard:** per-hop `applyImpact3D` could rebuild the 13k-arc TubeGeometry
  each time (the cost Phase H fought). In globe mode, gate **arc rebuild** to the final
  `done` event via an `_impactStreaming` flag (mirroring `_bulkLoadInProgress`); per-hop
  events do lightweight node-color re-digests only. In 2D Sigma, re-tint every hop.
- **Cancel:** the existing `_impactAbortController` is passed to the fetch; aborting ends
  the body read; the server's `is_disconnected()` check stops the BFS between hops
  (saves credits); the existing `AbortError` catch shows "cancelled."
- **Archive unchanged:** `saveToArchive(text, provider, resp)` receives the canonical
  `done` payload — same shape as today; restore keeps working.
- **Scope:** streaming covers the single-event path only.

---

## Section 4 — Testing & verification

**Backend (`tests/test_api.py` + new `tests/test_impact_stream.py`), `_llm_call` monkeypatched to a deterministic fake:**
- **Equivalence (critical):** `run_impact()` drained from `run_impact_stream` equals a
  captured baseline — proves the refactor changed nothing.
- **Event ordering:** `seeds` → `hop`(×N) → `refinement` → `done`, `done` always last.
- **Coverage reconciliation:** union of `seeds` + every `hop.new_impacts` +
  `refinement.updated` matches `done.result.impacts` (nothing revealed that isn't final;
  nothing final that was never streamed).
- **Error paths:** empty text and stranded-seed (`no_neighbors`) each emit an `error`
  event followed by a closing `done`.
- **Endpoint:** `POST /impact/stream` returns `application/x-ndjson`; every line parses
  as JSON; last line is `done`. (FastAPI `TestClient` reads the streamed body.)

**Frontend (Vitest, `web/src/__tests__/`):**
- NDJSON reader handles lines split across chunk boundaries (partial line buffered) and
  an empty final chunk.
- Events apply in order: a `refinement` verdict overwrites the same node's earlier `hop`
  verdict in the accumulator.
- `runImpactStream` resolves to the `done` result.

**Manual verification (live):** backend on `:8101`, frontend on `:5180`; fire a known
headline (e.g. *"Russia suspends Black Sea grain exports"*). Confirm seeds paint within
seconds, each hop ring lights up progressively, refinement re-tints, and the final state
+ archive entry match a non-streaming `/impact` run of the same headline.
- Per project memory: launch Python with `python -B` (or purge `__pycache__`) — OneDrive
  mtime rewrites can serve stale bytecode — and start servers **detached**, not as
  background tasks tied to the session.

---

## Files touched

| File | Change |
|---|---|
| `api/impact.py` | Extract `run_impact_stream` generator; `run_impact` becomes a drain wrapper |
| `api/main.py` | New `POST /impact/stream` (`StreamingResponse`, NDJSON, disconnect check) |
| `web/src/api.ts` | New `runImpactStream` NDJSON reader + `ImpactStreamEvent` type |
| `web/src/main.ts` | `handleImpactRun` single-event path → incremental tint; `_impactStreaming` globe guard |
| `tests/test_impact_stream.py` | New: equivalence, ordering, coverage, error paths |
| `tests/test_api.py` | Add `/impact/stream` endpoint test |
| `web/src/__tests__/` | NDJSON reader + ordered-apply tests |

No schema, DB, or pipeline changes. No change to `/impact`, `/impact/multi`, or the archive format.
