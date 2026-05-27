// Phase 5 API client. All graph data flows through here; never call fetch()
// against the API outside this module so URL/CORS quirks have a single home.

import { API_BASE_URL } from "./config";

// ---------------------------------------------------------------------------
// Shared types (mirror api/query.py's response shapes)
// ---------------------------------------------------------------------------

export type EdgeType =
  | "supplies"
  | "customer_of"
  | "competes_with"
  | "regulated_by"
  | "part_of";

export interface Provenance {
  filing: string;
  url: string;
  snippet: string;
  extracted_by: string;
}

export interface ApiNode {
  key: string;
  attributes: {
    label: string;
    type: "Company" | "Regulator" | "Commodity" | "Material" | "Region" | "Segment";
    sector: string | null;
    industry: string | null;
    country: string | null;
    tickers: string[];
    identifiers: Record<string, unknown>;
    aliases: string[];
    provisional: boolean;
    identity_unverified: boolean;
    metadata: Record<string, unknown>;
  };
}

export interface ApiEdge {
  key: string;
  source: string;
  target: string;
  attributes: {
    type: EdgeType;
    directed: boolean;
    confidence: number;
    weight: number | null;
    below_threshold: boolean;
    provenance: Provenance;
    additional_provenance: Provenance[];
    derived: boolean;
    underlying_edge_id?: string;
  };
  undirected: boolean;
}

export interface EgoResponse {
  center: string;
  nodes: ApiNode[];
  edges: ApiEdge[];
}

export interface SubgraphResponse {
  seed: string;
  hops: number;
  nodes: ApiNode[];
  edges: ApiEdge[];
  truncated: boolean;
}

export interface SearchHit {
  id: string;
  name: string;
  score: number;
  matched_alias_normalized: string;
}

export interface SearchResponse {
  q: string;
  results: SearchHit[];
}

export interface HealthResponse {
  status: "ok" | "INVARIANT_VIOLATED" | string;
  node_count: number;
  edge_count: number;
  core_edge_count: number;
  audit_edge_count: number;
  customer_of_rows_in_db: number;
}

// ---------------------------------------------------------------------------
// Low-level fetch + simple loading-event bus
// ---------------------------------------------------------------------------

type LoadingListener = (loading: boolean) => void;
const loadingListeners = new Set<LoadingListener>();
let inflight = 0;

export function onLoadingChange(fn: LoadingListener): () => void {
  loadingListeners.add(fn);
  return () => loadingListeners.delete(fn);
}

function notifyLoading() {
  for (const fn of loadingListeners) fn(inflight > 0);
}

// Default timeout for graph/search fetches. Impact calls use a higher
// timeout because the LLM backend can take 1–3 minutes; those go through
// runImpact() / runMultiImpact() which carry their own AbortSignal.
// 90 s is intentionally generous: the full-core /subgraph call (5 300+ nodes,
// 13 000+ edges) returns a large JSON payload that can take 45–70 s over a
// local FastAPI + SQLite stack on first load. Node/ego fetches complete in
// under 1 s so the longer ceiling costs nothing in practice.
const FETCH_TIMEOUT_MS = 90_000;

async function get<T>(path: string, params: Record<string, string | boolean | number | undefined | null> = {}): Promise<T> {
  const url = new URL(path, API_BASE_URL);
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === "") continue;
    url.searchParams.set(k, String(v));
  }
  inflight += 1;
  notifyLoading();
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS);
  try {
    const resp = await fetch(url.toString(), { method: "GET", signal: controller.signal });
    if (!resp.ok) {
      const body = await resp.text().catch(() => "");
      throw new ApiError(resp.status, `${resp.statusText} - ${body.slice(0, 200)}`);
    }
    try {
      return (await resp.json()) as T;
    } catch {
      throw new Error(`${path}: server returned non-JSON response`);
    }
  } finally {
    clearTimeout(timeoutId);
    inflight -= 1;
    notifyLoading();
  }
}

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

// ---------------------------------------------------------------------------
// Endpoints
// ---------------------------------------------------------------------------

export function getHealth(): Promise<HealthResponse> {
  return get<HealthResponse>("/health");
}

export function getNode(id: string): Promise<ApiNode> {
  // Path segments with embedded colons (cik:...) need encoding for the URL
  // parser, but the API uses :path so we keep them readable. encodeURIComponent
  // is too aggressive (it escapes the ':'); use encodeURI instead.
  return get<ApiNode>(`/node/${encodeURI(id)}`);
}

export function getEgo(
  id: string,
  opts: {
    types?: EdgeType[];
    includeProvisional?: boolean;
    includeInferred?: boolean;
  } = {},
): Promise<EgoResponse> {
  return get<EgoResponse>(`/node/${encodeURI(id)}/ego`, {
    types: opts.types?.length ? opts.types.join(",") : undefined,
    include_provisional: opts.includeProvisional ?? false,
    include_inferred: opts.includeInferred ?? false,
  });
}

export function getSubgraph(
  seed: string,
  opts: {
    hops?: number;
    types?: EdgeType[];
    includeProvisional?: boolean;
    includeInferred?: boolean;
  } = {},
): Promise<SubgraphResponse> {
  return get<SubgraphResponse>("/subgraph", {
    seed,
    hops: opts.hops ?? 2,
    types: opts.types?.length ? opts.types.join(",") : undefined,
    include_provisional: opts.includeProvisional ?? false,
    include_inferred: opts.includeInferred ?? false,
  });
}

export function getEdge(id: string): Promise<ApiEdge> {
  return get<ApiEdge>(`/edge/${encodeURI(id)}`);
}

export function search(q: string, limit = 10): Promise<SearchResponse> {
  return get<SearchResponse>("/search", { q, limit });
}

// ---------------------------------------------------------------------------
// Impact propagation (LLM-driven)
// ---------------------------------------------------------------------------

export interface ImpactEventVerdict {
  event_idx: number;
  event_text: string;
  direction: "positive" | "negative" | "no_effect";
  magnitude: number;
  hop: number;
  reasoning: string;
}

export interface ImpactVerdict {
  node_id: string;
  name: string;
  type: string;
  direction: "positive" | "negative" | "no_effect";
  magnitude: number;
  hop: number;
  reasoning: string;
  via_parent: string | null;
  edge_type: string | null;
  // Multi-event extensions (only present on merged verdicts from /impact/multi)
  mixed_signals?: boolean;
  event_verdicts?: ImpactEventVerdict[];
}

export interface ImpactResponse {
  /** Primary seed (first named entity, or commodity/region if none resolved).
   *  Kept for backward-compat — use `seeds` for the full list. */
  seed: ImpactVerdict | null;
  /** All hop-0 seeds: named entities from the news + commodity/region seed. */
  seeds?: ImpactVerdict[];
  impacts: ImpactVerdict[];
  provider?: string;
  model?: string;
  max_hops?: number;
  debug?: string[];
  error?: string;
}

export type ImpactProvider = "claude" | "ollama";

// ---------------------------------------------------------------------------
// Multi-news impact
// ---------------------------------------------------------------------------

/** One event's raw result within a multi-news response. */
export interface MultiImpactEvent {
  text: string;
  seed: ImpactVerdict | null;
  seeds?: ImpactVerdict[];
  impacts: ImpactVerdict[];
  error?: string;
}

/** Result of POST /impact/multi — merged across all events. */
export interface MultiImpactResponse {
  events: MultiImpactEvent[];
  /** Merged/netted verdicts — one per unique node across all events. */
  merged: ImpactVerdict[];
  provider?: string;
  model?: string;
  event_count?: number;
  total_nodes?: number;
  mixed_signal_nodes?: number;
  error?: string;
}

export interface DescribeResponse {
  node_id: string;
  description: string;
}

// Timeout for LLM-backed calls (describe, impact). The LLM can take 1-3 min.
const LLM_TIMEOUT_MS = 180_000;

export async function describeNode(nodeId: string): Promise<DescribeResponse> {
  const url = new URL(`/describe/${encodeURI(nodeId)}`, API_BASE_URL);
  inflight += 1;
  notifyLoading();
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), LLM_TIMEOUT_MS);
  try {
    const resp = await fetch(url.toString(), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
      signal: controller.signal,
    });
    if (!resp.ok) {
      const body = await resp.text().catch(() => "");
      throw new ApiError(resp.status, `${resp.statusText} - ${body.slice(0, 200)}`);
    }
    return (await resp.json()) as DescribeResponse;
  } finally {
    clearTimeout(timeoutId);
    inflight -= 1;
    notifyLoading();
  }
}

export async function runMultiImpact(
  texts: string[],
  opts: { provider?: ImpactProvider; signal?: AbortSignal } = {},
): Promise<MultiImpactResponse> {
  const url = new URL("/impact/multi", API_BASE_URL);
  inflight += 1;
  notifyLoading();
  try {
    const body: Record<string, unknown> = { texts };
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
    try {
      return (await resp.json()) as MultiImpactResponse;
    } catch {
      throw new Error("/impact/multi: server returned non-JSON response");
    }
  } finally {
    inflight -= 1;
    notifyLoading();
  }
}

export async function runImpact(
  text: string,
  opts: { provider?: ImpactProvider; signal?: AbortSignal } = {},
): Promise<ImpactResponse> {
  const url = new URL("/impact", API_BASE_URL);
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
    try {
      return (await resp.json()) as ImpactResponse;
    } catch {
      throw new Error("/impact: server returned non-JSON response");
    }
  } finally {
    inflight -= 1;
    notifyLoading();
  }
}
