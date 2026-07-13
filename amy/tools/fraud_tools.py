"""Fraud Detection Module (Phase 1) registry tools — see
amy/finance/fraud_engine.py for the scoring logic and its module docstring
for the "illustrative, not sourced from regulation" framing.

score_fraud_risk is read-only and never gated (read tools always execute
directly, per amy/tools/registry.py's invoke()) — it computes a score but
never persists it.

review_fraud_transaction is the tool that actually scores AND routes the
result through the tier-2 approval pipeline. It's registered RISK_WRITE so
it's correctly categorized in list_tools()/the assistant's catalog and so a
hypothetical future actor="agent" caller (no such caller exists yet in
Phase 1 — no reactive agent subscribes to fraud.detected) still gets
AGENT_GATE's own tier-2-by-default treatment on the *request to review* at
all. But the tier that actually matters — LOW/MEDIUM auto-apply vs.
HIGH/CRITICAL requiring a human approval — is decided INSIDE
fraud_engine.review_transaction() from the computed risk_level, via a
direct submit_action() call, not by this tool's static registry risk. For
the normal caller (a human via the API routes or the assistant chat loop,
actor="human"), invoke() skips AGENT_GATE entirely and this severity-based
routing is the only gate that applies — don't assume the registry's
risk="write" alone is what protects a HIGH/CRITICAL score from executing
silently; it's review_transaction()'s tier lookup that does that.

explain_fraud_score reads the PERSISTED score only — it never re-scores,
so its answer never drifts from what a human actually saw and (for HIGH/
CRITICAL) approved.
"""
from __future__ import annotations

from .registry import RISK_READ, RISK_WRITE, register_tool


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or []}


@register_tool("score_fraud_risk",
               "Compute a rule-based fraud risk score for a transaction "
               "(illustrative/simulated only — see amy/finance/fraud_engine.py). "
               "Read-only: does not persist the score or create an approval.",
               _obj({"transaction_id": {"type": "string"}}, ["transaction_id"]),
               RISK_READ)
def _t_score_fraud_risk(ctx, args):
    from ..finance.fraud_engine import score_transaction
    fe = ctx.open_finance()
    try:
        return score_transaction(fe, args["transaction_id"])
    finally:
        fe.close()


@register_tool("review_fraud_transaction",
               "Score a transaction for fraud risk and route the result "
               "through the approval pipeline: LOW/MEDIUM risk applies "
               "immediately, HIGH/CRITICAL parks as a tier-2 approval that "
               "only takes effect once a human approves it.",
               _obj({"transaction_id": {"type": "string"}}, ["transaction_id"]),
               RISK_WRITE)
def _t_review_fraud_transaction(ctx, args):
    from ..finance.fraud_engine import review_transaction
    return review_transaction(ctx, args["transaction_id"])


@register_tool("explain_fraud_score",
               "Explain why a transaction was flagged (or wasn't) using its "
               "STORED fraud score — never re-scores, so the answer always "
               "matches what a human actually saw. Honestly reports if the "
               "transaction hasn't been reviewed yet (HIGH/CRITICAL scores "
               "aren't persisted until a human approves them).",
               _obj({"transaction_id": {"type": "string"}}, ["transaction_id"]),
               RISK_READ)
def _t_explain_fraud_score(ctx, args):
    fe = ctx.open_finance()
    try:
        stored = fe.get_fraud_score(args["transaction_id"])
    finally:
        fe.close()
    if stored is None:
        return {"available": False,
               "reason": "this transaction hasn't been reviewed yet (or its "
                         "score is still a pending approval) — call "
                         "review_fraud_transaction first"}
    return {"available": True, "transaction_id": args["transaction_id"], **stored}


@register_tool("list_flagged_fraud_transactions",
               "List transactions with a stored fraud risk level above LOW, "
               "most recently scored first.",
               _obj({"risk_level": {"type": "string",
                                    "description": "MEDIUM|HIGH|CRITICAL"},
                     "limit": {"type": "integer"}}),
               RISK_READ)
def _t_list_flagged_fraud_transactions(ctx, args):
    fe = ctx.open_finance()
    try:
        return {"transactions": fe.list_flagged_transactions(
            risk_level=args.get("risk_level"), limit=int(args.get("limit") or 100))}
    finally:
        fe.close()
