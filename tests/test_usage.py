from __future__ import annotations

from schema import store


def _seed(conn, rows):
    """rows: (ts, input, output, cache, cost)"""
    for ts, i, o, c, cost in rows:
        conn.execute(
            "INSERT INTO llm_usage (ts, model, input_tokens, output_tokens, cache_read_tokens, cost_usd) "
            "VALUES (?,?,?,?,?,?)", (ts, "claude", i, o, c, cost))
    conn.commit()


def test_usage_buckets_daily():
    conn = store.connect(":memory:"); store.init_db(conn)
    _seed(conn, [
        ("2026-07-06T10:00:00", 100, 20, 5, 0.01),
        ("2026-07-06T14:30:00", 200, 40, 0, 0.02),   # same day
        ("2026-07-07T09:00:00", 50, 10, 1, 0.005),   # next day
    ])
    b = store.usage_buckets(conn, "day", since_days=365)
    assert [x["bucket"] for x in b] == ["2026-07-06", "2026-07-07"]
    assert b[0]["input_tokens"] == 300 and b[0]["output_tokens"] == 60
    assert b[0]["cache_read_tokens"] == 5 and b[0]["calls"] == 2
    assert abs(b[0]["cost_usd"] - 0.03) < 1e-9
    assert b[1]["input_tokens"] == 50 and b[1]["calls"] == 1


def test_usage_buckets_hourly():
    conn = store.connect(":memory:"); store.init_db(conn)
    _seed(conn, [
        ("2026-07-07T09:15:00", 10, 2, 0, 0.001),
        ("2026-07-07T09:45:00", 20, 4, 0, 0.002),    # same hour
        ("2026-07-07T10:05:00", 30, 6, 0, 0.003),    # next hour
    ])
    b = store.usage_buckets(conn, "hour", since_days=365)
    assert [x["bucket"] for x in b] == ["2026-07-07T09:00", "2026-07-07T10:00"]
    assert b[0]["input_tokens"] == 30 and b[0]["calls"] == 2


def test_usage_buckets_validates_granularity():
    conn = store.connect(":memory:"); store.init_db(conn)
    import pytest
    with pytest.raises(ValueError):
        store.usage_buckets(conn, "minute", since_days=1)


def test_record_llm_usage_inserts_and_is_failsafe(tmp_path):
    db = tmp_path / "u.db"
    conn = store.connect(db); store.init_db(conn); conn.close()
    store.record_llm_usage({"input_tokens": 12, "output_tokens": 3, "cache_read_tokens": 1,
                            "cost_usd": 0.004, "model": "claude"}, db_path=db)
    conn = store.connect(db)
    row = conn.execute("SELECT input_tokens, output_tokens, cost_usd FROM llm_usage").fetchone()
    assert row["input_tokens"] == 12 and row["output_tokens"] == 3 and abs(row["cost_usd"] - 0.004) < 1e-9
    # fail-safe: a bad path must not raise
    store.record_llm_usage({"input_tokens": 1}, db_path=tmp_path / "nope" / "x.db")


def test_migration_adds_llm_usage_table():
    conn = store.connect(":memory:")
    conn.execute("CREATE TABLE nodes (id TEXT PRIMARY KEY)")   # minimal pre-existing DB
    conn.commit()
    store._migrate_llm_usage(conn)
    assert conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='llm_usage'").fetchone()
    store._migrate_llm_usage(conn)   # idempotent


def test_prune_llm_usage():
    conn = store.connect(":memory:"); store.init_db(conn)
    _seed(conn, [
        ("2026-01-01T00:00:00", 1, 1, 0, 0.0),        # old → pruned
        ("2099-01-01T00:00:00", 1, 1, 0, 0.0),        # future → kept
    ])
    store.prune_llm_usage(conn, older_than_days=1)     # keep only ts >= now-1d
    assert conn.execute("SELECT COUNT(*) FROM llm_usage WHERE ts < '2026-06-01'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM llm_usage").fetchone()[0] == 1   # future row remains
