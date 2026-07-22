"""Computed financial fields. Every function returns None when inputs are missing,
so partial-coverage rows never produce fabricated numbers.

Defaults come from the doc's stated assumptions (6.3% rate) and are overridable via
preferences.yaml -> finance.
"""
from __future__ import annotations

from typing import Optional

DEFAULTS = {
    "rate": 0.063,            # 30-yr fixed, from the doc
    "term_years": 30,
    "down_payment_pct": 0.20,
    "insurance_pct": 0.005,   # annual, of price, when not otherwise known
    "tax_pct_fallback": 0.01,  # annual property tax est. when property_tax_annual is null
    "noi_ratio": 0.60,        # net operating income as a share of gross rent (rough)
    "closing_pct": 0.03,      # closing costs as a share of price, for cash-invested
}


def price_per_sqft(list_price: Optional[int], sqft: Optional[int]) -> Optional[int]:
    if list_price and sqft:
        return round(list_price / sqft)
    return None


def price_drop_pct(original: Optional[int], current: Optional[int]) -> Optional[float]:
    if original and current and original > current:
        return round((original - current) / original * 100, 1)
    return None


def _monthly_pi(loan: float, rate: float, term_years: int) -> float:
    r = rate / 12
    n = term_years * 12
    if r == 0:
        return loan / n
    return loan * (r * (1 + r) ** n) / ((1 + r) ** n - 1)


def monthly_piti(rec: dict, cfg: dict) -> Optional[int]:
    price = rec.get("list_price")
    if not price:
        return None
    loan = price * (1 - cfg["down_payment_pct"])
    pi = _monthly_pi(loan, cfg["rate"], cfg["term_years"])
    tax_annual = rec.get("property_tax_annual")
    if tax_annual is None:
        tax_annual = price * cfg["tax_pct_fallback"]
    insurance = price * cfg["insurance_pct"] / 12
    hoa = rec.get("hoa_monthly") or 0
    return round(pi + tax_annual / 12 + insurance + hoa)


def cap_rate(rec: dict, cfg: dict) -> Optional[float]:
    rent = rec.get("estimated_rent_monthly")
    price = rec.get("list_price")
    if not rent or not price:
        return None
    noi = rent * 12 * cfg["noi_ratio"]
    return round(noi / price * 100, 2)


def cash_on_cash(rec: dict, cfg: dict) -> Optional[float]:
    rent = rec.get("estimated_rent_monthly")
    price = rec.get("list_price")
    if not rent or not price:
        return None
    loan = price * (1 - cfg["down_payment_pct"])
    annual_ds = _monthly_pi(loan, cfg["rate"], cfg["term_years"]) * 12
    noi = rent * 12 * cfg["noi_ratio"]
    cash_invested = price * cfg["down_payment_pct"] + price * cfg["closing_pct"]
    if cash_invested <= 0:
        return None
    return round((noi - annual_ds) / cash_invested * 100, 2)


def recompute(rec: dict, cfg: Optional[dict] = None) -> dict:
    """Return rec with all [calc] fields recomputed in place."""
    cfg = {**DEFAULTS, **(cfg or {})}
    rec["price_per_sqft"] = price_per_sqft(rec.get("list_price"), rec.get("sqft"))
    rec["price_drop_pct"] = price_drop_pct(rec.get("original_list_price"), rec.get("list_price"))
    rec["monthly_piti"] = monthly_piti(rec, cfg)
    rec["cap_rate"] = cap_rate(rec, cfg)
    rec["cash_on_cash"] = cash_on_cash(rec, cfg)
    return rec
