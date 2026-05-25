// localStorage-backed archive for completed impact traces.
// Each entry lives for 24 hours, then is pruned on next load.

import type { ImpactResponse } from "./api";

export interface ArchiveEntry {
  id: string;
  timestamp: number;        // Date.now() when saved
  expiresAt: number;        // timestamp + 24 h
  text: string;             // the user's query text
  provider: string;         // "claude" | "ollama"
  seedName: string;
  seedDirection: string;
  nodesCount: number;
  maxHops: number;
  response: ImpactResponse; // full response, used to rebuild ImpactState
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

/** Persist a completed impact run. Returns the new entry. */
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
    response: resp,
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
