from __future__ import annotations

from pipeline import quality_metrics as qm


def test_seed_jaccard():
    assert qm.seed_jaccard(set(), set()) == 1.0                 # both empty → identical
    assert qm.seed_jaccard({"a"}, set()) == 0.0
    assert qm.seed_jaccard({"a", "b"}, {"a", "b"}) == 1.0
    assert qm.seed_jaccard({"a", "b"}, {"a"}) == 0.5            # 1 shared / 2 union
    assert qm.seed_jaccard({"a", "b", "c"}, {"a", "b"}) == 2 / 3


def test_primary_match():
    assert qm.primary_match("cik:1", "cik:1") is True
    assert qm.primary_match("cik:1", "cik:2") is False
    assert qm.primary_match(None, "cik:1") is False
    assert qm.primary_match(None, None) is False                # no primary either side ≠ a match


def test_direction_agreement_over_shared_nodes():
    a = {"x": "negative", "y": "positive", "z": "no_effect"}
    b = {"x": "negative", "y": "negative", "w": "positive"}
    agree, shared = qm.direction_agreement(a, b)
    assert shared == 2 and agree == 1                            # x agrees, y disagrees; z/w not shared


def test_direction_agreement_no_overlap():
    agree, shared = qm.direction_agreement({"x": "negative"}, {"y": "positive"})
    assert agree == 0 and shared == 0                            # nothing to compare


def test_classify_materiality_bands():
    items = [("hi", 8.0), ("mid", 3.0), ("lo", 0.5), ("k8", 1e9)]
    b = qm.classify_materiality(items, keep_thr=5.0, drop_thr=1.5, autokeep=1e9)
    assert b["auto_keep"] == ["hi", "k8"] and b["auto_drop"] == ["lo"] and b["judge"] == ["mid"]


def test_materiality_confusion():
    all_ids = {"a", "b", "c", "d", "e"}
    rule_keep = {"a", "b", "c"}          # rule keeps a,b,c ; drops d,e
    llm_keep = {"a", "b", "d"}           # llm  keeps a,b,d ; drops c,e
    r = qm.materiality_confusion(rule_keep, llm_keep, all_ids)
    assert r["false_drop"] == 1          # d: rule dropped, llm kept (recall loss)
    assert r["false_keep"] == 1          # c: rule kept, llm dropped (precision loss)
    assert r["agreement"] == 3 / 5       # a,b,e agree
    assert r["false_drop_ids"] == ["d"] and r["false_keep_ids"] == ["c"]


def test_materiality_confusion_empty():
    r = qm.materiality_confusion(set(), set(), set())
    assert r["agreement"] == 1.0 and r["n"] == 0


def test_retention_score_wes_components():
    # Perfect agreement everywhere → 1.0
    assert qm.retention_score(primary_match_rate=1.0, direction_agreement=1.0,
                              materiality_agreement=1.0) == 1.0
    # A dip in any component pulls it below 1.0, weighted.
    s = qm.retention_score(primary_match_rate=0.9, direction_agreement=1.0, materiality_agreement=1.0)
    assert 0.9 <= s < 1.0
