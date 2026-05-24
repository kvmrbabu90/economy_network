"""Phase 4: load resolved Nodes/Edges into SQLite + emit graphology graph.json.

Deterministic, no LLM/API/network. Three artifacts come out of this stage:

    econgraph.db          -- SQLite source of truth (PRD §6: "Graph store").
                             Every write goes through the validated Pydantic
                             upserts in schema/store.py.
    data/graph.json       -- graphology MultiGraph export. This is what
                             Phase 5's FastAPI serves and Phase 6's Sigma.js
                             renderer loads.
    <stats report>        -- printed to stdout; tells you what the graph
                             actually LOOKS LIKE before two more phases of
                             plumbing get built on top of it.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

from schema.models import Edge, EdgeType, Node, NodeType
from schema.store import (
    add_alias_rows,
    add_aliases,
    connect,
    init_db,
    upsert_edge,
    upsert_node,
)


log = logging.getLogger("build_graph")


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------

def _load_jsonl(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


# ---------------------------------------------------------------------------
# Part A -- load into SQLite
# ---------------------------------------------------------------------------

@dataclass
class LoadResult:
    nodes: int
    edges: int
    aliases: int
    orphan_edges: list[dict[str, str]]


def load_into_sqlite(
    db_path: Path,
    *,
    nodes: list[dict],
    edges: list[dict],
    aliases: list[dict],
    below_threshold_edges: Optional[list[dict]] = None,
    fresh: bool = True,
) -> LoadResult:
    """Wipe (if fresh) + load nodes/edges/aliases into SQLite via validated upserts.

    Referential integrity is checked AFTER nodes are loaded but BEFORE edges
    are inserted (sqlite FOREIGN KEY would reject orphans at insert time --
    we want to report them collectively rather than die on the first one).
    """
    if fresh and db_path.exists():
        db_path.unlink()
    conn = connect(db_path)
    init_db(conn)

    # Nodes first -- they must exist before edges reference them.
    for n in nodes:
        upsert_node(conn, n)
    log.info("Loaded %d nodes into %s", len(nodes), db_path)

    below_threshold_edges = below_threshold_edges or []

    # Referential-integrity check across BOTH layers -- audit edges must
    # reference nodes that exist too. Fail loud collectively.
    node_ids = {n["id"] for n in nodes}
    orphans: list[dict[str, str]] = []
    for e in edges + below_threshold_edges:
        if e["source"] not in node_ids:
            orphans.append({"edge_id": e["id"], "missing": "source", "id": e["source"]})
        if e["target"] not in node_ids:
            orphans.append({"edge_id": e["id"], "missing": "target", "id": e["target"]})
    if orphans:
        conn.close()
        raise RuntimeError(
            f"Referential-integrity FAILED: {len(orphans)} orphan edge endpoints. "
            f"First few: {orphans[:5]}"
        )

    for e in edges:
        upsert_edge(conn, e, below_threshold=False)
    log.info("Loaded %d core edges", len(edges))

    for e in below_threshold_edges:
        upsert_edge(conn, e, below_threshold=True)
    if below_threshold_edges:
        log.info("Loaded %d audit (below-threshold) edges", len(below_threshold_edges))

    # Aliases. We prefer the richer rows-based loader so alias_normalized
    # is persisted alongside the raw alias (Phase 5 /search needs it).
    n_aliases = add_alias_rows(conn, aliases) if aliases else 0
    log.info("Loaded %d aliases", n_aliases)

    # Quick sanity: counts read back.
    n_count = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
    e_count = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    a_count = conn.execute("SELECT COUNT(*) FROM aliases").fetchone()[0]
    conn.close()
    log.info("SQLite read-back: %d nodes / %d edges / %d aliases", n_count, e_count, a_count)
    return LoadResult(nodes=n_count, edges=e_count, aliases=a_count, orphan_edges=[])


# ---------------------------------------------------------------------------
# Part B -- emit graphology graph.json
# ---------------------------------------------------------------------------

def _undirected_for(edge_type: str) -> bool:
    # competes_with is the only undirected type in the MVP. supplies and
    # regulated_by stay directed (and `customer_of` is derived at query time
    # in Phase 5, never persisted).
    return edge_type == EdgeType.competes_with.value


def to_graphology(
    nodes: list[dict],
    edges: list[dict],
    *,
    below_threshold_edges: Optional[list[dict]] = None,
) -> dict[str, Any]:
    """Serialize to the graphology MultiGraph export shape.

    `below_threshold_edges` (default None) are included in the export with a
    `below_threshold: True` attribute so the renderer can dim or dash them.
    SQLite + edges.jsonl remain the clean above-cutoff core; this export is
    where the audit layer becomes visually accessible.
    """
    g_nodes = []
    for n in nodes:
        meta = n.get("metadata", {}) or {}
        is_provisional = bool(meta.get("provisional", False))
        identifiers = n.get("identifiers", {}) or {}
        attrs = {
            "label": n.get("name") or n["id"],
            "type": n.get("type", "Company"),
            "sector": n.get("sector"),
            "industry": n.get("industry"),
            "country": n.get("country"),
            "provisional": is_provisional,
            "identity_unverified": bool(meta.get("identity_unverified", False)),
            "tickers": n.get("tickers", []) or [],
            "cik": identifiers.get("cik"),
        }
        g_nodes.append({"key": n["id"], "attributes": attrs})

    g_edges = []

    def _emit_edge(e, below_threshold: bool):
        t = e["type"]
        attrs = {
            "type": t,
            "confidence": e.get("confidence"),
            "directed": not _undirected_for(t),
            "extracted_by": (e.get("provenance") or {}).get("extracted_by"),
            "filing": (e.get("provenance") or {}).get("filing"),
            "snippet": (e.get("provenance") or {}).get("snippet"),
            "additional_sources": len(e.get("additional_provenance", []) or []),
            "below_threshold": below_threshold,
        }
        g_edges.append(
            {
                "key": e["id"],
                "source": e["source"],
                "target": e["target"],
                "attributes": attrs,
                "undirected": _undirected_for(t),
            }
        )

    for e in edges:
        _emit_edge(e, below_threshold=False)
    for e in below_threshold_edges or []:
        _emit_edge(e, below_threshold=True)

    return {
        # graphology import respects these options.
        "options": {"type": "mixed", "multi": True, "allowSelfLoops": False},
        "attributes": {
            "name": "EconGraph (Consumer Staples MVP)",
            "phase": "4",
            "node_count": len(g_nodes),
            "edge_count": len(g_edges),
        },
        "nodes": g_nodes,
        "edges": g_edges,
    }


def write_graph_json(graph: dict[str, Any], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(graph, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Part C -- stats report
# ---------------------------------------------------------------------------

@dataclass
class Stats:
    nodes_by_type: dict[str, int]
    provisional_count: int
    edges_by_type: dict[str, int]            # above-cutoff (the "core" graph)
    edges_by_type_audit: dict[str, int]      # below-cutoff (the audit layer)
    top_by_total_degree: list[tuple[str, str, int, int, int]]  # (id, label, total, in, out)
    top_provisional_by_degree: list[tuple[str, str, int]]      # (id, label, total)
    components: int
    largest_component_size: int
    supply_layer: dict[str, int]


def _build_degree_tables(nodes: list[dict], edges: list[dict]):
    """Compute in/out degree + total degree per node id."""
    in_deg: Counter = Counter()
    out_deg: Counter = Counter()
    for e in edges:
        if _undirected_for(e["type"]):
            # Count undirected edges toward both endpoints' total degree only
            # (not as inbound/outbound). We'll add to total via in+out below.
            in_deg[e["source"]] += 0  # noop -- keep keys initialized
            in_deg[e["target"]] += 0
            # For total-degree purposes we tally undirected on both sides:
            out_deg[e["source"]] += 1  # treat as "endpoint" count
            out_deg[e["target"]] += 1
        else:
            out_deg[e["source"]] += 1
            in_deg[e["target"]] += 1
    return in_deg, out_deg


def _connected_components(node_ids: set[str], edges: list[dict]) -> tuple[int, int]:
    """Plain BFS over undirected adjacency. Returns (count, largest_size)."""
    adj: dict[str, set[str]] = defaultdict(set)
    for e in edges:
        s, t = e["source"], e["target"]
        adj[s].add(t)
        adj[t].add(s)
    seen: set[str] = set()
    sizes: list[int] = []
    for start in node_ids:
        if start in seen:
            continue
        size = 0
        stack = [start]
        while stack:
            n = stack.pop()
            if n in seen:
                continue
            seen.add(n)
            size += 1
            stack.extend(adj[n] - seen)
        sizes.append(size)
    return len(sizes), max(sizes) if sizes else 0


def compute_stats(
    nodes: list[dict],
    edges: list[dict],
    *,
    below_edges: Optional[list[dict]] = None,
) -> Stats:
    """Compute stats over (core ∪ audit) edges so the report matches the preview."""
    below_edges = below_edges or []
    full_edges = list(edges) + list(below_edges)

    nodes_by_type: Counter = Counter(n.get("type", "Company") for n in nodes)
    edges_by_type: Counter = Counter(e["type"] for e in edges)
    edges_by_type_audit: Counter = Counter(e["type"] for e in below_edges)
    provisional_count = sum(
        1 for n in nodes if (n.get("metadata") or {}).get("provisional")
    )

    name_by_id = {n["id"]: n.get("name") or n["id"] for n in nodes}
    provisional_ids = {
        n["id"] for n in nodes if (n.get("metadata") or {}).get("provisional")
    }
    # Degrees are computed over the FULL graph (core + audit) so the report
    # matches what the preview renders. Phase 3's strict cap kept slug
    # edges out of edges.jsonl, but they're still visible (dimmed) in
    # graph.json -- and the user wants the report to reflect what they SEE.
    in_deg, out_deg = _build_degree_tables(nodes, full_edges)
    totals = Counter()
    for nid in name_by_id:
        totals[nid] = in_deg[nid] + out_deg[nid]
    top_total = [
        (nid, name_by_id[nid], totals[nid], in_deg[nid], out_deg[nid])
        for nid, _ in totals.most_common(10)
    ]
    top_provisional = [
        (nid, name_by_id[nid], totals[nid])
        for nid in sorted(provisional_ids, key=lambda i: -totals[i])[:10]
    ]

    components, largest = _connected_components(set(name_by_id.keys()), full_edges)

    # Supply layer: count supplies edges in the FULL set (some may be capped
    # to provisional slug targets and live in the audit layer).
    supplies_edges = [e for e in full_edges if e["type"] == EdgeType.supplies.value]
    supplies_to_provisional = sum(
        1 for e in supplies_edges if e["target"].startswith("slug:")
    )
    supplies_to_real = len(supplies_edges) - supplies_to_provisional

    return Stats(
        nodes_by_type=dict(nodes_by_type),
        provisional_count=provisional_count,
        edges_by_type=dict(edges_by_type),
        edges_by_type_audit=dict(edges_by_type_audit),
        top_by_total_degree=top_total,
        top_provisional_by_degree=top_provisional,
        components=components,
        largest_component_size=largest,
        supply_layer={
            "total": len(supplies_edges),
            "to_real_filers": supplies_to_real,
            "to_provisional_slugs": supplies_to_provisional,
        },
    )


def print_stats(stats: Stats, *, total_nodes: int) -> None:
    print("=" * 72)
    print("Phase 4 graph stats")
    print("=" * 72)
    print(f"Total nodes: {total_nodes}")
    print("  by type:")
    for t, c in sorted(stats.nodes_by_type.items()):
        print(f"    {t:<12s} {c:>4}")
    print(f"  provisional (slug:) Company nodes: {stats.provisional_count}")
    print()
    print("Edges by type (core / audit):")
    all_types = sorted(set(stats.edges_by_type) | set(stats.edges_by_type_audit))
    for t in all_types:
        core = stats.edges_by_type.get(t, 0)
        audit = stats.edges_by_type_audit.get(t, 0)
        print(f"    {t:<14s} core={core:>4}  audit={audit:>4}")
    print()
    print("Top 10 nodes by total degree (in / out shown for directed edges):")
    for nid, label, total, indeg, outdeg in stats.top_by_total_degree:
        marker = "*" if nid.startswith("slug:") else " "
        print(f"  {marker} {total:>3d}  [in={indeg:>3d} out={outdeg:>3d}]  {nid:<40s}  {label!r}")
    print("  (* = provisional slug node)")
    print()
    print("Most-connected non-filer (slug:) nodes:")
    for nid, label, total in stats.top_provisional_by_degree:
        print(f"    {total:>3d}  {nid:<40s}  {label!r}")
    print()
    print(f"Connected components: {stats.components}  (largest = {stats.largest_component_size} nodes)")
    print()
    sl = stats.supply_layer
    print(
        f"Supply layer: {sl['total']} supplies edges "
        f"({sl['to_real_filers']} to real filers, {sl['to_provisional_slugs']} to provisional slugs)"
    )
    print(
        "  -- thin by design: 10-Ks disclose competitors + customer concentration richly\n"
        "     but not full supplier lists. The post-MVP commodity/material nodes thicken this."
    )
    print("=" * 72)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def run(*, data_root: Path, db_path: Path, graph_json_path: Path) -> Stats:
    nodes = _load_jsonl(data_root / "nodes.jsonl")
    edges = _load_jsonl(data_root / "edges.jsonl")
    aliases = _load_jsonl(data_root / "aliases.jsonl")
    below_path = data_root / "edges_below_threshold.jsonl"
    below = _load_jsonl(below_path) if below_path.exists() else []

    # Tier-3 Wikidata enrichment: if wikidata_companies.json is present,
    # merge {qid, country, hq, lat, lon} into each Company node's metadata.
    # The 3D view uses lat/lon for its globe-mode positioning; the inspector
    # surfaces the Wikidata link so every node is one click from its source.
    wiki_path = data_root / "wikidata_companies.json"
    if wiki_path.exists():
        wiki = json.loads(wiki_path.read_text(encoding="utf-8"))
        merged = 0
        for n in nodes:
            cik = (n.get("identifiers") or {}).get("cik")
            if not cik or cik not in wiki:
                continue
            md = n.setdefault("metadata", {})
            md["wikidata"] = wiki[cik]
            merged += 1
        log.info("Wikidata enrichment merged into %d company nodes", merged)
    log.info(
        "Inputs: %d nodes, %d edges (above), %d edges (below), %d aliases",
        len(nodes), len(edges), len(below), len(aliases),
    )

    # Phase 5 §A1: SQLite now holds BOTH layers, flagged by below_threshold.
    # The API uses that flag to honor the include_provisional toggle.
    load_into_sqlite(
        db_path,
        nodes=nodes,
        edges=edges,
        below_threshold_edges=below,
        aliases=aliases,
        fresh=True,
    )
    # Idempotency proof: load again with fresh=False; counts should not change.
    load_into_sqlite(
        db_path,
        nodes=nodes,
        edges=edges,
        below_threshold_edges=below,
        aliases=aliases,
        fresh=False,
    )

    # graph.json folds in the audit layer (tagged below_threshold=true) so
    # the preview can show provisional slug hubs without polluting edges.jsonl.
    graphology = to_graphology(nodes, edges, below_threshold_edges=below)
    write_graph_json(graphology, graph_json_path)
    log.info(
        "Wrote %s (%d nodes, %d above-cutoff + %d below-cutoff edges)",
        graph_json_path, len(nodes), len(edges), len(below),
    )

    # Stats are computed over (core ∪ audit) so the report matches the
    # rendered preview. The breakdown by type still shows core vs audit.
    stats = compute_stats(nodes, edges, below_edges=below)
    print_stats(stats, total_nodes=len(nodes))
    return stats


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="EconGraph Phase 4 graph build")
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--db-path", default="econgraph.db")
    parser.add_argument("--graph-json", default="data/graph.json")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    run(
        data_root=Path(args.data_root),
        db_path=Path(args.db_path),
        graph_json_path=Path(args.graph_json),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
