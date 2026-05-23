// Smoke tests over the API client. We don't exercise network: instead we
// stub `fetch` and verify that URL construction, param encoding, and JSON
// shape parsing all line up with what the Phase 5 API actually returns.

import { afterEach, describe, expect, it, vi } from "vitest";

import {
  getEgo,
  getHealth,
  getNode,
  getSubgraph,
  search,
  type EgoResponse,
  type HealthResponse,
  type ApiNode,
} from "../api";

function makeNode(key: string, label: string, provisional = false): ApiNode {
  return {
    key,
    attributes: {
      label,
      type: provisional ? "Company" : "Company",
      sector: "Consumer Staples",
      industry: "Household Products",
      country: "US",
      tickers: provisional ? [] : ["PG"],
      identifiers: {},
      aliases: [label],
      provisional,
      identity_unverified: provisional,
      metadata: {},
    },
  };
}

function mockFetch(body: unknown, ok = true, status = 200) {
  const fetchMock = vi.fn().mockResolvedValue({
    ok,
    status,
    statusText: ok ? "OK" : "Bad Request",
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as unknown as Response);
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("api client", () => {
  it("parses /health response", async () => {
    const body: HealthResponse = {
      status: "ok",
      node_count: 166,
      edge_count: 360,
      core_edge_count: 204,
      audit_edge_count: 156,
      customer_of_rows_in_db: 0,
    };
    const f = mockFetch(body);
    const r = await getHealth();
    expect(r.status).toBe("ok");
    expect(r.customer_of_rows_in_db).toBe(0);
    expect(f.mock.calls[0][0]).toContain("/health");
  });

  it("parses a Node response with provisional flag", async () => {
    const body = makeNode("slug:nestle", "Nestlé S.A.", true);
    const f = mockFetch(body);
    const r = await getNode("slug:nestle");
    expect(r.key).toBe("slug:nestle");
    expect(r.attributes.provisional).toBe(true);
    // URL must keep the colon intact for the API's :path converter.
    expect(f.mock.calls[0][0]).toContain("slug:nestle");
  });

  it("getEgo serializes the types and include_provisional params", async () => {
    const body: EgoResponse = {
      center: "cik:0000080424",
      nodes: [makeNode("cik:0000080424", "P&G")],
      edges: [],
    };
    const f = mockFetch(body);
    await getEgo("cik:0000080424", {
      types: ["supplies", "customer_of"],
      includeProvisional: true,
    });
    const url = f.mock.calls[0][0] as string;
    expect(url).toContain("/node/");
    expect(url).toContain("/ego");
    expect(url).toContain("types=supplies%2Ccustomer_of");
    expect(url).toContain("include_provisional=true");
  });

  it("getSubgraph passes hops and seed", async () => {
    mockFetch({ seed: "cik:0000080424", hops: 2, nodes: [], edges: [], truncated: false });
    const r = await getSubgraph("cik:0000080424", { hops: 2 });
    expect(r.seed).toBe("cik:0000080424");
    expect(r.hops).toBe(2);
  });

  it("search returns ranked candidates", async () => {
    mockFetch({
      q: "procter",
      results: [{ id: "cik:0000080424", name: "Procter & Gamble", score: 95, matched_alias_normalized: "procter and gamble" }],
    });
    const r = await search("procter");
    expect(r.results[0].id).toBe("cik:0000080424");
    expect(r.results[0].score).toBe(95);
  });

  it("throws ApiError on non-OK", async () => {
    mockFetch({ detail: "not found" }, false, 404);
    await expect(getNode("cik:9999999999")).rejects.toThrow();
  });
});
