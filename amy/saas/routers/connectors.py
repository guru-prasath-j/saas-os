"""Google OAuth connector and generic connector-registry routes."""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from ..db import User
from .. import paths, security
from ..deps import current_user, _connector_dir, _journal_user, _collab_db_path

router = APIRouter()


def _google_token_path(user: "User") -> Path:
    d = _connector_dir(user)
    d.mkdir(parents=True, exist_ok=True)
    return d / "google_token.json"


def _google_oauth_flow(redirect_uri: str):
    from google_auth_oauthlib.flow import Flow
    client_id = os.getenv("GOOGLE_CLIENT_ID", "")
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        raise HTTPException(status_code=501,
                            detail="GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET not set")
    from ...connectors.google import SCOPES
    extra_scopes = [
        "openid",
        "https://www.googleapis.com/auth/userinfo.email",
        "https://www.googleapis.com/auth/userinfo.profile",
    ]
    return Flow.from_client_config(
        {"web": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [redirect_uri],
        }},
        scopes=SCOPES + extra_scopes,
        redirect_uri=redirect_uri,
    )


@router.get("/api/connectors/google/status")
def google_status(user: User = Depends(current_user)):
    from ...connectors.google import load_credentials
    token = _google_token_path(user)
    creds = load_credentials(str(token))
    if not creds:
        return {"connected": False, "services": []}
    available = []
    for svc, api, ver in [("gmail", "gmail", "v1"), ("calendar", "calendar", "v3"),
                           ("tasks", "tasks", "v1")]:
        try:
            from googleapiclient.discovery import build
            build(api, ver, credentials=creds, cache_discovery=False)
            available.append(svc)
        except Exception:
            available.append(svc)
    return {"connected": True, "services": available,
            "email": getattr(creds, "token", None) and "linked"}


@router.get("/api/connectors/google/auth")
def google_auth_start(request: Request, token: str = "",
                      authorization: str = Header("")):
    import json, base64
    from fastapi.responses import RedirectResponse
    raw = token or (authorization.split(" ", 1)[1] if " " in authorization else "")
    if not raw:
        raise HTTPException(status_code=401, detail="missing bearer token")
    try:
        security.decode_token(raw)
    except Exception:
        raise HTTPException(status_code=401, detail="invalid token")
    redirect_uri = str(request.base_url).rstrip("/") + "/api/connectors/google/callback"
    flow = _google_oauth_flow(redirect_uri)
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )
    # Encode jwt + code_verifier together in state so callback can use both
    cv = getattr(flow, "code_verifier", None) or ""
    state_payload = base64.urlsafe_b64encode(
        json.dumps({"t": raw, "cv": cv}).encode()
    ).decode()
    # Rebuild auth_url with our state replacing the one google set
    from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
    parts = urlparse(auth_url)
    params = parse_qs(parts.query, keep_blank_values=True)
    params["state"] = [state_payload]
    new_query = urlencode({k: v[0] for k, v in params.items()})
    auth_url = urlunparse(parts._replace(query=new_query))
    return RedirectResponse(auth_url)


@router.get("/api/connectors/google/callback")
def google_auth_callback(request: Request, code: str = "", state: str = "",
                         error: str = ""):
    import json, base64
    from fastapi.responses import HTMLResponse
    if error:
        return HTMLResponse(f"<script>window.location='/?google_error={error}'</script>")
    try:
        payload = json.loads(base64.urlsafe_b64decode(state + "=="))
        uid = security.decode_token(payload["t"])
        code_verifier = payload.get("cv") or None
    except Exception:
        raise HTTPException(status_code=400, detail="invalid state")

    redirect_uri = str(request.base_url).rstrip("/") + "/api/connectors/google/callback"
    flow = _google_oauth_flow(redirect_uri)
    if code_verifier:
        flow.code_verifier = code_verifier
    import os as _os
    _os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"  # allow Google to return superset of requested scopes
    flow.fetch_token(code=code)
    creds = flow.credentials

    token_path = Path(paths.index_dir(uid)) / "connectors" / "google_token.json"
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json())

    import threading

    def _bg_sync():
        try:
            from ...connectors.google import build_google_providers
            build_google_providers(token_path.parent)
            from ...collab import CollabDB
            cdb_path = str(paths.index_dir(uid) / "collab.db")
            if Path(cdb_path).exists():
                cdb = CollabDB(cdb_path)
                try:
                    from ...events.scheduler import generate_and_store
                    generate_and_store(cdb)
                finally:
                    cdb.close()
        except Exception:
            pass

    threading.Thread(target=_bg_sync, daemon=True).start()

    return HTMLResponse("""<!DOCTYPE html><html><head>
    <style>body{font-family:sans-serif;background:#04060c;color:#e2e8f0;display:flex;align-items:center;justify-content:center;height:100vh;margin:0}
    .box{text-align:center;padding:40px;background:rgba(255,255,255,.05);border-radius:20px;border:1px solid rgba(255,255,255,.1)}
    h2{color:#22d3ee;margin-bottom:8px}p{color:#9aa6bf}</style></head>
    <body><div class="box"><h2>&#10003; Google connected</h2>
    <p>Gmail, Calendar and Tasks are now synced to your memory lake.</p>
    <p style="margin-top:20px"><a href="/" style="color:#22d3ee">&larr; Back to Amy</a></p>
    </div></body></html>""")


@router.delete("/api/connectors/google")
def google_disconnect(user: User = Depends(current_user)):
    token = _google_token_path(user)
    if token.exists():
        token.unlink()
    return {"disconnected": True}


def _friendly_google_error(kind: str, exc: Exception) -> str:
    msg = str(exc)
    if "accessNotConfigured" in msg or "has not been used in project" in msg:
        return f"{kind.capitalize()} API not enabled for this Google Cloud project — enable it in Google Cloud Console, then retry."
    if "HttpError 403" in msg:
        return f"{kind.capitalize()} access denied (403) — check API is enabled and scopes were granted."
    return f"error: {msg[:200]}"


@router.post("/api/connectors/google/sync")
def google_sync_now(user: User = Depends(current_user)):
    from ...connectors.google import build_google_providers
    providers = build_google_providers(_connector_dir(user))
    if not providers:
        raise HTTPException(status_code=400, detail="Google not connected")
    results = {}
    for kind, prov in providers.items():
        try:
            items = prov.list(limit=50)
            results[kind] = len(items)
        except Exception as e:
            results[kind] = _friendly_google_error(kind, e)
    _journal_user(user)
    return {"synced": results}


# ---------------------------------------------------------------------------
# CONNECTOR COMPLETION Part 3 — unified health status for the Connectors tab
# ---------------------------------------------------------------------------

# name -> (script filename stem, port) for the four self-hosted servers
# amy/saas/app.py's supervisor loop launches (see _LOCAL_MCP_SERVERS there —
# duplicated here as a plain data tuple, not imported, since importing
# amy.saas.app at module load time from a router it itself includes would be
# a circular import; the supervisor's live process/port state IS imported,
# lazily, inside the endpoint below).
_LOCAL_MCP_DESCRIPTORS = [
    ("Job Search (jobspy)", "jobspy", 8935),
    ("HackerNews", "hackernews", 8001),
    ("YouTube", "youtube", 8003),
    ("Dev.to", "devto", 8004),
]


def _rollup(store, connector_name: str) -> dict:
    """This connector's connector_status() row (there's at most one — the
    query GROUP BYs on connector name), or {} if it's never been called."""
    rows = store.connector_status(connector_name)
    return rows[0] if rows else {}


def _google_service_status(creds, store, scope_substring: str,
                           call_connector_name: str) -> dict:
    connected = creds is not None
    granted = list(getattr(creds, "scopes", None) or [])
    scopes_ok = connected and (not granted or any(scope_substring in s for s in granted))
    rollup = _rollup(store, call_connector_name)
    return {
        "connected": connected,
        "scopes_ok": scopes_ok,
        "last_success": rollup.get("last_ok_ts"),
        "last_error": rollup.get("last_error"),
        "last_error_ts": rollup.get("last_error_ts"),
    }


@router.get("/api/connectors/status")
def connectors_status(user: User = Depends(current_user)):
    """Everything the Connectors tab renders: Google services (from the
    OAuth token), local MCP servers (from the supervisor + registered
    connector rows), and external MCP connectors (GitHub/Plane/anything
    else registered) — reachability/health from the Part 1
    connector_calls ledger, never a live call made by this endpoint itself
    (keeps status checks fast and non-blocking)."""
    from ...collab import CollabDB
    from ...automation.store import AutomationStore
    from ...connectors.google import load_credentials
    from ...learning_feed.aggregator import tool_for as _learning_source_tool
    from ...saas.db import SessionLocal, McpConnector
    from ... import tools as tool_registry
    import os as _os

    cdb = CollabDB(_collab_db_path(user))
    try:
        store = AutomationStore(cdb)

        connectors_out: list[dict] = []

        # --- Google services -------------------------------------------------
        token_path = _google_token_path(user)
        creds = load_credentials(str(token_path))
        gmail = _google_service_status(creds, store, "gmail", "gmail")
        gmail.update({"name": "Gmail", "kind": "google", "tools": [],
                      "config_warning": None})
        connectors_out.append(gmail)

        calendar = _google_service_status(creds, store, "calendar", "google_calendar")
        calendar.update({"name": "Calendar / Meet", "kind": "google", "tools": [],
                         "config_warning": None, "sync_job": "meeting_prep_scan"})
        connectors_out.append(calendar)

        sheets = _google_service_status(creds, store, "spreadsheets", "sheets")
        sheets.update({"name": "Sheets", "kind": "google", "tools": [],
                       "config_warning": None})
        connectors_out.append(sheets)

        # --- registered MCP connectors (Layer 1) ------------------------------
        db = SessionLocal()
        try:
            rows = db.query(McpConnector).filter(McpConnector.user_id == user.id).all()
        finally:
            db.close()
        registered_by_key = {r.name.strip().lower(): r for r in rows}

        # --- local MCP servers (supervisor-managed) --------------------------
        try:
            from .. import app as _amy_app   # lazy: circular at module scope
            supervisor_procs = _amy_app._local_mcp_procs
            port_open = _amy_app._port_open
        except Exception:
            supervisor_procs, port_open = {}, (lambda p: False)

        # Users register sources under free-form names ("Hacker News",
        # "Dev.to") — strip non-alphanumerics before matching descriptor keys
        # ("hackernews", "devto") or the row never links up.
        def _squash(name: str) -> str:
            return "".join(ch for ch in name.lower() if ch.isalnum())

        for label, key, port in _LOCAL_MCP_DESCRIPTORS:
            row = next((r for k, r in registered_by_key.items()
                        if key in _squash(k)), None)
            proc = supervisor_procs.get(key)
            proc_alive = bool(proc is not None and proc.poll() is None)
            reachable = proc_alive or port_open(port)
            warning = None
            if key == "youtube" and not _os.getenv("YOUTUBE_API_KEY"):
                warning = ("YOUTUBE_API_KEY not set — the server starts but "
                          "search_videos returns no results.")
            rollup = _rollup(store, key)
            connectors_out.append({
                "name": label, "kind": "local_mcp", "port": port,
                "supervisor_up": reachable, "connected": row is not None,
                "last_success": rollup.get("last_ok_ts"),
                "last_error": rollup.get("last_error"),
                "last_error_ts": rollup.get("last_error_ts"),
                "tools": [{"name": t, "risk": "read"}
                         for t in (_learning_source_tool(label) or ())],
                "config_warning": warning if row is not None else (
                    warning or f"not registered as an MCP source yet — add it "
                    f"in Account -> MCP Sources (http://localhost:{port})"),
                "sync_job": "learning_feed_refresh" if key != "jobspy" else None,
            })

        # --- external MCP connectors (GitHub/Plane/anything else) ------------
        local_keys = {k for _l, k, _p in _LOCAL_MCP_DESCRIPTORS}
        for row in rows:
            key = row.name.strip().lower()
            if any(lk in _squash(key) for lk in local_keys):
                continue   # already covered above as a local server
            prefix = "github_" if "github" in key else ("plane_" if "plane" in key else None)
            reg_tools = ([{"name": t["name"], "risk": t["risk"]}
                         for t in tool_registry.list_tools()
                         if t["name"].startswith(prefix)] if prefix else [])
            # connector_calls rows are logged under the literal names
            # "github"/"plane" (see amy/connectors/mcp_call.py call sites),
            # not the user's own connector row name — an unrecognized
            # connector (no prefix) has no ledger rows to roll up either way.
            call_name = "github" if prefix == "github_" else ("plane" if prefix == "plane_" else key)
            rollup = _rollup(store, call_name)
            connectors_out.append({
                "name": row.name, "kind": "external_mcp", "connected": True,
                "last_success": rollup.get("last_ok_ts"),
                "last_error": rollup.get("last_error"),
                "last_error_ts": rollup.get("last_error_ts"),
                "tools": reg_tools, "config_warning": None,
                "sync_job": "connector_sensor_scan" if prefix else None,
            })

        return {"connectors": connectors_out}
    finally:
        cdb.close()


@router.get("/api/connectors")
def connectors_list(user: User = Depends(current_user)):
    from ...connectors import ConnectorRegistry
    return {"connectors": ConnectorRegistry(_connector_dir(user)).kinds(),
            "note": "private mode only; blocked in public portfolio"}


@router.get("/api/connectors/{kind}")
def connector_items(kind: str, mode: str = "private", limit: int = 50,
                    user: User = Depends(current_user)):
    from ...connectors import ConnectorRegistry
    try:
        items = ConnectorRegistry(_connector_dir(user)).list(kind, mode=mode, limit=limit)
        return {"kind": kind, "mode": mode, "items": items}
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown connector '{kind}'")
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
