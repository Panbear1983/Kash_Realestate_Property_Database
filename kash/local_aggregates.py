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

SAFE_CLARIFY = "Please specify a supported aggregate, for example: average list price of the entire database."


@dataclass(frozen=True)
class OverallPricePlan:
    aggregate: str = "average_list_price"
    scope: str = "all_listings"


def classify(message: str) -> OverallPricePlan | str | None:
    """Recognize one safe aggregate, clarify incomplete aggregate phrasing, otherwise no match."""
    text = str(message or "")
    if not _AVERAGE.search(text):
        return None
    if _PRICE.search(text) and _ALL_LISTINGS.search(text):
        return OverallPricePlan()
    if not _PRICE.search(text) or not _ALL_LISTINGS.search(text):
        return SAFE_CLARIFY
    return None
