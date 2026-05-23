"""Phase 3 unit tests: normalization, registry, resolver, dedupe, invariant."""

from __future__ import annotations

import pytest

from schema.models import (
    CandidateEdge,
    Edge,
    EdgeType,
    Node,
    NodeType,
    Provenance,
)
from pipeline.resolve import (
    HIGH_BAR,
    PROVISIONAL_CONFIDENCE_CAP,
    Registry,
    ResolutionDecision,
    ResolveReport,
    assert_single_node_invariant,
    dedupe_competes_with,
    normalize,
    resolve_target,
    seed_registry,
    to_slug,
)


# ---------------------------------------------------------------------------
# normalize()
# ---------------------------------------------------------------------------

WALMART_FRAGMENTS = [
    "Walmart",
    "Walmart Inc.",
    "Walmart, Inc.",
    "Wal-Mart Stores, Inc.",
    "Walmart Stores, Inc.",
    "WALMART INC",
    "walmart  inc",
]


def test_walmart_fragments_all_normalize_equal():
    """The acceptance backbone -- all five named Walmart variants must collapse."""
    normalized = {normalize(s) for s in WALMART_FRAGMENTS}
    assert normalized == {"walmart"}, f"got {normalized}"


def test_normalize_unicode_fold():
    assert normalize("Nestlé S.A.") == "nestle"
    assert normalize("Danone S.A.") == "danone"


def test_normalize_strips_legal_suffixes():
    assert normalize("The Procter & Gamble Company") == "procter and gamble"
    assert normalize("Kraft Heinz Company") == "kraft heinz"
    assert normalize("The Kraft Heinz Company") == "kraft heinz"
    assert normalize("Colgate-Palmolive Company") == "colgate palmolive"
    assert normalize("Suntory Global Spirits") == "suntory global spirits"


def test_normalize_idempotent():
    for s in WALMART_FRAGMENTS + ["Nestlé S.A.", "The Procter & Gamble Company"]:
        assert normalize(normalize(s)) == normalize(s)


def test_to_slug():
    assert to_slug(normalize("Red Bull GmbH")) == "red-bull"
    assert to_slug(normalize("Nestlé S.A.")) == "nestle"
    assert to_slug(normalize("Suntory Global Spirits")) == "suntory-global-spirits"


# ---------------------------------------------------------------------------
# Registry seeding
# ---------------------------------------------------------------------------

PG_NODE = {
    "id": "cik:0000080424",
    "type": "Company",
    "name": "The Procter & Gamble Company",
    "aliases": ["The Procter & Gamble Company"],
    "tickers": ["PG"],
    "identifiers": {"cik": "0000080424"},
    "sector": "Consumer Staples",
    "industry": "Household Products",
    "country": "US",
    "metadata": {},
}

WMT_NODE = {
    "id": "cik:0000104169",
    "type": "Company",
    "name": "Walmart",
    "aliases": ["Walmart"],
    "tickers": ["WMT"],
    "identifiers": {"cik": "0000104169"},
    "sector": "Consumer Staples",
    "industry": "Consumer Staples Merchandise Retail",
    "country": "US",
    "metadata": {},
}

FTC_NODE = {
    "id": "regulator:ftc",
    "type": "Regulator",
    "name": "Federal Trade Commission",
    "aliases": ["Federal Trade Commission"],
    "tickers": [],
    "identifiers": {},
    "metadata": {},
}


def _prov(filing="0000080424-25-000076"):
    return Provenance(
        filing=filing,
        url="https://www.sec.gov/Archives/edgar/data/80424/" + filing + ".htm",
        snippet="Sales to Walmart Inc. and its affiliates represent approximately 16% of our total sales",
        extracted_by="llm:claude-cli",
    )


def test_seed_registry_resolves_known_name():
    reg = seed_registry([PG_NODE, WMT_NODE], [FTC_NODE])
    assert reg.lookup("Walmart") == "cik:0000104169"
    assert reg.lookup("The Procter & Gamble Company") == "cik:0000080424"
    assert reg.lookup("PG") == "cik:0000080424"  # ticker seeded
    assert reg.lookup("WMT") == "cik:0000104169"


# ---------------------------------------------------------------------------
# resolve_target() -- exact / fuzzy / mint
# ---------------------------------------------------------------------------

def _cand(target_raw, edge_type="competes_with", source_id="cik:0000080424"):
    return CandidateEdge(
        source_id=source_id,
        target_raw=target_raw,
        type=EdgeType(edge_type),
        confidence=0.9,
        provenance=_prov(),
        verified=True,
    )


def test_all_walmart_fragments_resolve_to_one_cik():
    reg = seed_registry([PG_NODE, WMT_NODE], [FTC_NODE])
    review = []
    seen = set()
    for frag in WALMART_FRAGMENTS:
        d = resolve_target(_cand(frag, edge_type="supplies"), reg, review=review)
        assert d is not None, f"{frag!r} unexpectedly queued"
        seen.add(d.target_id)
    assert seen == {"cik:0000104169"}
    assert review == []


def test_unknown_target_mints_provisional_slug_and_reuses_it():
    reg = seed_registry([PG_NODE, WMT_NODE], [FTC_NODE])
    review = []
    d1 = resolve_target(_cand("Red Bull GmbH"), reg, review=review)
    assert d1.action == "minted-slug"
    assert d1.target_id == "slug:red-bull"
    assert d1.confidence_cap == PROVISIONAL_CONFIDENCE_CAP
    # Second filing also names Red Bull -> same canonical id.
    d2 = resolve_target(_cand("Red Bull"), reg, review=review)
    assert d2.target_id == "slug:red-bull"
    # The node was minted only once.
    minted = [n for n in reg.nodes_by_id.values() if n["id"] == "slug:red-bull"]
    assert len(minted) == 1
    assert minted[0]["metadata"]["provisional"] is True


def test_regulated_by_passes_target_through():
    reg = seed_registry([PG_NODE], [FTC_NODE])
    review = []
    ce = CandidateEdge(
        source_id="cik:0000080424",
        target_raw="regulator:ftc",
        type=EdgeType.regulated_by,
        confidence=1.0,
        provenance=Provenance(
            filing="", url="",
            snippet="regulators.yaml -> ftc",
            extracted_by="rule",
        ),
        verified=True,
    )
    d = resolve_target(ce, reg, review=review)
    assert d.target_id == "regulator:ftc"
    assert d.action == "exact"


# ---------------------------------------------------------------------------
# competes_with dedupe
# ---------------------------------------------------------------------------

def _edge(src, tgt, conf, edge_type="competes_with", snippet="A names B"):
    return Edge(
        source=src, target=tgt,
        type=EdgeType(edge_type),
        directed=(edge_type != "competes_with"),
        confidence=conf,
        provenance=Provenance(
            filing="f-" + src.split(":")[-1],
            url="https://www.sec.gov/x",
            snippet=snippet,
            extracted_by="llm:claude-cli",
        ),
    )


def test_competes_with_dedupes_unordered_pairs():
    edges = [
        _edge("cik:0000080424", "cik:0000055785", 0.85, snippet="P&G names KMB"),
        _edge("cik:0000055785", "cik:0000080424", 0.92, snippet="KMB names P&G"),
        _edge("cik:0000080424", "cik:0000021665", 0.80, snippet="P&G names CL"),
    ]
    out, before, after = dedupe_competes_with(edges)
    assert before == 3
    assert after == 2  # {PG,KMB} pair collapses; {PG,CL} stands alone
    # The {PG,KMB} survivor must keep the higher confidence and merge the other snippet.
    pgkmb = next(e for e in out if {e.source, e.target} == {"cik:0000080424", "cik:0000055785"})
    assert pgkmb.confidence == 0.92
    # The other side's snippet was folded in.
    all_snippets = [pgkmb.provenance.snippet] + [p.snippet for p in pgkmb.additional_provenance]
    assert "P&G names KMB" in all_snippets
    assert "KMB names P&G" in all_snippets


def test_dedupe_preserves_non_competes_edges():
    edges = [
        _edge("cik:0000080424", "cik:0000104169", 0.95, edge_type="supplies",
              snippet="P&G supplies Walmart"),
        _edge("cik:0000080424", "regulator:ftc", 1.0, edge_type="regulated_by",
              snippet="rule"),
    ]
    out, before, after = dedupe_competes_with(edges)
    assert before == 0
    assert after == 0
    assert len(out) == 2  # both pass through


# ---------------------------------------------------------------------------
# Single-node invariant
# ---------------------------------------------------------------------------

def test_invariant_pass_when_aliases_unique():
    reg = seed_registry([PG_NODE, WMT_NODE], [FTC_NODE])
    ok, collisions = assert_single_node_invariant(reg)
    assert ok is True
    assert collisions == []


def test_invariant_detects_alias_collision():
    """Manually wire an aliases list with a known collision and assert detection."""
    reg = Registry()
    reg.aliases = [
        {"alias": "Foo", "alias_normalized": "foo", "canonical_id": "cik:0000000001", "source": "x"},
        {"alias": "FOO", "alias_normalized": "foo", "canonical_id": "cik:0000000002", "source": "y"},
    ]
    ok, collisions = assert_single_node_invariant(reg)
    assert ok is False
    assert len(collisions) == 1
    assert collisions[0][0] == "foo"
