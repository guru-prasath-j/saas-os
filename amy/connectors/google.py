"""Real Google data-source providers — Gmail, Google Calendar, Google Tasks.

Implements the same Connector interface as the local providers, so they drop into
the registry with no caller changes. Private-only (never exposed in public mode).

Setup (per user):
  1. pip install google-api-python-client google-auth google-auth-oauthlib
  2. Create OAuth creds in Google Cloud, run the consent flow once, and save the
     resulting authorized-user token JSON to:  <user_connector_dir>/google_token.json
  3. The registry auto-detects the token and uses Google; otherwise falls back to
     the local file providers.

All Google libraries are imported lazily so the app runs fine without them.
"""
from __future__ import annotations

import datetime as _dt
import os

from .base import Connector, Item

# read-only scopes, except spreadsheets (needed to append custodial-account
# disbursement rows to the user's own existing Sheet — never used to create
# or restructure sheets, only append_disbursement_row's values().append)
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.events",  # read + create/edit events
    "https://www.googleapis.com/auth/tasks.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
]
TOKEN_FILENAME = "google_token.json"


def load_credentials(token_path: str):
    """Load an authorized-user token, refreshing if needed. Returns creds or None."""
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        if not os.path.exists(token_path):
            return None
        # Don't pass SCOPES here — avoids rejecting tokens that have a
        # superset of scopes (e.g. old token with calendar.readonly + new calendar.events)
        creds = Credentials.from_authorized_user_file(token_path)
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        return creds if (creds and creds.valid) else None
    except Exception:
        return None


class _GoogleBase(Connector):
    private_only = True

    def __init__(self, creds):
        self._creds = creds

    def _service(self, name, version):
        from googleapiclient.discovery import build
        return build(name, version, credentials=self._creds, cache_discovery=False)


class GmailProvider(_GoogleBase):
    kind = "email"

    def list(self, limit: int = 50) -> list[Item]:
        svc = self._service("gmail", "v1")
        res = svc.users().messages().list(userId="me", maxResults=limit, q="in:inbox").execute()
        items = []
        for m in res.get("messages", [])[:limit]:
            msg = svc.users().messages().get(
                userId="me", id=m["id"], format="metadata",
                metadataHeaders=["Subject", "From", "Date"]).execute()
            h = {x["name"]: x["value"] for x in msg.get("payload", {}).get("headers", [])}
            items.append(Item(kind="email", id=m["id"], title=h.get("Subject", "(no subject)"),
                              body=msg.get("snippet", ""), ts=h.get("Date", ""),
                              meta={"from": h.get("From", "")}))
        return items


class GoogleCalendarProvider(_GoogleBase):
    kind = "calendar"

    def list(self, limit: int = 50) -> list[Item]:
        svc = self._service("calendar", "v3")
        now = _dt.datetime.now(_dt.timezone.utc).isoformat()
        res = svc.events().list(calendarId="primary", timeMin=now, maxResults=limit,
                                singleEvents=True, orderBy="startTime").execute()
        out = []
        for e in res.get("items", []):
            start = e.get("start", {})
            # Meet join link, when the event has one — no separate Meet API/Workspace
            # account needed, conferenceData rides along on the normal calendar.events
            # scope/response already used here.
            meet_url = ""
            for ep in (e.get("conferenceData") or {}).get("entryPoints", []):
                if ep.get("entryPointType") == "video":
                    meet_url = ep.get("uri", "")
                    break
            out.append(Item(kind="calendar", id=e.get("id", ""), title=e.get("summary", "(busy)"),
                            body=e.get("description", ""),
                            ts=start.get("dateTime") or start.get("date", ""),
                            meta={"location": e.get("location", ""), "meet_url": meet_url}))
        return out


class GoogleTasksProvider(_GoogleBase):
    kind = "tasks"

    def list(self, limit: int = 50) -> list[Item]:
        svc = self._service("tasks", "v1")
        res = svc.tasks().list(tasklist="@default", maxResults=limit, showCompleted=False).execute()
        return [Item(kind="tasks", id=t.get("id", ""), title=t.get("title", ""),
                     body=t.get("notes", ""), ts=t.get("due", ""),
                     meta={"status": t.get("status", "")}) for t in res.get("items", [])]


def build_google_providers(data_dir) -> dict:
    """Return {kind: provider} if a valid Google token exists for this user, else {}."""
    creds = load_credentials(os.path.join(str(data_dir), TOKEN_FILENAME))
    if not creds:
        return {}
    return {
        "email": GmailProvider(creds),
        "calendar": GoogleCalendarProvider(creds),
        "tasks": GoogleTasksProvider(creds),
    }
