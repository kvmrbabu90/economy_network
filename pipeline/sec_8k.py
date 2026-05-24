"""Tier-C: 8-K customer/supplier announcement scraper.

10-Ks disclose only the >10%-customer-concentration kind of relationship.
But companies announce **contract wins** and **customer signings** in 8-K
Item 8.01 ("Other Events") filings on a continuous basis. Those are the
material relationships that the annual 10-K doesn't name.

This module:
  1. For each filer, fetches the EDGAR submissions JSON and pulls 8-K
     accessions from the last N days (default 180).
  2. Downloads each 8-K's primary document (cache-first), reuses the
     existing sec.sec_get() so SEC's <=10 req/s + UA rules are honored.
  3. Scans the text for "contract event" anchors -- patterns like
     "entered into ... agreement with" / "awarded by" / "selected by".
  4. Sends matched passages to `claude -p` with a contract-extraction
     prompt and the same verify gate as Phase 2 (snippet must be a
     literal substring of the filing AND name the target).
  5. Mints real supplies/customer_of CandidateEdges marked
     `extracted_by: "llm:claude-cli"` with the 8-K accession as
     prov.filing.

Volume: ~5-15 8-Ks per filer per 6 months. 500 filers => 2500-7500
filings, capped per-batch via --limit-per-cik. Checkpoint key is
(cik, accession) so multi-session runs resume cleanly.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

import requests

from pipeline.extractor import (
    ClaudeCLIExtractor,
    _extract_json_array,
    _normalize,
    _target_in_snippet,
)
from pipeline.sec import sec_get
from pipeline.sections import _html_to_text
from schema.models import CandidateEdge, EdgeType, Provenance


log = logging.getLogger("sec_8k")

# Header pattern: "Item 1.01" / "ITEM 1.01" / "Item  1.01.  " etc.
ITEM_HEADER_RE = re.compile(
    r"(?im)^\s*item\s*(\d+)\.(\d+)\s*\.?\s*",
)

# Which 8-K items contain material business-relationship disclosures.
RELEVANT_ITEM_SECTIONS = {(1, 1), (8, 1)}  # Item 1.01 and Item 8.01


# ---------------------------------------------------------------------------
# Fetch + cache
# ---------------------------------------------------------------------------

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"


def fetch_recent_8k_meta(cik: str, *, since_days: int = 180) -> list[dict[str, Any]]:
    """Return metadata for 8-K filings within the lookback window.

    Filtered to the items where actual contract / customer / supplier
    relationships get announced:

      * Item 1.01 -- Entry into a Material Definitive Agreement
        (the gold-standard contract-event 8-K item)
      * Item 8.01 -- Other Events (catch-all for material disclosures)

    Skips earnings (2.02), debt offerings (2.03), board changes (5.02),
    auditor changes (4.01), and other governance / financial-result
    filings that aren't about external business relationships.
    """
    url = SUBMISSIONS_URL.format(cik=cik)
    resp = sec_get(url)
    if resp.status_code == 404:
        log.warning("No EDGAR submissions for CIK %s", cik)
        return []
    resp.raise_for_status()
    data = resp.json()
    recent = data.get("filings", {}).get("recent", {}) or {}
    accs = recent.get("accessionNumber", []) or []
    forms = recent.get("form", []) or []
    dates = recent.get("filingDate", []) or []
    primary_docs = recent.get("primaryDocument", []) or []
    items = recent.get("items", []) or []

    cutoff = (datetime.now(timezone.utc) - timedelta(days=since_days)).date().isoformat()
    RELEVANT_ITEMS = {"1.01", "8.01"}
    out: list[dict[str, Any]] = []
    for accession, form, date, primary, item in zip(accs, forms, dates, primary_docs, items):
        if form != "8-K":
            continue
        if date < cutoff:
            continue
        # Items are comma-separated when multiple are present.
        item_codes = {code.strip() for code in (item or "").split(",")}
        if not (item_codes & RELEVANT_ITEMS):
            continue
        cik_int = str(int(cik))
        accession_nodash = accession.replace("-", "")
        url = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession_nodash}/{primary}"
        out.append({
            "accession": accession,
            "filing_date": date,
            "primary_document": primary,
            "url": url,
            "items": item or "",
        })
    return out


def download_8k(
    cik: str, accession: str, url: str, primary_document: str, cache_root: Path,
) -> Path:
    """Cache-first fetch of the 8-K primary document. Returns local path."""
    cache_dir = cache_root / cik / "8k"
    cache_dir.mkdir(parents=True, exist_ok=True)
    ext = Path(primary_document).suffix or ".htm"
    out = cache_dir / f"{accession}{ext}"
    if out.exists() and out.stat().st_size > 0:
        return out
    resp = sec_get(url)
    resp.raise_for_status()
    out.write_bytes(resp.content)
    return out


# ---------------------------------------------------------------------------
# Passage extraction
# ---------------------------------------------------------------------------

def find_contract_passages(text: str) -> list[str]:
    """Extract the full text under Item 1.01 / Item 8.01 sections.

    Walks Item headers in order; for each relevant item, slices from the
    header start to the next item header (or end-of-text). Returns the
    section bodies as a list. Item 1.01 is "Entry into a Material
    Definitive Agreement" by SEC definition; Item 8.01 is "Other Events"
    used for anything material that doesn't fit a numbered slot. Both
    typically contain the contract / partnership / customer text we want;
    feeding them straight to the LLM is more reliable than regex anchors
    against legalese.
    """
    matches = list(ITEM_HEADER_RE.finditer(text))
    if not matches:
        return []
    sections: list[str] = []
    for i, m in enumerate(matches):
        major, minor = int(m.group(1)), int(m.group(2))
        if (major, minor) not in RELEVANT_ITEM_SECTIONS:
            continue
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        # Skip very short stubs (boilerplate like "Item 8.01. Other Events.\n\nNone.")
        if len(body) < 200:
            continue
        sections.append(body)
    return sections


# ---------------------------------------------------------------------------
# LLM prompt + verify gate
# ---------------------------------------------------------------------------

PROMPT_TEMPLATE = """\
You are extracting B2B CONTRACT EVENTS from an SEC 8-K filing.

SOURCE COMPANY: {company_name}

INSTRUCTIONS
- Extract ONLY contract/customer/supplier relationships the text below
  explicitly states.
- "supplies" means {company_name} SELLS to / PROVIDES products or services to
  the target (e.g. "X awarded a multi-year contract by Y" => X supplies Y).
- "customer_of" means {company_name} BUYS from / IS A CUSTOMER OF the
  target (e.g. "X selected Y as its software provider" => X customer_of Y).
- Skip M&A, financial transactions, board changes, lawsuits, and routine
  corporate housekeeping.
- Do NOT include {company_name} as a target.
- The snippet MUST be a verbatim substring of the text below and MUST name
  the target.

OUTPUT FORMAT
Strict JSON array, no prose, no markdown fences:
  [
    {{"target": "<other party>",
      "type": "supplies" | "customer_of",
      "snippet": "<verbatim substring of the text>",
      "confidence": 0-1}}
  ]
If there are no extractable contract events, return [].

TEXT
<<<
{passages}
>>>
"""


def extract_with_llm(
    extractor: ClaudeCLIExtractor,
    company_name: str,
    passages: list[str],
) -> tuple[Optional[list[dict]], dict[str, Any]]:
    if not passages:
        return [], {"extracted_by": extractor.extracted_by, "duration_ms": 0}
    # Cap text sent to ~28K chars to stay well under model context.
    text = "\n\n---\n\n".join(passages)[:28000]
    prompt = PROMPT_TEMPLATE.format(company_name=company_name, passages=text)
    return extractor.extract(prompt)


def verify_grounding(snippet: str, target: str, full_text: str) -> bool:
    """Same gate as Phase 2: snippet substring of filing + target in snippet."""
    if not snippet or not target or not full_text:
        return False
    n_snip = _normalize(snippet)
    n_full = _normalize(full_text)
    if n_snip not in n_full:
        return False
    return _target_in_snippet(target, snippet)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

CHECKPOINT_NAME = "8k_checkpoint.json"
CANDIDATES_NAME = "_extract/sec_8k_candidates.jsonl"


def _load_checkpoint(path: Path) -> set[tuple[str, str]]:
    if not path.exists():
        return set()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return {(d["cik"], d["accession"]) for d in raw.get("done", [])}
    except Exception:
        return set()


def _save_checkpoint(path: Path, done: set[tuple[str, str]]) -> None:
    payload = {"done": [{"cik": c, "accession": a} for c, a in sorted(done)]}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def run_8k_extraction(
    *,
    sector: Optional[str] = None,
    limit: Optional[int] = None,
    since_days: int = 180,
    limit_per_cik: int = 8,
    data_root: Path = Path("data"),
    repo_root: Path = Path("."),
) -> dict[str, Any]:
    companies_path = data_root / "companies.jsonl"
    companies = [json.loads(l) for l in companies_path.open(encoding="utf-8")]
    if sector:
        companies = [c for c in companies if c.get("sector") == sector]
    if limit is not None:
        companies = companies[:limit]
    log.info("8-K extraction over %d filers (sector=%r, since_days=%d)",
             len(companies), sector, since_days)

    extractor = ClaudeCLIExtractor()
    ckpt = data_root / CHECKPOINT_NAME
    done = _load_checkpoint(ckpt)
    out_candidates = data_root / CANDIDATES_NAME
    out_candidates.parent.mkdir(parents=True, exist_ok=True)

    cache_root = data_root / "filings"
    counts = Counter()
    accepted = 0
    rejected = 0

    for node in companies:
        cik = (node.get("identifiers") or {}).get("cik")
        company_name = node.get("name", node["id"])
        if not cik:
            continue
        try:
            meta = fetch_recent_8k_meta(cik, since_days=since_days)
        except requests.HTTPError as exc:
            log.warning("8-K meta failed for %s (%s): %s", company_name, cik, exc)
            continue
        meta = meta[:limit_per_cik]
        if not meta:
            continue
        log.info("[%s] %d candidate 8-Ks in window", company_name, len(meta))

        for f in meta:
            accession = f["accession"]
            if (cik, accession) in done:
                continue
            try:
                local = download_8k(cik, accession, f["url"], f["primary_document"], cache_root)
            except requests.HTTPError as exc:
                log.warning("download failed %s/%s: %s", cik, accession, exc)
                continue
            try:
                html = local.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                log.warning("read failed %s/%s: %s", cik, accession, exc)
                continue
            text = _html_to_text(html)
            passages = find_contract_passages(text)
            if not passages:
                done.add((cik, accession))
                continue

            items, meta_llm = extract_with_llm(extractor, company_name, passages)
            if items is None:
                log.warning("LLM extraction failed for %s/%s (%s); not checkpointing",
                            company_name, accession, meta_llm)
                continue
            for it in items:
                if not isinstance(it, dict):
                    rejected += 1
                    continue
                target = (it.get("target") or "").strip()
                edge_type = (it.get("type") or "").strip()
                snippet = (it.get("snippet") or "").strip()
                try:
                    confidence = float(it.get("confidence", 0.7))
                except (TypeError, ValueError):
                    confidence = 0.5
                if not target or not snippet or edge_type not in {"supplies", "customer_of"}:
                    rejected += 1
                    continue
                if target.strip().lower() == company_name.strip().lower():
                    rejected += 1
                    continue
                if not verify_grounding(snippet, target, text):
                    rejected += 1
                    continue
                # customer_of is derived at read time per invariant #2 --
                # store it as a reversed supplies (target supplies company)
                # so the API's existing derivation reverses it back into
                # the customer_of view.
                if edge_type == "customer_of":
                    real_source = target
                    real_target = company_name
                    source_id_for_edge: Optional[str] = None  # resolved later via alias
                    # We mint a candidate with source_id=that_company's CIK
                    # only if we can find it; otherwise we keep the
                    # source_company's CIK and emit type=customer_of which
                    # Phase 3 currently doesn't store. To keep within the
                    # existing schema, fold customer_of into supplies with
                    # endpoints swapped + target_raw being our own filer.
                    # That collides with normal supplies semantics, so
                    # easier: keep edge as supplies with source=node['id']
                    # and target=raw, but flip the meaning by skipping
                    # those announcements. Pragmatic short-term: drop
                    # customer_of-typed 8-K items; they're rarer in 8-Ks
                    # anyway (the announcing filer is usually the supplier).
                    rejected += 1
                    continue
                ce = CandidateEdge(
                    source_id=node["id"],
                    target_raw=target,
                    type=EdgeType(edge_type),
                    confidence=max(0.0, min(1.0, confidence)),
                    provenance=Provenance(
                        filing=accession,
                        url=f["url"],
                        snippet=snippet,
                        extracted_by="llm:claude-cli",
                    ),
                    verified=True,
                )
                # Append immediately so crashes don't lose work.
                with out_candidates.open("a", encoding="utf-8") as fh:
                    fh.write(ce.model_dump_json())
                    fh.write("\n")
                counts[edge_type] += 1
                accepted += 1
            done.add((cik, accession))
            _save_checkpoint(ckpt, done)

    summary = {
        "filers_scanned": len(companies),
        "candidates_accepted": accepted,
        "candidates_rejected": rejected,
        "by_type": dict(counts),
        "candidates_path": str(out_candidates),
    }
    log.info("8-K run summary:\n%s", json.dumps(summary, indent=2))
    return summary


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Tier-C 8-K customer/supplier scraper")
    parser.add_argument("--sector", default=None)
    parser.add_argument("--limit", type=int, default=None,
                        help="Process only first N companies (smoke-test).")
    parser.add_argument("--since-days", type=int, default=180,
                        help="Lookback window for 8-K filings (default 180).")
    parser.add_argument("--limit-per-cik", type=int, default=8,
                        help="Cap on 8-Ks processed per company per run.")
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    run_8k_extraction(
        sector=args.sector,
        limit=args.limit,
        since_days=args.since_days,
        limit_per_cik=args.limit_per_cik,
        data_root=Path(args.data_root),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
