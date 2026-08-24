"""Pure recognition for the small, allow-listed local aggregate surface.

This module accepts no SQL, filters, or arbitrary fields from chat text.  It only recognizes
the explicit whole-pool average list-price question that the read-only store can answer without
rows, a model, or web access.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_AVERAGE = re.compile(r"\b(?:average|mean)\b", re.IGNORECASE)
_PRICE = re.compile(r"\b(?:price|list\s+price|cost)\b", re.IGNORECASE)
_ALL_LISTINGS = re.compile(
    r"\b(?:entire|whole|all)\s+(?:the\s+)?(?:database|pool|listings?)\b|"
    r"\bacross\s+(?:the\s+)?(?:entire|whole|all)\s+(?:database|pool|listings?)\b",
    re.IGNORECASE,
)

#: Non-price measures the router's aggregate vocabulary can average — their presence means
#: the question is a real (scoped) aggregate, not the bare "what is the average?".
_MEASURABLE = re.compile(
    r"\b(?:sqft|square\s+f(?:oo|ee)t|rent|year\s+built|days\s+on\s+market)\b",
    re.IGNORECASE,
)

SAFE_CLARIFY = "Please specify a supported aggregate, for example: average list price of the entire database."


@dataclass(frozen=True)
class OverallPricePlan:
    aggregate: str = "average_list_price"
    scope: str = "all_listings"


def classify(message: str) -> OverallPricePlan | str | None:
    """Recognize the whole-pool average, clarify the bare/underspecified ones, and return
    None for SCOPED averages ("average price in Great Kills", "average sqft…") — those now
    belong to the router's whitelisted aggregate vocabulary, which can actually scope them.
    Answering a scoped average with the whole-pool number was a wrong answer presented as
    a right one."""
    text = str(message or "")
    if not _AVERAGE.search(text):
        return None
    has_price = bool(_PRICE.search(text))
    if has_price and _ALL_LISTINGS.search(text):
        return OverallPricePlan()
    if has_price or _MEASURABLE.search(text):
        return None
    return SAFE_CLARIFY
