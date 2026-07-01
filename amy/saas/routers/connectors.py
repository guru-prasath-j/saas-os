"""Google OAuth connector and generic connector-registry routes."""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from ..db import User
from .. import paths, security
from ..deps import current_user, _connector_dir, _journal_user

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
