"""Deterministic review signals extracted from provider listing descriptions."""
from __future__ import annotations

from dataclasses import dataclass


_MULTIGENERATIONAL = ("separate entrance", "second kitchen", "dual meter", "dual electric meters", "in-law suite", "lower level studio")
_RENOVATION = ("as is", "tlc")
_FRICTION = ("busy road", "commercial")


@dataclass(frozen=True)
class DescriptionSignal:
    multigenerational: bool
    review_warning: bool
    reject: bool
    reasons: tuple[str, ...]


def score_description(description: str | None) -> DescriptionSignal:
    text = (description or "").lower()
    reasons = tuple(term for term in _MULTIGENERATIONAL + _RENOVATION + _FRICTION if term in text)
    return DescriptionSignal(
        multigenerational=any(term in text for term in _MULTIGENERATIONAL),
        review_warning=any(term in text for term in _RENOVATION + _FRICTION),
        reject=False,
        reasons=reasons,
    )


def priority_note(description: str | None) -> str | None:
    signal = score_description(description)
    return "; ".join(signal.reasons) or None


def multigenerational(listing: dict) -> tuple[bool, str]:
    """Whether a listing offers a self-contained second living area, and why.

    Uses the semantic read in the `signal_*` columns (kash/enrich/describe.py), which catches
    phrasings the keyword list misses — "private side entry to a finished basement with
    kitchenette" is a mother/daughter setup containing none of the literal terms — and falls
    back to substring matching for rows not yet extracted or when the extract rung was down.

    The two are unioned rather than the extraction replacing the keyword check: for this buyer
    a missed multigenerational home is the expensive error, and a union means adding extraction
    can only ever surface more candidates, never silently drop one the old path caught.
    """
    if listing.get("signal_multigenerational") or listing.get("signal_separate_entrance"):
        reason = (listing.get("signal_evidence") or "").strip()
        if listing.get("signal_separate_entrance"):
            return True, reason or "separate entrance"
        return True, reason or "multigenerational layout"
    signal = score_description(listing.get("listing_description"))
    if "separate entrance" in signal.reasons:
        return True, "separate entrance"
    return False, ""
