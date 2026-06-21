"""Regression tests for the morning-brief filter parsing.

The bug: Claude wraps its JSON array in conversational prose ("Most of this batch
is opinion… One qualifies:\n\n[…]"). A strict json.loads on that threw, so the
endpoint silently fell back to RAW, unfiltered, 15-word-truncated headlines —
letting opinion through and cutting headlines mid-sentence. The fix routes the
output through the impact engine's tolerant _parse_llm_json. These tests lock it.
"""
from __future__ import annotations

from api import news


def test_filter_parses_json_after_prose_preamble(monkeypatch):
    raw = [{"title": "Samsung cuts chip output", "source": "CNBC",
            "url": "http://x/1", "pub_date": "2026-06-21"}]
    preamble = (
        "Most of this batch is opinion, box office, and personal finance. "
        "One headline has a seed:\n\n"
        '[{"text": "Samsung cuts chip output", "source": "CNBC", "url": "http://x/1"}]'
    )
    monkeypatch.setattr(news, "_claude_call", lambda prompt, **k: preamble)
    out = news._filter_with_claude(raw)
    assert len(out) == 1
    assert out[0]["text"] == "Samsung cuts chip output"
    assert out[0]["url"] == "http://x/1"
    assert out[0]["pub_date"] == "2026-06-21"   # restored from the raw item


def test_filter_handles_markdown_fenced_json(monkeypatch):
    raw = [{"title": "X buys Y", "source": "S", "url": "http://x/1", "pub_date": None}]
    fenced = '```json\n[{"text": "X acquires Y", "source": "S", "url": "http://x/1"}]\n```'
    monkeypatch.setattr(news, "_claude_call", lambda prompt, **k: fenced)
    out = news._filter_with_claude(raw)
    assert out and out[0]["text"] == "X acquires Y"


def test_filter_empty_on_unparseable(monkeypatch):
    # No JSON array at all → empty (caller then applies its own raw fallback).
    monkeypatch.setattr(news, "_claude_call", lambda prompt, **k: "sorry, nothing qualifies today")
    assert news._filter_with_claude([{"title": "t", "source": "s", "url": "u", "pub_date": None}]) == []
