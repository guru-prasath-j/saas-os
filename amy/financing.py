"""Financing-model interface (Phase R7A-4) — strategy classes, not branches.

Each model answers: for {principal, term_months, annual_rate_or_markup},
what does the money really cost and what is the payment schedule? The
afford-check compares totals across the models a user's jurisdiction pack
enables; values profiles may flag models (e.g. an interest-free profile
flags the amortized-interest model) — flagged models stay visible with the
flag reason, so the comparison is honest rather than censored.

Adding a new model = one strategy class + a register() call. Which models a
user sees comes from pack JSON ("financing_models") — never from code.
"""
from __future__ import annotations


class FinancingModel:
    name = "base"
    description = ""

    def quote(self, principal: float, months: int, annual_rate: float = 0.0) -> dict:
        raise NotImplementedError

    def _base(self, principal: float, months: int, total: float) -> dict:
        return {
            "model": self.name,
            "description": self.description,
            "principal": round(principal, 2),
            "term_months": months,
            "total_cost": round(total, 2),
            "monthly_payment": round(total / months, 2) if months else round(total, 2),
            "cost_of_financing": round(total - principal, 2),
        }


class AmortizedInterest(FinancingModel):
    name = "amortized_interest"
    description = ("Conventional loan: each payment covers interest on the "
                   "remaining balance plus principal.")

    def quote(self, principal, months, annual_rate=0.0):
        r = annual_rate / 12.0
        if r <= 0 or months <= 0:
            total = principal
            q = self._base(principal, months, total)
        else:
            pmt = principal * r * (1 + r) ** months / ((1 + r) ** months - 1)
            q = self._base(principal, months, pmt * months)
            q["monthly_payment"] = round(pmt, 2)
        q["rate_meaning"] = "annual interest rate on the declining balance"
        return q


class ProfitRateMarkup(FinancingModel):
    name = "profit_rate_markup"
    description = ("Cost-plus financing: the financier buys the item and "
                   "sells it at a disclosed fixed markup, paid in equal "
                   "installments. No interest accrues.")

    def quote(self, principal, months, annual_rate=0.0):
        years = months / 12.0
        total = principal * (1 + annual_rate * years)
        q = self._base(principal, months, total)
        q["rate_meaning"] = "flat annual markup on the purchase price, fixed upfront"
        return q


class InstallmentZeroInterest(FinancingModel):
    name = "installment_zero_interest"
    description = "Equal installments of the price itself; zero financing cost."

    def quote(self, principal, months, annual_rate=0.0):
        q = self._base(principal, months, principal)
        q["rate_meaning"] = "no rate — pay exactly the price"
        return q


class LeaseToOwn(FinancingModel):
    name = "lease_to_own"
    description = ("Lease-to-own: the financier owns the asset; payments are "
                   "rent plus gradual buyout, ownership transfers at the end.")

    def quote(self, principal, months, annual_rate=0.0):
        years = months / 12.0
        total = principal * (1 + annual_rate * years)
        q = self._base(principal, months, total)
        q["rate_meaning"] = "annual lease factor on the asset value"
        return q


FINANCING_MODELS: dict[str, FinancingModel] = {}


def register(model: FinancingModel):
    FINANCING_MODELS[model.name] = model
    return model


for _m in (AmortizedInterest(), ProfitRateMarkup(),
           InstallmentZeroInterest(), LeaseToOwn()):
    register(_m)


def list_models() -> list[dict]:
    return [{"name": m.name, "description": m.description}
            for m in FINANCING_MODELS.values()]


def compare(principal: float, months: int, annual_rate: float = 0.0,
            enabled_models: list[str] | None = None,
            flagged_models: dict[str, str] | None = None) -> list[dict]:
    """Quotes for each enabled model, cheapest first. flagged_models maps
    model name → reason (from values profiles); flagged entries stay in the
    list, clearly marked."""
    names = enabled_models or list(FINANCING_MODELS)
    flagged = flagged_models or {}
    out = []
    for name in names:
        model = FINANCING_MODELS.get(name)
        if model is None:
            continue
        q = model.quote(principal, months, annual_rate)
        if name in flagged:
            q["flagged"] = True
            q["flag_reason"] = flagged[name]
        out.append(q)
    out.sort(key=lambda q: q["total_cost"])
    return out


def flagged_models_from_profiles(profiles: list[dict]) -> dict[str, str]:
    """Extract financing_type rules from enabled ValuesProfiles →
    {model_name: reason}."""
    flagged: dict[str, str] = {}
    for prof in profiles or []:
        for rule in prof.get("rules", []):
            if rule.get("kind") != "financing_type":
                continue
            for m in rule.get("flagged_models", []):
                flagged[m] = (f"{rule.get('reason', 'flagged by values profile')} "
                              f"[profile: {prof.get('name')}]")
    return flagged
