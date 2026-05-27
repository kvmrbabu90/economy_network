// graph-cache.ts — persist the full-core SubgraphResponse to IndexedDB so
// page refreshes skip the 45–70 s cold API fetch and load from disk in
// ~100–200 ms instead.
//
// Design constraints:
//  • Async throughout — never blocks the UI thread.
//  • All IDB errors are swallowed with a console.warn so a quota hit or
//    private-browsing restriction never breaks the app.
//  • Cache keyed by (seed + filter flags) so changing provisional/inferred
//    toggles transparently fetches fresh data for the new filter set.
//  • 24-hour TTL. Graph data changes when the pipeline re-runs (~weekly in
//    practice), so a day of staleness is acceptable. Call clearSubgraph()
//    from the browser console to force a cold refresh at any time.

import type { SubgraphResponse } from "./api";

const DB_NAME    = "econgraph-cache";
const DB_VERSION = 2;          // bumped to add "positions" store
const STORE      = "subgraph";
const POS_STORE  = "positions"; // layout position cache (skip FA2 on warm load)
const TTL_MS     = 24 * 60 * 60 * 1000; // 24 h

interface CacheEntry {
  /** Composite key: "<seed>:<filterFlags>" — doubles as the IDB keyPath. */
  cacheKey:  string;
  savedAt:   number;
  data:      SubgraphResponse;
}

interface PositionEntry {
  cacheKey:  string;
  savedAt:   number;
  /** Serialised as a plain object so IDB can store it. */
  positions: Record<string, { x: number; y: number }>;
}

// ---------------------------------------------------------------------------
// IDB helpers
// ---------------------------------------------------------------------------

function openDB(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    // 8-second safety timeout — onblocked can hang forever if another
    // connection is open with a lower version and never closes. Without
    // this, loadPositions (and any IDB reader) waits indefinitely and
    // the graph-overlay stays frozen at "Restoring layout…".
    const timer = setTimeout(() => {
      console.warn("[graph-cache] openDB timed out — IDB may be blocked");
      reject(new Error("IDB open timed out"));
    }, 8_000);

    const req = indexedDB.open(DB_NAME, DB_VERSION);
    req.onupgradeneeded = () => {
      // Create object stores on first open or version bump.
      const db = req.result;
      if (!db.objectStoreNames.contains(STORE)) {
        db.createObjectStore(STORE, { keyPath: "cacheKey" });
      }
      if (!db.objectStoreNames.contains(POS_STORE)) {
        db.createObjectStore(POS_STORE, { keyPath: "cacheKey" });
      }
    };
    req.onsuccess = () => {
      clearTimeout(timer);
      const db = req.result;
      // Close voluntarily when a newer-version upgrade is requested.
      // Without this, v1→v2 upgrades (or any future bump) stall forever:
      // the old connection never goes away, onblocked fires on the new
      // request, and the 8-second timeout kills the caller.
      db.onversionchange = () => { db.close(); };
      resolve(db);
    };
    req.onerror   = () => { clearTimeout(timer); reject(req.error); };
    // onblocked fires when a higher-version open is attempted while lower-
    // version connections are still open. The timeout above handles this,
    // but log it immediately so it shows up in the console right away.
    req.onblocked = () => {
      console.warn("[graph-cache] IDB open blocked — waiting for other connections to close");
    };
  });
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/**
 * Persist a SubgraphResponse to IndexedDB.
 * Fire-and-forget — the caller should NOT await this; it runs in the
 * background so it never delays the render pipeline.
 */
export async function saveSubgraph(
  cacheKey: string,
  data: SubgraphResponse,
): Promise<void> {
  try {
    const db = await openDB();
    const entry: CacheEntry = { cacheKey, savedAt: Date.now(), data };
    await new Promise<void>((resolve, reject) => {
      const tx = db.transaction(STORE, "readwrite");
      tx.objectStore(STORE).put(entry);
      tx.oncomplete = () => resolve();
      tx.onerror    = () => reject(tx.error);
    });
    db.close();
    const mb = (JSON.stringify(data).length / 1_048_576).toFixed(1);
    console.debug(`[graph-cache] saved ${data.nodes.length} nodes, ${data.edges.length} edges (${mb} MB) under key "${cacheKey}"`);
  } catch (err) {
    console.warn("[graph-cache] save failed — falling back to uncached:", err);
  }
}

/**
 * Load a previously saved SubgraphResponse from IndexedDB.
 * Returns null if:
 *  • No entry exists for this cacheKey.
 *  • The entry has expired (> 24 h old).
 *  • Any IDB error occurs.
 */
export async function loadSubgraph(
  cacheKey: string,
): Promise<SubgraphResponse | null> {
  try {
    const db = await openDB();
    const entry = await new Promise<CacheEntry | undefined>((resolve, reject) => {
      const tx  = db.transaction(STORE, "readonly");
      const req = tx.objectStore(STORE).get(cacheKey);
      req.onsuccess = () => resolve(req.result as CacheEntry | undefined);
      req.onerror   = () => reject(req.error);
    });
    db.close();

    if (!entry) return null;

    const ageMs = Date.now() - entry.savedAt;
    if (ageMs > TTL_MS) {
      console.debug(`[graph-cache] entry "${cacheKey}" expired (${Math.round(ageMs / 3_600_000)}h old) — will re-fetch`);
      return null;
    }

    const ageMin = Math.round(ageMs / 60_000);
    console.debug(`[graph-cache] hit "${cacheKey}" (${ageMin} min old) — skipping API fetch`);
    return entry.data;
  } catch (err) {
    console.warn("[graph-cache] load failed — falling back to API:", err);
    return null;
  }
}

/**
 * Persist FA2-computed layout positions to IndexedDB.
 * Fire-and-forget — the caller should NOT await this.
 * Positions are stored as a plain object keyed by node ID.
 * Allows subsequent warm loads to skip the 6+ s synchronous FA2 pass.
 */
export async function savePositions(
  cacheKey: string,
  positions: Map<string, { x: number; y: number }>,
): Promise<void> {
  try {
    const db = await openDB();
    const posObj: Record<string, { x: number; y: number }> = {};
    positions.forEach((v, k) => { posObj[k] = v; });
    const entry: PositionEntry = { cacheKey, savedAt: Date.now(), positions: posObj };
    await new Promise<void>((resolve, reject) => {
      const tx = db.transaction(POS_STORE, "readwrite");
      tx.objectStore(POS_STORE).put(entry);
      tx.oncomplete = () => resolve();
      tx.onerror    = () => reject(tx.error);
    });
    db.close();
    console.debug(`[graph-cache] saved ${positions.size} positions under key "${cacheKey}"`);
  } catch (err) {
    console.warn("[graph-cache] savePositions failed:", err);
  }
}

/**
 * Load previously saved layout positions from IndexedDB.
 * Returns null if no entry exists, the entry is expired, or any IDB error occurs.
 */
export async function loadPositions(
  cacheKey: string,
): Promise<Map<string, { x: number; y: number }> | null> {
  try {
    const db = await openDB();
    const entry = await new Promise<PositionEntry | undefined>((resolve, reject) => {
      const tx  = db.transaction(POS_STORE, "readonly");
      const req = tx.objectStore(POS_STORE).get(cacheKey);
      req.onsuccess = () => resolve(req.result as PositionEntry | undefined);
      req.onerror   = () => reject(req.error);
    });
    db.close();

    if (!entry) return null;

    const ageMs = Date.now() - entry.savedAt;
    if (ageMs > TTL_MS) {
      console.debug(`[graph-cache] positions "${cacheKey}" expired — will re-run FA2`);
      return null;
    }

    const map = new Map<string, { x: number; y: number }>();
    for (const [k, v] of Object.entries(entry.positions)) map.set(k, v);
    console.debug(`[graph-cache] loaded ${map.size} positions for "${cacheKey}" — skipping FA2`);
    return map;
  } catch (err) {
    console.warn("[graph-cache] loadPositions failed — will re-run FA2:", err);
    return null;
  }
}

/**
 * Wipe all cached subgraph entries.
 * Useful from the browser console: `import('./graph-cache').then(m => m.clearSubgraph())`
 * Or just: `indexedDB.deleteDatabase('econgraph-cache')`
 */
export async function clearSubgraph(): Promise<void> {
  try {
    const db = await openDB();
    await new Promise<void>((resolve, reject) => {
      const tx = db.transaction([STORE, POS_STORE], "readwrite");
      tx.objectStore(STORE).clear();
      tx.objectStore(POS_STORE).clear();
      tx.oncomplete = () => resolve();
      tx.onerror    = () => reject(tx.error);
    });
    db.close();
    console.info("[graph-cache] cleared (subgraph + positions)");
  } catch (err) {
    console.warn("[graph-cache] clear failed:", err);
  }
}
