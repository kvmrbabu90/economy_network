"""Regression tests for the morning-brief filter parsing + graph-gate.

The original bug: Claude wraps its JSON array in conversational prose ("Most of
this batch is opinion… One qualifies:\n\n[…]"). A strict json.loads on that threw,
so the endpoint silently fell back to RAW, unfiltered, 15-word-truncated headlines.
The fix routes the output through the impact engine's tolerant _parse_llm_json.

Later: a graph-gate drops headlines whose primary entity isn't a node (so the
brief only contains items the tool can actually seed a trace on).
"""
from __future__ import annotations

import subprocess
import sqlite3
from datetime import datetime, timedelta, timezone

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


# --- UTC pub_date normalisation --------------------------------------------

def test_fmt_pub_date_converts_positive_offset_to_utc():
    # 2026-06-21 00:30 at +13:00 is still 2026-06-20 in UTC — must NOT be stamped
    # a day ahead (which would win max recency weight downstream).
    dt = datetime(2026, 6, 21, 0, 30, tzinfo=timezone(timedelta(hours=13)))
    assert news._fmt_pub_date(dt) == "2026-06-20"


def test_fmt_pub_date_converts_negative_offset_to_utc():
    # 2026-06-21 23:30 at -05:00 is 2026-06-22 in UTC.
    dt = datetime(2026, 6, 21, 23, 30, tzinfo=timezone(timedelta(hours=-5)))
    assert news._fmt_pub_date(dt) == "2026-06-22"


def test_fmt_pub_date_assumes_utc_for_naive():
    # A tz-naive datetime is assumed UTC (no shift).
    assert news._fmt_pub_date(datetime(2026, 6, 21, 12, 0)) == "2026-06-21"


def test_fmt_pub_date_none_passthrough():
    assert news._fmt_pub_date(None) is None


# --- Claude CLI process-tree kill (Windows-safe timeout) --------------------

class _FakeProc:
    """Minimal stand-in for a subprocess.Popen used as a context manager."""

    def __init__(self, *, timeout: bool = False, stdout: bytes = b"", returncode: int = 0):
        self._timeout = timeout
        self._stdout = stdout
        self._returncode = returncode
        self.returncode = returncode
        self.pid = 4242
        self.communicate_calls = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def communicate(self, timeout=None):
        self.communicate_calls += 1
        # First call raises on the timeout scenario; the drain call succeeds.
        if self._timeout and self.communicate_calls == 1:
            raise subprocess.TimeoutExpired(cmd="claude", timeout=timeout)
        return (self._stdout, b"")


def test_claude_call_timeout_kills_tree_and_returns_empty(monkeypatch):
    monkeypatch.setattr(news, "_resolve_claude_binary", lambda: "claude.exe")
    fake = _FakeProc(timeout=True)
    monkeypatch.setattr(news.subprocess, "Popen", lambda *a, **k: fake)
    killed: dict = {}
    monkeypatch.setattr(
        news.subprocess, "call",
        lambda cmd, **k: killed.setdefault("cmd", cmd) or 0,
    )
    # Force the win32 branch so we exercise the taskkill path deterministically.
    monkeypatch.setattr(news.sys, "platform", "win32")

    out = news._claude_call("prompt", timeout=1)
    assert out == ""                                  # fail-open on timeout
    assert killed["cmd"][:4] == ["taskkill", "/F", "/T", "/PID"]
    assert killed["cmd"][4] == str(fake.pid)          # kills the whole tree by PID
    assert fake.communicate_calls == 2                # timeout + bounded drain


def test_claude_call_parses_envelope_result(monkeypatch):
    import json as _json
    monkeypatch.setattr(news, "_resolve_claude_binary", lambda: "claude.exe")
    envelope = _json.dumps({"result": "hello", "is_error": False}).encode("utf-8")
    fake = _FakeProc(stdout=envelope, returncode=0)
    monkeypatch.setattr(news.subprocess, "Popen", lambda *a, **k: fake)
    assert news._claude_call("prompt") == "hello"


def test_claude_call_nonzero_exit_returns_empty(monkeypatch):
    monkeypatch.setattr(news, "_resolve_claude_binary", lambda: "claude.exe")
    fake = _FakeProc(stdout=b"boom", returncode=2)
    monkeypatch.setattr(news.subprocess, "Popen", lambda *a, **k: fake)
    assert news._claude_call("prompt") == ""
