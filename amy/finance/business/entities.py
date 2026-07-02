"""Business entity CRUD — thin validation layer over FinanceEngine's
business_entities table. The actual SQL lives in engine.py (consistent with
every other table); this module is what the router calls.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from ..engine import VALID_CONSTITUTIONS, VALID_TRACKING_CLOSENESS
from .sensitivity import GSTIN_RE, PAN_RE

if TYPE_CHECKING:
    from ..engine import FinanceEngine


def _validate(pan: str | None, gstin: str | None, constitution: str,
              tracking_closeness: str):
    if pan and not PAN_RE.fullmatch(pan.upper()):
        raise ValueError("PAN must match the format AAAAA9999A")
    if gstin and not GSTIN_RE.fullmatch(gstin.upper()):
        raise ValueError("GSTIN must be a valid 15-character GSTIN")
    if constitution not in VALID_CONSTITUTIONS:
        raise ValueError(f"constitution must be one of {VALID_CONSTITUTIONS}")
    if tracking_closeness not in VALID_TRACKING_CLOSENESS:
        raise ValueError(f"tracking_closeness must be one of {VALID_TRACKING_CLOSENESS}")


def create_entity(fe: "FinanceEngine", name: str, pan: str | None = None,
                  gstin: str | None = None, constitution: str = "proprietorship",
                  registration_state: str | None = None,
                  financial_year: str | None = None,
                  tax_regime: str | None = None,
                  holds_depreciable_assets: bool = False,
                  tracking_closeness: str = "loose") -> str:
    if not name or not name.strip():
        raise ValueError("name is required")
    pan = pan.upper() if pan else None
    gstin = gstin.upper() if gstin else None
    _validate(pan, gstin, constitution, tracking_closeness)
    return fe.add_business_entity(
        name=name.strip(), pan=pan, gstin=gstin, constitution=constitution,
        registration_state=registration_state, financial_year=financial_year,
        tax_regime=tax_regime, holds_depreciable_assets=holds_depreciable_assets,
        tracking_closeness=tracking_closeness)


def list_entities(fe: "FinanceEngine") -> list[dict]:
    return fe.list_business_entities()


def get_entity(fe: "FinanceEngine", entity_id: str) -> dict | None:
    return fe.get_business_entity(entity_id)


def update_entity(fe: "FinanceEngine", entity_id: str, **kwargs) -> bool:
    if "pan" in kwargs and kwargs["pan"]:
        kwargs["pan"] = kwargs["pan"].upper()
    if "gstin" in kwargs and kwargs["gstin"]:
        kwargs["gstin"] = kwargs["gstin"].upper()
    if "constitution" in kwargs and kwargs["constitution"] not in VALID_CONSTITUTIONS:
        raise ValueError(f"constitution must be one of {VALID_CONSTITUTIONS}")
    if "tracking_closeness" in kwargs and kwargs["tracking_closeness"] not in VALID_TRACKING_CLOSENESS:
        raise ValueError(f"tracking_closeness must be one of {VALID_TRACKING_CLOSENESS}")
    if kwargs.get("pan") and not PAN_RE.fullmatch(kwargs["pan"]):
        raise ValueError("PAN must match the format AAAAA9999A")
    if kwargs.get("gstin") and not GSTIN_RE.fullmatch(kwargs["gstin"]):
        raise ValueError("GSTIN must be a valid 15-character GSTIN")
    return fe.update_business_entity(entity_id, **kwargs)


def delete_entity(fe: "FinanceEngine", entity_id: str) -> bool:
    return fe.delete_business_entity(entity_id)
