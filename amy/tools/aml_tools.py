"""AML Monitoring Module (Phase 2) registry tools — see
amy/finance/aml_engine.py for the detection logic and its module docstring
for the "illustrative, not sourced from regulation" framing, and for the
four design decisions (dedicated aml_graph.db, directed-cycle DFS, scoped
circular-transfer detection, cash-spike merchant-keyword heuristic).

score_aml_typologies is read-only and never gated — it runs the four
detectors but persists nothing.

scan_account_for_aml is registered RISK_WRITE (it opens/reconfirms
aml_cases rows — a real side effect) but, like Phase 1's
review_fraud_transaction, always executes directly for the normal caller
(actor="human" via the API routes or the assistant chat loop) with no
approval gate — matching the prompt's "read for scoring/investigation"
intent even though the registry classification is WRITE. Case OPENING is
deliberately ungated per amy/finance/aml_engine.py's open_case() docstring
("the case table holds the investigation, the approval table holds the
human decision point") — only escalate_aml_case and generate_aml_sar_draft
below actually gate anything, and they do it with a FIXED tier 2 inside
aml_engine.py's escalate_case()/generate_sar_draft() (not severity-
computed like Phase 1), since both are explicit human-requested steps, not
automatic detection output.

explain_aml_alert reads the STORED case only — never re-scans, so its
answer never drifts from what a human actually saw.
"""
from __future__ import annotations

from .registry import RISK_DESTRUCTIVE, RISK_READ, RISK_WRITE, register_tool


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or []}


@register_tool("score_aml_typologies",
               "Run the four AML typology detectors (structuring, layering, "
               "cash spike, circular transfer) for an account — illustrative/"
               "simulated only, see amy/finance/aml_engine.py. Read-only: "
               "does not open or update any case.",
               _obj({"account_id": {"type": "string"}}, ["account_id"]),
               RISK_READ)
def _t_score_aml_typologies(ctx, args):
    from ..finance.aml_engine import scan_account_for_aml
    import dataclasses
    fe = ctx.open_finance()
    try:
        candidates = scan_account_for_aml(fe, args["account_id"])
    finally:
        fe.close()
    return {"candidates": [dataclasses.asdict(c) for c in candidates]}


@register_tool("scan_account_for_aml",
               "Scan an account for AML typologies and open (or reconfirm) "
               "a case per triggered typology. Case opening itself is never "
               "approval-gated — only escalating a case or requesting a SAR "
               "draft is.",
               _obj({"account_id": {"type": "string"}}, ["account_id"]),
               RISK_WRITE)
def _t_scan_account_for_aml(ctx, args):
    from ..finance.aml_engine import investigate_account
    return {"cases": investigate_account(ctx, args["account_id"])}


@register_tool("escalate_aml_case",
               "Escalate an AML case — always parks as a tier-2 human "
               "approval (fixed, not severity-based) before the case status "
               "actually changes.",
               _obj({"case_id": {"type": "string"}}, ["case_id"]),
               RISK_WRITE)
def _t_escalate_aml_case(ctx, args):
    from ..finance.aml_engine import escalate_case
    return escalate_case(ctx, args["case_id"])


@register_tool("generate_aml_sar_draft",
               "Generate a DRAFT/DEMO SAR-style document for an AML case — "
               "NOT a real regulatory filing (the header is baked into the "
               "generated text itself). Always parks as a tier-2 human "
               "approval; never produced automatically by a scan.",
               _obj({"case_id": {"type": "string"}}, ["case_id"]),
               RISK_DESTRUCTIVE)
def _t_generate_aml_sar_draft(ctx, args):
    from ..finance.aml_engine import generate_sar_draft
    return generate_sar_draft(ctx, args["case_id"])


@register_tool("explain_aml_alert",
               "Explain an AML case using its STORED evidence/explanation — "
               "never re-scans, so the answer always matches what a human "
               "actually saw. Honestly reports if the case doesn't exist.",
               _obj({"case_id": {"type": "string"}}, ["case_id"]),
               RISK_READ)
def _t_explain_aml_alert(ctx, args):
    fe = ctx.open_finance()
    try:
        case = fe.get_aml_case(args["case_id"])
    finally:
        fe.close()
    if case is None:
        return {"available": False, "reason": "no such AML case on file"}
    return {"available": True, **case}
