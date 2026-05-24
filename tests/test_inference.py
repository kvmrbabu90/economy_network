"""Tier-2 co-mention closure: same snippet, same grounding, capped confidence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.inference import (
    INFERENCE_CONFIDENCE,
    closure_from_group,
    expand_pair_target,
    find_comention_groups,
    run_inference,
)
from pipeline.resolve import Registry, _resolve_inference_pair, seed_registry
from schema.models import CandidateEdge, EdgeType, Provenance


COSTCO_SNIPPET = (
    "Walmart, Target, Kroger, and Amazon are among our significant "
    "general merchandise retail competitors in the U.S."
)


def _ce(target_raw: str, snippet: str = COSTCO_SNIPPET, source_id: str = "cik:0000909832"):
    return {
        "source_id": source_id,
        "target_raw": target_raw,
        "type": "competes_with",
        "confidence": 0.98,
        "verified": True,
        "provenance": {
            "filing": "0000909832-25-000101",
            "url": "https://www.sec.gov/Archives/edgar/data/909832/x.htm",
            "snippet": snippet,
            "extracted_by": "llm:claude-cli",
        },
    }


def test_comention_group_picks_up_4way_list():
    rows = [
        _ce("Walmart"),
        _ce("Target"),
        _ce("Kroger"),
        _ce("Amazon"),
    ]
    groups = find_comention_groups(rows)
    assert len(groups) == 1
    g = groups[0]
    assert g.filer_cik == "cik:0000909832"
    assert set(g.targets) == {"Walmart", "Target", "Kroger", "Amazon"}


def test_comention_skips_pairs_smaller_than_3():
    # Two-way co-mentions are already covered by direct extraction.
    rows = [_ce("Walmart"), _ce("Target")]
    assert find_comention_groups(rows) == []


def test_comention_closure_produces_pairwise_edges_with_capped_confidence():
    rows = [_ce("Walmart"), _ce("Target"), _ce("Kroger"), _ce("Amazon")]
    groups = find_comention_groups(rows)
    edges = closure_from_group(groups[0])
    # C(4, 2) = 6 pairs.
    assert len(edges) == 6
    for ce in edges:
        assert ce.provenance.extracted_by == "inference:co-mention"
        assert ce.provenance.snippet == COSTCO_SNIPPET
        # Same source filing carried through for audit.
        assert ce.provenance.filing == "0000909832-25-000101"
        assert ce.confidence == INFERENCE_CONFIDENCE
        assert ce.type == EdgeType.competes_with.value or ce.type == EdgeType.competes_with
        # Pair-encoded target_raw survives until Phase 3 splits it.
        assert " || " in ce.target_raw


def test_expand_pair_target_roundtrip():
    assert expand_pair_target("Amazon || Walmart") == ("Amazon", "Walmart")
    assert expand_pair_target("Walmart") is None
    assert expand_pair_target(" || Walmart") is None  # missing left side


def test_resolve_inference_pair_returns_both_canonical_ids():
    nodes = [
        {"id": "cik:0000909832", "type": "Company", "name": "Costco",
         "aliases": [], "tickers": ["COST"], "identifiers": {},
         "sector": "Consumer Staples", "industry": "Consumer Staples Merchandise Retail",
         "country": "US", "metadata": {}},
        {"id": "cik:0000104169", "type": "Company", "name": "Walmart",
         "aliases": [], "tickers": ["WMT"], "identifiers": {},
         "sector": "Consumer Staples", "industry": "Consumer Staples Merchandise Retail",
         "country": "US", "metadata": {}},
        {"id": "cik:0000027419", "type": "Company", "name": "Target Corporation",
         "aliases": [], "tickers": ["TGT"], "identifiers": {},
         "sector": "Consumer Staples", "industry": "Consumer Staples Merchandise Retail",
         "country": "US", "metadata": {}},
    ]
    reg = seed_registry(nodes, [])
    ce = CandidateEdge(
        source_id="cik:0000909832",
        target_raw="Walmart || Target",
        type=EdgeType.competes_with,
        confidence=INFERENCE_CONFIDENCE,
        provenance=Provenance(
            filing="0000909832-25-000101",
            url="https://www.sec.gov/x",
            snippet=COSTCO_SNIPPET,
            extracted_by="inference:co-mention",
        ),
        verified=True,
    )
    pair = _resolve_inference_pair(ce, reg)
    assert pair == ("cik:0000104169", "cik:0000027419")


def test_run_inference_end_to_end(tmp_path):
    side = tmp_path / "llm_candidates.jsonl"
    out = tmp_path / "inferred.jsonl"
    rows = [_ce("Walmart"), _ce("Target"), _ce("Kroger"), _ce("Amazon")]
    side.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    summary = run_inference(llm_side_path=side, out_path=out)
    assert summary["groups"] == 1
    assert summary["candidates"] == 6
    assert out.exists()
    written = [
        json.loads(line) for line in out.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    assert len(written) == 6
    for r in written:
        assert r["provenance"]["extracted_by"] == "inference:co-mention"
