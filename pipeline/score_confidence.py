"""Phase K-1: Score source quality for every edge in edges.jsonl.

Two jobs in one pass:
  1. Assign `source_tier` and update `confidence` for every edge based on
     extraction provenance — no LLM calls needed.
  2. Extract explicit financial weights from snippets that already contain
     a named-entity + percentage (e.g. "Walmart accounted for 21% of net sales").

Run after resolve.py (which produces edges.jsonl) and before build_graph.py.
Reads + rewrites data/edges.jsonl and data/edges_below_threshold.jsonl in-place.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from pathlib import Path

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).parent.parent
DATA_DIR = REPO_ROOT / "data"

# ---------------------------------------------------------------------------
# Source tier + confidence rules
# ---------------------------------------------------------------------------

# After extract_weights.py runs, edges with explicit % will be upgraded to
# sec_explicit / 0.90.  This table covers everything else.
_TIER_RULES: list[tuple[callable, str, float]] = [
    # (predicate, source_tier, confidence)
    (lambda e: e["provenance"].get("extracted_by") == "manual:curation",
     "manual", 0.85),
    (lambda e: e["provenance"].get("extracted_by") == "rule",
     "sec_inferred", 0.75),
    (lambda e: e["provenance"].get("extracted_by") == "wikidata",
     "wikidata", 0.50),
    (lambda e: e["provenance"].get("extracted_by", "").startswith("llm")
        and _is_sec_filing(e["provenance"].get("filing", "")),
     "sec_inferred", 0.65),
    (lambda e: e["provenance"].get("extracted_by", "").startswith("llm")
        and _is_wikipedia(e["provenance"].get("filing", ""),
                          e["provenance"].get("url", "")),
     "wikipedia", 0.40),
    (lambda e: e["provenance"].get("extracted_by") in ("inference:co-mention",
                                                        "inference:gics-peer"),
     "sec_inferred", 0.60),
]

_DEFAULT_TIER = ("sec_inferred", 0.60)  # fallback


def _is_sec_filing(filing: str) -> bool:
    """True when the filing string looks like an SEC accession number or CIK path."""
    if not filing:
        return False
    # Accession number: NNNNNNNNNN-YY-NNNNNN
    if re.match(r"^\d{10}-\d{2}-\d{6}$", filing.strip()):
        return True
    # Could also be "cik:NNNN/..." style used by foreign filers
    return False


def _is_wikipedia(filing: str, url: str) -> bool:
    """True when the source is Wikipedia or a Wikidata page."""
    combined = (filing + " " + url).lower()
    return "wikipedia" in combined or "wikidata" in combined


# ---------------------------------------------------------------------------
# Weight extraction from existing snippets
# ---------------------------------------------------------------------------

# Patterns that indicate an explicit named-entity → % relationship.
# We require the snippet to contain a % number AND a recognisable attribution
# phrase.  The edge already connects the two companies, so we don't need to
# re-extract the entity name — just the number.
_ATTRIBUTION_RE = re.compile(
    r"""(?:accounted\s+for|represented|comprised|were\s+approximately|
         was\s+approximately|were\s+about|account\s+for)\s+
        approximately\s*
        (\d+(?:\.\d+)?)\s*%""",
    re.IGNORECASE | re.VERBOSE,
)

# Simpler fallback: "X% of (our/consolidated/total) net (sales|revenue)"
_SIMPLE_PCT_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*%\s+of\s+(?:our\s+|consolidated\s+|total\s+)*"
    r"(?:net\s+)?(?:sales|revenue|net\s+sales)",
    re.IGNORECASE,
)


def _extract_pct_from_snippet(snippet: str) -> float | None:
    """Return the first meaningful percentage (≥5%) found in a snippet, or None."""
    for pattern in (_ATTRIBUTION_RE, _SIMPLE_PCT_RE):
        m = pattern.search(snippet)
        if m:
            pct = float(m.group(1))
            if 5.0 <= pct <= 99.0:   # sanity: ignore 100% / dust-level <5%
                return pct
    return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _assign_tier(edge: dict) -> tuple[str, float]:
    """Return (source_tier, confidence) for one edge."""
    for predicate, tier, conf in _TIER_RULES:
        try:
            if predicate(edge):
                return tier, conf
        except Exception:
            pass
    return _DEFAULT_TIER


def _enrich_edge(edge: dict) -> dict:
    """Return an enriched copy of the edge dict with source_tier, confidence,
    and (if extractable) weight set."""
    e = dict(edge)
    tier, conf = _assign_tier(e)

    # Only update confidence if current value is not higher (preserve extraction
    # confidence when the extractor was already more discriminating).
    current_conf = float(e.get("confidence") or 0.0)
    e["confidence"] = max(current_conf, conf)
    e["source_tier"] = e.get("source_tier") or tier  # don't overwrite existing

    # Weight extraction from snippet (supplies edges only)
    if e.get("type") == "supplies" and e.get("weight") is None:
        snippet = (e.get("provenance") or {}).get("snippet") or ""
        pct = _extract_pct_from_snippet(snippet)
        if pct is not None:
            e["weight"] = round(pct / 100.0, 4)
            e["source_tier"] = "sec_explicit"
            e["confidence"] = 0.90
            log.info("Extracted weight %.2f from snippet for edge %s -> %s",
                     pct / 100.0, e.get("source"), e.get("target"))

    return e


def _process_file(path: Path) -> int:
    """Enrich all edges in a JSONL file. Returns count of enriched edges."""
    if not path.exists():
        return 0
    lines = path.read_text(encoding="utf-8").splitlines()
    enriched: list[str] = []
    count = 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            edge = json.loads(line)
            enriched.append(json.dumps(_enrich_edge(edge), ensure_ascii=False))
            count += 1
        except Exception as exc:
            log.warning("Skipping malformed edge line: %s", exc)
            enriched.append(line)  # keep original on error
    path.write_text("\n".join(enriched) + "\n", encoding="utf-8")
    return count


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(message)s",
                        stream=sys.stdout)
    core_path = DATA_DIR / "edges.jsonl"
    audit_path = DATA_DIR / "edges_below_threshold.jsonl"

    n_core = _process_file(core_path)
    n_audit = _process_file(audit_path)
    log.info("score_confidence: enriched %d core + %d audit edges", n_core, n_audit)

    # Quick stats
    tier_counts: dict[str, int] = {}
    weighted = 0
    if core_path.exists():
        for line in core_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            e = json.loads(line)
            tier = e.get("source_tier") or "unscored"
            tier_counts[tier] = tier_counts.get(tier, 0) + 1
            if e.get("weight") is not None:
                weighted += 1
    log.info("Source tiers (core): %s", tier_counts)
    log.info("Edges with financial weight: %d", weighted)


if __name__ == "__main__":
    main()
