# "So What?" Per-Hop Streaming — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the ~2-minute blocking `/impact` run into a live per-hop reveal — seeds paint instantly, each hop ring lights up as it's scored, refinement re-tints — by streaming NDJSON events the frontend applies incrementally.

**Architecture:** Refactor `run_impact()` in `api/impact.py` into a generator `run_impact_stream()` that yields an event dict at each existing boundary (seeds → hop → refinement → done); `run_impact()` becomes a thin wrapper that drains it and returns the final `done` payload, so `/impact` and `/impact/multi` are byte-for-byte unchanged. A new `POST /impact/stream` endpoint wraps the generator in a `StreamingResponse` (NDJSON). The frontend's `runImpactStream()` reads the streamed body and applies each event to a growing accumulator, re-tinting per hop (2D Sigma live; globe once at `done`).

**Tech Stack:** Python 3.11 / FastAPI / Starlette `StreamingResponse` / sqlite3 / pytest+TestClient; TypeScript / Vite / Sigma.js / 3d-force-graph / Vitest.

**Spec:** `docs/superpowers/specs/2026-06-17-impact-streaming-design.md`

**Operational note (project memory):** This repo lives in OneDrive, which rewrites file mtimes and can make Python serve stale `__pycache__` bytecode. When running anything here, launch Python with `python -B` (or delete `__pycache__` dirs first). Start long-running servers detached, not as session-bound background tasks.

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `api/impact.py` | BFS engine | Add `run_impact_stream` generator; reduce `run_impact` to a drain wrapper |
| `api/main.py` | HTTP surface | Add `POST /impact/stream`; import `Request`, `StreamingResponse` |
| `tests/test_impact_stream.py` | Backend stream tests | New file |
| `tests/test_api.py` | Endpoint tests | Add `/impact/stream` endpoint + 400 cases |
| `web/src/api.ts` | API client | Add `ImpactStreamEvent` type + `runImpactStream` NDJSON reader |
| `web/src/main.ts` | App wiring | Single-event path → incremental tint; `_impactStreaming` globe guard |
| `web/src/__tests__/impact-stream.test.ts` | Frontend reader tests | New file |

No schema, DB, or pipeline changes. `/impact`, `/impact/multi`, and the archive format are untouched.

---

## Task 1: Engine generator + drain wrapper

**Files:**
- Modify: `api/impact.py` (the `run_impact` function, currently lines ~821–1192)
- Test: `tests/test_impact_stream.py` (create)

The strategy: introduce `run_impact_stream(text, *, conn, provider=None)` containing the **current body of `run_impact` verbatim**, with five mechanical changes:
1. Wrap the post-validation body in `try: … finally: _restore_thread_local()`.
2. Each existing `return <dict>` becomes `yield {"event": "error", …}` (when it carried an `error`) followed by `yield {"event": "done", "result": <dict>}` then `return`.
3. After the hop-0 `impacts` init loop, `yield` a `seeds` event.
4. At the end of each hop iteration, `yield` a `hop` event (tracking which node_ids were first scored that hop).
5. After `_refinement_pass`, `yield` a `refinement` event, then a final `done` event.

Then `run_impact` becomes a wrapper that drains the generator.

- [ ] **Step 1: Write the failing test**

Create `tests/test_impact_stream.py`. The fake LLM **reads ids out of the prompt and echoes verdicts**, so it is deterministic and graph-agnostic. `MAX_FRONTIER` is patched high so `_sample_frontier`'s random shuffle never triggers (keeps runs deterministic).

```python
"""Tests for the streaming impact generator (run_impact_stream).

A deterministic fake LLM echoes verdicts derived from the prompt text, so the
BFS traverses the real econgraph.db graph reproducibly without calling Claude.
MAX_FRONTIER is patched high so the random frontier sampler never fires.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from api import impact as impact_mod
from schema.store import connect

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = REPO_ROOT / "econgraph.db"


@pytest.fixture
def conn():
    if not DB_PATH.exists():
        pytest.skip("econgraph.db missing; run `python -m pipeline.build_graph` first")
    c = connect(DB_PATH)
    yield c
    c.close()


def _fake_llm(prompt: str) -> str:
    """Route by prompt shape; echo verdicts for every id found in the prompt."""
    # Entity extraction: no named companies -> let the commodity seed anchor.
    if "Extract ONLY investable companies" in prompt:
        return "[]"
    # Seed selection: pick the FIRST candidate id from the "id | type | name" list.
    if "Pick the ONE node" in prompt:
        m = re.search(r"^\s*(\S+)\s*\|", prompt, re.MULTILINE)
        nid = m.group(1) if m else None
        return f'{{"node_id": {nid!r}, "direction": "negative", "magnitude": 0.9, "reasoning": "t"}}'
    # Ring scoring: one verdict per candidate id (first column of each "  id | ..." line).
    if "propagating a news shock" in prompt:
        ids = re.findall(r"^\s{2}(\S+)\s*\|", prompt, re.MULTILINE)
        objs = ", ".join(
            f'{{"node_id": {i!r}, "direction": "negative", "magnitude": 0.5, "reasoning": "t"}}'
            for i in ids
        )
        return f"[{objs}]"
    # Refinement: one verdict per "NODE: <id> (" header.
    if "refining impact assessments" in prompt:
        ids = re.findall(r"^NODE:\s*(\S+)\s*\(", prompt, re.MULTILINE)
        objs = ", ".join(
            f'{{"node_id": {i!r}, "direction": "negative", "magnitude": 0.9, "reasoning": "t"}}'
            for i in ids
        )
        return f"[{objs}]"
    return ""


@pytest.fixture(autouse=True)
def patch_llm_and_frontier(monkeypatch):
    monkeypatch.setattr(impact_mod, "_llm_call", _fake_llm)
    monkeypatch.setattr(impact_mod, "MAX_FRONTIER", 9999)  # disable random sampling


def test_stream_event_ordering(conn):
    events = list(impact_mod.run_impact_stream("global crude oil supply shock", conn=conn))
    kinds = [e["event"] for e in events]
    assert kinds[0] == "seeds"
    assert kinds[-1] == "done"
    assert "hop" in kinds
    # refinement appears (if any) before done; done is unique and last.
    assert kinds.count("done") == 1
    # No event after done.
    assert kinds.index("done") == len(kinds) - 1


def test_stream_reconciles_with_done(conn):
    events = list(impact_mod.run_impact_stream("global crude oil supply shock", conn=conn))
    streamed_ids = set()
    done = None
    for e in events:
        if e["event"] == "seeds":
            streamed_ids.update(v["node_id"] for v in e["seeds"])
        elif e["event"] == "hop":
            streamed_ids.update(v["node_id"] for v in e["new_impacts"])
        elif e["event"] == "done":
            done = e
    assert done is not None
    final_ids = {v["node_id"] for v in done["result"]["impacts"]}
    # Every final node was revealed in a stream event; nothing streamed is absent.
    assert streamed_ids == final_ids


def test_wrapper_equals_done_payload(conn):
    # The wrapper must return exactly what the generator's `done` event carried.
    events = list(impact_mod.run_impact_stream("global crude oil supply shock", conn=conn))
    done_result = next(e["result"] for e in events if e["event"] == "done")
    wrapped = impact_mod.run_impact("global crude oil supply shock", conn=conn)
    assert {v["node_id"] for v in wrapped["impacts"]} == {v["node_id"] for v in done_result["impacts"]}
    assert wrapped.get("max_hops") == done_result.get("max_hops")


def test_empty_text_emits_error_then_done(conn):
    events = list(impact_mod.run_impact_stream("   ", conn=conn))
    assert [e["event"] for e in events] == ["error", "done"]
    assert events[-1]["result"]["impacts"] == []
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -B -m pytest tests/test_impact_stream.py -v`
Expected: FAIL — `AttributeError: module 'api.impact' has no attribute 'run_impact_stream'`.

- [ ] **Step 3: Implement `run_impact_stream` and shrink `run_impact` to a wrapper**

In `api/impact.py`, replace the entire `def run_impact(...)` function (lines ~821–1192) with the generator below **plus** the wrapper. The interior seed/BFS/refinement logic is the current code verbatim; only the marked lines change. Where it says *"(verbatim from current `run_impact`)"*, paste the exact existing lines from that region — do not alter them.

```python
def run_impact_stream(
    text: str, *, conn: sqlite3.Connection, provider: Optional[str] = None
):
    """Streaming variant of run_impact. Yields event dicts:
      {"event":"seeds", ...} once, then {"event":"hop", ...} per hop,
      then {"event":"refinement", ...}, then {"event":"done","result":<full payload>}.
    Error cases yield {"event":"error", ...} then a closing {"event":"done", ...}.
    The `done.result` payload is identical to what the old run_impact returned."""
    text = (text or "").strip()
    if not text:
        result = {"error": "empty news text", "seed": None, "impacts": []}
        yield {"event": "error", "message": "empty news text"}
        yield {"event": "done", "result": result}
        return

    effective_provider = (provider or LLM_PROVIDER).lower()
    prev_thread_provider = getattr(_thread_local, "provider", None)
    _thread_local.provider = effective_provider
    debug_log: list[str] = []

    def _restore_thread_local() -> None:
        if prev_thread_provider is None:
            try:
                del _thread_local.provider
            except AttributeError:
                pass
        else:
            _thread_local.provider = prev_thread_provider

    try:
        # === Steps 1–5: seed identification ===
        # (verbatim from current run_impact: everything from
        #  "candidates = _list_seed_candidates(conn)" down through the
        #  construction of `all_seeds` and the `if not all_seeds:` block.)
        # CHANGE the `if not all_seeds:` early return to:
        #     yield {"event": "error", "message": "Could not identify any seed nodes from the news text"}
        #     yield {"event": "done", "result": {
        #         "error": "Could not identify any seed nodes from the news text",
        #         "seed": None, "impacts": [], "debug": debug_log}}
        #     return
        #
        # (Then, verbatim:)
        #     seeds_block = _build_seeds_block(all_seeds)
        #     impacts/visited/frontier init loop (Step 6)
        #     primary_seed_id = all_seeds[0]["node_id"]

        # === NEW: emit the seeds event right after hop-0 init ===
        yield {
            "event": "seeds",
            "seeds": [impacts[s["node_id"]] for s in all_seeds if s["node_id"] in impacts],
            "primary_seed_id": primary_seed_id,
        }

        # === Step 7: BFS ring by ring ===
        for hop in range(1, MAX_HOPS + 1):
            full_ring = _neighbors(conn, frontier, visited)
            log.info("hop %d: frontier=%d, ring=%d", hop, len(frontier), len(full_ring))
            debug_log.append(f"hop {hop}: frontier={len(frontier)}, raw_neighbors={len(full_ring)}")
            if not full_ring:
                debug_log.append(f"hop {hop}: no new neighbors -> stop")
                if hop == 1 and len(impacts) == len(all_seeds):
                    seed_names = ", ".join(s["name"] for s in all_seeds)
                    suggestion = (
                        "Try searching for a directly connected company (e.g. Apple, "
                        "Nvidia, AMD for TSMC; ExxonMobil, Chevron for crude oil)."
                    )
                    result = {
                        "error": (
                            f"The identified seed node(s) — {seed_names} — exist in the graph "
                            f"but have no recorded supply chain connections yet. {suggestion}"
                        ),
                        "seed": impacts.get(primary_seed_id),
                        "seeds": [impacts[s["node_id"]] for s in all_seeds if s["node_id"] in impacts],
                        "impacts": list(impacts.values()),
                        "provider": effective_provider,
                        "model": "claude-code-cli" if effective_provider == "claude" else OLLAMA_MODEL,
                        "max_hops": MAX_HOPS,
                        "debug": debug_log,
                        "refinement": {"considered": 0, "rescored": 0, "applied": 0},
                        "no_neighbors": True,
                    }
                    yield {"event": "error", "message": result["error"], "no_neighbors": True}
                    yield {"event": "done", "result": result}
                    return
                break

            sampled_flag = len(full_ring) > MAX_FRONTIER
            # (verbatim from current run_impact: the frontier cap/sampling block,
            #  chunking, chunk_prompts build, the ThreadPoolExecutor pool.map,
            #  and the verdict-application loop that sets impacts[nid] = {...}.)
            #
            # ADD a tracker: declare `hop_new_ids: list[str] = []` immediately
            # before the `for chunk_idx, ...` loop, and `hop_new_ids.append(nid)`
            # on the SAME branch that does `impacts[nid] = {...}` (right after it).

            # === NEW: emit the hop event ===
            yield {
                "event": "hop",
                "hop": hop,
                "new_impacts": [impacts[nid] for nid in hop_new_ids],
                "frontier_size": len(frontier),
                "ring_size": len(full_ring),
                "sampled": sampled_flag,
            }

            if chunk_failed and not new_frontier:
                break
            if not new_frontier:
                break
            frontier = new_frontier

        # === Refinement pass ===
        refinement_summary = _refinement_pass(
            text=text, impacts=impacts, seeds_block=seeds_block,
            conn=conn, debug_log=debug_log,
        )
        # _refinement_pass sets impacts[nid]["refined"] = True on every applied node.
        yield {
            "event": "refinement",
            "updated": [v for v in impacts.values() if v.get("refined")],
            "summary": refinement_summary,
        }

        # === Done ===
        seeds_response = [impacts[s["node_id"]] for s in all_seeds if s["node_id"] in impacts]
        result = {
            "seed": impacts.get(primary_seed_id),
            "seeds": seeds_response,
            "impacts": list(impacts.values()),
            "provider": effective_provider,
            "model": "claude-code-cli" if effective_provider == "claude" else OLLAMA_MODEL,
            "max_hops": MAX_HOPS,
            "debug": debug_log,
            "refinement": refinement_summary,
        }
        yield {"event": "done", "result": result}
    finally:
        _restore_thread_local()


def run_impact(
    text: str, *, conn: sqlite3.Connection, provider: Optional[str] = None
) -> dict[str, Any]:
    """Non-streaming wrapper: drain run_impact_stream, return the done payload."""
    final: dict[str, Any] = {}
    for ev in run_impact_stream(text, conn=conn, provider=provider):
        if ev["event"] == "done":
            final = ev["result"]
    return final
```

Notes for the implementer:
- The original `run_impact` set/restored the thread-local and had a `try/finally` only around `_refinement_pass`. In the generator the `try/finally` now spans the whole body so the thread-local is restored even if the consumer stops iterating early (matters for `/impact/stream` cancellation). Remove the old inner `try/finally` around `_refinement_pass` — the outer one covers it.
- `new_frontier`, `chunk_failed`, `seeds_block`, `visited`, `frontier`, `impacts`, `all_seeds`, `primary_seed_id` all come from the verbatim regions — keep their names exactly.

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -B -m pytest tests/test_impact_stream.py -v`
Expected: PASS (4 tests). If `econgraph.db` is absent, they skip — fetch it from the GitHub Release first.

- [ ] **Step 5: Run the existing suite to confirm no regression**

Run: `python -B -m pytest tests/ -q`
Expected: PASS (same count as before plus the 4 new). `run_impact` is exercised indirectly; nothing else changed.

- [ ] **Step 6: Commit**

```bash
git add api/impact.py tests/test_impact_stream.py
git commit -m "feat(impact): run_impact_stream generator; run_impact becomes drain wrapper"
```

---

## Task 2: `POST /impact/stream` endpoint

**Files:**
- Modify: `api/main.py` (imports near line 23; new endpoint after `post_impact`, ~line 308)
- Test: `tests/test_api.py` (add tests)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_api.py`. The fake LLM + frontier patch mirror Task 1 so the endpoint runs without Claude.

```python
import re as _re


def _fake_llm_for_stream(prompt: str) -> str:
    from api import impact as _impact  # local import; module already loaded
    if "Extract ONLY investable companies" in prompt:
        return "[]"
    if "Pick the ONE node" in prompt:
        m = _re.search(r"^\s*(\S+)\s*\|", prompt, _re.MULTILINE)
        nid = m.group(1) if m else None
        return f'{{"node_id": {nid!r}, "direction": "negative", "magnitude": 0.9, "reasoning": "t"}}'
    if "propagating a news shock" in prompt:
        ids = _re.findall(r"^\s{2}(\S+)\s*\|", prompt, _re.MULTILINE)
        objs = ", ".join(
            f'{{"node_id": {i!r}, "direction": "negative", "magnitude": 0.5, "reasoning": "t"}}'
            for i in ids
        )
        return f"[{objs}]"
    if "refining impact assessments" in prompt:
        ids = _re.findall(r"^NODE:\s*(\S+)\s*\(", prompt, _re.MULTILINE)
        objs = ", ".join(
            f'{{"node_id": {i!r}, "direction": "negative", "magnitude": 0.9, "reasoning": "t"}}'
            for i in ids
        )
        return f"[{objs}]"
    return ""


def test_impact_stream_ndjson(client, monkeypatch):
    from api import impact as impact_mod
    monkeypatch.setattr(impact_mod, "_llm_call", _fake_llm_for_stream)
    monkeypatch.setattr(impact_mod, "MAX_FRONTIER", 9999)
    r = client.post("/impact/stream", json={"text": "global crude oil supply shock"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/x-ndjson")
    lines = [ln for ln in r.text.splitlines() if ln.strip()]
    events = [json.loads(ln) for ln in lines]
    assert events[0]["event"] == "seeds"
    assert events[-1]["event"] == "done"
    assert "impacts" in events[-1]["result"]


def test_impact_stream_requires_text(client):
    r = client.post("/impact/stream", json={"text": "   "})
    assert r.status_code == 400


def test_impact_stream_rejects_bad_provider(client):
    r = client.post("/impact/stream", json={"text": "oil", "provider": "gpt4"})
    assert r.status_code == 400
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -B -m pytest tests/test_api.py::test_impact_stream_ndjson tests/test_api.py::test_impact_stream_requires_text tests/test_api.py::test_impact_stream_rejects_bad_provider -v`
Expected: FAIL — 404 Not Found (endpoint doesn't exist yet).

- [ ] **Step 3: Add imports**

In `api/main.py` line 23, change:

```python
from fastapi import Body, Depends, FastAPI, HTTPException, Query
```
to:
```python
from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
```

- [ ] **Step 4: Add the endpoint**

Insert after `post_impact` (after line 307), before `get_headlines`:

```python
@app.post("/impact/stream")
def post_impact_stream(payload: dict = Body(...), request: Request = None):
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

    import json as _json

    def gen():
        conn = connect(_DB_PATH)
        try:
            for ev in impact_mod.run_impact_stream(text, conn=conn, provider=provider_override):
                yield _json.dumps(ev, separators=(",", ":")) + "\n"
        finally:
            conn.close()

    return StreamingResponse(
        gen(),
        media_type="application/x-ndjson",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )
```

Note: we open the SQLite connection **inside** `gen()` (not via `Depends(get_conn)`) because `StreamingResponse` runs the generator after the handler returns — a `Depends`-managed connection would already be closed. The `request: Request` param is accepted for parity but `is_disconnected()` is an async check; this sync endpoint relies on the client closing the connection to stop iteration, which Starlette surfaces as a `GeneratorExit` into `gen()`, triggering the `finally` cleanup. (Cancellation correctness is covered by the frontend `AbortController` in Task 5.)

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -B -m pytest tests/test_api.py -q`
Expected: PASS (existing + 3 new).

- [ ] **Step 6: Commit**

```bash
git add api/main.py tests/test_api.py
git commit -m "feat(api): POST /impact/stream NDJSON endpoint over run_impact_stream"
```

---

## Task 3: Frontend NDJSON reader (`runImpactStream`)

**Files:**
- Modify: `web/src/api.ts` (add type + function after `runImpact`, ~line 384)
- Test: `web/src/__tests__/impact-stream.test.ts` (create)

- [ ] **Step 1: Write the failing test**

Create `web/src/__tests__/impact-stream.test.ts`. It builds a fake `Response` whose `body.getReader()` yields NDJSON bytes, with **one event split across two chunks** to prove the line buffer works.

```ts
import { afterEach, describe, expect, it, vi } from "vitest";
import { runImpactStream, type ImpactStreamEvent } from "../api";

function readerFrom(chunks: string[]) {
  const enc = new TextEncoder();
  let i = 0;
  return {
    body: {
      getReader() {
        return {
          read: async () =>
            i < chunks.length
              ? { done: false, value: enc.encode(chunks[i++]) }
              : { done: true, value: undefined },
          releaseLock() {},
        };
      },
    },
    ok: true,
    status: 200,
    statusText: "OK",
  } as unknown as Response;
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("runImpactStream", () => {
  it("parses NDJSON events in order, buffering split lines, and returns done.result", async () => {
    // The 'hop' line is deliberately split across two chunks.
    const chunks = [
      '{"event":"seeds","seeds":[{"node_id":"a"}],"primary_seed_id":"a"}\n{"event":"h',
      'op","hop":1,"new_impacts":[{"node_id":"b"}],"frontier_size":1,"ring_size":1,"sampled":false}\n',
      '{"event":"refinement","updated":[],"summary":{}}\n',
      '{"event":"done","result":{"impacts":[{"node_id":"a"},{"node_id":"b"}],"max_hops":3}}\n',
    ];
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(readerFrom(chunks)));

    const seen: string[] = [];
    const result = await runImpactStream("oil", {
      onEvent: (e: ImpactStreamEvent) => seen.push(e.event),
    });

    expect(seen).toEqual(["seeds", "hop", "refinement", "done"]);
    expect(result.impacts.map((v) => v.node_id)).toEqual(["a", "b"]);
  });

  it("throws ApiError on non-OK", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false, status: 502, statusText: "Bad Gateway",
        text: async () => "boom",
      } as unknown as Response),
    );
    await expect(runImpactStream("oil", { onEvent: () => {} })).rejects.toThrow();
  });
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd web && npm run test -- impact-stream`
Expected: FAIL — `runImpactStream is not exported` / type error.

- [ ] **Step 3: Implement the type and reader**

In `web/src/api.ts`, after `runImpact` (line 384), add:

```ts
// ---------------------------------------------------------------------------
// Streaming impact (NDJSON over a POST body) — per-hop live reveal.
// ---------------------------------------------------------------------------

export type ImpactStreamEvent =
  | { event: "seeds"; seeds: ImpactVerdict[]; primary_seed_id: string | null }
  | { event: "hop"; hop: number; new_impacts: ImpactVerdict[]; frontier_size: number; ring_size: number; sampled: boolean }
  | { event: "refinement"; updated: ImpactVerdict[]; summary: Record<string, unknown> }
  | { event: "error"; message: string; no_neighbors?: boolean }
  | { event: "done"; result: ImpactResponse };

/** POST /impact/stream and apply each NDJSON event via onEvent.
 *  Resolves to the final `done` event's result (same shape as runImpact). */
export async function runImpactStream(
  text: string,
  opts: { provider?: ImpactProvider; signal?: AbortSignal; onEvent: (ev: ImpactStreamEvent) => void },
): Promise<ImpactResponse> {
  const url = new URL("/impact/stream", API_BASE_URL);
  inflight += 1;
  notifyLoading();
  try {
    const body: Record<string, string> = { text };
    if (opts.provider) body.provider = opts.provider;
    const resp = await fetch(url.toString(), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: opts.signal,
    });
    if (!resp.ok) {
      const respBody = await resp.text().catch(() => "");
      throw new ApiError(resp.status, `${resp.statusText} - ${respBody.slice(0, 200)}`);
    }
    if (!resp.body) throw new Error("/impact/stream: no response body to stream");

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let doneResult: ImpactResponse | null = null;

    const handleLine = (line: string) => {
      const trimmed = line.trim();
      if (!trimmed) return;
      const ev = JSON.parse(trimmed) as ImpactStreamEvent;
      opts.onEvent(ev);
      if (ev.event === "done") doneResult = ev.result;
    };

    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let nl: number;
      while ((nl = buffer.indexOf("\n")) >= 0) {
        handleLine(buffer.slice(0, nl));
        buffer = buffer.slice(nl + 1);
      }
    }
    if (buffer.trim()) handleLine(buffer);   // flush any trailing partial line

    if (!doneResult) throw new Error("/impact/stream: stream ended without a done event");
    return doneResult;
  } finally {
    inflight -= 1;
    notifyLoading();
  }
}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd web && npm run test -- impact-stream`
Expected: PASS (2 tests).

- [ ] **Step 5: Type-check**

Run: `cd web && npx tsc --noEmit`
Expected: no errors.

- [ ] **Step 6: Commit**

```bash
git add web/src/api.ts web/src/__tests__/impact-stream.test.ts
git commit -m "feat(web): runImpactStream NDJSON reader with split-line buffering"
```

---

## Task 4: Wire the single-event path to streaming

**Files:**
- Modify: `web/src/main.ts` (`handleImpactRun`, lines ~1238–1280; add `_impactStreaming` flag near line 1046)

No unit test — `main.ts` is integration glue with no existing test harness (consistent with the repo). Correctness is covered by Task 3's reader tests plus the manual verification in Task 5.

- [ ] **Step 1: Add the streaming flag**

Near the other module flags (by `let _bulkLoadInProgress = false;`, line 1046), add:

```ts
// True while a per-hop streaming impact run is applying events. Used to keep
// the expensive globe arc work to the final `done` event (per-hop globe
// re-tints are skipped; 2D Sigma re-tints every hop).
let _impactStreaming = false;
```

- [ ] **Step 2: Add `runImpactStream` to the api import**

Line 53 currently:
```ts
import { describeNode, runImpact, runMultiImpact, type ImpactResponse, type MultiImpactResponse } from "./api";
```
Change to:
```ts
import { describeNode, runImpact, runImpactStream, runMultiImpact, type ImpactResponse, type ImpactVerdict, type MultiImpactResponse } from "./api";
```

- [ ] **Step 3: Replace the single-event branch body**

In `handleImpactRun`, replace the entire `if (texts.length === 1) { … }` block (lines ~1238–1281, down to but not including the `} else {` for the multi path) with:

```ts
    if (texts.length === 1) {
      // Single event — streaming /impact/stream path with per-hop reveal.
      // Load the full graph up front so seed/hop nodes exist to tint as
      // events arrive (the old blocking path loaded it after the await).
      if (g.order === 0) {
        hideImpactOverlay();
        await loadFullCore();
        showImpactOverlay(provider);
      }

      const acc = new Map<string, ImpactVerdict>();
      const reapply = (isFinal: boolean) => {
        impactState = buildImpactState(g, { impacts: [...acc.values()] } as ImpactResponse);
        refreshEdgeVisibility();                 // 2D Sigma tint + renderer.refresh()
        // Globe: arc recolour is comparatively heavy; do it once at the end.
        // 2D-only sessions never enter is3DRunning(), so they tint every hop above.
        if (is3DRunning() && isFinal) applyImpact3D(g, impactState, filters);
      };

      _impactStreaming = true;
      let resp: ImpactResponse;
      try {
        resp = await runImpactStream(texts[0], {
          provider,
          signal: _impactAbortController.signal,
          onEvent: (ev) => {
            if (ev.event === "seeds") {
              ev.seeds.forEach((v) => acc.set(v.node_id, v));
              reapply(false);
            } else if (ev.event === "hop") {
              ev.new_impacts.forEach((v) => acc.set(v.node_id, v));
              reapply(false);
              setImpactStatus(`[${niceProvider}] hop ${ev.hop} → ${acc.size} nodes…`);
            } else if (ev.event === "refinement") {
              ev.updated.forEach((v) => acc.set(v.node_id, v));
              reapply(false);
            } else if (ev.event === "error") {
              setImpactStatus(ev.message, true);
            }
          },
        });
      } finally {
        _impactStreaming = false;
      }

      // Finalize from the canonical `done` payload — identical to the old path.
      const hasAnySeeds = resp.seed != null || (Array.isArray(resp.seeds) && resp.seeds.length > 0);
      if ((resp as any).no_neighbors) {
        setImpactStatus(resp.error || "Seed node has no recorded supply chain connections.", true);
        return;
      }
      if (resp.error && !hasAnySeeds) {
        setImpactStatus(`Failed: ${resp.error}`, true);
        return;
      }
      if (!hasAnySeeds) {
        setImpactStatus(`No seed identified — try a more specific entity name.`, true);
        return;
      }

      acc.clear();
      resp.impacts.forEach((v) => acc.set(v.node_id, v));
      reapply(true);                              // final globe tint
      if (impactClearBtn) impactClearBtn.hidden = false;
      const seedNames = resp.seeds && resp.seeds.length > 0
        ? resp.seeds.map((s) => `${s.name} (${s.direction})`).join(", ")
        : resp.seed ? `${resp.seed.name} (${resp.seed.direction})` : "unknown";
      setImpactStatus(
        `[${niceProvider}] Seeds: ${seedNames} → ${resp.impacts.length} nodes across ${resp.max_hops || 3} hops`,
      );
      renderTop5(resp.impacts);
      saveToArchive(texts[0], provider, resp);

    } else {
```

Notes:
- The multi-event `} else { … }` block below is **unchanged**.
- `_impactAbortController` is already created earlier in `handleImpactRun` (line 1235); the existing `catch (AbortError)` and `finally` (lines 1306–1318) already cover cancel/cleanup for this path.
- The final `acc.clear()` + repopulate from `resp.impacts` guarantees the rendered state exactly equals the canonical `done` payload, even if a streamed `error` event preceded a partial `done`.

- [ ] **Step 4: Type-check the whole app**

Run: `cd web && npx tsc --noEmit`
Expected: no errors. (Confirms `ImpactVerdict`, `runImpactStream`, `is3DRunning`, `applyImpact3D`, `refreshEdgeVisibility`, `buildImpactState`, `loadFullCore`, `showImpactOverlay` are all in scope.)

- [ ] **Step 5: Commit**

```bash
git add web/src/main.ts
git commit -m "feat(web): per-hop streaming reveal for single-event impact runs"
```

---

## Task 5: End-to-end manual verification

**Files:** none (verification only).

- [ ] **Step 1: Start the backend (detached, no stale bytecode)**

From the repo root, launch detached with bytecode cache disabled:

```powershell
Start-Process -WindowStyle Hidden python -ArgumentList "-B","-m","uvicorn","api.main:app","--host","::","--port","8101"
```
Confirm: `curl http://localhost:8101/health` returns `{"status":"ok",...}`.

- [ ] **Step 2: Start the frontend (detached)**

```powershell
Start-Process -WindowStyle Hidden npm -ArgumentList "--prefix","web","run","dev","--","--port","5180"
```
Open `http://localhost:5180`.

- [ ] **Step 3: Fire a known headline in 2D**

In the impact input, run *"Russia suspends Black Sea grain exports"*. Confirm:
- Seeds tint within a few seconds (well before the full run finishes).
- The status line ticks `hop 1 → N nodes…`, `hop 2 → …`, `hop 3 → …`.
- New nodes light up at each hop rather than all at once.
- After refinement, some weak nodes deepen/flip; the final status shows total nodes across hops.

- [ ] **Step 4: Confirm equivalence with the non-streaming path**

Compare the final lit set against a non-streaming run of the same headline:
```bash
curl -s -X POST http://localhost:8101/impact -H "Content-Type: application/json" \
  -d '{"text":"Russia suspends Black Sea grain exports"}' | python -m json.tool | head -40
```
The streamed final state and this `/impact` response should list the same impacted nodes (LLM nondeterminism aside). Reload the page and restore the run from the archive — it should render identically (proves the saved `done` payload is intact).

- [ ] **Step 5: Confirm cancel works**

Start a run and click Cancel mid-trace. Confirm the status shows "Impact trace cancelled," the Run button re-enables, and the backend log shows the run stopping (no further hop calls after the disconnect).

- [ ] **Step 6: Spot-check the globe**

Switch to the globe and run a headline. Confirm nodes/arcs light up at completion without the 20–30s freeze Phase H fixed (per-hop globe re-tints are intentionally deferred to `done`).

---

## Self-Review

**Spec coverage:**
- Engine generator + drain wrapper → Task 1. ✓
- `POST /impact/stream` + event schema (seeds/hop/refinement/error/done) → Task 2 (endpoint), emitted in Task 1. ✓
- `done` payload byte-equivalent to `/impact` → Task 1 (`run_impact` returns the `done.result`; `test_wrapper_equals_done_payload`). ✓
- Frontend `runImpactStream` NDJSON reader w/ split-line buffering → Task 3. ✓
- Incremental per-hop tint, graph loaded up front, globe guard, cancel, archive unchanged → Task 4. ✓
- Tests: equivalence, ordering, coverage reconciliation, error paths, endpoint, reader → Tasks 1–3. ✓
- Manual verification incl. OneDrive `-B` / detached servers → Task 5. ✓
- Multi-event path untouched → Task 4 (only the `texts.length === 1` branch changes). ✓

**Placeholder scan:** The only "paste verbatim" markers in Task 1 Step 3 point at exact, identified line regions of the current `run_impact` (not unspecified work); all changed lines are shown in full. No TBD/TODO. ✓

**Type/name consistency:** `run_impact_stream`, `ImpactStreamEvent`, `runImpactStream`, `_impactStreaming`, `reapply`, `acc` used consistently across tasks. Event field names (`new_impacts`, `frontier_size`, `ring_size`, `sampled`, `updated`, `summary`, `result`, `primary_seed_id`) match between the generator (Task 1), endpoint test (Task 2), reader type (Task 3), and consumer (Task 4). ✓
