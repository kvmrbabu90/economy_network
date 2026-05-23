// Config constants. Keep at the top of the file so they're trivial to retune.

// The Phase 5 API base. Override via VITE_API_BASE_URL at build time if needed.
export const API_BASE_URL: string =
  (import.meta.env.VITE_API_BASE_URL as string | undefined) ?? "http://localhost:8001";

// "full"   — fetch the whole high-confidence core on load (default).
// "search" — start near-empty with the search box focused; switch here when
//            the full graph stops being readable at larger scale. One-line
//            change, no rewrite.
export const OPEN_MODE: "full" | "search" = "full";

// When OPEN_MODE = "full", we seed a subgraph fetch at this node and crawl
// at MAX_HOPS to cover the whole core. The SEC regulator is reached by every
// filer (every company has a regulated_by edge to it), so it makes the best
// connected seed.
export const FULL_VIEW_SEED = "regulator:sec";
export const FULL_VIEW_HOPS = 3;

// Persistent labels for the most-connected nodes; everything else only on hover.
export const LABEL_TOP_N = 12;
