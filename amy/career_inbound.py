"""Inbound HR-response detection (CAREER AUTOPILOT Part 5D) — closes the
application tracking loop.

Rides the EXISTING Gmail sync pass (amy/finance/sync/gmail_import.py's
sync_gmail accepts an optional inbound_hook — no second Gmail poll loop or
job): one extra targeted messages.list scoped to the open applications'
recorded HR contacts/domains, then per-message matching WITHOUT touching the
finance parser's message budget.

Matching (strongest first, never fabricated):
  (a) reply-thread: the inbound In-Reply-To/References header contains a
      Message-ID we stamped on a send_hr_email send (recorded in
      applications.thread_refs by the executor);
  (b) sender address == the application's recorded HR contact;
  (c) sender domain == the contact's domain, or the posting company's
      leading name token matches the sender's registrable domain label.
Unmatched mail — newsletters, job-board digests, anything HR-*looking* with
no open application behind it — is ignored, never classified, never parsed.

Classification is LOCAL-ONLY (sensitive=True: the mail body may contain
offer/compensation details), degrading to a deterministic keyword ladder
when no LLM is available. On a match:
  - the application status auto-updates (tier 1 via submit_action —
    executed + notification; this is Amy's own tracking data, not an
    external action), journals to the vault, and emits
    career.application_status_changed;
  - interview invite / offer detected additionally notify the user — these
    notifications plus the status-changed event ARE the extension points
    for the interview-prep pack (Part 5A-5C) and offer analysis (Part 5C),
    neither of which exists in this codebase yet; when they land they
    subscribe to the event, nothing here changes;
  - Amy NEVER auto-replies. This module drafts nothing; any future response
    draft must go through the approval inbox as tier 2 like every other
    external send.

Processed inbound gmail message ids are recorded per-application in
thread_refs["seen"], so a message re-fetched by a later poll window is a
no-op — sent→response moves exactly once per reply, not once per poll.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import re
from email.utils import parseaddr

_log = logging.getLogger("amy.career_inbound")

# Post-send, still-conversing statuses — the only ones inbound mail can
# advance. prepared/approved haven't been sent (nothing to reply to);
# terminal statuses stay terminal.
OPEN_STATUSES = ("sent", "response", "interview", "offer")

_STATUS_RANK = {"sent": 0, "response": 1, "interview": 2, "offer": 3}

# classification -> application status
_CLASSIFICATION_STATUS = {
    "rejection": "rejected",
    "interview": "interview",
    "offer": "offer",
    "info_request": "response",
    "other": "response",
}

_CLASSIFY_SYSTEM = (
    "Classify this reply to a job application. Respond with EXACTLY ONE "
    'JSON object: {"category": "<rejection|interview|info_request|offer|other>"}'
)

# Deterministic fallback ladder — ORDER MATTERS: a rejection often contains
# the word "interview" ("thank you for interviewing..."), so rejection
# patterns are checked first.
_KEYWORD_LADDER: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("rejection", ("unfortunately", "regret to inform", "not been selected",
                   "not moving forward", "won't be moving forward",
                   "will not be moving forward", "other candidates",
                   "decided to pursue", "position has been filled",
                   "not to proceed")),
    ("offer", ("pleased to offer", "offer of employment", "extend an offer",
               "offer letter", "compensation package", "joining bonus")),
    ("interview", ("interview", "schedule a call", "schedule a conversation",
                   "your availability", "meet with the team",
                   "technical discussion", "screening call")),
    ("info_request", ("could you provide", "please send", "please share",
                      "we need the following", "additional documents",
                      "fill out", "complete the attached")),
)

_GENERIC_COMPANY_TOKENS = frozenset({
    "the", "and", "inc", "llc", "ltd", "corp", "corporation", "group",
    "tech", "technologies", "technology", "solutions", "systems", "labs",
    "team", "global", "india", "software", "services", "consulting",
})


def _classify_keywords(text: str) -> str:
    low = text.lower()
    for category, needles in _KEYWORD_LADDER:
        if any(n in low for n in needles):
            return category
    return "other"


def classify_reply(ctx, subject: str, body: str) -> str:
    """LOCAL-ONLY classification (sensitive=True — the body may contain
    offer/compensation details, same routing class as GSTIN/PAN), degrading
    to the deterministic keyword ladder on any LLM absence/failure."""
    from .agents.reactive import _get_llm

    fallback = _classify_keywords(f"{subject}\n{body}")
    llm = _get_llm(ctx)
    if llm is None:
        return fallback
    try:
        text, provider = llm.generate(
            _CLASSIFY_SYSTEM,
            f"Subject: {subject}\n\n{body[:2000]}", sensitive=True)
        if provider == "template":
            return fallback
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            return fallback
        category = str(json.loads(m.group(0)).get("category", "")).strip().lower()
        if category in _CLASSIFICATION_STATUS:
            return category
    except Exception as exc:
        _log.warning("career_inbound: classification LLM failed, using "
                     "keyword fallback: %s", exc)
    return fallback


def _domain(addr: str) -> str:
    return addr.rsplit("@", 1)[-1].lower().strip() if "@" in addr else ""


_PUBLIC_SUFFIXY = frozenset({
    "com", "org", "net", "edu", "gov", "int", "in", "io", "co", "uk", "us",
    "de", "fr", "au", "ca", "ai", "app", "dev", "jobs", "careers",
})


def _registrable_label(domain: str) -> str:
    """'careers.acme.co.in' -> 'acme' (rightmost label that isn't a public-
    suffix-looking part — an approximation, but it's only ever used for a
    conservative company-name MATCH, never to send anything)."""
    for p in reversed([p for p in domain.split(".") if p]):
        if p not in _PUBLIC_SUFFIXY:
            return p
    return ""


def _company_token(company: str) -> str:
    for token in re.split(r"[^a-z0-9]+", (company or "").lower()):
        if len(token) >= 4 and token not in _GENERIC_COMPANY_TOKENS:
            return token
    return ""


def _msgid_tokens(header_value: str) -> set[str]:
    return set(re.findall(r"<[^<>\s]+>", header_value or ""))


class CareerInboundHook:
    """One Gmail-sync pass's worth of inbound matching state: the open
    applications, their contacts/thread refs, and the posting companies.
    Built once per sync by build_inbound_hook(); transport-free (sync_gmail
    fetches messages and calls handle() with parsed headers/body) so tests
    never need a Gmail service."""

    max_messages = 25   # career query budget — separate from the finance one

    def __init__(self, ctx, apps: list[dict]):
        self.ctx = ctx
        self._apps = apps   # each: {app, to_email, company, sent_refs, seen}

    @property
    def active(self) -> bool:
        return bool(self._apps)

    def gmail_query(self) -> str:
        """from:(...) clause covering every recorded HR contact and its
        domain — precise enough to not blow the message budget on
        newsletters. Portal/third-party applications with no captured
        contact can't be queried for (documented limitation: their replies
        are only caught if the company-name token matches a fetched
        sender's domain)."""
        terms: list[str] = []
        for entry in self._apps:
            to_email = entry["to_email"]
            if to_email:
                terms.append(to_email)
                dom = _domain(to_email)
                if dom:
                    terms.append(dom)
        if not terms:
            return ""
        uniq = sorted(set(terms))
        return "from:(" + " OR ".join(uniq) + ")"

    # -- matching ------------------------------------------------------------

    def _match(self, headers: dict) -> dict | None:
        from_email = parseaddr(headers.get("from", ""))[1].lower()
        if not from_email or from_email == (self.ctx.user_email or "").lower():
            return None   # our own outbound copy, not an HR reply
        reply_refs = (_msgid_tokens(headers.get("in-reply-to", ""))
                      | _msgid_tokens(headers.get("references", "")))
        from_dom = _domain(from_email)

        # (a) reply-thread of a send_hr_email message — strongest signal
        for entry in self._apps:
            if entry["sent_refs"] & reply_refs:
                return entry
        # (b) exact contact address
        for entry in self._apps:
            if entry["to_email"] and from_email == entry["to_email"]:
                return entry
        # (c) contact domain / company-name token vs sender domain
        for entry in self._apps:
            contact_dom = _domain(entry["to_email"] or "")
            if contact_dom and from_dom == contact_dom:
                return entry
            token = _company_token(entry["company"])
            if token and _registrable_label(from_dom).startswith(token):
                return entry
        return None

    # -- processing ----------------------------------------------------------

    def handle(self, gmail_msg_id: str, headers: dict, body: str) -> bool:
        """Match one inbound message; on a match classify + act. Returns
        True when the message belonged to an application (even if it was
        already seen), False when it's not career mail at all."""
        entry = self._match(headers)
        if entry is None:
            return False
        app = entry["app"]
        if gmail_msg_id in entry["seen"]:
            return True   # already processed by an earlier poll window
        if app["status"] not in OPEN_STATUSES:
            # went terminal earlier in this same pass (e.g. a rejection two
            # messages ago) — record seen, change nothing further
            self._record_seen(entry, gmail_msg_id)
            return True

        subject = headers.get("subject", "")
        classification = classify_reply(self.ctx, subject, body)
        new_status = _CLASSIFICATION_STATUS[classification]
        self._record_seen(entry, gmail_msg_id)

        current = app["status"]
        advances = (_STATUS_RANK.get(new_status, 99)
                    > _STATUS_RANK.get(current, -1))
        if new_status not in ("rejected",) and not advances:
            # e.g. a second info-request while already in 'response' — the
            # reply is recorded (seen) but the ladder only moves forward.
            return True

        note = (f"Inbound reply from {parseaddr(headers.get('from', ''))[1]} "
                f"classified locally as '{classification}': {subject[:120]}")
        from .automation.executors import submit_action
        submit_action(
            self.ctx, 1, "application_status_update",
            title=f"Application update: {classification} — "
                  f"{entry['company'] or 'unknown company'}",
            body=note,
            payload={"application_id": app["id"], "status": new_status,
                     "note": note, "trigger": "inbound_email",
                     "classification": classification},
            source="career_inbound",
            dedup_key=f"inbound_{app['id']}_{gmail_msg_id}",
            reasoning="Matched to the application by "
                      "thread/sender — Amy's own tracking data, so tier 1.")
        self._journal(app, entry, classification, new_status, subject)
        self._extension_hooks(app, entry, classification)
        app["status"] = new_status   # keep in-pass state consistent
        return True

    def _record_seen(self, entry: dict, gmail_msg_id: str) -> None:
        entry["seen"].add(gmail_msg_id)
        try:
            self.ctx.store.add_application_thread_ref(
                self.ctx.user_id, entry["app"]["id"], "seen", gmail_msg_id)
        except Exception:
            pass

    def _journal(self, app: dict, entry: dict, classification: str,
                 new_status: str, subject: str) -> None:
        try:
            from .agents.reactive import _journal
            _journal(self.ctx, {
                "id": f"inbound-{app['id']}-{new_status}",
                "type": "career.application_status_changed",
                "payload": {"application_id": app["id"],
                            "company": entry["company"],
                            "classification": classification,
                            "status": new_status, "subject": subject[:120]},
                "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(),
                "source": "career_inbound"})
        except Exception:
            pass

    def _extension_hooks(self, app: dict, entry: dict, classification: str) -> None:
        """Interview invite -> interview-prep extension point; offer ->
        offer-analysis extension point. Both are notifications today: the
        actual prep pack / offer analysis are Parts 5A-5C, which do not
        exist in this codebase yet — when they land they subscribe to
        career.application_status_changed (already emitted by the
        application_status_update executor) and these notifications become
        their user-visible trailhead. Nothing is fabricated here."""
        if classification not in ("interview", "offer"):
            return
        try:
            ns = self.ctx.notify_store()
            if classification == "interview":
                ref = f"interview_invite_{app['id']}"
                if not ns.exists_today("career_interview_invite", ref):
                    ns.create(
                        type="career_interview_invite",
                        title=f"Interview invite: {entry['company'] or 'a company'}",
                        body=("An inbound reply was classified as an interview "
                              "invite. Interview-prep pack + calendar linkage "
                              "arrive with Part 5A-5C — for now, check the "
                              "thread and confirm the slot yourself."),
                        priority="high",
                        related_entity={"entity_type": "application",
                                        "id": app["id"], "ref": ref})
            else:
                ref = f"offer_detected_{app['id']}"
                if not ns.exists_today("career_offer_detected", ref):
                    ns.create(
                        type="career_offer_detected",
                        title=f"Offer detected: {entry['company'] or 'a company'}",
                        body=("An inbound reply looks like an offer. Offer "
                              "analysis (Part 5C) isn't built yet — review "
                              "the terms yourself, and record the outcome on "
                              "the Career tab (accepted triggers the goal "
                              "wind-down proposal)."),
                        priority="high",
                        related_entity={"entity_type": "application",
                                        "id": app["id"], "ref": ref})
        except Exception:
            pass


def build_inbound_hook(ctx) -> CareerInboundHook | None:
    """None when there's nothing to match against (no open post-send
    applications) — callers pass inbound_hook=None straight through and the
    Gmail sync path stays exactly as it was."""
    from . import config
    if not config.agent_enabled("application_tracker"):
        return None
    try:
        apps = [a for a in ctx.store.list_applications(ctx.user_id)
                if a["status"] in OPEN_STATUSES]
    except Exception:
        return None
    if not apps:
        return None
    entries = []
    for app in apps:
        try:
            draft = json.loads(app.get("draft") or "{}")
        except Exception:
            draft = {}
        posting = ctx.store.get_posting(ctx.user_id, app["posting_id"]) or {}
        refs = app.get("thread_refs") or {}
        entries.append({
            "app": app,
            "to_email": (draft.get("to_email") or "").lower(),
            "company": posting.get("company") or "",
            "sent_refs": set(refs.get("sent") or []),
            "seen": set(refs.get("seen") or []),
        })
    return CareerInboundHook(ctx, entries)
