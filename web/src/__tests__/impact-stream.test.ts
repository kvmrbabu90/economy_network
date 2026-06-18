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
