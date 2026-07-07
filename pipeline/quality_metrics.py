"""Deterministic metrics for comparing the LLM path against the deterministic
scaffolding — the measurement foundation for "does minimizing LLM cost quality?".

Pure functions only (no I/O, no LLM). The live A/B harness (scripts/ab_quality.py)
feeds these the two sides to compare and aggregates the retention score.

Convention: the LLM path is treated as the reference ("ground truth") and the
deterministic path is scored against it. 1.0 = the deterministic path reproduces
the LLM path exactly.
"""
from __future__ import annotations

from typing import Optional


def seed_jaccard(a: set[str], b: set[str]) -> float:
    """Set overlap of two seed-id sets. 1.0 when both empty (trivially identical)."""
    union = a | b
    if not union:
        return 1.0
    return len(a & b) / len(union)


def primary_match(llm_primary: Optional[str], det_primary: Optional[str]) -> bool:
    """True iff both sides picked the SAME primary seed. A missing primary on either
    side is not a match (we want the deterministic pick to reproduce the LLM's)."""
    return llm_primary is not None and llm_primary == det_primary


def direction_agreement(a: dict[str, str], b: dict[str, str]) -> tuple[int, int]:
    """Over node-ids present in BOTH verdict maps, how many share the same direction.
    Returns (agree_count, shared_count) so the caller can pool across many events
    before taking a ratio (avoids small-sample ratio noise)."""
    shared = set(a) & set(b)
    agree = sum(1 for k in shared if a[k] == b[k])
    return agree, len(shared)


def classify_materiality(items: list[tuple[str, float]], keep_thr: float,
                         drop_thr: float, autokeep: float) -> dict[str, list[str]]:
    """Partition (id, prior) pairs into the rule's three bands. `autokeep` is the
    sentinel prior meaning always-keep (e.g. 8-K). Mirrors _materiality_prefilter's
    logic so the harness measures exactly what production does."""
    auto_keep, auto_drop, judge = [], [], []
    for cid, p in items:
        if p == autokeep or p >= keep_thr:
            auto_keep.append(cid)
        elif p < drop_thr:
            auto_drop.append(cid)
        else:
            judge.append(cid)
    return {"auto_keep": auto_keep, "auto_drop": auto_drop, "judge": judge}


def materiality_confusion(rule_keep: set[str], llm_keep: set[str],
                          all_ids: set[str]) -> dict:
    """Confusion of the rule-based materiality decision vs the LLM gate (reference).

    false_drop = rule dropped but LLM kept  → RECALL loss (the dangerous one:
                 a material event never traced).
    false_keep = rule kept but LLM dropped  → PRECISION loss (a trace wasted on noise).
    agreement  = fraction of candidates both sides decided the same way.
    """
    rule_drop = all_ids - rule_keep
    llm_drop = all_ids - llm_keep
    false_drop = rule_drop & llm_keep
    false_keep = rule_keep & llm_drop
    n = len(all_ids)
    agreement = (n - len(false_drop) - len(false_keep)) / n if n else 1.0
    return {
        "n": n,
        "agreement": agreement,
        "false_drop": len(false_drop),
        "false_keep": len(false_keep),
        "false_drop_rate": len(false_drop) / n if n else 0.0,
        "false_keep_rate": len(false_keep) / n if n else 0.0,
        "false_drop_ids": sorted(false_drop),
        "false_keep_ids": sorted(false_keep),
    }


# Weights for the aggregate retention score. Direction and materiality-recall are
# the quality-critical axes (a wrong direction or a dropped material event is a real
# defect); primary-seed match is important but a missed secondary seed is often
# recovered by propagation.
_W_PRIMARY = 0.3
_W_DIRECTION = 0.4
_W_MATERIALITY = 0.3


def retention_score(primary_match_rate: float, direction_agreement: float,
                    materiality_agreement: float) -> float:
    """Single 0-1 'quality retention' number: a weighted mean of the three axes.
    1.0 means the deterministic path reproduces the LLM path on all measured axes."""
    return (_W_PRIMARY * primary_match_rate
            + _W_DIRECTION * direction_agreement
            + _W_MATERIALITY * materiality_agreement)
