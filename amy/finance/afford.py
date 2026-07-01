"""Can I afford this? engine.

Evaluates a proposed spend against:
  1. Current estimated balance (monthly income - expenses so far)
  2. Upcoming bills/subscriptions due before next income cycle
  3. Active budget headroom for the relevant category
  4. Financial goal savings targets (delays any active finance goals)

Returns a structured verdict the API can return directly.
"""
from __future__ import annotations


_CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "food": ["food", "restaurant", "lunch", "dinner", "breakfast", "swiggy",
             "zomato", "grocery", "groceries", "cafe", "coffee", "snack", "eat"],
    "entertainment": ["movie", "netflix", "spotify", "prime", "hotstar", "game",
                      "concert", "show", "ott", "streaming", "disney", "youtube"],
    "transport": ["uber", "ola", "taxi", "cab", "fuel", "petrol", "train",
                  "flight", "bus", "metro", "rapido", "travel"],
    "shopping": ["clothes", "shirt", "shoes", "amazon", "flipkart", "myntra",
                 "buy", "purchase", "order", "bag", "watch", "gadget"],
    "health": ["medicine", "doctor", "pharmacy", "hospital", "gym", "fitness",
               "yoga", "clinic", "medical", "health"],
    "education": ["course", "book", "udemy", "coursera", "coaching", "class",
                  "tutorial", "certification", "learning", "skill"],
    "electronics": ["laptop", "phone", "mobile", "headphones", "keyboard",
                    "monitor", "tablet", "charger", "ipad", "macbook"],
}


def _category_hint(description: str) -> str | None:
    desc = description.lower()
    for cat, keywords in _CATEGORY_KEYWORDS.items():
        if any(k in desc for k in keywords):
            return cat
    return None


def can_afford(amount: float, description: str,
               finance_engine, collab_db=None) -> dict:
    """
    Returns:
      can_afford      — True / False / None (None when no income data exists)
      reasoning       — ordered list of explanation strings
      monthly_impact  — float, the spend amount
      risk_level      — 'low' | 'medium' | 'high' | 'unknown'
      goal_delay_months — float | None
    """
    reasoning: list[str] = []
    ov = finance_engine.overview()
    balance = ov["balance_estimate"]
    monthly_income = ov["monthly_income"]

    # 1. Guard: no income data → cannot assess
    if monthly_income == 0:
        reasoning.append(
            "⚠ No income sources recorded — add your monthly income first "
            "so Amy can assess affordability accurately.")
        return {
            "can_afford": None,
            "reasoning": reasoning,
            "monthly_impact": round(amount, 2),
            "risk_level": "unknown",
            "goal_delay_months": None,
        }

    reasoning.append(
        f"Estimated balance remaining this month: ₹{balance:,.0f}"
        f" (income ₹{monthly_income:,.0f})")

    # 2. Deduct upcoming bills from effective balance
    bills = ov["upcoming_bills"]
    bills_total = sum(b["monthly_cost"] for b in bills)
    effective_balance = balance - bills_total
    if bills_total:
        reasoning.append(
            f"Upcoming bills in next 30 days: ₹{bills_total:,.0f}"
            f" ({len(bills)} subscription{'s' if len(bills) != 1 else ''})"
            f" → effective balance ₹{effective_balance:,.0f}")

    # 3. Budget headroom for the matched category
    cat = _category_hint(description)
    budget_blocked = False
    if cat:
        for b in ov["budget_status"]:
            if b["category"].lower() == cat:
                if b["over_budget"]:
                    reasoning.append(
                        f"Already over budget for '{b['category']}' "
                        f"(spent ₹{b['spent']:,.0f} of ₹{b['limit']:,.0f} limit).")
                    budget_blocked = True
                elif b["headroom"] is not None:
                    if b["headroom"] < amount:
                        reasoning.append(
                            f"Budget headroom for '{b['category']}': ₹{b['headroom']:,.0f}"
                            f" — not enough for ₹{amount:,.0f}.")
                        budget_blocked = True
                    else:
                        reasoning.append(
                            f"Budget headroom for '{b['category']}': ₹{b['headroom']:,.0f} ✓")
                break

    # 4. Goal savings impact
    goal_delay_months = None
    if collab_db is not None:
        try:
            goals = collab_db.conn.execute(
                "SELECT title, progress FROM goals"
                " WHERE domain='finance' AND status='active'"
            ).fetchall()
            if goals:
                savings_rate = max(0.05, effective_balance / monthly_income)
                monthly_savings = monthly_income * savings_rate
                if monthly_savings > 0:
                    goal_delay_months = round(amount / monthly_savings, 1)
                    goal_titles = ", ".join(g["title"] for g in goals[:2])
                    reasoning.append(
                        f"Spending ₹{amount:,.0f} may delay financial goal(s)"
                        f" ({goal_titles}) by ~{goal_delay_months} month(s).")
        except Exception:
            pass

    # 5. Final verdict
    pct_of_income = amount / monthly_income * 100

    can = effective_balance >= amount and not budget_blocked

    if pct_of_income < 5:
        risk_level = "low"
    elif pct_of_income < 20:
        risk_level = "medium"
    else:
        risk_level = "high"

    if not effective_balance >= amount:
        risk_level = "high"

    if can:
        remaining = effective_balance - amount
        reasoning.append(
            f"✓ Affordable — ₹{amount:,.0f} is {pct_of_income:.1f}% of monthly income"
            f" and leaves ₹{remaining:,.0f} available.")
    elif budget_blocked and effective_balance >= amount:
        reasoning.append(
            f"✗ Cash available (₹{effective_balance:,.0f}) but blocked by budget limit.")
    else:
        reasoning.append(
            f"✗ ₹{amount:,.0f} exceeds effective balance ₹{effective_balance:,.0f}"
            f" after upcoming bills.")

    return {
        "can_afford": can,
        "reasoning": reasoning,
        "monthly_impact": round(amount, 2),
        "risk_level": risk_level,
        "goal_delay_months": goal_delay_months,
    }
