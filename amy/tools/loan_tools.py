"""Loan Underwriting Module (Phase 5) registry tools — see
amy/finance/loan_engine.py for the underwriting logic and its module
docstring for the "illustrative underwriting simulator, not a real
lending decision" framing.

simulate_loan is read-only and never gated — dry-runs underwrite()
without persisting anything.

apply_for_loan is registered RISK_WRITE (it persists a loan_applications
row — a real side effect) but, like every prior phase's write tool,
always executes directly for the normal caller (actor="human" via the
API routes or the assistant chat loop). The gate that actually matters —
every application ALWAYS requiring a human decision before it takes
effect — is enforced INSIDE loan_engine.apply_for_loan() via a fixed
tier-2 submit_action() call, not by this tool's registry classification.

explain_loan_rejection / simulate_refinancing / compare_loan_offers /
explain_emi are all read-only and trace back to STORED decision data —
none of them re-run underwrite(), so none can drift from what a human
actually saw when they made (or will make) the real decision.
"""
from __future__ import annotations

from .registry import RISK_READ, RISK_WRITE, register_tool


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or []}


_APPLY_PARAMS = _obj({
    "loan_type": {"type": "string", "description": "personal|home|business|auto|education"},
    "jurisdiction": {"type": "string", "description": "india|uae|us"},
    "amount_requested": {"type": "number"},
    "term_months": {"type": "integer"},
    "financing_structure": {"type": "string",
                            "description": "optional: murabaha|ijara|musharakah|qard_hasan "
                                          "(only where the jurisdiction's loan_config allows it)"},
}, ["loan_type", "jurisdiction", "amount_requested", "term_months"])


@register_tool("simulate_loan",
               "Dry-run the Amy Loan Simulator underwriting decision — "
               "illustrative only, not a real lending decision. Does not "
               "create an application or persist anything.",
               _APPLY_PARAMS, RISK_READ)
def _t_simulate_loan(ctx, args):
    from ..finance.loan_engine import underwrite
    fe = ctx.open_finance()
    try:
        return underwrite(fe, ctx.collab, args["loan_type"], args["jurisdiction"],
                          float(args["amount_requested"]), int(args["term_months"]),
                          args.get("financing_structure"))
    finally:
        fe.close()


@register_tool("apply_for_loan",
               "Apply for a loan — computes the underwriting decision and "
               "ALWAYS parks it as a tier-2 human approval (this engine "
               "proposes, it never auto-approves regardless of the "
               "computed risk category).",
               _APPLY_PARAMS, RISK_WRITE)
def _t_apply_for_loan(ctx, args):
    from ..finance.loan_engine import apply_for_loan
    return apply_for_loan(ctx, args["loan_type"], args["jurisdiction"],
                          float(args["amount_requested"]), int(args["term_months"]),
                          args.get("financing_structure"))


@register_tool("explain_loan_rejection",
               "Explain why a loan application was rejected, using its "
               "STORED decision data — never re-underwrites. Honestly "
               "reports if the application isn't rejected (or doesn't exist).",
               _obj({"application_id": {"type": "string"}}, ["application_id"]),
               RISK_READ)
def _t_explain_loan_rejection(ctx, args):
    from ..finance.loan_engine import explain_loan_rejection
    fe = ctx.open_finance()
    try:
        return explain_loan_rejection(fe, ctx.store, args["application_id"])
    finally:
        fe.close()


@register_tool("simulate_refinancing",
               "What-if preview of a stored loan application's EMI at a "
               "hypothetical new rate. Never persisted, never mutates the "
               "real schedule — a simulation only.",
               _obj({"application_id": {"type": "string"},
                    "new_rate": {"type": "number"}},
                   ["application_id", "new_rate"]),
               RISK_READ)
def _t_simulate_refinancing(ctx, args):
    from ..finance.loan_engine import simulate_refinancing
    fe = ctx.open_finance()
    try:
        return simulate_refinancing(fe, args["application_id"], float(args["new_rate"]))
    finally:
        fe.close()


@register_tool("compare_loan_offers",
               "Side-by-side comparison of stored loan applications' "
               "decisions (rate, EMI, risk category, approval probability).",
               _obj({"application_ids": {"type": "array"}}, ["application_ids"]),
               RISK_READ)
def _t_compare_loan_offers(ctx, args):
    from ..finance.loan_engine import compare_loan_offers
    fe = ctx.open_finance()
    try:
        return compare_loan_offers(fe, ctx.store, list(args["application_ids"]))
    finally:
        fe.close()


@register_tool("explain_emi",
               "Explain a stored loan application's EMI — rate, method, "
               "and its first/last installment from the generated "
               "schedule (if one exists yet).",
               _obj({"application_id": {"type": "string"}}, ["application_id"]),
               RISK_READ)
def _t_explain_emi(ctx, args):
    from ..finance.loan_engine import explain_emi
    fe = ctx.open_finance()
    try:
        return explain_emi(fe, args["application_id"])
    finally:
        fe.close()
