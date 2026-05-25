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
     regional sink nodes (US, EU, China, India, Japan, Brazil, SEA, Korea,
     Australia, Canada, Mexico, Middle East, Africa, LatAm) with lat/lon.
     Each ``config/industry_to_retail.yaml`` row maps a B2C sub-industry
     to the markets it serves globally. For US companies that list is used
     verbatim. For non-US companies Phase D adds country-aware routing:
     ``config/country_default_retail_markets.yaml`` maps country codes to
     the primary markets that country's companies actually serve. The final
     market list is the INTERSECTION of (industry base markets) with (country
     primary markets UNION industry base markets), so:
       - US companies are unchanged.
       - Samsung Electronics (KR) loses Brazil, gains Korea.
       - LVMH (FR) loses Brazil/India, keeps EU/US/China/Japan.
     We emit: cik:Y --supplies--> region:M-consumer per (filer, market) pair.

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
    # Phase D: country-default retail markets. Keys are ISO-2 country codes;
    # values are dicts with a "primary" list of region IDs.
    country_defaults_raw = _load_yaml(CONFIG_DIR / "country_default_retail_markets.yaml") or {}
    # country_defaults: { "KR": ["region:korea-consumer", ...], ... }
    country_defaults: dict[str, list[str]] = {
        code: entry.get("primary", [])
        for code, entry in country_defaults_raw.items()
        if isinstance(entry, dict)
    }
    # Phase D: company-level sub-industry overrides for Wikidata companies that
    # are misclassified as "Industrial Conglomerates" but have real B2C presence.
    # Maps canonical-id -> GICS sub-industry for retail-market routing only.
    sub_industry_overrides: dict[str, str] = (
        _load_yaml(CONFIG_DIR / "company_sub_industry_overrides.yaml") or {}
    )

    commodities_by_slug = {row["id"]: row for row in commodities}
    markets_by_slug = {row["id"]: row for row in retail_markets}
    # Build a normalized index "us" -> region:us-consumer etc. so the
    # industry_to_retail rule strings can be terse.
    region_aliases = {
        row["region_code"].lower(): row["id"]
        for row in retail_markets
    }
    # Also map common shortnames and Phase D region codes
    region_aliases.update({
        "us": "region:us-consumer",
        "eu": "region:eu-consumer",
        "china": "region:china-consumer",
        "india": "region:india-consumer",
        "japan": "region:japan-consumer",
        "brazil": "region:brazil-consumer",
        "southeast-asia": "region:southeast-asia-consumer",
        "sea": "region:southeast-asia-consumer",
        "korea": "region:korea-consumer",
        "kr": "region:korea-consumer",
        "australia": "region:australia-consumer",
        "au": "region:australia-consumer",
        "canada": "region:canada-consumer",
        "ca": "region:canada-consumer",
        "mexico": "region:mexico-consumer",
        "mx": "region:mexico-consumer",
        "middle-east": "region:middle-east-consumer",
        "me": "region:middle-east-consumer",
        "africa": "region:africa-consumer",
        "af": "region:africa-consumer",
        "latam": "region:latam-consumer",
        "latin-america": "region:latam-consumer",
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
        # companies.jsonl "id" field is already the canonical prefixed form
        # (cik:NNNN for SEC filers, wikidata:Qxxxx for Phase B non-filers).
        canonical_source = company.get("id") or company.get("cik")
        if not canonical_source:
            continue
        # Legacy fallback: bare CIK number without prefix
        if not any(canonical_source.startswith(p) for p in ("cik:", "wikidata:", "slug:")):
            canonical_source = f"cik:{canonical_source}"
        sub_industry = (
            sub_industry_overrides.get(canonical_source)
            or company.get("sub_industry")
            or company.get("industry")
        )
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
        #
        # Phase D: country-aware routing.
        # For US companies: use industry_to_retail list verbatim (no change).
        # For non-US companies: compute the UNION of the industry base markets
        # and the country's primary markets, then keep only those markets that
        # appear in the industry base list OR are in the country's primary list.
        # This means:
        #   - Markets the industry never serves are never added (e.g. a Korean
        #     semiconductor fab won't get a Brazil retail edge even if KR
        #     defaults include Brazil in some other context — it doesn't).
        #   - Markets the industry does serve but the country doesn't primary-
        #     serve are dropped (e.g. Brazilian pharma companies won't get a
        #     Japan edge just because Pharmaceuticals maps to Japan globally).
        #   - Country-primary markets get added even if not in the industry
        #     global list (e.g. Korea-consumer for Samsung Electronics).
        company_country = company.get("country") or "US"
        industry_markets_raw: list[str] = ind_to_retail.get(sub_industry) or []
        # Resolve to canonical region IDs
        industry_market_ids: list[str] = []
        for m in industry_markets_raw:
            rid = region_aliases.get(m.lower().strip())
            if rid and rid in markets_by_slug:
                industry_market_ids.append(rid)
            elif rid is None:
                unknown_markets[m] += 1
        if not industry_market_ids:
            # Industry is not B2C — no retail edges for this company.
            continue

        if company_country == "US":
            # US companies: use industry list as-is (backward-compatible).
            effective_markets = industry_market_ids
        else:
            # Non-US companies: intersect industry markets with country defaults,
            # then append country-primary markets that aren't already in the list.
            country_primary = country_defaults.get(company_country, [])
            if not country_primary:
                # Country not in our map → fall back to industry list unchanged.
                effective_markets = industry_market_ids
            else:
                country_primary_set = set(country_primary)
                industry_set = set(industry_market_ids)
                # Keep industry markets that overlap with country primary list.
                kept = [m for m in industry_market_ids if m in country_primary_set]
                # Add country-primary markets that exist in the catalog but
                # aren't in the industry global list (e.g. korea-consumer for
                # Samsung Electronics, which the global Consumer Electronics
                # list didn't enumerate because Korea wasn't in the original
                # 7-market roster).
                extra = [
                    m for m in country_primary
                    if m not in industry_set and m in markets_by_slug
                ]
                # Fallback: if intersection is empty, use country primary
                # (better than leaving the company with no retail edges at all).
                if not kept:
                    effective_markets = country_primary
                else:
                    effective_markets = kept + extra

        for region_id in effective_markets:
            if region_id not in markets_by_slug:
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
