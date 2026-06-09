"""Phase K-2: Identify high-centrality hub nodes.

Loads the resolved graph from data/edges.jsonl + data/nodes.jsonl, computes
approximate betweenness centrality via NetworkX, and writes the top-N nodes
to data/hubs.jsonl.  These hubs are the priority targets for
pipeline/extract_weights.py — they appear in the traversal path of most
real impact traces.

Usage:
    python -m pipeline.identify_hubs [--top N] [--cik-only]
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).parent.parent
DATA_DIR = REPO_ROOT / "data"


def main(top: int = 60, cik_only: bool = False) -> None:
    try:
        import networkx as nx
    except ImportError:
        log.error("networkx is not installed. Run: pip install networkx")
        sys.exit(1)

    # ── Load nodes ─────────────────────────────────────────────────────────
    nodes_path = DATA_DIR / "nodes.jsonl"
    if not nodes_path.exists():
        log.error("nodes.jsonl not found at %s — run resolve.py first", nodes_path)
        sys.exit(1)

    node_names: dict[str, str] = {}
    with nodes_path.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            n = json.loads(line)
            node_names[n["id"]] = n.get("name") or n["id"]

    # ── Load edges ─────────────────────────────────────────────────────────
    edges_path = DATA_DIR / "edges.jsonl"
    if not edges_path.exists():
        log.error("edges.jsonl not found at %s — run resolve.py first", edges_path)
        sys.exit(1)

    G = nx.DiGraph()
    with edges_path.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            e = json.loads(line)
            src, tgt = e.get("source"), e.get("target")
            if src and tgt:
                G.add_edge(src, tgt)

    log.info("Graph: %d nodes, %d edges", G.number_of_nodes(), G.number_of_edges())

    # ── Betweenness centrality (approximate for speed) ─────────────────────
    # k=500 samples gives good accuracy for large graphs; full computation
    # would take too long on 5k+ nodes.
    log.info("Computing betweenness centrality (k=500 samples)...")
    centrality = nx.betweenness_centrality(G, k=min(500, G.number_of_nodes()),
                                           normalized=True)

    # ── Sort and filter ────────────────────────────────────────────────────
    ranked = sorted(centrality.items(), key=lambda x: x[1], reverse=True)

    hubs = []
    for node_id, score in ranked:
        if cik_only and not node_id.startswith("cik:"):
            continue
        hubs.append({
            "id": node_id,
            "name": node_names.get(node_id, node_id),
            "centrality": round(score, 6),
            "has_filing": node_id.startswith("cik:"),
        })
        if len(hubs) >= top:
            break

    # ── Write output ───────────────────────────────────────────────────────
    out_path = DATA_DIR / "hubs.jsonl"
    with out_path.open("w", encoding="utf-8") as fh:
        for hub in hubs:
            fh.write(json.dumps(hub, ensure_ascii=False) + "\n")

    log.info("Wrote %d hubs to %s", len(hubs), out_path)

    # Print summary
    print(f"\nTop 20 hub nodes:")
    for h in hubs[:20]:
        filing_flag = "[SEC]" if h["has_filing"] else "     "
        print(f"  {filing_flag} {h['centrality']:.4f}  {h['name']}  ({h['id']})")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s",
                        stream=sys.stdout)
    parser = argparse.ArgumentParser(description="Identify hub nodes by betweenness centrality")
    parser.add_argument("--top", type=int, default=60,
                        help="Number of top hubs to output (default: 60)")
    parser.add_argument("--cik-only", action="store_true",
                        help="Only include SEC filers (cik: prefix) — have cached filings")
    args = parser.parse_args()
    main(top=args.top, cik_only=args.cik_only)
