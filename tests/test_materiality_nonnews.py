"""Tests for the deterministic non-news pre-drop classifier (_looks_like_non_news).

Guards the two properties that matter:
  1. It DROPS investment commentary / advice / analyst actions / dividend-earnings
     logistics — the noise that flooded the impact panels.
  2. It NEVER blind-drops a headline that names a hard material event (M&A, earnings,
     regulatory, dividend action, …) — those defer to the LLM gate instead.
"""
from __future__ import annotations

import pytest

from pipeline.ingest_news import _looks_like_non_news

# --- should be dropped: commentary / advice / analyst / logistics (no material trigger)
NON_NEWS = [
    "IT stocks are beginning to offer bank FD-like dividend yields. But is that a trap for investors?",
    "International Business Machines (NYSE:IBM) Shares Up 1.8% - Should You Buy?",
    "Arista Networks (NYSE:ANET) Trading Down 1.8% - Time to Sell?",
    "Lockheed Martin (NYSE:LMT) Trading 2.4% Higher - Still a Buy?",
    "TCS dividend announcement, record date, Q1 payout history and results preview",
    "TCS dividend announcement amount, time today: Record date, March 2026 quarter details",
    "Analog Devices (ADI) Wins Higher Price Target as AI Chip Boom Accelerates",
    "Is Texas Capital Bancshares (TCBI) Fairly Valued On the Latest View",
    "3 Reasons to Buy This Dividend Stock Right Now",
    "Nvidia stock: Here's why it could keep climbing",
    "Phillips 66's Quarterly Earnings Preview: What You Need to Know",
    # analyst rating upgrades/downgrades DO drop (scoped to rating/stock context)
    "UBS downgrades Truist Financial stock rating on CEO change uncertainty",
    "Raymond James upgrades Weyerhaeuser stock rating on valuation",
    "Royal Caribbean Cruises (NYSE:RCL) Upgraded to Strong-Buy at BMO Capital Markets",
    # how-to guides + thought-leadership / educational content (not an event)
    "How to protect yourself from evolving cyber threats",
    "Transforming Computer System Validation In The Life Sciences Industry",
    "The Future of AI in Enterprise Software",
    "A Guide to Building a Dividend Portfolio",
    # price-move / market-reaction after-effects with no material cause named
    "IT Stocks Rally: Infosys, LTM, Tech Mahindra Shares Jump Over 4% After TCS Q1 Meets Estimates",
    "Micron stock falls 6% despite strong quarter",
    "Nvidia shares climb 3% in early trading",
]

# --- must NOT be dropped: a hard material event is named (defer to LLM, don't blind-drop)
MATERIAL_OR_DEFER = [
    "EasyJet shares soar 10% on Castlelake's $7.3B takeover bid for budget airline",
    "Is Vertex Pharmaceuticals (VRTX) Undervalued After CASGEVY Won Expanded FDA Approval?",
    "U.S. Bancorp (USB) Could Be 40% Undervalued On Dividend Hike And Stress Test Result",
    "Crinetics Stock Soars 99% After Vertex Agrees to Buy Biotech Company",
    "Pfizer to acquire Seagen for $43 billion",
    "Boeing wins $10B contract from Delta Air Lines",
    "Apple reports Q3 earnings beat and raises full-year guidance",
    "FDA approves Eli Lilly's new weight-loss drug",
    "Intel to lay off 15,000 workers in restructuring",
    "Boeing CEO steps down amid safety probe",
    # measured false-drops (validate-nonnews-drops workflow) — real events wrapped in
    # an "upgrade"/opinion/logistics hook must NOT be blind-dropped:
    "Bunge upgrades soybean facility in Italy",                                    # capex, not an analyst upgrade
    "Seaspan and Maersk Expand Fleet Upgrades to Improve Bunker Fuel Efficiency",  # fleet capex
    "GE HealthCare (GEHC) Introduces AI-Driven Upgrade Pathways to Modernize Legacy Imaging Suites",  # product intro
    "Samsung's Preliminary Quarterly Profit Just Jumped 19-Fold -- and Micron Stock Fell on the News. Here's Why.",  # earnings result
    "Monster Beverage to double shares in 2-for-1 stock split",                    # corporate action
    "Broadcom Is 24% Off Its High and Just Unveiled a Custom AI Chip With OpenAI. Time to Buy the Dip?",  # product/partnership
    "Is Air Products And Chemicals (APD) Undervalued After Dropping Its Louisiana Clean Energy Project?",  # project cancellation
    "American International Group (AIG) Could Be 8% Undervalued Following Executive Appointments",  # leadership change
    "Argus raises Stryker stock price target on cyberattack recovery",              # cyberattack incident
    "Labor dispute threatens the future of geothermal power and lithium extraction",  # labor dispute, not a "future of" puff piece
    # price-moves that DO name a material cause must defer, not blind-drop:
    "EasyJet shares soar 10% on Castlelake's $7.3B takeover bid for budget airline",  # move + $-takeover
    "TCS Q1 results 2026: Net profit rises to Rs 13,349 cr, revenue rises 14%",       # fundamental (revenue), not a share-price move
]


@pytest.mark.parametrize("headline", NON_NEWS)
def test_drops_non_news_commentary(headline):
    assert _looks_like_non_news(headline) is True, f"should drop: {headline!r}"


@pytest.mark.parametrize("headline", MATERIAL_OR_DEFER)
def test_never_blind_drops_material_events(headline):
    assert _looks_like_non_news(headline) is False, f"must NOT drop (material named): {headline!r}"


def test_empty_and_none_are_safe():
    assert _looks_like_non_news("") is False
    assert _looks_like_non_news(None) is False
