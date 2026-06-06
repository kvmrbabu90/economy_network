// Config constants. Keep at the top of the file so they're trivial to retune.

// The Phase 5 API base. Override via VITE_API_BASE_URL at build time if needed.
// Default 8101 (not 8001) because 8000-range ports tend to be busy with other
// dev servers; the Phase 5 CORS rule matches any localhost port so the only
// thing to keep in sync is uvicorn's --port flag.
export const API_BASE_URL: string =
  // Use 127.0.0.1 (IPv4) not localhost — on Windows, localhost resolves to
  // ::1 (IPv6) first, but uvicorn --host 0.0.0.0 only binds IPv4. The browser
  // would silently connect to ::1:8101 (refused) and show "API unreachable".
  (import.meta.env.VITE_API_BASE_URL as string | undefined) ?? "http://127.0.0.1:8101";

// "full"   — fetch the whole high-confidence core on load (default).
// "search" — start near-empty with the search box focused; switch here when
//            the full graph stops being readable at larger scale. One-line
//            change, no rewrite.
// Phase 7 flip: "full" mode opens with all ~2400 nodes loaded -- the
// resulting hairball isn't a useful first impression at full-economy scale.
// "search" opens with the search box focused; the user fans out from a
// chosen node via the established click-expand / double-click-recenter
// gestures. The "Full graph" button in the topbar still loads the whole
// thing on demand.
export const OPEN_MODE: "full" | "search" = "search";

// When OPEN_MODE = "full", we seed a subgraph fetch at this node and crawl
// at MAX_HOPS to cover the whole core. The SEC regulator is reached by every
// filer (every company has a regulated_by edge to it), so it makes the best
// connected seed.
export const FULL_VIEW_SEED = "regulator:sec";
export const FULL_VIEW_HOPS = 3;

// Persistent labels for the most-connected nodes; everything else only on hover.
export const LABEL_TOP_N = 12;
