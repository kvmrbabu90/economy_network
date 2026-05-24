"""Commodity + retail-market nodes and their `supplies` edges.

Two product additions stacked into one rule-based pipeline:

  1. Raw-material commodities. Each row in ``config/commodities.yaml``
     becomes a single ``Commodity`` node placed on the globe at its #1
     producer (country centroid or US-state centroid -- the producer
     listed in the YAML). For every S&P 500 filer whose GICS sub-industry
     is in ``config/industry_to_commodities.yaml``, we emit a candidate
       commodity:X --supplies--> cik:Y
     so the consumer industry's supply chain becomes visible on the
     graph. Provenance: the YAML row that placed the commodity.

  2. Retail-consumer markets. ``config/retail_markets.yaml`` defines
     a fixed roster of regional sink nodes (US, EU, China, India,
     Japan, Brazil, Southeast Asia) with their lat/lon. Each
     ``config/industry_to_retail.yaml`` row maps a B2C sub-industry
     to the markets it serves; we emit
       cik:Y --supplies--> region:M-consumer
     for every (filer, market) pair, which the API surfaces as a
     derived ``customer_of`` edge (region M-consumer is a customer of
     the filer).

Outputs:
  data/commodity_nodes.jsonl         -- Commodity + Region nodes
  data/_extract/commodity_candidates.jsonl  -- candidate edges for resolve
"""
from __future__ import annotations

import json
import logging
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

from schema.models import (
    CandidateEdge,
    EdgeType,
    Node,
    NodeType,
    Provenance,
)

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO_ROOT / "config"


def _load_yaml(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _commodity_node(row: dict[str, Any]) -> Node:
    """Materialise a Commodity node with Wikidata-style HQ coords so the
    globe view picks it up automatically."""
    return Node(
        id=row["id"],
        type=NodeType.Commodity,
        name=row["name"],
        aliases=row.get("aliases", []) or [],
        identifiers={},
        country=row.get("top_producer_country_code"),
        metadata={
            "category": row.get("category"),
            "top_producer": row.get("top_producer"),
            "us_state": row.get("us_state"),
            "unit": row.get("unit"),
            "source": row.get("source"),
            # Re-using the wikidata sub-blob is intentional: the globe
            # renderer keys off metadata.wikidata.{lat,lon} for HQ
            # placement, so commodities slot in without any frontend
            # changes.
            "wikidata": {
                "lat": float(row["lat"]),
                "lon": float(row["lon"]),
                "label": row["name"],
            },
        },
    )


def _region_node(row: dict[str, Any]) -> Node:
    return Node(
        id=row["id"],
        type=NodeType.Region,
        name=row["name"],
        aliases=[],
        identifiers={},
        country=row.get("region_code"),
        metadata={
            "population_m": row.get("population_m"),
            "wikidata": {
                "lat": float(row["lat"]),
                "lon": float(row["lon"]),
                "label": row["name"],
            },
        },
    )


def _provenance_for_commodity(commodity_row: dict[str, Any]) -> Provenance:
    return Provenance(
        filing="rule:industry-to-commodity",
        url=(
            "https://github.com/kvmrbabu90/economy_network/blob/main/"
            "config/industry_to_commodities.yaml"
        ),
        snippet=f"{commodity_row['name']} ({commodity_row.get('source', 'curated')})",
        extracted_by="rule",
    )


def _provenance_for_retail(market_row: dict[str, Any]) -> Provenance:
    return Provenance(
        filing="rule:industry-to-retail",
        url=(
            "https://github.com/kvmrbabu90/economy_network/blob/main/"
            "config/industry_to_retail.yaml"
        ),
        snippet=f"{market_row['name']}",
        extracted_by="rule",
    )


def run_commodities(
    companies_path: Path,
    out_nodes: Path,
    out_candidates: Path,
) -> dict[str, Any]:
    """Build commodity + region nodes and their candidate edges.

    Returns a summary dict for the orchestrator's logs.
    """
    commodities = _load_yaml(CONFIG_DIR / "commodities.yaml")
    retail_markets = _load_yaml(CONFIG_DIR / "retail_markets.yaml")
    ind_to_comm = _load_yaml(CONFIG_DIR / "industry_to_commodities.yaml")
    ind_to_retail = _load_yaml(CONFIG_DIR / "industry_to_retail.yaml")

    commodities_by_slug = {row["id"]: row for row in commodities}
    markets_by_slug = {row["id"]: row for row in retail_markets}
    # Build a normalized index "us" -> region:us-consumer etc. so the
    # industry_to_retail rule strings can be terse.
    region_aliases = {
        row["region_code"].lower(): row["id"]
        for row in retail_markets
    }
    # Also map common shortnames
    region_aliases.update({
        "us": "region:us-consumer",
        "eu": "region:eu-consumer",
        "china": "region:china-consumer",
        "india": "region:india-consumer",
        "japan": "region:japan-consumer",
        "brazil": "region:brazil-consumer",
        "southeast-asia": "region:southeast-asia-consumer",
        "sea": "region:southeast-asia-consumer",
    })

    # --- nodes ---
    nodes: list[Node] = []
    for row in commodities:
        nodes.append(_commodity_node(row))
    for row in retail_markets:
        nodes.append(_region_node(row))

    out_nodes.parent.mkdir(parents=True, exist_ok=True)
    with out_nodes.open("w", encoding="utf-8") as f:
        for n in nodes:
            f.write(n.model_dump_json() + "\n")
    log.info("Wrote %d commodity + region nodes -> %s", len(nodes), out_nodes)

    # --- candidate edges ---
    candidates: list[CandidateEdge] = []
    by_kind: Counter = Counter()
    unknown_commodities: Counter = Counter()
    unknown_markets: Counter = Counter()

    companies = [json.loads(line) for line in companies_path.open(encoding="utf-8")]
    for company in companies:
        cik = company.get("cik") or company.get("id")
        if not cik:
            continue
        canonical_source = cik if cik.startswith("cik:") else f"cik:{cik}"
        sub_industry = company.get("sub_industry") or company.get("industry")
        if not sub_industry:
            continue

        # 1. Commodities the industry consumes  ->  commodity --supplies--> filer
        for commodity_slug_short in (ind_to_comm.get(sub_industry) or []):
            commodity_id = (
                commodity_slug_short
                if commodity_slug_short.startswith("commodity:")
                else f"commodity:{commodity_slug_short}"
            )
            commodity_row = commodities_by_slug.get(commodity_id)
            if not commodity_row:
                unknown_commodities[commodity_slug_short] += 1
                continue
            try:
                ce = CandidateEdge(
                    source_id=commodity_id,
                    target_raw=canonical_source,
                    type=EdgeType.supplies,
                    confidence=0.85,  # rule-based, curated, high confidence
                    provenance=_provenance_for_commodity(commodity_row),
                    verified=True,
                )
                candidates.append(ce)
                by_kind["commodity->filer"] += 1
            except Exception as e:
                log.warning("commodity edge skipped %s -> %s: %s",
                            commodity_id, canonical_source, e)

        # 2. Retail markets the filer serves  ->  filer --supplies--> region
        for market_short in (ind_to_retail.get(sub_industry) or []):
            region_id = region_aliases.get(market_short.lower().strip())
            if not region_id or region_id not in markets_by_slug:
                unknown_markets[market_short] += 1
                continue
            market_row = markets_by_slug[region_id]
            try:
                ce = CandidateEdge(
                    source_id=canonical_source,
                    target_raw=region_id,
                    type=EdgeType.supplies,
                    confidence=0.85,
                    provenance=_provenance_for_retail(market_row),
                    verified=True,
                )
                candidates.append(ce)
                by_kind["filer->retail"] += 1
            except Exception as e:
                log.warning("retail edge skipped %s -> %s: %s",
                            canonical_source, region_id, e)

    out_candidates.parent.mkdir(parents=True, exist_ok=True)
    with out_candidates.open("w", encoding="utf-8") as f:
        for ce in candidates:
            f.write(ce.model_dump_json() + "\n")
    log.info(
        "Wrote %d candidate edges -> %s (commodity->filer=%d, filer->retail=%d)",
        len(candidates), out_candidates,
        by_kind.get("commodity->filer", 0),
        by_kind.get("filer->retail", 0),
    )
    if unknown_commodities:
        log.warning("unknown commodity slugs in rules: %s", dict(unknown_commodities))
    if unknown_markets:
        log.warning("unknown retail market codes in rules: %s", dict(unknown_markets))

    return {
        "nodes_written": len(nodes),
        "commodities_in_catalog": len(commodities),
        "retail_markets_in_catalog": len(retail_markets),
        "candidate_edges": len(candidates),
        "by_kind": dict(by_kind),
        "unknown_commodities": dict(unknown_commodities),
        "unknown_markets": dict(unknown_markets),
        "nodes_path": str(out_nodes),
        "candidates_path": str(out_candidates),
    }


def main() -> None:
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--data-root", default=str(REPO_ROOT / "data"))
    args = p.parse_args()

    data_root = Path(args.data_root)
    summary = run_commodities(
        companies_path=data_root / "companies.jsonl",
        out_nodes=data_root / "commodity_nodes.jsonl",
        out_candidates=data_root / "_extract" / "commodity_candidates.jsonl",
    )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
