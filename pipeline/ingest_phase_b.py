"""Phase B ingestion: Wikidata-anchored expansion for non-US companies.

Enumerates top companies with no US listing from Wikidata (EU, Asia, MENA, LATAM)
and appends them to data/companies.jsonl.

Target: ~750 new companies across four regional buckets.

Pipeline invariants (CLAUDE.md):
  - One row per entity: deduplicates against existing nodes before writing.
  - Canonical IDs: non-filers -> wikidata:Qxxxxxx
  - Cache-first: SPARQL results cached to data/cache/phase_b_sparql_<region>.json
  - Rate limit: <= 1 req/s to Wikidata SPARQL
  - Never store customer_of; only supplies (derived at query time)

Outputs:
    data/companies.jsonl       -- APPENDED (existing rows stay)
    data/cache/phase_b_sparql_<region>.json -- cached SPARQL results
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import threading
from pathlib import Path
from typing import Any, Optional

import requests
import yaml

from schema.models import Node, NodeType

log = logging.getLogger("ingest_phase_b")

WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"

# ---------------------------------------------------------------------------
# Rate limiter (<=1 req/s for Wikidata)
# ---------------------------------------------------------------------------
_lock = threading.Lock()
_last_req = 0.0
_MIN_INTERVAL = 1.1  # slight buffer above 1s


def _user_agent() -> str:
    ua = os.environ.get("EDGAR_USER_AGENT", "").strip()
    if not ua:
        ua = "EconGraph/1.0 (kondaru.mk@gmail.com)"
    return ua + " (EconGraph Phase B)"


def _sparql_get(query: str, timeout: float = 120.0) -> dict[str, Any]:
    global _last_req
    with _lock:
        wait = _MIN_INTERVAL - (time.monotonic() - _last_req)
        if wait > 0:
            time.sleep(wait)
        _last_req = time.monotonic()
    headers = {
        "User-Agent": _user_agent(),
        "Accept": "application/sparql-results+json",
    }
    resp = requests.get(
        WIKIDATA_SPARQL,
        params={"query": query, "format": "json"},
        headers=headers,
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()


def _qid_of(url: str) -> Optional[str]:
    if not url:
        return None
    m = re.search(r"/(Q\d+)$", url)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# Country -> Wikidata Q-id mappings
# ---------------------------------------------------------------------------

COUNTRY_QIDS = {
    # Europe
    "DE": "Q183", "FR": "Q142", "NL": "Q55", "CH": "Q39",
    "SE": "Q34", "DK": "Q35", "FI": "Q33", "NO": "Q20",
    "IT": "Q38", "ES": "Q29", "BE": "Q31", "AT": "Q40",
    "PT": "Q45", "IE": "Q27", "LU": "Q32", "PL": "Q36",
    # Asia
    "JP": "Q17", "KR": "Q884", "CN": "Q148", "HK": "Q8646",
    "TW": "Q865", "IN": "Q668", "SG": "Q334", "MY": "Q833",
    "TH": "Q869", "ID": "Q252",
    # MENA
    "SA": "Q851", "AE": "Q878", "QA": "Q846", "KW": "Q817",
    "EG": "Q79", "MA": "Q1028", "NG": "Q1033", "ZA": "Q258",
    # Latin America
    "BR": "Q155", "MX": "Q96", "AR": "Q414", "CL": "Q298",
    "CO": "Q739", "PE": "Q419",
}

# Grouped regions for cache files
REGIONS = {
    "europe": ["DE", "FR", "NL", "CH", "SE", "DK", "FI", "NO", "IT", "ES", "BE", "AT", "PT", "IE", "LU", "PL"],
    "asia": ["JP", "KR", "CN", "HK", "TW", "IN", "SG", "MY", "TH", "ID"],
    "mena": ["SA", "AE", "QA", "KW", "EG", "MA", "NG", "ZA"],
    "latam": ["BR", "MX", "AR", "CL", "CO", "PE"],
}

# Country per-region caps for countries already heavily represented
COUNTRY_CAPS = {
    "BR": 50,  # already has 7
    "MX": 30,  # already has 5
    "ZA": 30,  # already has 2, OK
}

# How many companies per region to target
REGION_TARGETS = {
    "europe": 250,
    "asia": 250,
    "mena": 100,
    "latam": 150,
}

# ---------------------------------------------------------------------------
# Wikidata industry Q-id -> GICS sector + sub-industry
# ---------------------------------------------------------------------------

INDUSTRY_MAP: dict[str, dict[str, str]] = {
    # Banks / Financial
    "Q35709": {"sector": "Financials", "industry": "Diversified Banks"},
    "Q22687": {"sector": "Financials", "industry": "Life & Health Insurance"},
    "Q43267": {"sector": "Financials", "industry": "Diversified Banks"},
    "Q837525": {"sector": "Financials", "industry": "Investment Banking & Brokerage"},
    "Q746403": {"sector": "Financials", "industry": "Asset Management & Custody Banks"},
    "Q837056": {"sector": "Financials", "industry": "Investment Banking & Brokerage"},
    # Energy
    "Q1547032": {"sector": "Energy", "industry": "Integrated Oil & Gas"},
    "Q43229": {"sector": "Energy", "industry": "Integrated Oil & Gas"},  # general enterprise
    "Q206934": {"sector": "Energy", "industry": "Oil & Gas Exploration & Production"},
    "Q211272": {"sector": "Utilities", "industry": "Electric Utilities"},
    "Q3508735": {"sector": "Utilities", "industry": "Electric Utilities"},
    # Auto
    "Q786820": {"sector": "Consumer Discretionary", "industry": "Automobile Manufacturers"},
    "Q9453": {"sector": "Consumer Discretionary", "industry": "Automobile Manufacturers"},
    # Pharma / Health
    "Q174174": {"sector": "Health Care", "industry": "Pharmaceuticals"},
    "Q507443": {"sector": "Health Care", "industry": "Pharmaceuticals"},
    "Q507442": {"sector": "Health Care", "industry": "Biotechnology"},
    # Materials
    "Q11642": {"sector": "Materials", "industry": "Steel"},
    "Q42515": {"sector": "Materials", "industry": "Copper"},
    "Q11814": {"sector": "Materials", "industry": "Gold"},
    "Q11665": {"sector": "Materials", "industry": "Commodity Chemicals"},
    "Q131681": {"sector": "Materials", "industry": "Specialty Chemicals"},
    # Retail
    "Q268592": {"sector": "Consumer Discretionary", "industry": "Broadline Retail"},
    "Q1063239": {"sector": "Consumer Discretionary", "industry": "Broadline Retail"},
    # Consumer Staples
    "Q131285": {"sector": "Consumer Staples", "industry": "Packaged Foods & Meats"},
    "Q156": {"sector": "Consumer Staples", "industry": "Brewers"},  # beer
    "Q160018": {"sector": "Consumer Staples", "industry": "Soft Drinks & Non-alcoholic Beverages"},
    # Telecom
    "Q29023": {"sector": "Communication Services", "industry": "Integrated Telecommunication Services"},
    "Q194142": {"sector": "Communication Services", "industry": "Integrated Telecommunication Services"},
    # IT
    "Q7397": {"sector": "Information Technology", "industry": "Systems Software"},
    "Q17156": {"sector": "Information Technology", "industry": "Semiconductors"},
    "Q11462": {"sector": "Information Technology", "industry": "Semiconductors"},
    # Industrials
    "Q210167": {"sector": "Industrials", "industry": "Aerospace & Defense"},
    "Q4830453": {"sector": "Industrials", "industry": "Industrial Conglomerates"},  # general
    "Q783794": {"sector": "Industrials", "industry": "Industrial Conglomerates"},  # company
}

# Sector-only fallbacks by sector string contained in Wikidata description
SECTOR_KEYWORDS: list[tuple[list[str], str, str]] = [
    (["bank", "banking", "financial services"], "Financials", "Diversified Banks"),
    (["insurance"], "Financials", "Life & Health Insurance"),
    (["asset management", "investment fund"], "Financials", "Asset Management & Custody Banks"),
    (["oil", "gas", "petroleum", "energy"], "Energy", "Integrated Oil & Gas"),
    (["electric", "electricity", "power utility"], "Utilities", "Electric Utilities"),
    (["automobile", "automotive", "car manufacturer"], "Consumer Discretionary", "Automobile Manufacturers"),
    (["pharmaceutical", "pharma", "drug"], "Health Care", "Pharmaceuticals"),
    (["semiconductor", "chip"], "Information Technology", "Semiconductors"),
    (["software", "technology"], "Information Technology", "Systems Software"),
    (["telecom", "telecommunications", "mobile network"], "Communication Services", "Integrated Telecommunication Services"),
    (["retail", "supermarket", "hypermarket"], "Consumer Discretionary", "Broadline Retail"),
    (["mining", "mine"], "Materials", "Metals & Mining"),
    (["steel", "iron"], "Materials", "Steel"),
    (["chemical"], "Materials", "Specialty Chemicals"),
    (["food", "beverage"], "Consumer Staples", "Packaged Foods & Meats"),
    (["real estate", "property", "reit"], "Real Estate", "Real Estate Services"),
    (["airline", "aviation", "air transport"], "Industrials", "Passenger Airlines"),
    (["logistics", "shipping", "freight"], "Industrials", "Air Freight & Logistics"),
    (["construction", "infrastructure", "engineering"], "Industrials", "Construction & Engineering"),
    (["media", "broadcast", "television"], "Communication Services", "Broadcasting"),
    (["hospital", "health care", "healthcare"], "Health Care", "Health Care Facilities"),
    (["luxury", "fashion", "apparel"], "Consumer Discretionary", "Apparel, Accessories & Luxury Goods"),
    (["tobacco"], "Consumer Staples", "Tobacco"),
]


def _guess_gics(industry_qids: list[str], description: str) -> tuple[str, str]:
    """Map Wikidata industry Q-ids + description to (GICS sector, GICS sub-industry)."""
    for qid in industry_qids:
        if qid in INDUSTRY_MAP:
            m = INDUSTRY_MAP[qid]
            return m["sector"], m["industry"]
    # Fall back to description keyword matching
    desc_lower = (description or "").lower()
    for keywords, sector, sub_industry in SECTOR_KEYWORDS:
        if any(kw in desc_lower for kw in keywords):
            return sector, sub_industry
    # Default: Industrials / Industrial Conglomerates — safe "unknown company" bucket
    return "Industrials", "Industrial Conglomerates"


# ---------------------------------------------------------------------------
# SPARQL queries
# ---------------------------------------------------------------------------

def _build_sparql_query(country_qids: list[str], limit: int) -> str:
    """Build SPARQL query for companies in the given countries."""
    country_values = " ".join(f"wd:{q}" for q in country_qids)
    return f"""
SELECT DISTINCT ?company ?companyLabel ?companyDescription ?revenue ?hq ?hqLabel ?coord ?hqCoord ?industry WHERE {{
  VALUES ?country {{ {country_values} }}
  ?company wdt:P31 ?type ;
           wdt:P17 ?country .
  FILTER(?type IN (wd:Q4830453, wd:Q783794, wd:Q891723, wd:Q167037))
  FILTER NOT EXISTS {{ ?company wdt:P5531 ?cik . }}
  FILTER NOT EXISTS {{ ?company wdt:P576 ?dissolved . }}
  OPTIONAL {{ ?company wdt:P2139 ?revenue . }}
  OPTIONAL {{ ?company wdt:P159 ?hq . }}
  OPTIONAL {{ ?company wdt:P625 ?coord . }}
  OPTIONAL {{ ?company wdt:P159/wdt:P625 ?hqCoord . }}
  OPTIONAL {{ ?company wdt:P452 ?industry . }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
}}
ORDER BY DESC(?revenue)
LIMIT {limit}
"""


def _build_sparql_country_only(country_qids: list[str], limit: int) -> str:
    """Simpler query for countries where the main query times out."""
    country_values = " ".join(f"wd:{q}" for q in country_qids)
    return f"""
SELECT DISTINCT ?company ?companyLabel ?companyDescription ?hqCoord ?industry WHERE {{
  VALUES ?country {{ {country_values} }}
  ?company wdt:P31 wd:Q4830453 ;
           wdt:P17 ?country .
  FILTER NOT EXISTS {{ ?company wdt:P5531 ?cik . }}
  FILTER NOT EXISTS {{ ?company wdt:P576 ?dissolved . }}
  OPTIONAL {{ ?company wdt:P159/wdt:P625 ?hqCoord . }}
  OPTIONAL {{ ?company wdt:P452 ?industry . }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
}}
LIMIT {limit}
"""


# ---------------------------------------------------------------------------
# Fetch with cache
# ---------------------------------------------------------------------------

def fetch_region_companies(
    region: str,
    country_codes: list[str],
    target_count: int,
    cache_dir: Path,
) -> list[dict[str, Any]]:
    """Fetch companies for a region, using cache if available."""
    cache_file = cache_dir / f"phase_b_sparql_{region}.json"
    if cache_file.exists():
        log.info("Cache hit for region %s: %s", region, cache_file)
        return json.loads(cache_file.read_text(encoding="utf-8"))

    country_qids = [COUNTRY_QIDS[cc] for cc in country_codes if cc in COUNTRY_QIDS]
    if not country_qids:
        log.warning("No country Q-ids found for region %s", region)
        return []

    log.info("Fetching companies for region %s (countries: %s, target: %d)", region, country_codes, target_count)

    all_rows: list[dict[str, Any]] = []
    # Try to fetch in batches of countries to avoid timeout
    batch_size = 4  # countries per query
    limit_per_batch = max(50, target_count // max(1, len(country_codes) // batch_size + 1))

    for i in range(0, len(country_codes), batch_size):
        batch_codes = country_codes[i:i + batch_size]
        batch_qids = [COUNTRY_QIDS[cc] for cc in batch_codes if cc in COUNTRY_QIDS]
        if not batch_qids:
            continue

        query = _build_sparql_query(batch_qids, limit=limit_per_batch * 2)
        try:
            data = _sparql_get(query)
            bindings = data.get("results", {}).get("bindings", [])
            log.info("  Batch %s: got %d rows from SPARQL", batch_codes, len(bindings))
            for row in bindings:
                qid = _qid_of(row.get("company", {}).get("value", ""))
                if not qid:
                    continue
                country_code = None
                # Determine country from batch (we queried by country filter)
                # We'll resolve country per company in enrichment step
                # For now, try to infer from HQ or use batch hint
                hq_coord = row.get("hqCoord", {}).get("value") or row.get("coord", {}).get("value")
                lat, lon = None, None
                if hq_coord:
                    m = re.match(r"Point\(([\-\d.]+)\s+([\-\d.]+)\)", hq_coord)
                    if m:
                        lon = float(m.group(1))
                        lat = float(m.group(2))
                industry_qid = _qid_of(row.get("industry", {}).get("value", ""))
                all_rows.append({
                    "qid": qid,
                    "name": row.get("companyLabel", {}).get("value", ""),
                    "description": row.get("companyDescription", {}).get("value", ""),
                    "hq": row.get("hqLabel", {}).get("value", ""),
                    "lat": lat,
                    "lon": lon,
                    "revenue": row.get("revenue", {}).get("value"),
                    "industry_qid": industry_qid,
                    "country_batch": batch_codes,  # used to guess country
                })
        except Exception as exc:
            log.warning("SPARQL query failed for %s: %s — trying fallback", batch_codes, exc)
            # Try simpler fallback query
            try:
                query2 = _build_sparql_country_only(batch_qids, limit=limit_per_batch)
                data2 = _sparql_get(query2)
                bindings2 = data2.get("results", {}).get("bindings", [])
                log.info("  Fallback batch %s: got %d rows", batch_codes, len(bindings2))
                for row in bindings2:
                    qid = _qid_of(row.get("company", {}).get("value", ""))
                    if not qid:
                        continue
                    hq_coord = row.get("hqCoord", {}).get("value", "")
                    lat, lon = None, None
                    if hq_coord:
                        m = re.match(r"Point\(([\-\d.]+)\s+([\-\d.]+)\)", hq_coord)
                        if m:
                            lon = float(m.group(1))
                            lat = float(m.group(2))
                    industry_qid = _qid_of(row.get("industry", {}).get("value", ""))
                    all_rows.append({
                        "qid": qid,
                        "name": row.get("companyLabel", {}).get("value", ""),
                        "description": row.get("companyDescription", {}).get("value", ""),
                        "hq": "",
                        "lat": lat,
                        "lon": lon,
                        "revenue": None,
                        "industry_qid": industry_qid,
                        "country_batch": batch_codes,
                    })
            except Exception as exc2:
                log.error("Fallback also failed for %s: %s", batch_codes, exc2)

    # Deduplicate by qid
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for row in all_rows:
        if row["qid"] not in seen and row["name"]:
            seen.add(row["qid"])
            deduped.append(row)

    log.info("Region %s: %d unique companies after dedup (target was %d)", region, len(deduped), target_count)
    # Cache results
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(deduped, indent=2), encoding="utf-8")
    return deduped


# ---------------------------------------------------------------------------
# Enrich with country mapping
# ---------------------------------------------------------------------------

def _fetch_company_countries(qids: list[str]) -> dict[str, str]:
    """Fetch country codes for a list of Q-ids."""
    CHUNK = 80
    out: dict[str, str] = {}
    # We need the ISO 2-letter code (P297 on the country)
    for i in range(0, len(qids), CHUNK):
        chunk = qids[i:i + CHUNK]
        values = " ".join(f"wd:{q}" for q in chunk)
        query = f"""
SELECT ?company ?countryCode WHERE {{
  VALUES ?company {{ {values} }}
  ?company wdt:P17 ?country .
  ?country wdt:P297 ?countryCode .
}}
"""
        try:
            data = _sparql_get(query)
            for row in data.get("results", {}).get("bindings", []):
                qid = _qid_of(row.get("company", {}).get("value", ""))
                code = row.get("countryCode", {}).get("value", "")
                if qid and code:
                    out.setdefault(qid, code.upper())
        except Exception as exc:
            log.warning("Country lookup chunk failed: %s", exc)
    return out


# ---------------------------------------------------------------------------
# Gics sub-industry validation
# ---------------------------------------------------------------------------

def _load_valid_subindustries(config_root: Path) -> set[str]:
    path = config_root / "gics_subindustry_to_industry.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return set(data.keys())


# ---------------------------------------------------------------------------
# Build Node records
# ---------------------------------------------------------------------------

def _build_wikidata_node(
    row: dict[str, Any],
    country_code: str,
    valid_subindustries: set[str],
) -> Optional[Node]:
    """Convert a SPARQL row into a validated wikidata: Node."""
    qid = row.get("qid", "")
    name = (row.get("name") or "").strip()
    if not qid or not name:
        return None
    if name.startswith("Q") and name[1:].isdigit():
        # Name is just the Q-id (no English label) — skip
        return None

    industry_qids = []
    if row.get("industry_qid"):
        industry_qids.append(row["industry_qid"])

    sector, sub_industry = _guess_gics(industry_qids, row.get("description", ""))

    # Validate sub_industry against known list; fall back to safe default
    if sub_industry not in valid_subindustries:
        log.debug("Sub-industry %r not in map for %s — using Industrial Conglomerates", sub_industry, name)
        sub_industry = "Industrial Conglomerates"
        sector = "Industrials"

    node_id = f"wikidata:{qid}"
    metadata: dict[str, Any] = {
        "wikidata": {
            "qid": qid,
            "lat": row.get("lat"),
            "lon": row.get("lon"),
            "hq": row.get("hq"),
            "country": country_code,
            "description": row.get("description", ""),
        }
    }

    try:
        return Node(
            id=node_id,
            type=NodeType.Company,
            name=name,
            aliases=[name],
            tickers=[],
            identifiers={"wikidata": qid},
            sector=sector,
            industry=sub_industry,
            country=country_code,
            metadata=metadata,
        )
    except Exception as exc:
        log.debug("Node validation failed for %s (%s): %s", name, qid, exc)
        return None


# ---------------------------------------------------------------------------
# Load existing known Q-ids and names from companies.jsonl
# ---------------------------------------------------------------------------

def _load_existing(companies_path: Path) -> tuple[set[str], set[str]]:
    """Returns (known_qids, known_normalized_names)."""
    known_qids: set[str] = set()
    known_names: set[str] = set()
    if not companies_path.exists():
        return known_qids, known_names
    for line in companies_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
            node_id = obj.get("id", "")
            if node_id.startswith("wikidata:"):
                qid = node_id[len("wikidata:"):]
                known_qids.add(qid)
            wikidata_id = (obj.get("identifiers") or {}).get("wikidata")
            if wikidata_id:
                known_qids.add(wikidata_id)
            name = (obj.get("name") or "").strip().lower()
            if name:
                known_names.add(name)
        except Exception:
            continue
    return known_qids, known_names


def _load_wikidata_companies_json(path: Path) -> set[str]:
    """Load Q-ids from the wikidata_companies.json side file (CIK-mapped Q-ids)."""
    if not path.exists():
        return set()
    data = json.loads(path.read_text(encoding="utf-8"))
    qids: set[str] = set()
    for entry in data.values():
        if isinstance(entry, dict) and entry.get("qid"):
            qids.add(entry["qid"])
    return qids


# ---------------------------------------------------------------------------
# Append new nodes to companies.jsonl
# ---------------------------------------------------------------------------

def _append_nodes(companies_path: Path, new_nodes: list[Node]) -> int:
    """Append new nodes to companies.jsonl, skipping any that already exist."""
    # Re-read existing to get current set (paranoid dedup)
    existing_ids: set[str] = set()
    lines: list[str] = []
    if companies_path.exists():
        for line in companies_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                lines.append(line)
                try:
                    obj = json.loads(line)
                    existing_ids.add(obj.get("id", ""))
                except Exception:
                    pass

    added = 0
    with companies_path.open("a", encoding="utf-8") as fh:
        for node in new_nodes:
            if node.id in existing_ids:
                continue
            fh.write(node.model_dump_json())
            fh.write("\n")
            existing_ids.add(node.id)
            added += 1
    return added


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

def run(
    *,
    data_root: Path,
    config_root: Path,
) -> dict[str, Any]:
    companies_path = data_root / "companies.jsonl"
    wikidata_companies_path = data_root / "wikidata_companies.json"
    cache_dir = data_root / "cache"

    # Load existing state
    known_qids, known_names = _load_existing(companies_path)
    # Also exclude Q-ids from the CIK-mapped side file
    cik_qids = _load_wikidata_companies_json(wikidata_companies_path)
    known_qids |= cik_qids

    log.info("Existing: %d known Q-ids, %d known company names", len(known_qids), len(known_names))

    valid_subindustries = _load_valid_subindustries(config_root)
    log.info("Loaded %d valid GICS sub-industries", len(valid_subindustries))

    all_raw_rows: list[dict[str, Any]] = []
    region_counts: dict[str, int] = {}

    for region, country_codes in REGIONS.items():
        target = REGION_TARGETS[region]
        raw_rows = fetch_region_companies(region, country_codes, target, cache_dir)
        all_raw_rows.extend(raw_rows)
        region_counts[region] = len(raw_rows)

    log.info("Total raw rows across all regions: %d", len(all_raw_rows))

    # Deduplicate across regions by Q-id
    seen_qids: set[str] = set(known_qids)
    unique_rows: list[dict[str, Any]] = []
    for row in all_raw_rows:
        qid = row.get("qid", "")
        if not qid or qid in seen_qids:
            continue
        name = (row.get("name") or "").strip()
        if not name or name.lower() in known_names:
            continue
        seen_qids.add(qid)
        unique_rows.append(row)

    log.info("After dedup against existing: %d unique new rows", len(unique_rows))

    # Batch-fetch country codes from Wikidata
    qids_to_enrich = [r["qid"] for r in unique_rows]
    log.info("Fetching country codes for %d companies...", len(qids_to_enrich))
    country_map = _fetch_company_countries(qids_to_enrich)
    log.info("Got country codes for %d/%d companies", len(country_map), len(qids_to_enrich))

    # Build nodes
    new_nodes: list[Node] = []
    per_region_added: dict[str, int] = {r: 0 for r in REGIONS}
    country_counts: dict[str, int] = {}

    for row in unique_rows:
        qid = row["qid"]
        # Get country code from enrichment or guess from batch
        country_code = country_map.get(qid)
        if not country_code:
            # Guess from the batch countries list — use first country in batch
            batch = row.get("country_batch", [])
            country_code = batch[0] if batch else "XX"

        # Apply per-country caps
        cc_count = country_counts.get(country_code, 0)
        cap = COUNTRY_CAPS.get(country_code)
        if cap is not None and cc_count >= cap:
            log.debug("Country cap hit for %s (%d >= %d), skipping %s", country_code, cc_count, cap, row.get("name"))
            continue

        node = _build_wikidata_node(row, country_code, valid_subindustries)
        if node is None:
            continue

        new_nodes.append(node)
        country_counts[country_code] = cc_count + 1

        # Figure out which region for stats
        for region, codes in REGIONS.items():
            if country_code in codes:
                per_region_added[region] = per_region_added.get(region, 0) + 1
                break

    log.info("Built %d valid new company nodes", len(new_nodes))

    # Append to companies.jsonl
    added = _append_nodes(companies_path, new_nodes)
    log.info("Appended %d new companies to %s", added, companies_path)

    # Read final count
    total_count = sum(1 for line in companies_path.read_text(encoding="utf-8").splitlines() if line.strip())

    summary = {
        "region_raw_counts": region_counts,
        "unique_rows_after_dedup": len(unique_rows),
        "nodes_built": len(new_nodes),
        "nodes_added": added,
        "total_companies": total_count,
        "per_region_added": per_region_added,
        "top_countries": sorted(country_counts.items(), key=lambda x: -x[1])[:15],
    }
    log.info("Phase B ingestion summary:\n%s", json.dumps(summary, indent=2))
    return summary


def main() -> int:
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="EconGraph Phase B — Wikidata company expansion")
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--config-root", default="config")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    summary = run(
        data_root=Path(args.data_root),
        config_root=Path(args.config_root),
    )
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
