"""Connector tests — local email/calendar/tasks providers + private/public gating.

Run:  pytest tests/test_connectors.py -v
"""
import json
import os
import tempfile

import pytest

from amy.connectors import ConnectorRegistry


def _dir():
    d = tempfile.mkdtemp(prefix="amy_conn_")
    json.dump([{"id": "1", "subject": "Hello", "snippet": "hi there", "date": "2026-06-20"}],
              open(os.path.join(d, "email.json"), "w"))
    json.dump([{"id": "e1", "summary": "Meeting", "start": "2026-06-22"}],
              open(os.path.join(d, "calendar.json"), "w"))
    json.dump([{"id": "t1", "title": "Do thing", "due": "2026-06-25"}],
              open(os.path.join(d, "tasks.json"), "w"))
    return d


def test_private_mode_reads_all_kinds():
    reg = ConnectorRegistry(_dir())
    assert reg.list("email", mode="private")[0]["title"] == "Hello"
    assert reg.list("calendar", mode="private")[0]["title"] == "Meeting"
    assert reg.list("tasks", mode="private")[0]["title"] == "Do thing"


def test_public_mode_blocks_all_private_connectors():
    reg = ConnectorRegistry(_dir())
    for kind in ("email", "calendar", "tasks"):
        with pytest.raises(PermissionError):
            reg.list(kind, mode="public")


def test_missing_file_returns_empty():
    reg = ConnectorRegistry(tempfile.mkdtemp(prefix="amy_conn_"))
    assert reg.list("email", mode="private") == []


def test_unknown_kind_raises():
    reg = ConnectorRegistry(tempfile.mkdtemp(prefix="amy_conn_"))
    with pytest.raises(KeyError):
        reg.list("slack", mode="private")


def test_google_falls_back_to_local_without_token():
    # no google_token.json -> registry uses local providers, never crashes
    reg = ConnectorRegistry(tempfile.mkdtemp(prefix="amy_conn_"))
    assert reg.source("email") == "local"
    assert reg.list("email", mode="private") == []


def test_build_google_providers_empty_without_token():
    from amy.connectors.google import build_google_providers
    assert build_google_providers(tempfile.mkdtemp(prefix="amy_conn_")) == {}
