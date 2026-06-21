"""Unit tests for the directional-expectation eval scorer (no DB / no LLM)."""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import backtest  # noqa: E402


def test_classify_all_branches():
    assert backtest.classify("positive", {"direction": "positive"}) == "correct"
    assert backtest.classify("negative", {"direction": "negative"}) == "correct"
    assert backtest.classify("positive", {"direction": "negative"}) == "opposite"
    assert backtest.classify("positive", {"direction": "no_effect"}) == "no_effect"
    assert backtest.classify("positive", {"direction": "unscored"}) == "unscored"
    assert backtest.classify("positive", None) == "not_in_trace"


def test_score_case_outcomes_and_not_in_graph():
    case = {"id": "c1", "headline": "h", "expect": [
        {"entity": "Alpha", "direction": "positive"},
        {"entity": "Beta", "direction": "negative"},
        {"entity": "Ghost", "direction": "positive"},
    ]}
    impacts_by_id = {
        "n:alpha": {"node_id": "n:alpha", "direction": "positive", "magnitude": 0.8},
        "n:beta": {"node_id": "n:beta", "direction": "positive", "magnitude": 0.5},
    }
    resolve = {"Alpha": "n:alpha", "Beta": "n:beta", "Ghost": None}.get
    res = backtest.score_case(case, impacts_by_id, resolve)
    outs = {r["entity"]: r["outcome"] for r in res["rows"]}
    assert outs == {"Alpha": "correct", "Beta": "opposite", "Ghost": "not_in_graph"}


def test_aggregate_excludes_not_in_graph_from_denominator():
    results = [{"rows": [
        {"outcome": "correct"}, {"outcome": "correct"},
        {"outcome": "opposite"}, {"outcome": "not_in_graph"},
    ]}]
    agg = backtest.aggregate(results)
    assert agg["total"] == 4
    assert agg["scorable"] == 3              # not_in_graph excluded
    assert agg["counts"]["correct"] == 2
    assert agg["accuracy"] == round(2 / 3, 3)


def test_majority_impacts_picks_majority_direction_then_max_mag():
    runs = [
        [{"node_id": "x", "direction": "positive", "magnitude": 0.6}],
        [{"node_id": "x", "direction": "positive", "magnitude": 0.8}],
        [{"node_id": "x", "direction": "negative", "magnitude": 0.9}],
    ]
    out = backtest._majority_impacts(runs)
    assert out["x"]["direction"] == "positive"   # 2 of 3 runs
    assert out["x"]["magnitude"] == 0.8          # highest-mag among the majority


def test_load_cases_round_trip(tmp_path):
    p = tmp_path / "c.jsonl"
    p.write_text('{"id":"a","headline":"h","expect":[{"entity":"X","direction":"positive"}]}\n\n',
                 encoding="utf-8")
    cases = backtest.load_cases(p)
    assert len(cases) == 1 and cases[0]["id"] == "a"
