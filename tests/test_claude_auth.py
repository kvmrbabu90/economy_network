"""_claude_call must distinguish a logged-out CLI (raise ClaudeAuthError so batch jobs defer)
from an ordinary error result (tolerant empty string, as before)."""
from __future__ import annotations

import json

import pytest

from api import impact


class _FakeProc:
    def __init__(self, stdout=b"", stderr=b"", returncode=0):
        self._out, self._err, self.returncode = stdout, stderr, returncode

    def __enter__(self): return self
    def __exit__(self, *a): return False
    def communicate(self, timeout=None): return (self._out, self._err)


def _patch_cli(monkeypatch, envelope=None, stderr=b"", returncode=0):
    monkeypatch.setattr(impact, "_resolve_claude_binary", lambda: "claude")
    out = json.dumps(envelope).encode() if envelope is not None else b""
    monkeypatch.setattr(impact.subprocess, "Popen", lambda *a, **k: _FakeProc(out, stderr, returncode))


def test_logged_out_envelope_raises_auth_error(monkeypatch):
    _patch_cli(monkeypatch, {"is_error": True, "result": "Not logged in · Please run /login"})
    with pytest.raises(impact.ClaudeAuthError):
        impact._claude_call("hi")


def test_logged_out_on_nonzero_exit_raises(monkeypatch):
    _patch_cli(monkeypatch, envelope=None, stderr=b"Not logged in \xc2\xb7 Please run /login", returncode=1)
    with pytest.raises(impact.ClaudeAuthError):
        impact._claude_call("hi")


def test_ordinary_is_error_returns_empty_not_raise(monkeypatch):
    # a non-auth error stays the tolerant empty-string path (callers treat "" like an Ollama miss)
    _patch_cli(monkeypatch, {"is_error": True, "result": "Model temporarily overloaded"})
    assert impact._claude_call("hi") == ""


def test_logged_out_regex_is_narrow():
    r = impact._CLAUDE_LOGGED_OUT_RE
    assert r.search("Not logged in · Please run /login")
    assert r.search("please run /login")
    assert r.search("Please login")
    assert not r.search("The user logged in successfully and saw the dashboard")  # ordinary text
