"""/reliability/summary + /reliability/classes endpoints."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from api import main as api_main
from pipeline import reliability as reliability_mod
from schema import store


@pytest.fixture
def client(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    conn = store.connect(str(db))
    store.init_db(conn)
    store.replace_signal_reliability(conn, [
        {"hop": 1, "direction": "positive", "mag_band": "high", "horizon_days": 1, "clean": 1,
         "n": 10, "hit_rate": 0.7, "ci_low": 0.4, "ci_high": 0.9, "mean_abn": 0.01,
         "median_abn": 0.008, "ic": 0.3, "computed_at": "2026-10-10T00:00:00+00:00"},
        {"hop": 2, "direction": "negative", "mag_band": "med", "horizon_days": 1, "clean": 0,
         "n": 4, "hit_rate": 0.5, "ci_low": 0.15, "ci_high": 0.85, "mean_abn": -0.002,
         "median_abn": -0.001, "ic": None, "computed_at": "2026-10-10T00:00:00+00:00"},
    ])
    conn.commit()
    conn.close()
    api_main.set_db_path(db)
    monkeypatch.setattr(reliability_mod, "_SCORECARD", tmp_path / "scorecard.json")
    return TestClient(api_main.app)


def test_summary_not_computed_when_no_scorecard(client):
    r = client.get("/reliability/summary")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "not_computed"
    assert body["coverage"]["outcome_rows"] == 0


def test_summary_serves_scorecard(client, tmp_path):
    (tmp_path / "scorecard.json").write_text(json.dumps({
        "generated_at": "2026-10-10T00:00:00+00:00",
        "overall": {"1": {"n": 10, "hits": 7, "hit_rate": 0.7, "ci_low": 0.4, "ci_high": 0.9,
                          "mean_abn": 0.01, "median_abn": 0.008, "ic": 0.3}},
        "by_hop_horizon": [], "by_magband": [], "coverage": {"outcome_rows": 10},
        "classes": [],
    }), encoding="utf-8")
    r = client.get("/reliability/summary")
    assert r.status_code == 200 and r.json()["status"] == "ok"
    assert r.json()["overall"]["1"]["hit_rate"] == 0.7


def test_classes_endpoint_filters(client):
    assert client.get("/reliability/classes").json()["count"] == 2
    r = client.get("/reliability/classes", params={"hop": 1})
    assert r.json()["count"] == 1 and r.json()["classes"][0]["hop"] == 1
    assert client.get("/reliability/classes", params={"clean": 1}).json()["count"] == 1
    assert client.get("/reliability/classes", params={"horizon": 99}).json()["count"] == 0
