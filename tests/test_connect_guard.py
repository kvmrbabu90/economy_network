"""connect(None)/connect("") must fall back to the canonical DB, not str()->"None" into a
stray empty database file (the footgun that read as "the whole graph vanished")."""
from __future__ import annotations

from schema import store


def _opened_file(conn):
    return conn.execute("PRAGMA database_list").fetchone()["file"]


def test_connect_none_falls_back_to_default(monkeypatch, tmp_path):
    fake = tmp_path / "fallback.db"
    monkeypatch.setattr(store, "default_db_path", lambda: str(fake))
    conn = store.connect(None)
    try:
        assert _opened_file(conn).endswith("fallback.db")   # not a stray 'None' file
    finally:
        conn.close()


def test_connect_empty_string_falls_back_to_default(monkeypatch, tmp_path):
    fake = tmp_path / "fallback.db"
    monkeypatch.setattr(store, "default_db_path", lambda: str(fake))
    conn = store.connect("")
    try:
        assert _opened_file(conn).endswith("fallback.db")
    finally:
        conn.close()
