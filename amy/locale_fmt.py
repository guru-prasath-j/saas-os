"""Locale layer (Phase R7B) — money/number formatting per user preference.

Two grouping styles ship (both pure formatting, no locale tables needed):
  western  1,234,567.89
  indian   12,34,567.89   (lakh/crore grouping)

The symbol/grouping/decimals for a currency come from jurisdiction packs;
per-user overrides live on the users table (home_jurisdiction, language).
LLM prompts (briefings/digests) receive a locale hint via prompt_hint().
"""
from __future__ import annotations


def group_number(value: float, grouping: str = "western",
                 decimals: int = 2) -> str:
    neg = value < 0
    value = abs(float(value))
    whole = int(value)
    frac = f"{value - whole:.{decimals}f}"[2:] if decimals else ""
    s = str(whole)
    if grouping == "indian":
        if len(s) > 3:
            head, tail = s[:-3], s[-3:]
            parts = []
            while len(head) > 2:
                parts.insert(0, head[-2:])
                head = head[:-2]
            if head:
                parts.insert(0, head)
            s = ",".join(parts) + "," + tail
    else:
        parts = []
        while len(s) > 3:
            parts.insert(0, s[-3:])
            s = s[:-3]
        if s:
            parts.insert(0, s)
        s = ",".join(parts)
    out = s + (f".{frac}" if decimals and frac else "")
    return ("-" if neg else "") + out


def format_money(amount: float, currency: dict | None = None,
                 decimals: int | None = None, sign: bool = False) -> str:
    """currency: a pack currency block {code, symbol, grouping, decimals}.
    Falls back to a bare number when no currency is given."""
    cur = currency or {}
    symbol = cur.get("symbol", "")
    grouping = cur.get("grouping", "western")
    dec = decimals if decimals is not None else int(cur.get("decimals", 2))
    body = group_number(abs(amount), grouping, dec)
    prefix = "+" if (sign and amount > 0) else ("-" if amount < 0 else "")
    sep = " " if len(symbol) > 1 else ""   # "AED 100" vs "$100" / "₹100"
    return f"{prefix}{symbol}{sep}{body}" if symbol else f"{prefix}{body}"


def prompt_hint(currency: dict | None, language: str | None) -> str:
    """One-line locale hint injected into LLM prompts for briefings/digests."""
    cur = currency or {}
    bits = []
    if cur.get("code"):
        grouping = ("Indian lakh/crore digit grouping"
                    if cur.get("grouping") == "indian" else "standard grouping")
        bits.append(f"Currency: {cur['code']} ({cur.get('symbol', '')}), {grouping}.")
    if language:
        bits.append(f"Respond in language: {language}.")
    return " ".join(bits)
