"""Loan Underwriting Module (Phase 5) — an illustrative underwriting
SIMULATOR, not a real lending decision engine and not a regulated
financial product. Final phase of the "Banking Risk Intelligence" series
— consumes Phase 3's stored credit score (`FinanceEngine.
get_latest_credit_score`) and Phase 4's jurisdiction `loan_config`
(`amy.jurisdictions.load_pack`/`loan_config`) as READ-ONLY inputs; never
recomputes credit factors or jurisdiction limits inline here.

Approval probabilities, recommended rates, and EMI schedules are computed
from the formulas below — they are not offers. Every generated
explanation carries the disclaimer text verbatim (see `underwrite()`'s
`_disclaimer` field) so it can't be mistaken for a real decision if shown
out of context.

Islamic financing (Murabaha/Ijara/Musharakah/Qard Hasan) is modeled at a
deliberately SIMPLIFIED level (see `emi_islamic_markup`) — real Islamic
finance contracts have materially different legal structures per product
and jurisdiction-specific Shariah-board requirements this simulation does
not implement.

Two reuse decisions worth knowing:

1. Affordability reuses `amy/finance/afford.py`'s `can_afford()` AS-IS,
   unmodified — treating the proposed EMI as the "spend" answers exactly
   the question afford.py already answers ("can this recurring outflow
   fit current cashflow"). No new affordability calculator was written.

2. Debt-to-income restates the `category == 'EMI/Loan'` signal
   `amy/finance/credit_engine.py`'s `_factor_debt()` uses, rather than
   importing that private helper — Phase 5 needs DTI *including* the
   hypothetical new EMI being applied for, which credit_engine has no
   concept of. Same "small enough to restate locally" precedent
   `aml_engine.py`'s cash-spike signal set relative to `fraud_engine.py`.

Explicitly not built (skip unless trivially cheap — noted per the
prompt's own permission): floating-rate loans, prepayment, foreclosure,
moratorium/payment-holiday handling. All amortization here assumes a
fixed rate held for the full term and on-schedule payments.
"""
from __future__ import annotations

import datetime as _dt

_ISLAMIC_STRUCTURES = ("murabaha", "ijara", "musharakah", "qard_hasan")

# Illustrative flat starting rates per loan type, independent of
# jurisdiction — Phase 4's loan_config only names an interest-calculation
# METHOD (simple|compound|reducing_balance), not a rate figure, so a rate
# model belongs here, not in the JSON packs.
BASE_RATES = {   # illustrative, not sourced from real market rates
    "personal": 0.12,
    "home": 0.08,
    "business": 0.11,
    "auto": 0.09,
    "education": 0.07,
}
RATE_FLOOR = 0.02   # illustrative threshold, not sourced from regulation


# ---------------------------------------------------------------------------
# Interest / amortization calculators
# ---------------------------------------------------------------------------

def emi_reducing_balance(principal: float, annual_rate: float, months: int) -> float:
    if months <= 0:
        raise ValueError("months must be positive")
    if annual_rate <= 0:
        return round(principal / months, 2)
    r = annual_rate / 12
    factor = (1 + r) ** months
    return round(principal * r * factor / (factor - 1), 2)


def emi_flat_rate(principal: float, annual_rate: float, months: int) -> float:
    """Simple-interest total spread evenly across installments."""
    if months <= 0:
        raise ValueError("months must be positive")
    years = months / 12
    total_interest = principal * annual_rate * years
    return round((principal + total_interest) / months, 2)


def emi_compound(principal: float, annual_rate: float, months: int) -> float:
    """Simplified: total repayment via monthly-compounded growth over the
    term, spread evenly across installments. True compound-interest
    amortization isn't a standard consumer-loan shape (most real
    'compound' retail products are actually reducing-balance in
    practice) — an illustrative simplification, not a real product
    model."""
    if months <= 0:
        raise ValueError("months must be positive")
    total = principal * ((1 + annual_rate / 12) ** months)
    return round(total / months, 2)


def emi_islamic_markup(principal: float, profit_rate: float, months: int,
                       structure: str = "murabaha") -> float:
    """ONE shared simplified cost-plus markup model across Murabaha
    (sale-based), Ijara (lease-based), and Musharakah (partnership-based)
    for this demo — real contracts differ materially in legal structure
    and require Shariah-board review this simulation does not implement.
    qard_hasan (a benevolent, interest/profit-free loan) is a
    structurally distinct, well-defined exception: profit_rate is forced
    to 0 regardless of the computed rate — not a simplification, an
    honest special case."""
    if months <= 0:
        raise ValueError("months must be positive")
    if structure == "qard_hasan":
        profit_rate = 0.0
    years = months / 12
    total_profit = principal * profit_rate * years
    return round((principal + total_profit) / months, 2)


def _add_months(d: _dt.date, n: int) -> _dt.date:
    month0 = d.month - 1 + n
    year = d.year + month0 // 12
    month = month0 % 12 + 1
    leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
    days_in_month = [31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    day = min(d.day, days_in_month[month - 1])
    return _dt.date(year, month, day)


def build_schedule(principal: float, annual_rate: float, months: int, method: str,
                   structure: str | None = None, start_date: _dt.date | None = None) -> list[dict]:
    """A REAL amortization table for reducing_balance (interest
    recomputed on the declining balance each period); an even-principal-
    split table for simple/compound/islamic (their EMI is fixed up front
    and doesn't vary installment to installment). The final installment
    absorbs rounding drift so the balance lands on exactly 0."""
    start = start_date or _dt.date.today()
    rows: list[dict] = []

    if method == "reducing_balance":
        emi = emi_reducing_balance(principal, annual_rate, months)
        r = annual_rate / 12
        balance = principal
        for i in range(1, months + 1):
            interest = round(balance * r, 2) if r else 0.0
            principal_part = round(emi - interest, 2)
            balance = round(balance - principal_part, 2)
            if i == months:
                principal_part = round(principal_part + balance, 2)
                balance = 0.0
            rows.append({"installment_number": i, "due_date": _add_months(start, i).isoformat(),
                        "principal": principal_part, "interest": interest,
                        "emi": round(principal_part + interest, 2), "balance": max(balance, 0.0)})
        return rows

    if method == "simple":
        emi = emi_flat_rate(principal, annual_rate, months)
    elif method == "compound":
        emi = emi_compound(principal, annual_rate, months)
    elif method == "islamic":
        emi = emi_islamic_markup(principal, annual_rate, months, structure or "murabaha")
    else:
        raise ValueError(f"unknown interest calculation method {method!r}")

    principal_part = round(principal / months, 2)
    balance = principal
    for i in range(1, months + 1):
        interest = round(emi - principal_part, 2)
        this_principal = principal_part
        balance = round(balance - this_principal, 2)
        if i == months:
            this_principal = round(this_principal + balance, 2)
            balance = 0.0
        rows.append({"installment_number": i, "due_date": _add_months(start, i).isoformat(),
                    "principal": this_principal, "interest": interest,
                    "emi": round(this_principal + interest, 2), "balance": max(balance, 0.0)})
    return rows


def _recommended_rate(loan_type: str, credit_score: int | None) -> float:
    base = BASE_RATES.get(loan_type, 0.10)
    if credit_score is None:
        adj = 0.02    # uncertainty premium — no score on file is a risk signal, not neutral
    elif credit_score >= 750:
        adj = -0.02
    elif credit_score >= 650:
        adj = 0.0
    elif credit_score >= 550:
        adj = 0.03
    else:
        adj = 0.06
    return round(max(RATE_FLOOR, base + adj), 4)


def _debt_to_income(fe, new_emi: float, monthly_income: float) -> float | None:
    if monthly_income <= 0:
        return None
    txns = fe.list_transactions(limit=5000)
    existing_emi_total = sum(abs(t.get("amount") or 0) for t in txns
                             if t.get("category") == "EMI/Loan")
    months = len({(t.get("date") or "")[:7] for t in txns if t.get("date")}) or 1
    existing_monthly_emi = existing_emi_total / months
    return round((existing_monthly_emi + new_emi) / monthly_income, 4)


def _affordability_score(afford_result: dict) -> float:
    if afford_result["can_afford"] is None:
        return 40.0   # unknown (no income data) — below-neutral, never fabricated confidence
    band = {"low": 90.0, "medium": 70.0, "high": 55.0} if afford_result["can_afford"] \
        else {"low": 30.0, "medium": 20.0, "high": 10.0}
    return band.get(afford_result["risk_level"], 60.0 if afford_result["can_afford"] else 15.0)


def _approval_probability(credit_score: int | None, dti: float | None, max_dti: float,
                          meets_min_income: bool, affordability_score: float) -> float:
    score_component = ((credit_score - 300) / 600) if credit_score is not None else 0.35
    dti_component = max(0.0, 1 - (dti / max_dti)) if (max_dti and dti is not None) else 0.5
    income_component = 1.0 if meets_min_income else 0.15
    afford_component = affordability_score / 100.0
    prob = (0.35 * score_component + 0.25 * dti_component
           + 0.15 * income_component + 0.25 * afford_component)
    return round(min(1.0, max(0.0, prob)), 2)


def _risk_category(probability: float) -> str:
    if probability >= 0.65:
        return "LOW"
    if probability >= 0.35:
        return "MEDIUM"
    return "HIGH"


# ---------------------------------------------------------------------------
# Underwriting decision (pure) + application lifecycle (side-effecting)
# ---------------------------------------------------------------------------

def underwrite(fe, collab_db, loan_type: str, jurisdiction: str, amount_requested: float,
               term_months: int, financing_structure: str | None = None) -> dict:
    """Pure/read-only — builds the full underwriting decision contract.
    Never persists anything; see apply_for_loan() below for that."""
    from ..jurisdictions import PackError, load_pack
    from ..jurisdictions import loan_config as get_loan_config
    from .afford import can_afford

    if amount_requested <= 0:
        raise ValueError("amount_requested must be positive")
    if term_months <= 0:
        raise ValueError("term_months must be positive")

    try:
        pack = load_pack(jurisdiction)
    except PackError as exc:
        raise ValueError(str(exc))
    lc = get_loan_config(pack)
    if lc is None:
        raise ValueError(f"jurisdiction {jurisdiction!r} has no loan_config — "
                         "Loan Underwriting isn't configured for it")
    limits = lc["loan_limits"].get(loan_type)
    if limits is None:
        raise ValueError(f"loan_type {loan_type!r} isn't configured for jurisdiction {jurisdiction!r}")

    if financing_structure is not None:
        if financing_structure not in _ISLAMIC_STRUCTURES:
            raise ValueError(f"financing_structure must be one of {_ISLAMIC_STRUCTURES}")
        if not lc.get("islamic_finance_available"):
            raise ValueError(f"Islamic financing is not available in jurisdiction "
                             f"{jurisdiction!r} per its loan_config")

    cap = limits["amount"]
    was_capped = amount_requested > cap
    recommended_amount = min(amount_requested, cap)

    mi = lc["minimum_income"]
    monthly_income = fe.effective_monthly_income()
    min_income_monthly = mi["amount"] / 12 if mi["basis"] == "annual" else mi["amount"]
    meets_min_income = monthly_income >= min_income_monthly

    credit_row = fe.get_latest_credit_score()
    credit_score = credit_row["score"] if credit_row else None

    rate = _recommended_rate(loan_type, credit_score)
    method = "islamic" if financing_structure else lc["interest_calculation_defaults"]
    if method == "reducing_balance":
        emi = emi_reducing_balance(recommended_amount, rate, term_months)
    elif method == "simple":
        emi = emi_flat_rate(recommended_amount, rate, term_months)
    elif method == "compound":
        emi = emi_compound(recommended_amount, rate, term_months)
    else:
        emi = emi_islamic_markup(recommended_amount, rate, term_months,
                                 financing_structure or "murabaha")

    max_dti = lc["max_debt_to_income_ratio"]
    dti = _debt_to_income(fe, emi, monthly_income)
    dti_ok = dti is not None and dti <= max_dti

    afford_result = can_afford(emi, f"Loan EMI ({loan_type})", fe, collab_db)
    affordability_score = _affordability_score(afford_result)

    probability = _approval_probability(credit_score, dti, max_dti, meets_min_income,
                                        affordability_score)
    risk_category = _risk_category(probability)

    positive_factors: list[str] = []
    risk_factors: list[str] = []
    if credit_score is not None:
        (positive_factors if credit_score >= 650 else risk_factors).append(
            f"Amy Score {credit_score}/900 on file (an internal signal, not a bureau score).")
    else:
        risk_factors.append("No Amy Score on file — treated as an uncertainty premium on "
                            "the rate and approval probability.")
    if meets_min_income:
        positive_factors.append(f"Effective monthly income {monthly_income:,.0f} meets the "
                                f"{jurisdiction} minimum of {min_income_monthly:,.0f}.")
    else:
        risk_factors.append(f"Effective monthly income {monthly_income:,.0f} is below the "
                            f"{jurisdiction} minimum of {min_income_monthly:,.0f}.")
    if dti is not None:
        (positive_factors if dti_ok else risk_factors).append(
            f"Debt-to-income after this loan would be {dti:.0%} vs a {max_dti:.0%} "
            f"cap for {jurisdiction}.")
    else:
        risk_factors.append("No income data to compute a debt-to-income ratio.")
    if was_capped:
        risk_factors.append(f"Requested {amount_requested:,.0f} exceeds the {jurisdiction} "
                            f"{loan_type} limit of {cap:,.0f} — capped to {cap:,.0f}.")
    if afford_result["can_afford"] is True:
        positive_factors.append(f"The {emi:,.0f}/month installment fits current cashflow "
                                f"({afford_result['risk_level']} risk).")
    elif afford_result["can_afford"] is False:
        last_reason = afford_result["reasoning"][-1] if afford_result["reasoning"] else ""
        risk_factors.append(f"The {emi:,.0f}/month installment does not fit current "
                            f"cashflow: {last_reason}")

    return {
        "application_id": None,   # filled in by apply_for_loan() once persisted
        "loan_type": loan_type,
        "jurisdiction": jurisdiction,
        "financing_structure": financing_structure,
        "amount_requested": amount_requested,
        "term_months": term_months,
        "interest_calculation_method": method,
        "approval_probability": probability,
        "risk_category": risk_category,
        "recommended_rate": rate,
        "recommended_amount": recommended_amount,
        "emi": emi,
        "debt_to_income_ratio": dti,
        "affordability_score": affordability_score,
        "explanation": {
            "positive_factors": positive_factors,
            "risk_factors": risk_factors,
            "credit_score_used": credit_score,
            "jurisdiction_limits_applied": {
                "jurisdiction": jurisdiction,
                "loan_type": loan_type,
                "loan_limit": cap,
                "loan_limit_currency": limits["currency"],
                "was_capped": was_capped,
                "minimum_income_required": min_income_monthly,
                "minimum_income_basis": mi["basis"],
                "meets_minimum_income": meets_min_income,
                "max_debt_to_income_ratio": max_dti,
            },
        },
        "_disclaimer": ("Amy Loan Simulator — an illustrative underwriting simulation, not a "
                        "real lending decision, not a licensed lender's system, and not a "
                        "regulated financial product. Nothing here is an offer."),
    }


def apply_for_loan(ctx, loan_type: str, jurisdiction: str, amount_requested: float,
                   term_months: int, financing_structure: str | None = None) -> dict:
    """Persists the application and ALWAYS parks a fixed tier-2 approval —
    never severity-computed like Phase 1's fraud tiering. 'This engine
    proposes, it doesn't auto-approve' means approval_probability/
    risk_category are advisory info on the approval card, not a gate that
    lets any application skip human review."""
    from ..automation.executors import submit_action

    fe = ctx.open_finance()
    try:
        decision = underwrite(fe, ctx.collab, loan_type, jurisdiction,
                              amount_requested, term_months, financing_structure)
        application_id = fe.create_loan_application(
            loan_type, jurisdiction, amount_requested, term_months,
            financing_structure, decision)
    finally:
        fe.close()
    decision["application_id"] = application_id

    try:
        ctx.events().emit("loan.requested", {
            "application_id": application_id, "loan_type": loan_type,
            "jurisdiction": jurisdiction, "amount_requested": amount_requested,
        }, source="loan_engine")
    except Exception:
        pass

    factors = decision["explanation"]["positive_factors"] + decision["explanation"]["risk_factors"]
    result = submit_action(
        ctx, 2, "loan_decision",
        title=f"Loan decision — {loan_type} ({jurisdiction}), {decision['risk_category']} risk",
        body=(f"Requested {amount_requested:,.0f}, recommended "
              f"{decision['recommended_amount']:,.0f} at {decision['recommended_rate']:.2%}, "
              f"EMI {decision['emi']:,.0f}/mo. Approval probability "
              f"{decision['approval_probability']:.0%}. This engine proposes only — "
              f"approving here is the actual lending decision."),
        payload={"application_id": application_id, "decision": decision},
        source="loan_engine",
        dedup_key=None,   # a fresh application is never a duplicate of an earlier one
        reasoning="; ".join(factors) or "No factors computed.",
        risk="destructive",
        affected_entity=f"loan_application_id={application_id}")

    fe = ctx.open_finance()
    try:
        fe.set_loan_application_approval_id(application_id, result.get("approval_id") or "")
    finally:
        fe.close()

    return {"application_id": application_id, "decision": decision, "approval": result}


def _reconcile(fe, store, app: dict) -> dict:
    """Rejection has no dedicated executor — the standard
    executors.reject() only marks the approvals row 'rejected', with no
    per-action-type hook. Reconciling lazily on read (instead of adding a
    parallel loan-specific reject endpoint) keeps the Approval Inbox the
    ONE place a human decides, same as every other phase — a human
    rejecting through the normal Approval Inbox UI is still correctly
    reflected here."""
    if app["status"] == "pending" and app.get("approval_id"):
        ap = store.get_approval(app["approval_id"])
        if ap and ap["status"] in ("rejected", "expired"):
            fe.update_loan_application_status(app["id"], ap["status"])
            app["status"] = ap["status"]
    return app


def get_application(fe, store, application_id: str) -> dict | None:
    app = fe.get_loan_application(application_id)
    if app is None:
        return None
    return _reconcile(fe, store, app)


def list_applications(fe, store, status: str | None = None,
                      jurisdiction: str | None = None, limit: int = 100) -> list[dict]:
    apps = fe.list_loan_applications(status=status, jurisdiction=jurisdiction, limit=limit)
    return [_reconcile(fe, store, a) for a in apps]


# ---------------------------------------------------------------------------
# Assistant tools' logic — all read STORED data only, never re-underwrite.
# ---------------------------------------------------------------------------

def explain_loan_rejection(fe, store, application_id: str) -> dict:
    app = get_application(fe, store, application_id)
    if app is None:
        return {"available": False, "reason": "no such loan application"}
    if app["status"] != "rejected":
        return {"available": False,
               "reason": f"application status is {app['status']!r}, not rejected"}
    exp = app["decision"].get("explanation", {})
    return {"available": True, "application_id": application_id,
           "risk_factors": exp.get("risk_factors", []),
           "credit_score_used": exp.get("credit_score_used"),
           "jurisdiction_limits_applied": exp.get("jurisdiction_limits_applied", {})}


def simulate_refinancing(fe, application_id: str, new_rate: float) -> dict:
    """What-if preview at a hypothetical rate using the STORED principal/
    term/method — never persisted, never mutates the real schedule."""
    app = fe.get_loan_application(application_id)
    if app is None:
        return {"available": False, "reason": "no such loan application"}
    if new_rate <= 0:
        return {"available": False, "reason": "new_rate must be positive"}
    decision = app["decision"]
    method = decision.get("interest_calculation_method", "reducing_balance")
    principal = decision.get("recommended_amount") or app["amount_requested"]
    months = app["term_months"]
    structure = app.get("financing_structure")
    if method == "reducing_balance":
        new_emi = emi_reducing_balance(principal, new_rate, months)
    elif method == "simple":
        new_emi = emi_flat_rate(principal, new_rate, months)
    elif method == "compound":
        new_emi = emi_compound(principal, new_rate, months)
    else:
        new_emi = emi_islamic_markup(principal, new_rate, months, structure or "murabaha")
    current_emi = decision.get("emi") or 0
    return {"available": True, "application_id": application_id,
           "current_rate": decision.get("recommended_rate"), "current_emi": current_emi,
           "simulated_rate": new_rate, "simulated_emi": new_emi,
           "monthly_difference": round(current_emi - new_emi, 2),
           "note": "Simulation only — not persisted, not an offer."}


def compare_loan_offers(fe, store, application_ids: list[str]) -> dict:
    offers = []
    for aid in application_ids:
        app = get_application(fe, store, aid)
        if app is None:
            offers.append({"application_id": aid, "available": False,
                          "reason": "no such application"})
            continue
        d = app["decision"]
        offers.append({"application_id": aid, "available": True, "status": app["status"],
                       "loan_type": app["loan_type"], "jurisdiction": app["jurisdiction"],
                       "recommended_amount": d.get("recommended_amount"),
                       "recommended_rate": d.get("recommended_rate"), "emi": d.get("emi"),
                       "risk_category": d.get("risk_category"),
                       "approval_probability": d.get("approval_probability")})
    return {"offers": offers}


def explain_emi(fe, application_id: str) -> dict:
    app = fe.get_loan_application(application_id)
    if app is None:
        return {"available": False, "reason": "no such loan application"}
    decision = app["decision"]
    schedule = fe.get_loan_schedule(application_id)
    return {"available": True, "application_id": application_id,
           "emi": decision.get("emi"), "recommended_rate": decision.get("recommended_rate"),
           "recommended_amount": decision.get("recommended_amount"),
           "term_months": app["term_months"],
           "interest_calculation_method": decision.get("interest_calculation_method"),
           "schedule_generated": bool(schedule),
           "first_installment": schedule[0] if schedule else None,
           "last_installment": schedule[-1] if schedule else None}
