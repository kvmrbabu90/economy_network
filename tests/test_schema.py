"""Phase 0 acceptance tests.

These tests verify the schema invariants from CLAUDE.md and the PRD §4 example
records. They are deliberately narrow — Phase 0 does NOT build ingestion,
extraction, API, or frontend logic, so don't add coverage for those here.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from schema import (
    Edge,
    EdgeType,
    Node,
    NodeType,
    Provenance,
    connect,
    get_edge,
    get_node,
    init_db,
    upsert_edge,
    upsert_node,
)


# --- PRD §4 example records -------------------------------------------------

PG_NODE = {
    "id": "cik:0000080424",
    "type": "Company",
    "name": "The Procter & Gamble Company",
    "aliases": ["P&G", "Procter & Gamble", "PG"],
    "tickers": ["PG"],
    "identifiers": {"cik": "0000080424", "wikidata": "Q170298"},
    "sector": "Consumer Staples",
    "industry": "Household Products",
    "country": "US",
    "metadata": {},
}

COSTCO_NODE = {
    "id": "cik:0000909832",
    "type": "Company",
    "name": "Costco Wholesale Corporation",
    "aliases": ["Costco"],
    "tickers": ["COST"],
    "identifiers": {"cik": "0000909832"},
    "sector": "Consumer Staples",
    "industry": "Consumer Staples Distribution & Retail",
    "country": "US",
    "metadata": {},
}

KMB_NODE = {
    "id": "cik:0000055785",
    "type": "Company",
    "name": "Kimberly-Clark Corporation",
    "aliases": ["Kimberly-Clark", "KMB"],
    "tickers": ["KMB"],
    "identifiers": {"cik": "0000055785"},
    "sector": "Consumer Staples",
    "industry": "Household Products",
    "country": "US",
    "metadata": {},
}


def _prov(filing: str = "0000080424-24-000123") -> Provenance:
    return Provenance(
        filing=filing,
        url=f"https://www.sec.gov/Archives/edgar/data/80424/{filing}.txt",
        snippet="Our largest customer, Wal-Mart Stores, Inc. and its affiliates...",
        extracted_by="llm",
    )


# --- fixtures ---------------------------------------------------------------

@pytest.fixture()
def conn(tmp_path):
    db = tmp_path / "test.db"
    c = connect(db)
    init_db(c)
    yield c
    c.close()


# --- node round-trip --------------------------------------------------------

def test_pg_node_roundtrip(conn):
    upsert_node(conn, PG_NODE)
    got = get_node(conn, "cik:0000080424")
    assert got is not None
    assert got.id == PG_NODE["id"]
    assert got.name == PG_NODE["name"]
    assert got.aliases == PG_NODE["aliases"]
    assert got.tickers == PG_NODE["tickers"]
    assert got.identifiers == PG_NODE["identifiers"]
    assert got.sector == "Consumer Staples"
    assert got.industry == "Household Products"


def test_costco_node_roundtrip(conn):
    upsert_node(conn, COSTCO_NODE)
    got = get_node(conn, "cik:0000909832")
    assert got is not None
    assert got.type == NodeType.Company.value or got.type == NodeType.Company
    assert got.tickers == ["COST"]


def test_node_id_must_be_canonical():
    with pytest.raises(ValidationError):
        Node(id="PG", type=NodeType.Company, name="Procter & Gamble")
    with pytest.raises(ValidationError):
        Node(id="ticker:PG", type=NodeType.Company, name="Procter & Gamble")


def test_node_name_must_be_non_empty():
    with pytest.raises(ValidationError):
        Node(id="cik:0000080424", type=NodeType.Company, name="")


# --- edge round-trip --------------------------------------------------------

def test_supplies_edge_roundtrip(conn):
    upsert_node(conn, PG_NODE)
    upsert_node(conn, COSTCO_NODE)
    edge = Edge(
        source="cik:0000080424",
        target="cik:0000909832",
        type=EdgeType.supplies,
        confidence=0.82,
        provenance=_prov(),
    )
    upsert_edge(conn, edge)
    got = get_edge(conn, edge.id)
    assert got is not None
    assert got.source == "cik:0000080424"
    assert got.target == "cik:0000909832"
    assert got.type == EdgeType.supplies.value or got.type == EdgeType.supplies
    assert got.confidence == pytest.approx(0.82)
    assert got.provenance.extracted_by == "llm"
    assert got.provenance.filing == "0000080424-24-000123"


def test_competes_with_edge_roundtrip(conn):
    upsert_node(conn, PG_NODE)
    upsert_node(conn, KMB_NODE)
    edge = Edge(
        source="cik:0000080424",
        target="cik:0000055785",
        type=EdgeType.competes_with,
        directed=False,
        confidence=0.95,
        provenance=Provenance(
            filing="0000080424-24-000123",
            url="https://www.sec.gov/Archives/edgar/data/80424/0000080424-24-000123.txt",
            snippet="We compete with Kimberly-Clark, Colgate-Palmolive, and others.",
            extracted_by="llm",
        ),
    )
    upsert_edge(conn, edge)
    got = get_edge(conn, edge.id)
    assert got is not None
    assert got.target == "cik:0000055785"
    assert "Kimberly-Clark" in got.provenance.snippet


# --- invariant checks -------------------------------------------------------

def test_regulated_by_requires_regulator_target():
    """regulated_by edges must point to a regulator: node (CLAUDE.md §8 + PHASE0 §2)."""
    with pytest.raises(ValidationError):
        Edge(
            source="cik:0000080424",
            target="cik:0000909832",  # a Company, not a regulator
            type=EdgeType.regulated_by,
            confidence=1.0,
            provenance=_prov(),
        )


def test_regulated_by_accepts_regulator_target():
    edge = Edge(
        source="cik:0000080424",
        target="regulator:ftc",
        type=EdgeType.regulated_by,
        confidence=1.0,
        provenance=Provenance(
            filing="rule:regulators.yaml",
            url="https://github.com/kvmrbabu90/economy_network/blob/main/config/regulators.yaml",
            snippet="Household Products -> FTC",
            extracted_by="rule",
        ),
    )
    assert edge.target == "regulator:ftc"


def test_edge_requires_provenance():
    """Invariant #3: no edge enters the graph without provenance."""
    with pytest.raises(ValidationError):
        Edge(
            source="cik:0000080424",
            target="cik:0000909832",
            type=EdgeType.supplies,
            confidence=0.5,
            # provenance omitted
        )


def test_edge_confidence_bounds():
    with pytest.raises(ValidationError):
        Edge(
            source="cik:0000080424",
            target="cik:0000909832",
            type=EdgeType.supplies,
            confidence=1.5,
            provenance=_prov(),
        )


def test_no_customer_of_edge_type():
    """Invariant #2: customer_of is derived, never stored. Enum must not contain it."""
    assert "customer_of" not in {e.value for e in EdgeType}


# --- Phase 1 Part A: Segment + part_of schema headroom ----------------------

GOOGLE_CLOUD_SEGMENT = {
    "id": "seg:cik0001652044:google-cloud",
    "type": "Segment",
    "name": "Google Cloud",
    "aliases": ["GCP"],
    "tickers": [],
    "identifiers": {"parent_cik": "0001652044"},
    "sector": "Communication Services",
    "industry": "Interactive Media & Services",
    "country": "US",
    "metadata": {"note": "Phase 1 schema-headroom example; not populated by ingest."},
}

ALPHABET_PARENT = {
    "id": "cik:0001652044",
    "type": "Company",
    "name": "Alphabet Inc.",
    "aliases": ["Alphabet", "Google"],
    "tickers": ["GOOGL", "GOOG"],
    "identifiers": {"cik": "0001652044"},
    "sector": "Communication Services",
    "industry": "Interactive Media & Services",
    "country": "US",
    "metadata": {},
}


def test_segment_node_roundtrip(conn):
    upsert_node(conn, ALPHABET_PARENT)
    upsert_node(conn, GOOGLE_CLOUD_SEGMENT)
    got = get_node(conn, "seg:cik0001652044:google-cloud")
    assert got is not None
    assert got.type == NodeType.Segment.value or got.type == NodeType.Segment
    assert got.name == "Google Cloud"
    assert got.identifiers["parent_cik"] == "0001652044"


def test_segment_id_must_match_format():
    # Missing parent CIK in the body.
    with pytest.raises(ValidationError):
        Node(id="seg:google-cloud", type=NodeType.Segment, name="Google Cloud")
    # Parent body not lower-kebab.
    with pytest.raises(ValidationError):
        Node(
            id="seg:cik0001652044:Google_Cloud",
            type=NodeType.Segment,
            name="Google Cloud",
        )


def test_part_of_edge_validates_and_roundtrips(conn):
    upsert_node(conn, ALPHABET_PARENT)
    upsert_node(conn, GOOGLE_CLOUD_SEGMENT)
    edge = Edge(
        source="seg:cik0001652044:google-cloud",
        target="cik:0001652044",
        type=EdgeType.part_of,
        confidence=1.0,
        provenance=Provenance(
            filing="0001652044-24-000123",
            url="https://www.sec.gov/Archives/edgar/data/1652044/0001652044-24-000123.txt",
            snippet="Google Cloud is a reportable operating segment of Alphabet Inc.",
            extracted_by="manual",
        ),
    )
    upsert_edge(conn, edge)
    got = get_edge(conn, edge.id)
    assert got is not None
    assert got.source == "seg:cik0001652044:google-cloud"
    assert got.target == "cik:0001652044"
    assert got.type == EdgeType.part_of.value or got.type == EdgeType.part_of


def test_part_of_target_must_be_cik():
    """A Segment must roll up to a Company filer, not a regulator/commodity/etc."""
    with pytest.raises(ValidationError):
        Edge(
            source="seg:cik0001652044:google-cloud",
            target="regulator:ftc",
            type=EdgeType.part_of,
            confidence=1.0,
            provenance=_prov(),
        )
    with pytest.raises(ValidationError):
        Edge(
            source="seg:cik0001652044:google-cloud",
            target="slug:crude-oil",
            type=EdgeType.part_of,
            confidence=1.0,
            provenance=_prov(),
        )


def test_part_of_source_must_be_segment_id():
    """part_of edges must originate from a seg: node id (id-prefix convention)."""
    with pytest.raises(ValidationError):
        Edge(
            source="cik:0000080424",  # a Company, not a Segment
            target="cik:0001652044",
            type=EdgeType.part_of,
            confidence=1.0,
            provenance=_prov(),
        )
