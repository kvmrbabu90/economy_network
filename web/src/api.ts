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

async function get<T>(path: string, params: Record<string, string | boolean | number | undefined | null> = {}): Promise<T> {
  const url = new URL(path, API_BASE_URL);
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === "") continue;
    url.searchParams.set(k, String(v));
  }
  inflight += 1;
  notifyLoading();
  try {
    const resp = await fetch(url.toString(), { method: "GET" });
    if (!resp.ok) {
      const body = await resp.text().catch(() => "");
      throw new ApiError(resp.status, `${resp.statusText} - ${body.slice(0, 200)}`);
    }
    return (await resp.json()) as T;
  } finally {
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
  opts: { types?: EdgeType[]; includeProvisional?: boolean } = {},
): Promise<EgoResponse> {
  return get<EgoResponse>(`/node/${encodeURI(id)}/ego`, {
    types: opts.types?.length ? opts.types.join(",") : undefined,
    include_provisional: opts.includeProvisional ?? false,
  });
}

export function getSubgraph(
  seed: string,
  opts: { hops?: number; types?: EdgeType[]; includeProvisional?: boolean } = {},
): Promise<SubgraphResponse> {
  return get<SubgraphResponse>("/subgraph", {
    seed,
    hops: opts.hops ?? 2,
    types: opts.types?.length ? opts.types.join(",") : undefined,
    include_provisional: opts.includeProvisional ?? false,
  });
}

export function getEdge(id: string): Promise<ApiEdge> {
  return get<ApiEdge>(`/edge/${encodeURI(id)}`);
}

export function search(q: string, limit = 10): Promise<SearchResponse> {
  return get<SearchResponse>("/search", { q, limit });
}
