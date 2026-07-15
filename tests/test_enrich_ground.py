"""Invariant #4 grounding (pipeline.enrich._ground): capsule content the article text does
NOT literally support is dropped, using WHOLE-TOKEN matching so a short org/ticker or an
embedded number can't false-ground ("GE" inside "large", "12" inside "2012")."""
from __future__ import annotations

from pipeline.enrich import _ground, _token_present
from schema.models import ArticleCapsule


def test_token_present_rejects_substring_but_keeps_whole_token():
    assert _token_present("GE", "large gains were reported") is False   # "ge" inside "large"
    assert _token_present("GM", "an augment to the plan") is False      # "gm" inside "augment"
    assert _token_present("GE", "GE said today") is True
    assert _token_present("Broadcom", "broadcom unveiled a chip") is True
    assert _token_present("S&P", "the s&p 500 rose") is True            # internal punctuation ok


def test_ground_drops_hallucinated_short_org():
    cap = ArticleCapsule(event_type="other", affected=["GE", "Broadcom"])
    _ground(cap, "Broadcom announced a new AI chip; nothing large happened.")
    assert cap.affected == ["Broadcom"]                                 # GE only appears in "large"


def test_ground_drops_money_embedded_in_larger_number():
    cap = ArticleCapsule(event_type="other", money="12%")
    _ground(cap, "The firm was founded in 2012 and employs 1200 people.")
    assert cap.money is None                                            # "12" only inside 2012/1200


def test_ground_keeps_standalone_money_and_org():
    cap = ArticleCapsule(event_type="other", money="$12 billion", affected=["Pfizer"])
    _ground(cap, "Pfizer said the deal is worth $12 billion in cash.")
    assert cap.money == "$12 billion" and cap.affected == ["Pfizer"]
