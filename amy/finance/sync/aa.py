"""Account Aggregator (AA) stub.

India's Account Aggregator framework (RBI-regulated) requires:
  1. Registration as a Financial Information User (FIU) with an AA operator
     (Setu AA, Finvu, OneMoney, CAMS FinServ, etc.)
  2. API credentials: Client ID + Client Secret from the AA operator
  3. Consent UI: redirect user to AA consent screen, receive handle via callback
  4. JWS-signed API requests using a registered X.509 certificate
  5. Webhook to receive encrypted FI data push
  6. ECDH key pair (P-256) for decrypting the received data
  7. FI data decryption + schema parsing per RBI FI data standards

None of this can be built without external credentials and RBI registration.
This module provides:
  - A config slot via env vars so a real integration can be dropped in later
  - AAProvider implementing the SyncProvider interface
  - A status() method describing what's missing

Environment variables to wire in a real provider:
  AA_PROVIDER        — e.g. "setu", "finvu", "onemoney"  (required)
  AA_CLIENT_ID       — your FIU client ID from the AA operator  (required)
  AA_CLIENT_SECRET   — your FIU client secret  (required)
  AA_BASE_URL        — sandbox/prod base URL (defaults per provider if known)
  AA_REDIRECT_URI    — callback URI for consent redirect
  AA_WEBHOOK_SECRET  — for verifying signed webhook payloads
"""
from __future__ import annotations

import os
from typing import TYPE_CHECKING

from . import SyncProvider, SyncResult

if TYPE_CHECKING:
    from ..engine import FinanceEngine


# ---------------------------------------------------------------------------
# Config (read at import time so tests can patch os.environ)
# ---------------------------------------------------------------------------

def _cfg() -> dict[str, str]:
    return {
        "provider":       os.environ.get("AA_PROVIDER", ""),
        "client_id":      os.environ.get("AA_CLIENT_ID", ""),
        "client_secret":  os.environ.get("AA_CLIENT_SECRET", ""),
        "base_url":       os.environ.get("AA_BASE_URL", ""),
        "redirect_uri":   os.environ.get("AA_REDIRECT_URI", ""),
        "webhook_secret": os.environ.get("AA_WEBHOOK_SECRET", ""),
    }


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class AANotConfiguredError(Exception):
    """Raised when AA credentials are not present."""


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------

class AAProvider(SyncProvider):
    """
    Stub AA provider.  Call status() to inspect configuration.
    sync() raises AANotConfiguredError until real credentials are set.
    """
    method = "aa"

    def available(self) -> bool:
        cfg = _cfg()
        return bool(cfg["provider"] and cfg["client_id"] and cfg["client_secret"])

    def status(self) -> dict:
        cfg = _cfg()
        if self.available():
            return {
                "configured": True,
                "provider": cfg["provider"],
                "base_url": cfg["base_url"] or "(default for provider)",
                "has_redirect_uri": bool(cfg["redirect_uri"]),
                "has_webhook_secret": bool(cfg["webhook_secret"]),
            }

        missing = [k for k in ("provider", "client_id", "client_secret")
                   if not cfg[k]]
        return {
            "configured": False,
            "missing_env_vars": [f"AA_{k.upper()}" for k in missing],
            "setup_steps": [
                "1. Register as a Financial Information User (FIU) with an RBI-approved AA operator "
                "(Setu — sahamati.org.in, Finvu, OneMoney, CAMS FinServ, etc.)",
                "2. Complete KYC and technical onboarding with the chosen operator",
                "3. Generate / receive API credentials (Client ID + Secret)",
                "4. Set environment variables: AA_PROVIDER, AA_CLIENT_ID, AA_CLIENT_SECRET",
                "5. Optionally set AA_BASE_URL (sandbox vs prod), AA_REDIRECT_URI, AA_WEBHOOK_SECRET",
            ],
            "why_required": (
                "Account Aggregator data flow requires signed consent from the user, "
                "encrypted data delivery via webhook, and ECDH decryption — "
                "all of which depend on a live FIU registration."
            ),
        }

    def sync(
        self,
        engine: "FinanceEngine",
        account_id: str,
        consent_handle: str | None = None,
    ) -> SyncResult:
        """
        Initiate an AA data fetch for the given account.

        Real implementation (once configured) would:
          1. Validate consent_handle (or create a new consent request)
          2. POST /FI/request with signed JWS body
          3. Await webhook callback with encrypted FI data
          4. Decrypt via ECDH, parse per RBI FI data schema
          5. Map FI transactions → FinanceEngine.add_transaction(...)

        Until configured, raises AANotConfiguredError.
        """
        if not self.available():
            st = self.status()
            raise AANotConfiguredError(
                "AA not configured. Missing: "
                + ", ".join(st.get("missing_env_vars", []))
                + ". See GET /api/finance/accounts/{id}/sync/aa/status"
            )

        # When credentials exist but real logic isn't wired yet:
        raise NotImplementedError(
            "Real AA data fetch requires FIU registration and a live webhook endpoint. "
            "Wire the full flow in sync/aa_live.py once credentials are available."
        )
