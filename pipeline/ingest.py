"""Phase 1 ingestion: S&P 500 roster + cached latest-10-K filings.

Pipeline stage 1 of the layered, file-based, restartable design (CLAUDE.md
invariant #5). Outputs:

    data/sources/sp500_wikipedia_<YYYYMMDD>.html  -- raw HTML for reproducibility
    data/filings/<cik>/<accession>.{txt,htm,html} -- cached 10-K primary docs
    data/companies.jsonl                          -- one validated Node per company

This stage emits NO edges and NO Segment nodes — extraction (Phase 2) and
resolution (Phase 3) own those. Re-running this script must serve every
already-cached filing from disk (cache-first per invariant #6).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

import requests
from bs4 import BeautifulSoup

from schema.models import Node, NodeType
from .sec import get_user_agent, sec_get


log = logging.getLogger("ingest")


# ---------------------------------------------------------------------------
# Data shapes (internal to this module)
# ---------------------------------------------------------------------------

@dataclass
class RawRow:
    """One row out of the Wikipedia S&P 500 table, before dedupe."""

    ticker: str
    name: str
    gics_sector: str
    gics_sub_industry: str
    cik: str  # 10-digit zero-padded


@dataclass
class CompanyRecord:
    """Deduped, ready to render as a Node + later to fetch its 10-K."""

    cik: str  # 10-digit zero-padded
    name: str
    tickers: list[str]
    sector: Optional[str] = None
    industry: Optional[str] = None
    aliases: list[str] = field(default_factory=list)
    accession: Optional[str] = None
    filing_url: Optional[str] = None
    filing_date: Optional[str] = None
    primary_document: Optional[str] = None
    filing_local_path: Optional[str] = None
    filing_status: str = "pending"  # pending | cached | fetched | missing
    # Phase A additions: sub_industry surfaced explicitly (was previously
    # collapsed into industry by the S&P 500 Wikipedia scraper), and
    # country_code so the foreign-filer roster can carry non-US origins.
    sub_industry: Optional[str] = None
    country_code: str = "US"


# ---------------------------------------------------------------------------
# Wikipedia roster
# ---------------------------------------------------------------------------

WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"


def fetch_sp500_html(sources_dir: Path, ua: str) -> Path:
    """Download the S&P 500 list HTML to disk for reproducibility, return its path.

    Cache-first: if today's snapshot already exists, return it without a refetch.
    """
    sources_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.date.today().strftime("%Y%m%d")
    out = sources_dir / f"sp500_wikipedia_{stamp}.html"
    if out.exists():
        log.info("Wikipedia snapshot cached: %s", out)
        return out
    log.info("Fetching S&P 500 list from Wikipedia: %s", WIKI_URL)
    resp = requests.get(WIKI_URL, headers={"User-Agent": ua}, timeout=30)
    resp.raise_for_status()
    out.write_text(resp.text, encoding="utf-8")
    log.info("Wrote Wikipedia snapshot: %s (%d bytes)", out, len(resp.text))
    return out


def parse_sp500_table(html: str) -> list[RawRow]:
    """Header-keyed parse of the constituents table.

    Raises ValueError if any of Symbol / Security / GICS Sector /
    GICS Sub-Industry / CIK is missing — never guess column positions.
    """
    soup = BeautifulSoup(html, "html.parser")
    # The constituents table is the first wikitable on the page and has id
    # "constituents". We anchor on the id to avoid future page churn.
    table = soup.find("table", {"id": "constituents"})
    if table is None:
        # Fall back to first wikitable just in case the id changes.
        table = soup.find("table", {"class": "wikitable"})
    if table is None:
        raise ValueError("Could not locate the S&P 500 constituents table in the HTML")

    header_cells = table.find("tr").find_all(["th", "td"])
    headers = [c.get_text(strip=True) for c in header_cells]

    # Wikipedia sometimes renders "GICSSector" (no space) and sometimes
    # "GICS Sector" — its templating is inconsistent. We still key by NAME,
    # but normalize whitespace + case for the lookup so future churn between
    # "GICS Sector" / "GICSSector" / "Gics Sector" does not silently misalign.
    def _norm(s: str) -> str:
        return "".join(s.split()).lower()

    norm_headers = [_norm(h) for h in headers]
    required = ["Symbol", "Security", "GICS Sector", "GICS Sub-Industry", "CIK"]
    col_index: dict[str, int] = {}
    for name in required:
        try:
            col_index[name] = norm_headers.index(_norm(name))
        except ValueError as exc:
            raise ValueError(
                f"Expected column {name!r} not found in S&P 500 table headers: {headers}"
            ) from exc

    rows: list[RawRow] = []
    for tr in table.find_all("tr")[1:]:
        cells = tr.find_all(["td", "th"])
        if len(cells) < max(col_index.values()) + 1:
            continue
        texts = [c.get_text(" ", strip=True) for c in cells]
        ticker = texts[col_index["Symbol"]]
        name = texts[col_index["Security"]]
        sector = texts[col_index["GICS Sector"]]
        sub_industry = texts[col_index["GICS Sub-Industry"]]
        cik_raw = texts[col_index["CIK"]]
        cik_digits = "".join(ch for ch in cik_raw if ch.isdigit())
        if not cik_digits:
            log.warning("Skipping row with non-numeric CIK %r (ticker=%s)", cik_raw, ticker)
            continue
        cik_padded = cik_digits.zfill(10)
        rows.append(
            RawRow(
                ticker=ticker,
                name=name,
                gics_sector=sector,
                gics_sub_industry=sub_industry,
                cik=cik_padded,
            )
        )
    return rows


def dedupe_by_cik(rows: Iterable[RawRow]) -> list[CompanyRecord]:
    """Collapse share-class duplicates into one CompanyRecord per CIK.

    Multiple Wikipedia rows can share a CIK (GOOGL+GOOG, BRK.A+BRK.B). They
    represent ONE company — the alphabet of share classes is incidental.
    We accumulate all tickers and take the first row's name/sector/industry,
    logging a warning if any field disagrees across rows.
    """
    by_cik: dict[str, CompanyRecord] = {}
    for row in rows:
        rec = by_cik.get(row.cik)
        if rec is None:
            by_cik[row.cik] = CompanyRecord(
                cik=row.cik,
                name=row.name,
                tickers=[row.ticker],
                sector=row.gics_sector,
                industry=row.gics_sub_industry,
                aliases=[row.name],
            )
            continue
        # Duplicate CIK: accumulate ticker, keep first name/sector/industry.
        if row.ticker not in rec.tickers:
            rec.tickers.append(row.ticker)
        if row.name not in rec.aliases:
            rec.aliases.append(row.name)
        if row.gics_sector != rec.sector:
            log.warning(
                "CIK %s has conflicting sectors across rows (%s vs %s); keeping first",
                row.cik, rec.sector, row.gics_sector,
            )
        if row.gics_sub_industry != rec.industry:
            log.warning(
                "CIK %s has conflicting sub-industries across rows (%s vs %s); keeping first",
                row.cik, rec.industry, row.gics_sub_industry,
            )
    return list(by_cik.values())


# ---------------------------------------------------------------------------
# EDGAR submissions + 10-K download
# ---------------------------------------------------------------------------

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"


def latest_filings_for_cik(
    cik: str,
    *,
    form_filter: tuple[str, ...] = ("10-K",),
    years: int = 1,
) -> list[dict[str, str]]:
    """Return up to `years` most-recent filings of any form in form_filter.

    Default behaviour matches the original S&P 500 pipeline (latest 10-K).
    Pass form_filter=("20-F",) for foreign-private-issuer annual reports.

    Each item: {accession, filing_date, primary_document, filing_url, form}.
    Empty list if none found. Goes through the global rate limiter.
    """
    url = SUBMISSIONS_URL.format(cik=cik)
    resp = sec_get(url)
    if resp.status_code == 404:
        log.warning("No EDGAR submissions JSON for CIK %s (404)", cik)
        return []
    resp.raise_for_status()
    data = resp.json()

    recent = data.get("filings", {}).get("recent", {}) or {}
    accs = recent.get("accessionNumber", []) or []
    forms = recent.get("form", []) or []
    dates = recent.get("filingDate", []) or []
    primary_docs = recent.get("primaryDocument", []) or []

    results: list[dict[str, str]] = []
    for accession, form, date, primary in zip(accs, forms, dates, primary_docs):
        if form not in form_filter:
            continue
        cik_int = str(int(cik))
        accession_nodash = accession.replace("-", "")
        filing_url = (
            f"https://www.sec.gov/Archives/edgar/data/{cik_int}/"
            f"{accession_nodash}/{primary}"
        )
        results.append(
            {
                "accession": accession,
                "filing_date": date,
                "primary_document": primary,
                "filing_url": filing_url,
                "form": form,
            }
        )
        if len(results) >= years:
            break
    return results


def latest_10k_for_cik(cik: str, years: int = 1) -> list[dict[str, str]]:
    """Backwards-compatible alias: latest 10-K filing(s) for a CIK."""
    return latest_filings_for_cik(cik, form_filter=("10-K",), years=years)


def download_filing(
    cik: str,
    accession: str,
    filing_url: str,
    primary_document: str,
    cache_root: Path,
) -> tuple[Path, str]:
    """Cache-first fetch of a 10-K primary document.

    Returns (local_path, status) where status is 'cached' or 'fetched'.
    """
    cache_dir = cache_root / cik
    cache_dir.mkdir(parents=True, exist_ok=True)
    # Preserve the original file extension (.htm, .html, .txt) so downstream
    # parsers can dispatch on it.
    ext = Path(primary_document).suffix or ".txt"
    out = cache_dir / f"{accession}{ext}"
    if out.exists() and out.stat().st_size > 0:
        return out, "cached"
    resp = sec_get(filing_url)
    resp.raise_for_status()
    out.write_bytes(resp.content)
    return out, "fetched"


# ---------------------------------------------------------------------------
# Node construction + jsonl emit
# ---------------------------------------------------------------------------

def build_company_node(rec: CompanyRecord) -> Node:
    """Render a CompanyRecord into a validated Pydantic Node."""
    filings_meta: list[dict[str, Any]] = []
    if rec.accession and rec.filing_url:
        filings_meta.append(
            {
                "form": "10-K",
                "accession": rec.accession,
                "url": rec.filing_url,
                "filing_date": rec.filing_date,
                "primary_document": rec.primary_document,
                "local_path": rec.filing_local_path,
                "status": rec.filing_status,
            }
        )
    metadata: dict[str, Any] = {"filings": filings_meta}
    if not filings_meta:
        metadata["no_10k_found"] = True
    return Node(
        id=f"cik:{rec.cik}",
        type=NodeType.Company,
        name=rec.name,
        aliases=rec.aliases,
        tickers=rec.tickers,
        identifiers={"cik": rec.cik},
        sector=rec.sector,
        industry=rec.industry,
        country="US",
        metadata=metadata,
    )


def write_companies_jsonl(records: list[CompanyRecord], out_path: Path) -> int:
    """Validate each record through the Node model and write one JSON per line."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with out_path.open("w", encoding="utf-8") as fh:
        for rec in records:
            node = build_company_node(rec)
            fh.write(node.model_dump_json())
            fh.write("\n")
            n += 1
    return n


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def run(
    sector: Optional[str],
    years: int,
    data_root: Path,
) -> dict[str, Any]:
    ua = get_user_agent()  # fail loudly if missing
    sources_dir = data_root / "sources"
    filings_dir = data_root / "filings"
    companies_jsonl = data_root / "companies.jsonl"

    html_path = fetch_sp500_html(sources_dir, ua)
    html = html_path.read_text(encoding="utf-8")
    raw_rows = parse_sp500_table(html)
    log.info("Parsed %d S&P 500 rows from Wikipedia", len(raw_rows))

    if sector:
        before = len(raw_rows)
        raw_rows = [r for r in raw_rows if r.gics_sector == sector]
        log.info("Filtered to sector %r: %d -> %d rows", sector, before, len(raw_rows))

    records = dedupe_by_cik(raw_rows)
    log.info(
        "Deduped by CIK: %d rows -> %d unique companies", len(raw_rows), len(records)
    )

    fetched = 0
    cached = 0
    missing = 0
    for rec in records:
        try:
            filings = latest_10k_for_cik(rec.cik, years=years)
        except requests.HTTPError as exc:
            log.warning("EDGAR submissions error for CIK %s: %s", rec.cik, exc)
            filings = []
        if not filings:
            log.warning("No 10-K located for %s (CIK %s)", rec.name, rec.cik)
            rec.filing_status = "missing"
            missing += 1
            continue
        latest = filings[0]
        rec.accession = latest["accession"]
        rec.filing_url = latest["filing_url"]
        rec.filing_date = latest["filing_date"]
        rec.primary_document = latest["primary_document"]
        try:
            local_path, status = download_filing(
                rec.cik,
                rec.accession,
                rec.filing_url,
                rec.primary_document,
                filings_dir,
            )
        except requests.HTTPError as exc:
            log.warning("10-K fetch failed for %s (%s): %s", rec.name, rec.cik, exc)
            rec.filing_status = "missing"
            missing += 1
            continue
        rec.filing_local_path = str(local_path)
        rec.filing_status = status
        if status == "cached":
            cached += 1
        else:
            fetched += 1

    n_written = write_companies_jsonl(records, companies_jsonl)
    summary = {
        "rows_parsed": len(raw_rows),
        "companies": len(records),
        "filings_fetched": fetched,
        "filings_cached": cached,
        "filings_missing": missing,
        "companies_jsonl": str(companies_jsonl),
        "companies_written": n_written,
    }
    log.info("Run summary: %s", json.dumps(summary, indent=2))
    return summary


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="EconGraph Phase 1 ingestion")
    parser.add_argument(
        "--sector",
        default=None,
        help='GICS sector to restrict the run to (e.g. "Consumer Staples")',
    )
    parser.add_argument(
        "--years",
        type=int,
        default=1,
        help="How many 10-Ks per company (default 1: latest only).",
    )
    parser.add_argument(
        "--data-root",
        default="data",
        help="Root directory for artifacts (default: ./data).",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="Logging level (default INFO).",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    if args.years < 1:
        parser.error("--years must be >= 1")
    run(sector=args.sector, years=args.years, data_root=Path(args.data_root))
    return 0


if __name__ == "__main__":
    sys.exit(main())
