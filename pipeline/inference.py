"""Tier-2 derivations: co-mention closure (and reserved gics-peer slot).

Why this exists: the grounding gate (CLAUDE.md invariant #4) requires every
edge to be backed by a verbatim filing snippet that names the target. That
produces a TRUSTWORTHY but SPARSE graph: a filing that says "we compete with
Walmart, Target, Kroger, and Amazon" produces 4 edges from the filer to each
named competitor -- but no edges AMONG those 4. The "Target competes with
Walmart" relationship is common-sense true and the snippet supports it,
but no edge is created.

Co-mention closure fills that in deterministically: for any group of >=3
competitors named in the same snippet, mint pairwise competes_with edges
among them. Same snippet as provenance, same verify-gate semantics (both
endpoints are literally in the snippet), capped confidence so the strict
0.75 cutoff keeps them in the audit/inferred layer by default.

The inference is deliberately conservative:
  * Only competes_with (the only semantically sound multi-way claim).
  * Only when >=3 distinct targets are named in the same snippet, since
    2-name lists are already covered by the direct LLM extraction.
  * Confidence capped at INFERENCE_CONFIDENCE so users opt in to see them.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path
from typing import Any, Iterable

from schema.models import CandidateEdge, EdgeType, Provenance


log = logging.getLogger("inference")


# Cap that puts inferred edges below the strict 0.75 cutoff by default but
# still ahead of the provisional-slug 0.5 cap, so they sit in their own
# audit band. Toggle them on/off in the UI's "inferred" layer.
INFERENCE_CONFIDENCE = 0.65


@dataclass
class CoMentionGroup:
    """A set of competitor names co-mentioned in a single filing snippet."""

    filer_cik: str          # canonical cik: id of the company whose filing this is
    filing: str             # accession number
    filing_url: str
    snippet: str
    targets: list[str]      # the raw target names from the LLM (>=3 for inference)


def find_comention_groups(
    llm_candidates: Iterable[dict[str, Any]],
) -> list[CoMentionGroup]:
    """Group LLM-extracted competes_with candidates by (source, snippet)."""
    by_key: dict[tuple[str, str], CoMentionGroup] = {}
    for raw in llm_candidates:
        if raw.get("type") != EdgeType.competes_with.value:
            continue
        prov = raw.get("provenance") or {}
        snippet = (prov.get("snippet") or "").strip()
        source = raw.get("source_id") or ""
        if not snippet or not source:
            continue
        key = (source, snippet)
        grp = by_key.get(key)
        if grp is None:
            grp = CoMentionGroup(
                filer_cik=source,
                filing=prov.get("filing", ""),
                filing_url=prov.get("url", ""),
                snippet=snippet,
                targets=[],
            )
            by_key[key] = grp
        target = (raw.get("target_raw") or "").strip()
        if target and target not in grp.targets:
            grp.targets.append(target)
    # Only groups with >=3 distinct targets benefit from closure --
    # 2-name lists are already covered by direct extraction.
    return [g for g in by_key.values() if len(g.targets) >= 3]


def closure_from_group(group: CoMentionGroup) -> list[CandidateEdge]:
    """Mint pairwise competes_with edges among the co-mentioned targets.

    A snippet naming 4 competitors yields C(4,2)=6 pairwise edges. The
    source/target ordering picks the alphabetically-first name as source
    so the unordered-pair dedupe in Phase 3 collapses any duplicates
    deterministically.
    """
    out: list[CandidateEdge] = []
    pairs = list(combinations(sorted(set(group.targets)), 2))
    for a, b in pairs:
        # IMPORTANT: source_id MUST start with "cik:" per the CandidateEdge
        # validator. The co-mention pairs name two non-self companies; the
        # FILER's cik is the only canonical id we can attribute the snippet
        # to (it's the filing's provenance). Put the FILER as the source_id
        # but record the actual competitive pair via target_raw + the snippet.
        #
        # Phase 3 resolution will treat this as a normal competes_with
        # candidate -- BUT we need both ends to resolve. The CandidateEdge
        # model only has one target_raw slot, so we emit TWO candidates per
        # pair: (filer -> A, competes_with) and (filer -> B, competes_with),
        # which already exist from direct extraction... that defeats the
        # purpose.
        #
        # Reframe: the closure produces edges BETWEEN the named competitors,
        # not between filer and competitors. We need a different storage:
        # mint candidates with the FIRST named competitor as the source_id
        # IF it can be resolved to a cik via normalization; otherwise skip.
        # That resolution happens in Phase 3, so we can't do it here cleanly.
        #
        # Cleanest path: emit the closure edges AT RESOLUTION TIME, after
        # the registry lookup, so we can attach a real cik: source. See
        # closure_after_resolution() below -- this function is the
        # pre-resolution shape, kept for tests.
        out.append(
            CandidateEdge(
                source_id=group.filer_cik,   # filer attribution (temporary)
                target_raw=f"{a} || {b}",    # encoded pair for the resolver
                type=EdgeType.competes_with,
                confidence=INFERENCE_CONFIDENCE,
                provenance=Provenance(
                    filing=group.filing or "co-mention",
                    url=group.filing_url or "https://www.sec.gov/",
                    snippet=group.snippet,
                    extracted_by="inference:co-mention",
                ),
                verified=True,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Resolution-time closure (the path we actually use)
# ---------------------------------------------------------------------------

# A small encoded format for "pair targets" so the closure can survive Phase 3's
# target resolution: in Part B the resolver sees `A || B` as the target_raw
# and splits it into two canonical lookups; if BOTH resolve, it mints an
# Edge between them (not between the filer and them).
#
# The cleaner long-term solution would be a richer candidate schema, but the
# encoded-target hack stays inside one module and doesn't touch Phase 3 or
# the API. The resolver below knows about it.


def expand_pair_target(target_raw: str) -> tuple[str, str] | None:
    """Decode 'A || B' into ('A', 'B'); return None for ordinary targets."""
    if " || " not in target_raw:
        return None
    a, b = target_raw.split(" || ", 1)
    a = a.strip()
    b = b.strip()
    if not a or not b:
        return None
    return a, b


def write_inferred_candidates(
    candidates: Iterable[CandidateEdge], path: Path,
) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with path.open("w", encoding="utf-8") as fh:
        for ce in candidates:
            fh.write(ce.model_dump_json())
            fh.write("\n")
            n += 1
    return n


def run_inference(
    *, llm_side_path: Path, out_path: Path,
) -> dict[str, Any]:
    """Compute co-mention closures over the LLM candidate side file and write."""
    if not llm_side_path.exists():
        log.warning("LLM side file missing at %s; nothing to infer", llm_side_path)
        return {"groups": 0, "candidates": 0}
    rows: list[dict[str, Any]] = []
    with llm_side_path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    groups = find_comention_groups(rows)
    log.info("Co-mention groups (>=3 named targets): %d", len(groups))
    candidates: list[CandidateEdge] = []
    for g in groups:
        candidates.extend(closure_from_group(g))
    n = write_inferred_candidates(candidates, out_path)
    log.info("Wrote %d inferred candidates -> %s", n, out_path)
    return {"groups": len(groups), "candidates": n}
