"""Phase 2 Part B unit tests.

Focused on the verify gate (the grounding check that CLAUDE.md invariant #4
hinges on) and the JSON-array parser. The actual LLM call is not exercised
here -- it's covered by the end-to-end run in docs/PHASE2_PROMPT.md.
"""

from __future__ import annotations

import pytest

from pipeline.extractor import (
    _extract_json_array,
    _normalize,
    _target_in_snippet,
    verify_grounding,
)


# ---------------------------------------------------------------------------
# Verify gate (PHASE2_PROMPT.md acceptance: required unit test)
# ---------------------------------------------------------------------------

PG_FILING_EXCERPT = (
    "We also sell direct to consumers. Sales to Walmart Inc. and its "
    "affiliates represent approximately 16% of our total sales in 2025 "
    "and 2024 and 15% in 2023. No other customer represents more than 10% "
    "of our total sales."
)


def test_grounded_snippet_passes():
    snippet = "Sales to Walmart Inc. and its affiliates represent approximately 16% of our total sales"
    assert verify_grounding(snippet, "Walmart", PG_FILING_EXCERPT) is True


def test_fabricated_snippet_is_rejected():
    """The model claims a Costco edge but the text doesn't say it.

    This is the prompt's required unit test: a fabricated snippet must NEVER
    enter edges_raw, no matter how plausible the wording.
    """
    fabricated = "Sales to Costco represent approximately 12% of our total sales."
    assert verify_grounding(fabricated, "Costco", PG_FILING_EXCERPT) is False


def test_grounded_but_target_missing_is_rejected():
    """If the snippet IS in the filing but doesn't mention the target,
    the model is misattributing the relationship. Reject."""
    snippet = "No other customer represents more than 10% of our total sales."
    assert verify_grounding(snippet, "Costco", PG_FILING_EXCERPT) is False


def test_grounding_tolerates_whitespace_normalization():
    """The grounding check normalizes whitespace on BOTH sides so curly
    quotes / non-breaking spaces don't trip it up."""
    snippet = "Sales to  Walmart   Inc.\nand its affiliates represent approximately 16%"
    assert verify_grounding(snippet, "Walmart", PG_FILING_EXCERPT) is True


def test_target_distinctive_token_fallback():
    """If the snippet uses a distinctive token of the target (without the
    legal suffix) we still match: 'Kimberly-Clark' -> 'kimberly-clark'."""
    snippet = (
        "We compete with Kimberly-Clark and other branded paper-product makers "
        "in feminine care and family care."
    )
    full = "blah blah " + snippet + " more text"
    # Caller might pass the full legal name.
    assert verify_grounding(snippet, "Kimberly-Clark Corporation", full) is True


def test_normalize_collapses_whitespace_and_dashes():
    assert _normalize("foo  bar\nbaz qux") == "foo bar baz qux"
    assert _normalize("—") == "-"


def test_target_in_snippet_stopword_filter():
    """The fallback shouldn't match on generic words like 'company'."""
    assert _target_in_snippet("Company Inc", "no other customer represents") is False


# ---------------------------------------------------------------------------
# JSON-array parser (handles markdown fences + prose wrappers)
# ---------------------------------------------------------------------------

def test_json_array_parses_clean():
    assert _extract_json_array('[{"a": 1}]') == [{"a": 1}]
    assert _extract_json_array("[]") == []


def test_json_array_strips_markdown_fence():
    s = "```json\n[{\"target\": \"X\"}]\n```"
    assert _extract_json_array(s) == [{"target": "X"}]


def test_json_array_extracts_from_prose():
    s = "Sure! Here you go:\n[{\"a\": 1}, {\"a\": 2}]\nLet me know if you need more."
    assert _extract_json_array(s) == [{"a": 1}, {"a": 2}]


def test_json_array_returns_none_on_garbage():
    assert _extract_json_array("not even close to json") is None
    assert _extract_json_array("") is None
