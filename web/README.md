# EconGraph web frontend (Phase 6)

A Vite + TypeScript + Sigma.js app that calls the Phase 5 API and lets you
navigate the graph by clicking.

## Run

You need both the API and the dev server running.

```sh
# Terminal 1 — start the API (Phase 5)
cd <repo-root>
uvicorn api.main:app --port 8101

# Terminal 2 — start the Vite dev server
cd <repo-root>/web
npm install        # first time only
npm run dev        # serves http://localhost:5180 (or the next free port)
```

Then open `http://localhost:5180` in a browser. The status pill in the top
right should turn green and read `166 nodes · 204 core + 156 audit` once
the API is reachable.

To point the frontend at a non-default API URL, override at start time:

```sh
VITE_API_BASE_URL=http://localhost:8888 npm run dev
```

## What you can do

- **Search.** Type a name, ticker, or alias in the top bar (`procter`, `PG`,
  `walmart`). Pick a result to re-center on that node.
- **Click a node.** Expands its neighbors in place; existing nodes stay,
  new ones fan out, layout re-settles.
- **Double-click a node.** Re-centers the canvas on that node and resets
  the view to its ego graph.
- **Click an edge.** Opens the provenance panel showing the verbatim
  filing snippet, the source filing accession, and a link to the SEC
  document. For a derived `customer_of` edge, you see the underlying
  `supplies` edge's provenance — same evidence, reversed view.
- **Edge-type filters (left panel).** Toggle `supplies` / `customer_of` /
  `competes_with` / `regulated_by` on or off. Affects what's rendered
  AND what's fetched on the next navigation.
- **Provisional toggle.** Off by default — clean high-confidence core only.
  On — adds the grey halo of below-threshold edges and provisional slug
  nodes (Nestlé, Unilever, Red Bull, ...).

## Three acceptance diagrams (Phase 6)

See [`docs/screenshots/`](../docs/screenshots/) for the captured states:

1. `01-full-core.png` — open-graph view of the high-confidence core (43
   nodes, 222 edges) with the regulator cluster at top.
2. `02-costco-ego.png` — Costco re-centered on, surrounded by its
   regulators and the competitors named in its 10-K.
3. `03-pg-ego.png` — P&G's four-player neighborhood.
4. `04-pg-walmart-chain.png` — the connected value chain: P&G as one
   hub, Walmart as the other, joined by P&G's supplies edge and
   surrounded by the shared regulator cluster.

## Configuration constants

All in [`src/config.ts`](src/config.ts):

- `API_BASE_URL` — overridden by `VITE_API_BASE_URL`.
- `OPEN_MODE` — `"full"` (default; load the whole core on open) or
  `"search"` (start near-empty with the search box focused). Flip to
  `"search"` when the graph stops being readable at larger scale.
- `FULL_VIEW_SEED` — node id to seed the open-graph subgraph from.
  Defaults to `regulator:sec`, which every filer touches via its
  `regulated_by` edge.
- `LABEL_TOP_N` — number of high-degree nodes that get persistent labels;
  the rest only appear on hover.

## Build

```sh
npm run build        # type-check + production bundle into dist/
npm run preview      # serve the production build locally
npm test             # vitest smoke tests over the API client
```
