"""Phase 2 orchestrator: cached 10-K -> data/edges_raw.jsonl (CandidateEdges).

Two gated parts:

    Part A (deterministic, always runs)
      * Load every company's cached 10-K, normalize to .txt, and locate the
        competition + customer-concentration passages (pipeline/sections.py).
      * Resolve each company's GICS Sub-Industry -> GICS Industry via
        config/gics_subindustry_to_industry.yaml (fixes the level mismatch
        between Phase 1's storage and config/regulators.yaml's keys).
      * Generate rule-extracted `regulated_by` CandidateEdges + Regulator
        Nodes (pipeline/regulators.py).

    Part B (LLM extraction; gated -- skipped unless --run-llm is passed)
      * pipeline/extractor.py extracts `supplies` / `competes_with` candidates
        from each filing's section text using `claude -p` headless (or a
        Gemma fallback). The grounding verify gate then drops any candidate
        whose snippet is fabricated.

Outputs:
    data/regulator_nodes.jsonl        -- validated Regulator Nodes
    data/extract_sections.jsonl       -- per-filing section-extraction report
    data/edges_raw.jsonl              -- all CandidateEdges (rule + LLM)
    data/extract_checkpoint.json      -- (Part B) completed (cik, section) pairs
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any, Optional

from pipeline.regulators import (
    RuleExtractResult,
    extract_rule_edges,
    load_regulators_config,
    load_subindustry_map,
    write_regulator_nodes_jsonl,
)
from pipeline.sections import SectionResult, extract_filing_sections


log = logging.getLogger("extract")


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------

def load_companies(jsonl_path: Path) -> list[dict[str, Any]]:
    with jsonl_path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def resolve_filing_path(local_path: str, repo_root: Path) -> Path:
    """`local_path` is written relative to the repo root by Phase 1."""
    p = Path(local_path)
    return p if p.is_absolute() else (repo_root / p)


def write_sections_report(results: list[SectionResult], out_path: Path) -> int:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with out_path.open("w", encoding="utf-8") as fh:
        for r in results:
            row = asdict(r)
            # Don't store the section text in the report -- the .txt cache is
            # the source of truth and this report is meant to be skim-readable.
            row["sections"] = {k: len(v) for k, v in row["sections"].items()}
            fh.write(json.dumps(row))
            fh.write("\n")
            n += 1
    return n


# ---------------------------------------------------------------------------
# Part A
# ---------------------------------------------------------------------------

def _section_pass(
    companies: list[dict[str, Any]], *, repo_root: Path,
) -> list[SectionResult]:
    """Section extraction over a (possibly batch-scoped) company subset."""
    section_results: list[SectionResult] = []
    for node in companies:
        for f in (node.get("metadata") or {}).get("filings", []) or []:
            lp = f.get("local_path")
            if not lp:
                continue
            htm = resolve_filing_path(lp, repo_root)
            if not htm.exists():
                log.warning("Filing missing on disk: %s", htm)
                continue
            try:
                section_results.append(extract_filing_sections(htm))
            except Exception as exc:
                log.warning("Section extraction failed for %s: %s", htm, exc)
    comp_found = sum(1 for r in section_results if r.found.get("competition"))
    cust_found = sum(1 for r in section_results if r.found.get("customers"))
    log.info(
        "Section extraction: %d filings, competition=%d, customers=%d",
        len(section_results), comp_found, cust_found,
    )
    return section_results


def _rule_pass(
    companies: list[dict[str, Any]], *, config_root: Path,
) -> RuleExtractResult:
    """Rule (regulated_by) extraction. Should always run over the FULL registry
    in Phase 7 so sector-scoped LLM batches don't erase prior rule edges."""
    sub_to_industry = load_subindustry_map(
        config_root / "gics_subindustry_to_industry.yaml"
    )
    cfg = load_regulators_config(config_root / "regulators.yaml")
    rule = extract_rule_edges(companies, cfg, sub_to_industry)
    log.info(
        "Rule extraction: %d regulator nodes, %d regulated_by candidates over %d companies",
        len(rule.regulator_nodes), len(rule.candidates), len(rule.per_company),
    )
    return rule


def run_part_a(
    companies: list[dict[str, Any]],
    *,
    repo_root: Path,
    data_root: Path,
    config_root: Path,
) -> tuple[RuleExtractResult, list[SectionResult]]:
    """Legacy combined entrypoint; kept for direct callers + tests.

    Equivalent to ``_rule_pass(companies) + _section_pass(companies)``. The
    Phase 7 orchestrator calls the split helpers directly so it can run rule
    over the full registry while scoping sections to the current batch.
    """
    section_results = _section_pass(companies, repo_root=repo_root)
    rule = _rule_pass(companies, config_root=config_root)
    return rule, section_results


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------

def write_edges_raw(
    rule: RuleExtractResult,
    llm_candidates: list,
    out_path: Path,
    *,
    inferred_candidates: Optional[list] = None,
) -> int:
    """Truncate-and-write edges_raw.jsonl: rule + LLM + inference candidates."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with out_path.open("w", encoding="utf-8") as fh:
        for ce in rule.candidates:
            fh.write(ce.model_dump_json())
            fh.write("\n")
            n += 1
        for ce in llm_candidates:
            fh.write(ce.model_dump_json())
            fh.write("\n")
            n += 1
        for ce in (inferred_candidates or []):
            fh.write(ce.model_dump_json())
            fh.write("\n")
            n += 1
    return n


def _load_inferred(path: Path) -> list:
    if not path.exists():
        return []
    from schema.models import CandidateEdge
    rows = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(CandidateEdge.model_validate_json(line))
            except Exception as exc:
                log.warning("Skipping malformed inferred row: %s", exc)
    return rows


def summary_dict(
    *,
    sector: Optional[str],
    n_companies: int,
    section_results: list[SectionResult],
    rule: RuleExtractResult,
    llm_candidates_by_type: dict[str, int],
    llm_accepted: int,
    llm_rejected: int,
    edges_raw_path: Path,
    edges_written: int,
) -> dict[str, Any]:
    comp_found = sum(1 for r in section_results if r.found.get("competition"))
    cust_found = sum(1 for r in section_results if r.found.get("customers"))
    by_type = {"regulated_by": len(rule.candidates), **llm_candidates_by_type}
    return {
        "sector": sector,
        "companies": n_companies,
        "filings_with_competition_section": comp_found,
        "filings_with_customers_section": cust_found,
        "rule_candidates": len(rule.candidates),
        "regulator_nodes_minted": len(rule.regulator_nodes),
        "llm_candidates_accepted": llm_accepted,
        "llm_candidates_rejected": llm_rejected,
        "candidates_by_type": by_type,
        "edges_raw_path": str(edges_raw_path),
        "edges_raw_count": edges_written,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def run(
    *,
    sector: Optional[str],
    run_llm: bool,
    limit: Optional[int],
    data_root: Path,
    config_root: Path,
    repo_root: Path,
) -> dict[str, Any]:
    all_companies = load_companies(data_root / "companies.jsonl")

    # Phase 7: rule (regulated_by) extraction MUST cover the full registry,
    # not just this batch's sector. Otherwise a sector-scoped batch erases
    # prior batches' rule edges + regulator nodes.
    log.info("Rule pass covers full registry: %d companies", len(all_companies))
    rule = _rule_pass(all_companies, config_root=config_root)

    # Sections + LLM work are scoped to this batch.
    companies = all_companies
    if sector:
        before = len(companies)
        companies = [c for c in companies if c.get("sector") == sector]
        log.info("Sector filter %r: %d -> %d companies", sector, before, len(companies))
    if limit is not None:
        companies = companies[:limit]
        log.info("--limit applied: %d companies", len(companies))
    section_results = _section_pass(companies, repo_root=repo_root)

    # Stash the section-extraction report and the regulator nodes for Phase 3.
    sections_out = data_root / "extract_sections.jsonl"
    n_sections = write_sections_report(section_results, sections_out)
    log.info("Wrote %d section reports -> %s", n_sections, sections_out)

    regs_out = data_root / "regulator_nodes.jsonl"
    n_regs = write_regulator_nodes_jsonl(rule.regulator_nodes, regs_out)
    log.info("Wrote %d regulator nodes -> %s", n_regs, regs_out)

    llm_candidates: list = []
    llm_candidates_by_type: dict[str, int] = {}
    llm_accepted = llm_rejected = 0
    if run_llm:
        # Part B is wired in `pipeline.extractor`. The orchestrator stays out
        # of the LLM details -- it just receives the verified candidates back.
        from pipeline.extractor import run_llm_extraction  # local import: optional
        llm_result = run_llm_extraction(
            companies=companies,
            section_results=section_results,
            data_root=data_root,
            repo_root=repo_root,
        )
        llm_candidates = llm_result.candidates
        llm_candidates_by_type = llm_result.counts_by_type
        llm_accepted = llm_result.accepted
        llm_rejected = llm_result.rejected
        log.info(
            "LLM extraction: accepted=%d rejected=%d by_type=%s",
            llm_accepted, llm_rejected, llm_candidates_by_type,
        )
    else:
        # When we skip re-running the LLM, the side file from previous runs
        # is still the source of truth -- load it back so reassembly doesn't
        # silently lose every LLM-extracted edge (which was the original
        # cause of "Nike has no competitors" in the rebuilt graph).
        llm_side_path = data_root / "_extract" / "llm_candidates.jsonl"
        if llm_side_path.exists():
            cached_llm = _load_inferred(llm_side_path)
            llm_candidates = cached_llm
            llm_candidates_by_type = {}
            for c in cached_llm:
                t = (c.type.value if hasattr(c.type, "value") else c.type) or "unknown"
                llm_candidates_by_type[t] = llm_candidates_by_type.get(t, 0) + 1
            log.info(
                "Skipping Part B (LLM extraction); loaded %d cached LLM candidates from %s",
                len(cached_llm), llm_side_path,
            )
        else:
            log.info("Skipping Part B (LLM extraction); pass --run-llm to enable")

    # Tier-2 derivations: co-mention closure over LLM candidates. Cheap,
    # deterministic, no model calls. Run after every batch so the inferred
    # candidates stay in sync with the LLM side file.
    from pipeline.inference import run_inference
    inference_out = data_root / "_extract" / "inferred_candidates.jsonl"
    inf_summary = run_inference(
        llm_side_path=data_root / "_extract" / "llm_candidates.jsonl",
        out_path=inference_out,
    )
    inferred = _load_inferred(inference_out)
    log.info(
        "Inference: %d co-mention groups -> %d candidate edges loaded",
        inf_summary.get("groups", 0), len(inferred),
    )

    # Tier-3: Wikidata enrichment candidates (competitor + supplier edges
    # pulled from Wikidata's P:1830 / P:1071). Loaded if the file exists;
    # produced by `python -m pipeline.wikidata`.
    wikidata_path = data_root / "_extract" / "wikidata_candidates.jsonl"
    wikidata_cands = _load_inferred(wikidata_path) if wikidata_path.exists() else []
    if wikidata_cands:
        log.info("Wikidata: %d candidate edges loaded", len(wikidata_cands))

    # Tier-C: 8-K contract-event candidates (supplies/customer_of edges
    # extracted from 8-K Item 8.01 filings via claude -p). Loaded if the
    # file exists; produced by `python -m pipeline.sec_8k --sector X`.
    sec8k_path = data_root / "_extract" / "sec_8k_candidates.jsonl"
    sec8k_cands = _load_inferred(sec8k_path) if sec8k_path.exists() else []
    if sec8k_cands:
        log.info("8-K: %d candidate edges loaded", len(sec8k_cands))

    edges_raw = data_root / "edges_raw.jsonl"
    n_edges = write_edges_raw(
        rule,
        llm_candidates + wikidata_cands + sec8k_cands,
        edges_raw,
        inferred_candidates=inferred,
    )
    log.info("Wrote %d candidate edges -> %s", n_edges, edges_raw)

    summary = summary_dict(
        sector=sector,
        n_companies=len(companies),
        section_results=section_results,
        rule=rule,
        llm_candidates_by_type=llm_candidates_by_type,
        llm_accepted=llm_accepted,
        llm_rejected=llm_rejected,
        edges_raw_path=edges_raw,
        edges_written=n_edges,
    )
    log.info("Run summary:\n%s", json.dumps(summary, indent=2))
    return summary


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="EconGraph Phase 2 extraction")
    parser.add_argument("--sector", default=None,
                        help='GICS sector to restrict the run (e.g. "Consumer Staples")')
    parser.add_argument("--run-llm", action="store_true",
                        help="Run Part B (LLM extraction). Off by default; Part A always runs.")
    parser.add_argument("--limit", type=int, default=None,
                        help="Process only the first N companies (smoke-test).")
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--config-root", default="config")
    parser.add_argument("--repo-root", default=".",
                        help="Repo root used to resolve filings' local paths.")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    run(
        sector=args.sector,
        run_llm=args.run_llm,
        limit=args.limit,
        data_root=Path(args.data_root),
        config_root=Path(args.config_root),
        repo_root=Path(args.repo_root),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
