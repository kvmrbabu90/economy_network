"""Phase 1 Part B unit tests for the ingestion pipeline.

These exercise the pure functions only (parsing, dedupe, node construction).
The HTTP/SEC parts are covered by the end-to-end acceptance run in
docs/PHASE1_PROMPT.md, not here.
"""

from __future__ import annotations

from pipeline.ingest import (
    CompanyRecord,
    RawRow,
    build_company_node,
    dedupe_by_cik,
    parse_sp500_table,
)


def test_dedupe_collapses_share_classes_to_one_node():
    """Two rows sharing a CIK collapse into one company with both tickers.

    This is the prompt's fallback test (PHASE1_PROMPT.md Acceptance test #2):
    if no consumer-staples filer has multiple share classes in the live data,
    we still want a unit-test guarantee that the dedupe works.
    """
    rows = [
        RawRow(
            ticker="GOOGL",
            name="Alphabet Inc. (Class A)",
            gics_sector="Communication Services",
            gics_sub_industry="Interactive Media & Services",
            cik="0001652044",
        ),
        RawRow(
            ticker="GOOG",
            name="Alphabet Inc. (Class C)",
            gics_sector="Communication Services",
            gics_sub_industry="Interactive Media & Services",
            cik="0001652044",
        ),
    ]
    out = dedupe_by_cik(rows)
    assert len(out) == 1
    rec = out[0]
    assert rec.cik == "0001652044"
    assert set(rec.tickers) == {"GOOGL", "GOOG"}
    # Both names are kept as aliases so downstream resolution can match either.
    assert "Alphabet Inc. (Class A)" in rec.aliases
    assert "Alphabet Inc. (Class C)" in rec.aliases


def test_dedupe_preserves_independent_companies():
    rows = [
        RawRow("PG", "P&G", "Consumer Staples", "Household Products", "0000080424"),
        RawRow("COST", "Costco", "Consumer Staples",
               "Consumer Staples Distribution & Retail", "0000909832"),
    ]
    out = dedupe_by_cik(rows)
    assert len(out) == 2
    by_cik = {r.cik: r for r in out}
    assert by_cik["0000080424"].tickers == ["PG"]
    assert by_cik["0000909832"].tickers == ["COST"]


def test_build_company_node_validates_through_pydantic():
    """A deduped record must produce a Node that passes the canonical-ID validator."""
    rec = CompanyRecord(
        cik="0000080424",
        name="The Procter & Gamble Company",
        tickers=["PG"],
        sector="Consumer Staples",
        industry="Household Products",
        aliases=["The Procter & Gamble Company"],
        accession="0000080424-24-000123",
        filing_url="https://www.sec.gov/Archives/edgar/data/80424/000008042424000123/pg.htm",
        filing_date="2024-08-08",
        primary_document="pg.htm",
        filing_local_path="data/filings/0000080424/0000080424-24-000123.htm",
        filing_status="cached",
    )
    node = build_company_node(rec)
    assert node.id == "cik:0000080424"
    assert node.tickers == ["PG"]
    assert node.sector == "Consumer Staples"
    assert node.industry == "Household Products"
    assert node.metadata["filings"][0]["accession"] == "0000080424-24-000123"


def test_build_company_node_flags_missing_10k():
    rec = CompanyRecord(
        cik="0000123456",
        name="Example Filer Inc.",
        tickers=["EXMPL"],
        sector="Industrials",
        industry="Industrial Conglomerates",
        aliases=["Example Filer Inc."],
    )
    node = build_company_node(rec)
    assert node.metadata["filings"] == []
    assert node.metadata.get("no_10k_found") is True


def test_parse_sp500_table_header_keyed():
    """The parser keys on column header NAME, not index — reorder columns to prove it."""
    html = """
    <html><body>
    <table id="constituents" class="wikitable">
      <tr>
        <th>Headquarters Location</th>
        <th>GICS Sub-Industry</th>
        <th>Security</th>
        <th>CIK</th>
        <th>GICS Sector</th>
        <th>Symbol</th>
      </tr>
      <tr>
        <td>Cincinnati, Ohio</td>
        <td>Household Products</td>
        <td>Procter & Gamble</td>
        <td>80424</td>
        <td>Consumer Staples</td>
        <td>PG</td>
      </tr>
      <tr>
        <td>Issaquah, Washington</td>
        <td>Consumer Staples Distribution &amp; Retail</td>
        <td>Costco Wholesale</td>
        <td>909832</td>
        <td>Consumer Staples</td>
        <td>COST</td>
      </tr>
    </table>
    </body></html>
    """
    rows = parse_sp500_table(html)
    assert len(rows) == 2
    by_ticker = {r.ticker: r for r in rows}
    assert by_ticker["PG"].cik == "0000080424"  # zero-padded
    assert by_ticker["PG"].gics_sector == "Consumer Staples"
    assert by_ticker["PG"].gics_sub_industry == "Household Products"
    assert by_ticker["COST"].cik == "0000909832"
    assert by_ticker["COST"].name == "Costco Wholesale"


def test_parse_sp500_table_raises_when_required_column_missing():
    """If the table loses a column we depend on, fail loud rather than guess."""
    html = """
    <table id="constituents" class="wikitable">
      <tr>
        <th>Symbol</th>
        <th>Security</th>
        <th>GICS Sector</th>
        <!-- missing GICS Sub-Industry -->
        <th>CIK</th>
      </tr>
      <tr><td>PG</td><td>Procter & Gamble</td><td>Consumer Staples</td><td>80424</td></tr>
    </table>
    """
    import pytest
    with pytest.raises(ValueError, match="GICS Sub-Industry"):
        parse_sp500_table(html)
