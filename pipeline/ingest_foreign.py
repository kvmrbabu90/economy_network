"""Phase A ingestion: foreign-private-issuer 20-F filings.

Companies with US ADR listings file Form 20-F with the SEC (the
international counterpart to the 10-K). The EDGAR API, throttle,
and cache layout are identical to what pipeline.ingest already uses
for the S&P 500 roster -- we just point at a different roster and
filter for a different form.

Reads:  config/foreign_filers.yaml      -- curated CIK + GICS + country
Writes: data/companies.jsonl             -- APPENDED (existing US filers stay)
        data/filings/<cik>/<accession>.htm -- same cache as 10-Ks

Why a separate module rather than extending pipeline.ingest:
  * Source-of-truth difference. S&P 500 is scraped from Wikipedia;
    foreign filers are a hand-curated YAML.
  * Country attribution. Every S&P 500 entry is country="US"; foreign
    filers carry their own ISO code (drives globe placement +
    Wikidata HQ lookup).
  * Form metadata. The node's filing record needs form="20-F" so the
    LLM extractor (which keys behavior off form) treats them
    correctly downstream.
"""
from __future__ import annotations

import logging
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterable

import requests
import yaml

from schema.models import Node, NodeType

from .ingest import (
    CompanyRecord,
    download_filing,
    latest_filings_for_cik,
)
from .sec import get_user_agent

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
ROSTER_PATH = REPO_ROOT / "config" / "foreign_filers.yaml"


def load_roster(path: Path = ROSTER_PATH) -> list[dict[str, Any]]:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or []


def _build_foreign_node(rec: CompanyRecord, *, form: str = "20-F") -> Node:
    """Same as ingest.build_company_node but parametrised on country +
    form. Marks `filings[0].form = "20-F"` so the section extractor /
    LLM step can dispatch on it later."""
    filings_meta: list[dict[str, Any]] = []
    if rec.accession and rec.filing_url:
        filings_meta.append(
            {
                "form": form,
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
        metadata["no_filing_found"] = True
    return Node(
        id=f"cik:{rec.cik}",
        type=NodeType.Company,
        name=rec.name,
        aliases=rec.aliases,
        tickers=rec.tickers,
        identifiers={"cik": rec.cik},
        sector=rec.sector,
        industry=rec.industry,
        country=rec.country_code or "US",
        metadata=metadata,
    )


def _merge_into_companies_jsonl(
    existing_path: Path,
    new_nodes: list[Node],
) -> int:
    """Append new foreign nodes to data/companies.jsonl, deduping by CIK.

    If an existing row has the same CIK we keep the EXISTING (S&P 500
    entry wins) -- this is unlikely to happen but is a safety net so
    re-running ingest_foreign over a corpus that includes US-domiciled
    companies doesn't overwrite their 10-K provenance.
    """
    existing_lines: list[str] = []
    existing_ciks: set[str] = set()
    if existing_path.exists():
        for line in existing_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            existing_lines.append(line)
            try:
                import json as _json
                obj = _json.loads(line)
                if "id" in obj and isinstance(obj["id"], str) and obj["id"].startswith("cik:"):
                    existing_ciks.add(obj["id"].split(":", 1)[1])
            except Exception:
                continue
    added = 0
    skipped = 0
    with existing_path.open("w", encoding="utf-8") as fh:
        for line in existing_lines:
            fh.write(line)
            fh.write("\n")
        for node in new_nodes:
            cik = node.id.split(":", 1)[1]
            if cik in existing_ciks:
                log.info("skip (already in companies.jsonl): %s %s", cik, node.name)
                skipped += 1
                continue
            fh.write(node.model_dump_json())
            fh.write("\n")
            added += 1
            existing_ciks.add(cik)
    log.info("companies.jsonl: %d existing + %d added (%d skipped as dup)",
             len(existing_lines), added, skipped)
    return added


def _company_records_from_roster(roster: Iterable[dict[str, Any]]) -> list[CompanyRecord]:
    out: list[CompanyRecord] = []
    for row in roster:
        cik = str(row["cik"]).strip()
        if len(cik) < 10:
            cik = cik.zfill(10)
        # Map sub_industry -> industry via the same yaml the S&P 500 pipeline uses.
        # We avoid re-importing the loader to keep this module light; the resolve
        # stage normalises industry from sub_industry too if missing.
        rec = CompanyRecord(
            cik=cik,
            name=str(row["name"]).strip(),
            tickers=[str(row["ticker"]).strip()] if row.get("ticker") else [],
            aliases=[str(row["name"]).strip()],
            sector=row.get("gics_sector"),
            industry=None,  # filled later by sub_industry -> industry map
            sub_industry=row.get("gics_sub_industry"),
        )
        # Attach country via attribute so _build_foreign_node can read it.
        rec.country_code = str(row.get("country_code") or "US").strip().upper()  # type: ignore[attr-defined]
        out.append(rec)
    return out


def run(
    *,
    data_root: Path,
    years: int = 1,
    roster_path: Path = ROSTER_PATH,
    form_filter: tuple[str, ...] = ("20-F",),
) -> dict[str, Any]:
    get_user_agent()  # fail loudly if EDGAR_USER_AGENT missing

    # Match the S&P 500 ingest convention: the `industry` field on a
    # company node actually carries the *sub-industry* string. The
    # downstream rule pass (pipeline.regulators.extract_rule_edges)
    # reads node["industry"] and looks it up in
    # config/gics_subindustry_to_industry.yaml. So we copy
    # sub_industry into the node's industry slot, NOT the broader
    # industry-group name. (Confirmed by reading pipeline.ingest.py
    # line ~182: `industry=row.gics_sub_industry`.)
    roster = load_roster(roster_path)
    records = _company_records_from_roster(roster)
    for r in records:
        if r.sub_industry:
            r.industry = r.sub_industry

    filings_dir = data_root / "filings"
    fetched = cached = missing = 0
    for rec in records:
        try:
            filings = latest_filings_for_cik(
                rec.cik, form_filter=form_filter, years=years
            )
        except requests.HTTPError as exc:
            log.warning("EDGAR submissions error for CIK %s (%s): %s",
                        rec.cik, rec.name, exc)
            filings = []
        if not filings:
            log.warning("No %s located for %s (CIK %s)",
                        "/".join(form_filter), rec.name, rec.cik)
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
            log.warning("%s fetch failed for %s (%s): %s",
                        latest.get("form", "filing"), rec.name, rec.cik, exc)
            rec.filing_status = "missing"
            missing += 1
            continue
        rec.filing_local_path = str(local_path)
        rec.filing_status = status
        if status == "cached":
            cached += 1
        else:
            fetched += 1

    nodes = [_build_foreign_node(r, form=form_filter[0]) for r in records if r.accession]
    added = _merge_into_companies_jsonl(data_root / "companies.jsonl", nodes)

    summary = {
        "roster_size": len(records),
        "filings_fetched": fetched,
        "filings_cached": cached,
        "filings_missing": missing,
        "companies_added": added,
        "form_filter": list(form_filter),
    }
    log.info("Foreign ingest summary: %s", summary)
    return summary


def main() -> None:
    import argparse, json

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    p = argparse.ArgumentParser()
    p.add_argument("--data-root", default=str(REPO_ROOT / "data"))
    p.add_argument("--years", type=int, default=1)
    p.add_argument("--roster", default=str(ROSTER_PATH))
    args = p.parse_args()
    summary = run(
        data_root=Path(args.data_root),
        years=args.years,
        roster_path=Path(args.roster),
    )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
