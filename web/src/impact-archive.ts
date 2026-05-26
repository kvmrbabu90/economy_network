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
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(entries));
  } catch {
    // Quota exceeded — drop the oldest entry and retry once.
    const trimmed = entries.slice(0, entries.length - 1);
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify(trimmed)); } catch { /* give up */ }
  }
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
  const entry: ArchiveEntry = {
    id: `${now}-${Math.random().toString(36).slice(2, 7)}`,
    timestamp: now,
    expiresAt: now + TTL_MS,
    text,
    provider,
    seedName: resp.seed?.name ?? "Unknown",
    seedDirection: resp.seed?.direction ?? "no_effect",
    nodesCount: resp.impacts.length,
    maxHops: resp.max_hops ?? 3,
    isMulti: false,
    response: resp,
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
    multiResponse: resp,
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
