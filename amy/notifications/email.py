"""SMTP email delivery for high-priority notifications.

Behaviour when SMTP is not configured (SMTP_HOST not set):
  - send_email() returns False immediately, no exception raised.
  - Callers log the skip and continue — the notification still exists in-app.
  - Nothing fails loudly; the app degrades to in-app-only mode silently.

Required env vars for email delivery:
  SMTP_HOST   — mail server hostname (e.g. "smtp.gmail.com")
  SMTP_PORT   — port (default 587 for TLS / STARTTLS)
  SMTP_USER   — login username
  SMTP_PASS   — login password
  SMTP_FROM   — From address (defaults to SMTP_USER if unset)

Only HIGH-priority notifications are emailed (budget overages, bills due < 3d).
"""
from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage


def smtp_configured() -> bool:
    return bool(os.environ.get("SMTP_HOST", "").strip())


def send_email(to: str, subject: str, body: str) -> bool:
    """Send a plain-text email. Returns True on success, False on any failure.

    Never raises — all exceptions are swallowed so callers don't need try/except.
    When SMTP is not configured, returns False immediately without any I/O.
    """
    if not smtp_configured():
        return False   # silently degrade to in-app-only

    host = os.environ["SMTP_HOST"].strip()
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER", "")
    password = os.environ.get("SMTP_PASS", "")
    from_addr = os.environ.get("SMTP_FROM", user) or user

    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = from_addr
        msg["To"] = to
        msg.set_content(body)

        with smtplib.SMTP(host, port, timeout=10) as smtp:
            smtp.ehlo()
            smtp.starttls()
            if user and password:
                smtp.login(user, password)
            smtp.send_message(msg)
        return True
    except Exception:
        return False


def maybe_send_alert(to: str, notification: dict) -> bool:
    """Send an email alert for a high-priority notification, if SMTP is configured.

    Returns True if email was sent, False if skipped (no SMTP or low priority).
    """
    if notification.get("priority") != "high":
        return False
    subject = f"[Amy] {notification['title']}"
    body = (
        f"{notification['body']}\n\n"
        "— Amy Personal OS\n"
        "(To manage your notification preferences, visit your Amy dashboard.)"
    )
    return send_email(to, subject, body)
