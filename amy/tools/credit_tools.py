"""Amy Credit Score Module (Phase 3) registry tools — see
amy/finance/credit_engine.py for the scoring logic and its module
docstring for the "Amy Score — an internal signal, not a credit bureau
score" framing and the two honesty notes (payment_history is a proxy;
overdrafts/savings have no account-balance data to draw on).

All four tools are RISK_READ, per the prompt's explicit instruction:
compute_credit_score computes AND persists a credit_scores row, but that
write has no external effect and needs no approval gate — unlike Phase
1/2's detection tools, which persisted risk-relevant flags with real
downstream consequence (an approval, a potential block). This is closer
in kind to auto_categorize/budget-suggestion refreshes elsewhere in this
codebase, which also aren't approval-gated.
"""
from __future__ import annotations

from .registry import RISK_READ, register_tool


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or []}


@register_tool("compute_credit_score",
               "Compute and store the current Amy Score (illustrative "
               "internal signal, NOT a credit bureau score) from existing "
               "finance/fraud/AML data. Read-only in effect — no approval "
               "gate, no external action.",
               _obj({}),
               RISK_READ)
def _t_compute_credit_score(ctx, args):
    from ..finance.credit_engine import record_score
    return record_score(ctx)


@register_tool("credit_score_history",
               "List past Amy Score computations, most recent first.",
               _obj({"limit": {"type": "integer"}}),
               RISK_READ)
def _t_credit_score_history(ctx, args):
    fe = ctx.open_finance()
    try:
        return {"history": fe.list_credit_score_history(limit=int(args.get("limit") or 100))}
    finally:
        fe.close()


@register_tool("explain_credit_score",
               "Explain the latest STORED Amy Score using its stored "
               "factors/explanation — never recomputes, so the answer "
               "always matches what was last actually computed. Honestly "
               "reports if no score has been computed yet.",
               _obj({}),
               RISK_READ)
def _t_explain_credit_score(ctx, args):
    fe = ctx.open_finance()
    try:
        latest = fe.get_latest_credit_score()
    finally:
        fe.close()
    if latest is None:
        return {"available": False, "reason": "no Amy Score has been computed yet — "
                                              "call compute_credit_score first"}
    return {"available": True, **latest}


@register_tool("improve_credit_score",
               "Suggestions for improving the Amy Score, derived from the "
               "latest STORED computation's lowest-scoring AVAILABLE "
               "factors only — never suggests fixing a factor marked "
               "available:false (e.g. overdrafts, bureau_score).",
               _obj({}),
               RISK_READ)
def _t_improve_credit_score(ctx, args):
    fe = ctx.open_finance()
    try:
        latest = fe.get_latest_credit_score()
    finally:
        fe.close()
    if latest is None:
        return {"available": False, "reason": "no Amy Score has been computed yet — "
                                              "call compute_credit_score first"}
    return {"available": True, "score": latest["score"],
           "suggestions": latest["improvement_suggestions"]}
