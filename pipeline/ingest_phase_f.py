"""Phase F: Exchange-index expansion for major non-US stock exchanges.

Enumerates companies listed on major stock exchanges (NSE/BSE, TSE, LSE,
Frankfurt, KRX, HKEX, ASX, SSE) via Wikidata P:414 (stock exchange listing)
and appends new companies to data/companies.jsonl.

Why Phase F over Phase B?
  Phase B ranked by Wikidata P:2139 (revenue) — most non-US companies have
  sparse or missing revenue data on Wikidata, so they rank low. This leaves
  NIFTY 50, FTSE 100, Nikkei 225, etc. significantly under-represented.
  Phase F queries by exchange membership directly, giving us actual index members.

Target exchanges and expected additions:
  NSE India         → ~450 new Indian companies
  Tokyo SE          → ~260 new Japanese companies
  London SE         → ~170 new UK companies
  Frankfurt SE      → ~90  new German companies
  Korea Exchange    → ~185 new Korean companies
  Hong Kong SE      → ~130 new HK/CN companies
  ASX               → ~175 new Australian companies
  Shanghai SE       → ~130 new Chinese companies

Pipeline invariants (CLAUDE.md):
  - One row per entity: deduplicates by Q-id against existing companies.jsonl
  - Canonical IDs: wikidata:Qxxxxxx for exchange-listed non-SEC companies
  - Cache-first: SPARQL results cached to data/cache/phase_f_sparql_{slug}.json
  - Rate limit: ≤1 req/s to Wikidata SPARQL
  - Skips SEC filers (those already have cik: ids via Phase A)

Output:
    data/companies.jsonl  -- APPENDED (existing rows untouched)
    data/cache/phase_f_sparql_{slug}.json -- cached per-exchange SPARQL results
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Optional

import requests

from schema.models import Node, NodeType

log = logging.getLogger("ingest_phase_f")

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR  = REPO_ROOT / "data"
CACHE_DIR = DATA_DIR / "cache"

WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"

# ---------------------------------------------------------------------------
# Rate limiter (≤1 req/s for Wikidata)
# ---------------------------------------------------------------------------
_lock = threading.Lock()
_last_req = 0.0
_MIN_INTERVAL = 1.2  # slight buffer above 1 s


def _ua() -> str:
    ua = os.environ.get("EDGAR_USER_AGENT", "EconGraph/1.0 (kondaru.mk@gmail.com)")
    return ua + " (EconGraph Phase F exchange ingestion)"


def _sparql(query: str, timeout: float = 90.0) -> list[dict[str, Any]]:
    global _last_req
    with _lock:
        wait = _MIN_INTERVAL - (time.monotonic() - _last_req)
        if wait > 0:
            time.sleep(wait)
        _last_req = time.monotonic()
    resp = requests.get(
        WIKIDATA_SPARQL,
        params={"query": query, "format": "json"},
        headers={"User-Agent": _ua(), "Accept": "application/sparql-results+json"},
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json().get("results", {}).get("bindings", [])


def _qid(url: str) -> Optional[str]:
    m = re.search(r"/(Q\d+)$", url)
    return m.group(1) if m else None


def _parse_coord(point: str) -> tuple[Optional[float], Optional[float]]:
    """Parse 'Point(lon lat)' → (lat, lon)."""
    m = re.match(r"Point\(([\-\d.]+)\s+([\-\d.]+)\)", point or "")
    if not m:
        return None, None
    return float(m.group(2)), float(m.group(1))  # lat, lon


# ---------------------------------------------------------------------------
# Target country expansions
# ---------------------------------------------------------------------------
# Strategy: query by country (P:17) + has any exchange listing (P:414).
# This is more reliable than querying by specific exchange Q-id because:
#   - Country Q-ids are stable and well-known
#   - Not all companies have their exact exchange Q-id set correctly in Wikidata
#   - A country filter automatically gets NSE + BSE + regional exchanges together
#
# Wikidata country Q-ids confirmed via diagnostic SPARQL:
#   IN Q668  → NSE: 154, BSE: 70 companies
#   JP Q17   → TSE: 2189 companies
#   DE Q183  → FSE: 104 companies
#   KR Q884  → KRX: 400, KOSDAQ: 144 companies
#   AU Q408  → ASX: 320 companies
#   CN Q148  → SZSE: 529, HKEX: 435, SSE: 394 companies
#   HK Q8646 → HKEX: 88 companies
#   TW Q865  → TWSE: 114 companies
#   GB Q145  → LSE: 366 companies

COUNTRY_EXPANSIONS: dict[str, dict[str, Any]] = {
    "india": {
        "country_qid": "Q668",
        "country": "IN",
        "name": "India (NSE/BSE listed)",
        "target_n": 350,
    },
    "japan": {
        "country_qid": "Q17",
        "country": "JP",
        "name": "Japan (TSE listed)",
        "target_n": 350,
    },
    "united_kingdom": {
        "country_qid": "Q145",
        "country": "GB",
        "name": "United Kingdom (LSE listed)",
        "target_n": 300,
    },
    "germany": {
        "country_qid": "Q183",
        "country": "DE",
        "name": "Germany (Frankfurt listed)",
        "target_n": 150,
    },
    "south_korea": {
        "country_qid": "Q884",
        "country": "KR",
        "name": "South Korea (KRX listed)",
        "target_n": 300,
    },
    "china_mainland": {
        "country_qid": "Q148",
        "country": "CN",
        "name": "China (SSE/SZSE listed)",
        "target_n": 300,
    },
    "hong_kong": {
        "country_qid": "Q8646",
        "country": "HK",
        "name": "Hong Kong (HKEX listed)",
        "target_n": 120,
    },
    "australia": {
        "country_qid": "Q408",
        "country": "AU",
        "name": "Australia (ASX listed)",
        "target_n": 280,
    },
    "taiwan": {
        "country_qid": "Q865",
        "country": "TW",
        "name": "Taiwan (TWSE listed)",
        "target_n": 150,
    },
}

# ---------------------------------------------------------------------------
# GICS classification helpers (mirrors ingest_phase_b.py)
# ---------------------------------------------------------------------------

INDUSTRY_MAP: dict[str, dict[str, str]] = {
    # Banks / Financial
    "Q35709":  {"sector": "Financials", "industry": "Diversified Banks"},
    "Q22687":  {"sector": "Financials", "industry": "Life & Health Insurance"},
    "Q43267":  {"sector": "Financials", "industry": "Diversified Banks"},
    "Q837525": {"sector": "Financials", "industry": "Investment Banking & Brokerage"},
    "Q746403": {"sector": "Financials", "industry": "Asset Management & Custody Banks"},
    "Q837056": {"sector": "Financials", "industry": "Investment Banking & Brokerage"},
    # Energy
    "Q1547032": {"sector": "Energy",   "industry": "Integrated Oil & Gas"},
    "Q206934":  {"sector": "Energy",   "industry": "Oil & Gas Exploration & Production"},
    "Q211272":  {"sector": "Utilities", "industry": "Electric Utilities"},
    "Q3508735": {"sector": "Utilities", "industry": "Electric Utilities"},
    # Auto
    "Q786820": {"sector": "Consumer Discretionary", "industry": "Automobile Manufacturers"},
    "Q9453":   {"sector": "Consumer Discretionary", "industry": "Automobile Manufacturers"},
    # Pharma / Health
    "Q174174": {"sector": "Health Care", "industry": "Pharmaceuticals"},
    "Q507443": {"sector": "Health Care", "industry": "Pharmaceuticals"},
    "Q507442": {"sector": "Health Care", "industry": "Biotechnology"},
    # Materials
    "Q11642":  {"sector": "Materials", "industry": "Steel"},
    "Q11814":  {"sector": "Materials", "industry": "Gold"},
    "Q11665":  {"sector": "Materials", "industry": "Commodity Chemicals"},
    "Q131681": {"sector": "Materials", "industry": "Specialty Chemicals"},
    # Retail / Consumer
    "Q268592":  {"sector": "Consumer Discretionary", "industry": "Broadline Retail"},
    "Q1063239": {"sector": "Consumer Discretionary", "industry": "Broadline Retail"},
    "Q131285":  {"sector": "Consumer Staples", "industry": "Packaged Foods & Meats"},
    "Q156":     {"sector": "Consumer Staples", "industry": "Brewers"},
    "Q160018":  {"sector": "Consumer Staples", "industry": "Soft Drinks & Non-alcoholic Beverages"},
    # Telecom
    "Q29023":  {"sector": "Communication Services", "industry": "Integrated Telecommunication Services"},
    "Q194142": {"sector": "Communication Services", "industry": "Integrated Telecommunication Services"},
    # IT
    "Q7397":  {"sector": "Information Technology", "industry": "Systems Software"},
    "Q17156": {"sector": "Information Technology", "industry": "Semiconductors"},
    "Q11462": {"sector": "Information Technology", "industry": "Semiconductors"},
    # Industrials
    "Q210167": {"sector": "Industrials", "industry": "Aerospace & Defense"},
    "Q4830453": {"sector": "Industrials", "industry": "Industrial Conglomerates"},
    "Q783794":  {"sector": "Industrials", "industry": "Industrial Conglomerates"},
}

SECTOR_KEYWORDS: list[tuple[list[str], str, str]] = [
    (["bank", "banking", "financial services"],       "Financials",               "Diversified Banks"),
    (["insurance"],                                    "Financials",               "Life & Health Insurance"),
    (["asset management", "investment fund"],          "Financials",               "Asset Management & Custody Banks"),
    (["oil", "gas", "petroleum"],                      "Energy",                   "Integrated Oil & Gas"),
    (["electric", "electricity", "power utility"],     "Utilities",                "Electric Utilities"),
    (["automobile", "automotive", "car manufacturer"], "Consumer Discretionary",   "Automobile Manufacturers"),
    (["pharmaceutical", "pharma", "drug"],             "Health Care",              "Pharmaceuticals"),
    (["hospital", "health care", "healthcare"],        "Health Care",              "Health Care Facilities"),
    (["semiconductor", "chip"],                        "Information Technology",   "Semiconductors"),
    (["software", "technology company", "it services"],"Information Technology",  "IT Consulting & Other Services"),
    (["telecom", "telecommunications", "mobile network"],"Communication Services","Integrated Telecommunication Services"),
    (["retail", "supermarket", "hypermarket"],         "Consumer Discretionary",   "Broadline Retail"),
    (["mining", "mine"],                               "Materials",                "Metals & Mining"),
    (["steel", "iron"],                                "Materials",                "Steel"),
    (["chemical"],                                     "Materials",                "Specialty Chemicals"),
    (["food", "beverage", "dairy"],                    "Consumer Staples",         "Packaged Foods & Meats"),
    (["real estate", "property", "reit"],              "Real Estate",              "Real Estate Services"),
    (["airline", "aviation", "air transport"],         "Industrials",              "Passenger Airlines"),
    (["logistics", "shipping", "freight"],             "Industrials",              "Air Freight & Logistics"),
    (["construction", "infrastructure", "engineering"],"Industrials",             "Construction & Engineering"),
    (["media", "broadcast", "television"],             "Communication Services",   "Broadcasting"),
    (["luxury", "fashion", "apparel"],                 "Consumer Discretionary",   "Apparel, Accessories & Luxury Goods"),
    (["tobacco"],                                      "Consumer Staples",         "Tobacco"),
    (["conglomerate", "holding company"],              "Industrials",              "Industrial Conglomerates"),
]


def _guess_gics(industry_qid: Optional[str], description: str) -> tuple[str, str]:
    if industry_qid and industry_qid in INDUSTRY_MAP:
        m = INDUSTRY_MAP[industry_qid]
        return m["sector"], m["industry"]
    desc_lower = (description or "").lower()
    for keywords, sector, sub_industry in SECTOR_KEYWORDS:
        if any(kw in desc_lower for kw in keywords):
            return sector, sub_industry
    return "Industrials", "Industrial Conglomerates"


# ---------------------------------------------------------------------------
# Wikidata SPARQL: publicly-traded companies in a given country
# ---------------------------------------------------------------------------

def _build_country_exchange_query(country_qid: str, limit: int) -> str:
    """Get publicly-traded companies in a country (P:17=country AND P:414=any exchange).
    Ordered by market cap descending so the LIMIT keeps the most important companies.
    Excludes SEC filers (P:5531 CIK) since those are already in the dataset as cik: nodes.
    """
    return f"""
SELECT DISTINCT ?company ?companyLabel ?companyDescription ?marketCap ?coord ?industry WHERE {{
  ?company wdt:P17 wd:{country_qid} .
  ?company wdt:P414 ?anyExchange .
  FILTER NOT EXISTS {{ ?company wdt:P576 ?dissolved . }}
  FILTER NOT EXISTS {{ ?company wdt:P5531 ?cik . }}
  OPTIONAL {{ ?company wdt:P2226 ?marketCap . }}
  OPTIONAL {{ ?company wdt:P159/wdt:P625 ?coord . }}
  OPTIONAL {{ ?company wdt:P452 ?industry . }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
}}
ORDER BY DESC(?marketCap)
LIMIT {limit}
"""


def _build_country_query_simple(country_qid: str, limit: int) -> str:
    """Simpler fallback: no coords/market cap to avoid SPARQL timeout."""
    return f"""
SELECT DISTINCT ?company ?companyLabel ?companyDescription ?industry WHERE {{
  ?company wdt:P17 wd:{country_qid} .
  ?company wdt:P414 ?anyExchange .
  FILTER NOT EXISTS {{ ?company wdt:P576 ?dissolved . }}
  FILTER NOT EXISTS {{ ?company wdt:P5531 ?cik . }}
  OPTIONAL {{ ?company wdt:P452 ?industry . }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
}}
LIMIT {limit}
"""


def fetch_country_companies(
    slug: str,
    country_qid: str,
    target_n: int,
    cache_dir: Path,
) -> list[dict[str, Any]]:
    """Fetch publicly-traded companies for a country expansion (cache-first)."""
    cache_file = cache_dir / f"phase_f_sparql_{slug}.json"
    if cache_file.exists():
        log.info("[%s] Cache hit → %s", slug, cache_file)
        return json.loads(cache_file.read_text(encoding="utf-8"))

    log.info("[%s] Querying Wikidata for country %s (limit %d) …", slug, country_qid, target_n)
    try:
        bindings = _sparql(_build_country_exchange_query(country_qid, target_n))
        log.info("[%s] Got %d rows from primary query", slug, len(bindings))
    except Exception as exc:
        log.warning("[%s] Primary query failed: %s — trying simple fallback", slug, exc)
        try:
            bindings = _sparql(_build_country_query_simple(country_qid, target_n))
            log.info("[%s] Fallback got %d rows", slug, len(bindings))
        except Exception as exc2:
            log.error("[%s] Fallback also failed: %s — skipping", slug, exc2)
            return []

    # Parse into flat dicts, dedup by Q-id (DISTINCT in SPARQL helps but multiple
    # optional fields can still produce cartesian-product duplicates).
    seen: set[str] = set()
    rows: list[dict[str, Any]] = []
    for b in bindings:
        qid_val = _qid(b.get("company", {}).get("value", ""))
        if not qid_val:
            continue
        name = b.get("companyLabel", {}).get("value", "").strip()
        # Skip blank labels or bare Q-id labels (Wikidata fallback when no English label)
        if not name or (name.startswith("Q") and name[1:].isdigit()):
            continue
        if qid_val in seen:
            continue
        seen.add(qid_val)

        lat, lon = _parse_coord(b.get("coord", {}).get("value", ""))
        market_cap_raw = b.get("marketCap", {}).get("value")
        market_cap = float(market_cap_raw) if market_cap_raw else None
        industry_qid = _qid(b.get("industry", {}).get("value", ""))
        description = b.get("companyDescription", {}).get("value", "")

        rows.append({
            "qid": qid_val,
            "name": name,
            "description": description,
            "lat": lat,
            "lon": lon,
            "market_cap": market_cap,
            "industry_qid": industry_qid,
        })

    log.info("[%s] %d unique companies after dedup", slug, len(rows))
    if rows:
        log.info("[%s] Top 5 samples: %s", slug, [r["name"] for r in rows[:5]])

    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    return rows


# ---------------------------------------------------------------------------
# Country lookup (batched) — used for HKEX where country varies
# ---------------------------------------------------------------------------

def fetch_company_countries(qids: list[str]) -> dict[str, str]:
    """Batch-fetch ISO-2 country codes for a list of Q-ids (P:17 → P:297)."""
    CHUNK = 60
    result: dict[str, str] = {}
    for start in range(0, len(qids), CHUNK):
        chunk = qids[start:start + CHUNK]
        values = " ".join(f"wd:{q}" for q in chunk)
        query = f"""
SELECT ?company ?countryCode WHERE {{
  VALUES ?company {{ {values} }}
  ?company wdt:P17 ?countryEntity .
  ?countryEntity wdt:P297 ?countryCode .
}}
"""
        try:
            bindings = _sparql(query, timeout=30)
            for b in bindings:
                cqid = _qid(b.get("company", {}).get("value", ""))
                code = b.get("countryCode", {}).get("value", "")
                if cqid and code and cqid not in result:
                    result[cqid] = code.upper()
        except Exception as exc:
            log.warning("Country lookup failed for chunk at %d: %s", start, exc)
    return result


# ---------------------------------------------------------------------------
# Build a Node record from raw row data
# ---------------------------------------------------------------------------

def _make_node(
    qid: str,
    name: str,
    description: str,
    country: str,
    lat: Optional[float],
    lon: Optional[float],
    sector: str,
    industry: str,
) -> Node:
    return Node(
        id=f"wikidata:{qid}",
        type=NodeType.Company,
        name=name,
        aliases=[name],
        tickers=[],
        identifiers={"wikidata": qid},
        sector=sector,
        industry=industry,
        country=country,
        metadata={
            "wikidata": {
                "qid": qid,
                "lat": lat,
                "lon": lon,
                "hq": "",
                "country": country,
                "description": description,
            }
        },
    )


# ---------------------------------------------------------------------------
# Main ingestion logic
# ---------------------------------------------------------------------------

def run(data_dir: Path = DATA_DIR, cache_dir: Path = CACHE_DIR) -> dict[str, Any]:
    companies_path = data_dir / "companies.jsonl"

    # Build a set of existing IDs (both wikidata Q-ids and cik numbers)
    existing_qids: set[str] = set()
    existing_names_lower: set[str] = set()
    if companies_path.exists():
        for line in companies_path.open(encoding="utf-8"):
            c = json.loads(line)
            cid = c.get("id", "")
            if cid.startswith("wikidata:"):
                existing_qids.add(cid[len("wikidata:"):])
            # Also track CIK-linked Wikidata Q-ids (wikidata aliases file)
            wid = (c.get("identifiers") or {}).get("wikidata")
            if wid:
                existing_qids.add(wid)
            nm = (c.get("name") or "").strip().lower()
            if nm:
                existing_names_lower.add(nm)

    log.info("Existing dataset: %d Q-ids already known", len(existing_qids))

    total_added = 0
    by_exchange: dict[str, int] = {}

    with companies_path.open("a", encoding="utf-8") as out_f:
        for slug, cfg in COUNTRY_EXPANSIONS.items():
            country_qid    = cfg["country_qid"]
            default_country = cfg["country"]
            target_n       = cfg["target_n"]
            display_name   = cfg["name"]

            rows = fetch_country_companies(slug, country_qid, target_n, cache_dir)
            if not rows:
                log.warning("[%s] No rows returned — skipping", slug)
                by_exchange[slug] = 0
                continue

            added = 0
            for row in rows:
                qid_val = row["qid"]
                if qid_val in existing_qids:
                    continue
                name = row["name"]
                if not name:
                    continue
                # Belt-and-braces: skip name-exact matches across exchanges
                if name.strip().lower() in existing_names_lower:
                    existing_qids.add(qid_val)
                    continue

                sector, industry = _guess_gics(row.get("industry_qid"), row.get("description", ""))
                node = _make_node(
                    qid=qid_val,
                    name=name,
                    description=row.get("description", ""),
                    country=default_country,
                    lat=row.get("lat"),
                    lon=row.get("lon"),
                    sector=sector,
                    industry=industry,
                )
                out_f.write(node.model_dump_json() + "\n")
                existing_qids.add(qid_val)
                existing_names_lower.add(name.strip().lower())
                added += 1

            log.info("[%s] Added %d new companies (from %d fetched)", display_name, added, len(rows))
            by_exchange[slug] = added
            total_added += added

    log.info("Phase F complete: %d new companies appended to companies.jsonl", total_added)
    return {"total_added": total_added, "by_exchange": by_exchange}


def main() -> None:
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(description="EconGraph Phase F — exchange-index expansion")
    parser.add_argument("--data-dir",  default=str(DATA_DIR))
    parser.add_argument("--cache-dir", default=str(CACHE_DIR))
    parser.add_argument(
        "--clear-cache",
        action="store_true",
        help="Delete cached SPARQL results and re-fetch from Wikidata",
    )
    args = parser.parse_args()

    data_dir  = Path(args.data_dir)
    cache_dir = Path(args.cache_dir)

    if args.clear_cache:
        for f in cache_dir.glob("phase_f_sparql_*.json"):
            f.unlink()
            log.info("Cleared cache: %s", f)

    summary = run(data_dir=data_dir, cache_dir=cache_dir)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
