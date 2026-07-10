"""LIFE AUTOPILOT L7 — the two new UI-support routes
(/api/life/habits-overview, /api/life/health/targets), via TestClient.
Mirrors test_connectors_status.py's fixture pattern."""
from __future__ import annotations

import os
import tempfile
import uuid

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def app_client():
    data_dir = tempfile.mkdtemp(prefix="amy_life_ui_test_")
    os.environ["AMY_SAAS_DATA"] = data_dir
    from amy.saas.app import app
    from amy.saas.db import init_db
    from amy.saas import tenancy
    init_db()
    c = TestClient(app)
    email = f"lifeui-{uuid.uuid4().hex[:8]}@test.com"
    r = c.post("/auth/signup", json={"email": email, "password": "test1234"})
    assert r.status_code == 200, r.text
    token = r.json()["token"]
    uid = r.json()["user"]["id"]
    tenancy.ensure_dirs(uid)
    return c, {"Authorization": f"Bearer {token}"}, uid, data_dir


def test_habits_overview_empty(app_client):
    c, headers, uid, data_dir = app_client
    r = c.get("/api/life/habits-overview", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json() == {"habits": []}


def test_habits_overview_includes_streak_grace_and_link_fields(app_client):
    c, headers, uid, data_dir = app_client
    r = c.post("/api/habits", json={"title": "Read", "frequency": "daily"}, headers=headers)
    assert r.status_code == 200, r.text
    hid = r.json()["id"]

    r = c.get("/api/life/habits-overview", headers=headers)
    assert r.status_code == 200, r.text
    habits = r.json()["habits"]
    assert len(habits) == 1
    h = habits[0]
    assert h["id"] == hid
    assert "streak_grace" in h
    assert h["linked"] is False
    assert h["signal_type"] is None

    r = c.post(f"/api/life/habits/{hid}/link",
              json={"signal_type": "reading_minutes", "signal_params": {}, "mode": "auto_complete"},
              headers=headers)
    assert r.status_code == 200, r.text

    r = c.get("/api/life/habits-overview", headers=headers)
    h = r.json()["habits"][0]
    assert h["linked"] is True
    assert h["signal_type"] == "reading_minutes"
    assert h["mode"] == "auto_complete"


def test_health_targets_route_honest_unavailable(app_client):
    c, headers, uid, data_dir = app_client
    r = c.get("/api/life/health/targets", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["available"] is False


def test_wellbeing_route_empty_list(app_client):
    c, headers, uid, data_dir = app_client
    r = c.get("/api/life/wellbeing", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json() == {"weeks": []}
