"""CONNECTOR COMPLETION Part 3 — GET /api/connectors/status shape, with a
mocked healthy connector and a mocked failing connector. No live network
calls: the endpoint itself never makes one (it reads the connector_calls
ledger + registered rows), so nothing needs mocking beyond seeding data.
"""
from __future__ import annotations

import os
import tempfile
import uuid

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def app_client():
    data_dir = tempfile.mkdtemp(prefix="amy_conn_status_test_")
    os.environ["AMY_SAAS_DATA"] = data_dir
    from amy.saas.app import app
    from amy.saas.db import init_db
    from amy.saas import tenancy
    init_db()
    c = TestClient(app)
    # unique email per fixture invocation — the users table is process-wide
    # (amy_saas.db's engine binds once at first import), so a fixed email
    # collides across the multiple tests in this module
    email = f"connstatus-{uuid.uuid4().hex[:8]}@test.com"
    r = c.post("/auth/signup", json={"email": email, "password": "test1234"})
    assert r.status_code == 200, r.text
    token = r.json()["token"]
    uid = r.json()["user"]["id"]
    tenancy.ensure_dirs(uid)
    return c, {"Authorization": f"Bearer {token}"}, uid, data_dir


def _by_name(connectors, name):
    return next(c for c in connectors if c["name"] == name)


def test_status_shape_with_no_connectors_registered(app_client):
    c, headers, uid, data_dir = app_client
    r = c.get("/api/connectors/status", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "connectors" in body
    names = {row["name"] for row in body["connectors"]}
    assert {"Gmail", "Calendar / Meet", "Sheets", "Job Search (jobspy)",
            "HackerNews", "YouTube", "Dev.to"} <= names

    gmail = _by_name(body["connectors"], "Gmail")
    assert gmail["kind"] == "google"
    assert gmail["connected"] is False   # no token linked in this test

    hn = _by_name(body["connectors"], "HackerNews")
    assert hn["kind"] == "local_mcp"
    assert hn["connected"] is False   # not registered as an MCP source
    assert "not registered" in (hn["config_warning"] or "")

    youtube = _by_name(body["connectors"], "YouTube")
    assert "YOUTUBE_API_KEY" in (youtube["config_warning"] or "") or not youtube["connected"]


def test_status_reflects_healthy_and_failing_external_connectors(app_client):
    c, headers, uid, data_dir = app_client

    r = c.post("/api/mcp/connectors", headers=headers, json={
        "name": "GitHub", "server_url": "https://api.githubcopilot.com/mcp/x/all",
        "auth_type": "api_key", "auth_value": "ghp_dummy", "risk_tier": "official"})
    assert r.status_code == 200, r.text

    r = c.post("/api/mcp/connectors", headers=headers, json={
        "name": "Plane", "server_url": "https://mcp.plane.so/http/api-key/mcp",
        "auth_type": "api_key", "auth_value": "plane_dummy", "auth_extra": "acme",
        "risk_tier": "official"})
    assert r.status_code == 200, r.text

    # Seed the connector_calls ledger directly (this is what the sensors/
    # tools would have logged via amy/connectors/mcp_call.py in real use).
    from amy.collab import CollabDB
    from amy.automation.store import AutomationStore
    from amy.saas import paths
    cdb = CollabDB(str(paths.index_dir(uid) / "collab.db"))
    try:
        store = AutomationStore(cdb)
        store.log_connector_call(uid, "github", "list_pull_requests", True, 120)
        store.log_connector_call(uid, "plane", "list_work_items", False, 500,
                                 "HTTP 401 from https://mcp.plane.so — check your token")
    finally:
        cdb.close()

    r = c.get("/api/connectors/status", headers=headers)
    assert r.status_code == 200, r.text
    connectors = r.json()["connectors"]

    github = _by_name(connectors, "GitHub")
    assert github["kind"] == "external_mcp"
    assert github["connected"] is True
    assert github["last_success"] is not None
    assert github["last_error"] is None
    tool_names = {t["name"] for t in github["tools"]}
    assert "github_list_prs" in tool_names and "github_comment" in tool_names
    write_tools = {t["name"]: t["risk"] for t in github["tools"]}
    assert write_tools["github_comment"] == "write"

    plane = _by_name(connectors, "Plane")
    assert plane["kind"] == "external_mcp"
    assert plane["last_error"] and "401" in plane["last_error"]
    plane_tools = {t["name"] for t in plane["tools"]}
    assert "plane_create_task" in plane_tools
