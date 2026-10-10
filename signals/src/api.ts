// API client for the standalone Signals dashboard. Consumes the shared EconGraph reliability API.
import { API_BASE_URL } from "./config";

export interface RelStat {
  n: number; hits: number; hit_rate: number; ci_low: number; ci_high: number;
  mean_abn: number; median_abn: number; ic: number | null;
  net_hit_rate?: number; net_ci_low?: number; net_ci_high?: number; mean_net?: number;
  dir_acc?: number | null; mean_gap?: number | null;
}
export interface RelHopHorizon { hop: number; horizon_days: number; all: RelStat; clean: RelStat | null; }
export interface RelMagBand extends RelStat { mag_band: string; horizon_days: number; }
export interface RelCoverage {
  directional_signals: number; signals_with_mature_outcome: number; outcome_rows: number;
  mature_by_horizon: Record<string, number>; distinct_events: number; distinct_nodes: number;
}
export interface RelIntraday {
  horizons: number[];
  overall: Record<string, RelStat>;
  by_hop_horizon: RelHopHorizon[];
  coverage: {
    outcome_rows: number; distinct_events: number; distinct_nodes: number;
    latency_min: number | null; mature_by_horizon: Record<string, number>;
  };
}
export interface RelDynStat extends RelStat { hop?: number; avg_hold?: number; }
export interface RelDynamic {
  overall: RelDynStat;
  by_hop: RelDynStat[];
  coverage: {
    outcome_rows: number; distinct_nodes: number; avg_hold: number;
    exit_reasons: Record<string, number>; max_hold: number | null;
  };
}
export interface ReliabilitySummary {
  status: "ok" | "not_computed";
  message?: string;
  generated_at?: string;
  meta?: { horizons: number[]; mag_bands: Record<string, string>; z: number; ic_min_n: number; benchmark: string; entry?: string; cost_bps?: number };
  overall: Record<string, RelStat>;
  by_hop_horizon: RelHopHorizon[];
  by_magband: RelMagBand[];
  coverage: RelCoverage;
  intraday?: RelIntraday | null;
  dynamic?: RelDynamic | null;
}

async function get<T>(path: string): Promise<T> {
  const resp = await fetch(new URL(path, API_BASE_URL).toString(), { method: "GET" });
  if (!resp.ok) throw new Error(`${path}: ${resp.status} ${resp.statusText}`);
  return (await resp.json()) as T;
}

export function getReliabilitySummary(): Promise<ReliabilitySummary> {
  return get<ReliabilitySummary>("/reliability/summary");
}
