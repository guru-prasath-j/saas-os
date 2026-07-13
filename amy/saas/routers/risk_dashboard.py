"""Compliance/Risk Dashboard (Phase 6) — read-only aggregation over
Phases 1-5 (Fraud, AML, Credit Score, Loan Underwriting). No new
detection/scoring logic lives here — every number below traces to an
existing table via an existing FinanceEngine/loan_engine read method.
No materialized cache table either: this is a single-user demo system,
plain Python-side counting over the existing list methods is trivially
fast at this scale (see the module-tree entry in CLAUDE.md for the full
reasoning on both calls).

The credit summary is explicitly "your score over time," never a
population distribution — there is no population in a single-user
system, and fabricating one would violate this whole series' honesty
rules just as much as inventing a fraud score would.
"""
from __future__ import annotations

import datetime as _dt
from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException

from ..db import User
from ..deps import current_user
from .automation import _ctx

router = APIRouter()

_EXPLAIN_TOOL_BY_TYPE = {
    "fraud": ("explain_fraud_score", "transaction_id"),
    "aml": ("explain_aml_alert", "case_id"),
    "credit": ("explain_credit_score", None),
    "loan": ("explain_loan_rejection", "application_id"),
}


# ---------------------------------------------------------------------------
# Summaries — pure aggregation, no side effects
# ---------------------------------------------------------------------------

def _fraud_summary(fe) -> dict:
    # list_flagged_transactions() only returns risk_level != 'LOW' rows
    # (see fraud_engine.py) — total_scored below is the true denominator,
    # LOW count is derived as total minus the non-LOW rows it returned.
    flagged = fe.list_flagged_transactions(limit=1000)
    total_scored = fe.conn.execute(
        "SELECT COUNT(*) n FROM transactions WHERE fraud_scored_at IS NOT NULL"
    ).fetchone()["n"]
    risk_level_counts = {"LOW": 0, "MEDIUM": 0, "HIGH": 0, "CRITICAL": 0}
    blocked_count = 0
    trend: dict[str, int] = defaultdict(int)
    for t in flagged:
        lvl = t.get("fraud_risk_level") or "MEDIUM"
        risk_level_counts[lvl] = risk_level_counts.get(lvl, 0) + 1
        if t.get("fraud_action") == "block":
            blocked_count += 1
        if lvl in ("HIGH", "CRITICAL") and t.get("fraud_scored_at"):
            month = t["fraud_scored_at"][:7]
            trend[month] += 1
    risk_level_counts["LOW"] = max(0, total_scored - sum(
        v for k, v in risk_level_counts.items() if k != "LOW"))
    high_critical = risk_level_counts["HIGH"] + risk_level_counts["CRITICAL"]
    fraud_rate = round(high_critical / total_scored, 4) if total_scored else None
    return {
        "total_scored": total_scored,
        "risk_level_counts": risk_level_counts,
        "blocked_count": blocked_count,
        "fraud_rate": fraud_rate,
        "monthly_high_critical_trend": dict(sorted(trend.items())),
        "recent_flagged": flagged[:10],
    }


def _aml_summary(fe) -> dict:
    cases = fe.list_aml_cases(limit=1000)
    status_counts: dict[str, int] = defaultdict(int)
    typology_counts: dict[str, int] = defaultdict(int)
    for c in cases:
        status_counts[c["status"]] += 1
        typology_counts[c["typology"]] += 1
    open_or_escalated = status_counts.get("open", 0) + status_counts.get(
        "investigating", 0) + status_counts.get("escalated", 0)
    return {
        "total_cases": len(cases),
        "status_counts": dict(status_counts),
        "typology_counts": dict(typology_counts),
        "open_or_escalated": open_or_escalated,
        "recent_cases": cases[:10],
    }


def _credit_summary(fe) -> dict:
    latest = fe.get_latest_credit_score()
    history = fe.list_credit_score_history(limit=24)   # most-recent-first
    trend = None
    if len(history) >= 2:
        delta = history[0]["score"] - history[1]["score"]
        trend = "improving" if delta > 0 else "declining" if delta < 0 else "flat"
    return {"latest": latest, "history": history, "trend": trend}


def _loan_summary(fe, store) -> dict:
    from ...finance.loan_engine import list_applications

    apps = list_applications(fe, store, limit=1000)
    status_counts: dict[str, int] = defaultdict(int)
    by_type: dict[str, int] = defaultdict(int)
    total_approved_amount = 0.0
    for a in apps:
        status_counts[a["status"]] += 1
        by_type[a["loan_type"]] += 1
        if a["status"] == "approved":
            total_approved_amount += a.get("recommended_amount") or 0.0
    decided = status_counts.get("approved", 0) + status_counts.get("rejected", 0)
    approval_rate = round(status_counts.get("approved", 0) / decided, 4) if decided else None
    return {
        "total_applications": len(apps),
        "status_counts": dict(status_counts),
        "approval_rate": approval_rate,
        "total_approved_amount": round(total_approved_amount, 2),
        "by_loan_type": dict(by_type),
        "recent_applications": apps[:10],
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/api/risk/dashboard/fraud")
def risk_dashboard_fraud(user: User = Depends(current_user)):
    cdb, ctx = _ctx(user, with_llm=False)
    try:
        fe = ctx.open_finance()
        try:
            return _fraud_summary(fe)
        finally:
            fe.close()
    finally:
        cdb.close()


@router.get("/api/risk/dashboard/aml")
def risk_dashboard_aml(user: User = Depends(current_user)):
    cdb, ctx = _ctx(user, with_llm=False)
    try:
        fe = ctx.open_finance()
        try:
            return _aml_summary(fe)
        finally:
            fe.close()
    finally:
        cdb.close()


@router.get("/api/risk/dashboard/credit")
def risk_dashboard_credit(user: User = Depends(current_user)):
    cdb, ctx = _ctx(user, with_llm=False)
    try:
        fe = ctx.open_finance()
        try:
            return _credit_summary(fe)
        finally:
            fe.close()
    finally:
        cdb.close()


@router.get("/api/risk/dashboard/loans")
def risk_dashboard_loans(user: User = Depends(current_user)):
    cdb, ctx = _ctx(user, with_llm=False)
    try:
        fe = ctx.open_finance()
        try:
            return _loan_summary(fe, ctx.store)
        finally:
            fe.close()
    finally:
        cdb.close()


def _executive_summary(fe, store) -> dict:
    return {
        "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "fraud": _fraud_summary(fe),
        "aml": _aml_summary(fe),
        "credit": _credit_summary(fe),
        "loans": _loan_summary(fe, store),
    }


@router.get("/api/risk/dashboard/executive")
def risk_dashboard_executive(user: User = Depends(current_user)):
    cdb, ctx = _ctx(user, with_llm=False)
    try:
        fe = ctx.open_finance()
        try:
            return _executive_summary(fe, ctx.store)
        finally:
            fe.close()
    finally:
        cdb.close()


def _explain(ctx, type_: str, id_: str) -> dict:
    """Dispatches to the actual explain_* tool already built in Phases
    1/2/3/5 via the tool registry — never reimplements the explanation.
    'credit' ignores id_ (the score is a per-user singleton)."""
    from ...tools import invoke as tool_invoke

    entry = _EXPLAIN_TOOL_BY_TYPE.get(type_)
    if entry is None:
        raise ValueError(f"type must be one of {list(_EXPLAIN_TOOL_BY_TYPE)}")
    tool_name, arg_key = entry
    if arg_key and not id_:
        raise ValueError(f"id is required for type={type_!r}")
    args = {arg_key: id_} if arg_key else {}
    return tool_invoke(ctx, tool_name, args, actor="human")


@router.get("/api/risk/dashboard/explain")
def risk_dashboard_explain(type: str, id: str = "", user: User = Depends(current_user)):
    cdb, ctx = _ctx(user, with_llm=False)
    try:
        try:
            return _explain(ctx, type, id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    finally:
        cdb.close()
