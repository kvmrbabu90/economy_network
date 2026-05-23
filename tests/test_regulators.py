"""Part A4 unit tests: rule-based regulated_by edge generation."""

from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.regulators import (
    extract_rule_edges,
    load_regulators_config,
    load_subindustry_map,
    resolve_gics_industry,
)


# Anchor to the repo-root config files so the rules under test match what
# ships in the repo (avoids drift between fixtures and prod config).
REPO_ROOT = Path(__file__).resolve().parent.parent
CFG_DIR = REPO_ROOT / "config"


@pytest.fixture(scope="module")
def cfg():
    return load_regulators_config(CFG_DIR / "regulators.yaml")


@pytest.fixture(scope="module")
def sub_to_industry():
    return load_subindustry_map(CFG_DIR / "gics_subindustry_to_industry.yaml")


def test_food_company_gets_fda_usda_ftc_sec(cfg, sub_to_industry):
    """A packaged-food filer must end up with FDA + USDA + FTC + SEC at minimum.

    This is the Phase 2 Part A acceptance gate, encoded as a unit test so it
    doesn't decay silently.
    """
    pg_like = [{
        "id": "cik:0000021344",
        "name": "The Coca-Cola Company",
        "sector": "Consumer Staples",
        "industry": "Soft Drinks & Non-alcoholic Beverages",  # sub-industry
        "metadata": {},
    }, {
        "id": "cik:0000077476",
        "name": "PepsiCo",
        "sector": "Consumer Staples",
        "industry": "Soft Drinks & Non-alcoholic Beverages",
        "metadata": {},
    }, {
        "id": "cik:0000080424",
        "name": "Procter & Gamble",
        "sector": "Consumer Staples",
        "industry": "Household Products",
        "metadata": {},
    }, {
        "id": "cik:0000219135",
        "name": "Conagra Brands",
        "sector": "Consumer Staples",
        "industry": "Packaged Foods & Meats",
        "metadata": {},
    }]
    res = extract_rule_edges(pg_like, cfg, sub_to_industry)
    by_company = res.per_company
    conagra_regs = set(by_company["cik:0000219135"])
    # Food Products industry adds FDA + USDA; Consumer Staples sector adds FTC;
    # _default adds SEC. So Conagra (packaged foods) must have all four.
    assert {"regulator:sec", "regulator:ftc", "regulator:fda", "regulator:usda"}.issubset(
        conagra_regs
    ), f"missing some of FDA/USDA/FTC/SEC for Conagra; got {sorted(conagra_regs)}"
    # P&G (household products) gets FTC + CPSC + EPA + FDA (cosmetics/OTC), not USDA.
    pg_regs = set(by_company["cik:0000080424"])
    assert "regulator:ftc" in pg_regs and "regulator:sec" in pg_regs
    assert "regulator:cpsc" in pg_regs


def test_unmapped_sub_industry_raises(sub_to_industry):
    with pytest.raises(KeyError, match="not mapped"):
        resolve_gics_industry(
            "Quantum Energy Storage Devices",  # not in the rollup
            sub_to_industry,
            company_name="Bogus Co",
        )


def test_sub_industry_required(sub_to_industry):
    with pytest.raises(KeyError, match="missing GICS sub-industry"):
        resolve_gics_industry(None, sub_to_industry, company_name="Nameless Co")


def test_candidate_edge_targets_are_regulator_ids(cfg, sub_to_industry):
    sample = [{
        "id": "cik:0000080424",
        "name": "Procter & Gamble",
        "sector": "Consumer Staples",
        "industry": "Household Products",
        "metadata": {},
    }]
    res = extract_rule_edges(sample, cfg, sub_to_industry)
    assert res.candidates, "expected at least one regulated_by candidate"
    for ce in res.candidates:
        assert ce.target_raw.startswith("regulator:")
        assert ce.type == "regulated_by"
        assert ce.verified is True
        assert ce.provenance.extracted_by == "rule"
        # rule-extracted edges have no filing / url -- that's allowed by the
        # Provenance model and is the correct shape for non-filing-derived edges.
        assert ce.provenance.filing == ""
        assert ce.provenance.url == ""
