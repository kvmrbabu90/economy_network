"""Regression tests for the morning-brief filter parsing + graph-gate.

The original bug: Claude wraps its JSON array in conversational prose ("Most of
this batch is opinion… One qualifies:\n\n[…]"). A strict json.loads on that threw,
so the endpoint silently fell back to RAW, unfiltered, 15-word-truncated headlines.
The fix routes the output through the impact engine's tolerant _parse_llm_json.

Later: a graph-gate drops headlines whose primary entity isn't a node (so the
brief only contains items the tool can actually seed a trace on).
"""
from __future__ import annotations

import sqlite3

import pytest

from api import news


# Parser tests bypass the graph-gate (it needs the real DB) — they only exercise
# the tolerant JSON extraction.
def _bypass_gate(monkeypatch):
    monkeypatch.setattr(news, "_graph_gate", lambda headlines: headlines)


def test_filter_parses_json_after_prose_preamble(monkeypatch):
    _bypass_gate(monkeypatch)
    raw = [{"title": "Samsung cuts chip output", "source": "CNBC",
            "url": "http://x/1", "pub_date": "2026-06-21"}]
    preamble = (
        "Most of this batch is opinion, box office, and personal finance. "
        "One headline has a seed:\n\n"
        '[{"text": "Samsung cuts chip output", "source": "CNBC", "url": "http://x/1", "entity": "Samsung"}]'
    )
    monkeypatch.setattr(news, "_claude_call", lambda prompt, **k: preamble)
    out = news._filter_with_claude(raw)
    assert len(out) == 1
    assert out[0]["text"] == "Samsung cuts chip output"
    assert out[0]["url"] == "http://x/1"
    assert out[0]["pub_date"] == "2026-06-21"   # restored from the raw item


def test_filter_handles_markdown_fenced_json(monkeypatch):
    _bypass_gate(monkeypatch)
    raw = [{"title": "X buys Y", "source": "S", "url": "http://x/1", "pub_date": None}]
    fenced = '```json\n[{"text": "X acquires Y", "source": "S", "url": "http://x/1", "entity": "X"}]\n```'
    monkeypatch.setattr(news, "_claude_call", lambda prompt, **k: fenced)
    out = news._filter_with_claude(raw)
    assert out and out[0]["text"] == "X acquires Y"


def test_filter_empty_on_no_array(monkeypatch):
    _bypass_gate(monkeypatch)
    # Claude responded but with no JSON array (e.g. "nothing qualifies") → a VALID
    # empty result, not a failure. Returns [] (does not raise).
    monkeypatch.setattr(news, "_claude_call", lambda prompt, **k: "Nothing qualifies today.")
    assert news._filter_with_claude([{"title": "t", "source": "s", "url": "u", "pub_date": None}]) == []


def test_filter_raises_when_claude_unavailable(monkeypatch):
    # Empty CLI output = the CLI itself failed (non-zero exit / 401 / timeout).
    # Must RAISE so the caller serves an empty brief rather than raw junk.
    monkeypatch.setattr(news, "_claude_call", lambda prompt, **k: "")
    with pytest.raises(RuntimeError):
        news._filter_with_claude([{"title": "t", "source": "s", "url": "u", "pub_date": None}])


# --- Graph-gate -------------------------------------------------------------

def test_entity_resolves_across_types():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE nodes (id TEXT, name TEXT, type TEXT)")
    conn.execute("CREATE TABLE aliases (node_id TEXT, alias_normalized TEXT)")
    conn.execute("INSERT INTO nodes VALUES ('cik:1', 'Apple Inc.', 'Company')")
    conn.execute("INSERT INTO nodes VALUES ('commodity:wheat', 'Wheat', 'Commodity')")
    conn.execute("INSERT INTO aliases VALUES ('cik:1', 'aapl')")
    assert news._entity_resolves(conn, "Wheat")          # exact name
    assert news._entity_resolves(conn, "apple")          # starts-with → "apple inc."
    assert news._entity_resolves(conn, "aapl")           # alias
    assert not news._entity_resolves(conn, "Zzzcorp")    # absent
    assert not news._entity_resolves(conn, "")           # empty


def test_graph_gate_drops_unresolvable(monkeypatch):
    if not news._DB_PATH.exists():
        pytest.skip("econgraph.db missing")
    # Only "Apple" resolves; SpaceX and the empty-entity item are dropped.
    monkeypatch.setattr(news, "_entity_resolves", lambda conn, name: name == "Apple")
    out = news._graph_gate([
        {"text": "a", "entity": "Apple"},
        {"text": "b", "entity": "SpaceX"},
        {"text": "c", "entity": ""},
    ])
    assert [h["text"] for h in out] == ["a"]
