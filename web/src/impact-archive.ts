// localStorage-backed archive for completed impact traces.
// Each entry lives for 24 hours, then is pruned on next load.

import type { ImpactResponse, MultiImpactResponse } from "./api";

export interface ArchiveEntry {
  id: string;
  timestamp: number;        // Date.now() when saved
  expiresAt: number;        // timestamp + 24 h
  text: string;             // single-event text, OR joined texts for display
  texts?: string[];         // multi-event: individual news items
  provider: string;         // "claude" | "ollama"
  seedName: string;
  seedDirection: string;
  nodesCount: number;
  maxHops: number;
  isMulti?: boolean;
  response?: ImpactResponse;          // single-event result
  multiResponse?: MultiImpactResponse; // multi-event result
}

const STORAGE_KEY = "econgraph_impact_archive";
const TTL_MS = 24 * 60 * 60 * 1000;

function readRaw(): ArchiveEntry[] {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw ? (JSON.parse(raw) as ArchiveEntry[]) : [];
  } catch {
    return [];
  }
}

function writeRaw(entries: ArchiveEntry[]): void {
  // Try progressively smaller slices (drop oldest first — entries are newest-first)
  // until the write succeeds or we give up after 3 attempts.
  let list = entries;
  for (let attempt = 0; attempt < 3; attempt++) {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(list));
      return;
    } catch {
      if (list.length === 0) return; // nothing left to drop
      // Drop the oldest entry (last in the newest-first array).
      list = list.slice(0, list.length - 1);
    }
  }
  // Last-ditch: try writing whatever is left.
  try { localStorage.setItem(STORAGE_KEY, JSON.stringify(list)); } catch { /* give up */ }
}

/** Drop entries older than 24 h. Called automatically on every read. */
export function pruneExpired(): void {
  const now = Date.now();
  const entries = readRaw().filter((e) => e.expiresAt > now);
  writeRaw(entries);
}

/** Returns all non-expired entries, newest first. */
export function loadArchive(): ArchiveEntry[] {
  pruneExpired();
  return readRaw().sort((a, b) => b.timestamp - a.timestamp);
}

/** Persist a single-event impact run. */
export function saveToArchive(
  text: string,
  provider: string,
  resp: ImpactResponse,
): ArchiveEntry {
  const now = Date.now();
  // Strip debug logs before archiving to save localStorage space.
  // The debug field can be 100+ lines and is only useful during development.
  const { debug: _debug, ...respWithoutDebug } = resp as ImpactResponse & { debug?: unknown };
  void _debug;
  // Prefer seed name from the seeds array (multi-seed path) so "Unknown"
  // never shows when named-entity seeds resolved but the commodity seed was null.
  const primarySeed = resp.seed ?? resp.seeds?.[0] ?? null;
  const entry: ArchiveEntry = {
    id: `${now}-${Math.random().toString(36).slice(2, 7)}`,
    timestamp: now,
    expiresAt: now + TTL_MS,
    text,
    provider,
    seedName: primarySeed?.name ?? "Unknown",
    seedDirection: primarySeed?.direction ?? "no_effect",
    nodesCount: resp.impacts.length,
    maxHops: resp.max_hops ?? 3,
    isMulti: false,
    response: respWithoutDebug as ImpactResponse,
  };
  const entries = readRaw();
  entries.unshift(entry);
  writeRaw(entries);
  return entry;
}

/** Persist a multi-event impact run. */
export function saveMultiToArchive(
  texts: string[],
  provider: string,
  resp: MultiImpactResponse,
): ArchiveEntry {
  const now = Date.now();
  const mixedCount = resp.mixed_signal_nodes ?? 0;
  const seedName = resp.events
    .map(e => e.seed?.name ?? e.seeds?.[0]?.name ?? "?")
    .join(" + ");
  // Strip per-event debug logs before archiving to save localStorage space.
  const slimResp: MultiImpactResponse = {
    ...resp,
    events: resp.events.map(ev => {
      const { debug: _d, refinement: _r, ...evSlim } = ev as typeof ev & { debug?: unknown; refinement?: unknown };
      void _d; void _r;
      return evSlim as typeof ev;
    }),
  };
  const entry: ArchiveEntry = {
    id: `${now}-${Math.random().toString(36).slice(2, 7)}`,
    timestamp: now,
    expiresAt: now + TTL_MS,
    text: texts.join(" | "),
    texts,
    provider,
    seedName,
    seedDirection: mixedCount > 0 ? "mixed" : (resp.merged[0]?.direction ?? "no_effect"),
    nodesCount: resp.merged.length,
    maxHops: 3,
    isMulti: true,
    multiResponse: slimResp,
  };
  const entries = readRaw();
  entries.unshift(entry);
  writeRaw(entries);
  return entry;
}

/** Remove a single entry by id. */
export function removeFromArchive(id: string): void {
  writeRaw(readRaw().filter((e) => e.id !== id));
}
