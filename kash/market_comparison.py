"""Deterministic plans for local-versus-external property-market comparisons.

The property pool contains asking prices, so ordinary requests for a local average default to
an *active list-price* comparison.  The reply labels that assumption instead of forcing a user
to learn internal field names.  Sale-price questions and contradictory geographic scopes still
clarify before either the database or the web provider is touched.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


_CLARIFY_SCOPE = (
    "Do you want New York City or New York State for the external benchmark? "
    "I can compare active listing prices; for example: 'average Staten Island price versus "
    "New York State.'"
)
_CLARIFY_SALES = (
    "Kash's local pool has active listing prices, not verified closed-sale prices. "
    "Should I compare active listing prices instead?"
)
_AVERAGE = re.compile(r"\b(?:average|mean)\b", re.IGNORECASE)
_LOCAL = re.compile(r"\b(?:staten island|si)\b", re.IGNORECASE)
_PRICE = re.compile(r"\b(?:list(?:ing)?\s+price|sale\s+price|price|cost)\b", re.IGNORECASE)
_SALE = re.compile(r"\b(?:sale|sold|closed)\s+price\b", re.IGNORECASE)
_NY_STATE = re.compile(r"\b(?:new\s+york|ny)\s+state\b", re.IGNORECASE)
_NYC = re.compile(r"\b(?:nyc|new\s+york\s+city)\b", re.IGNORECASE)
_EXCLUDING = re.compile(r"\b(?:excluding|outside|not\s+in|minus|rest\s+of)\b", re.IGNORECASE)
_CONTRADICTORY_NYC_STATE = re.compile(r"\bnyc\s+state\b", re.IGNORECASE)


@dataclass(frozen=True)
class MarketComparison:
    """A fixed comparison plan; no user text becomes SQL or a provider request verbatim."""

    web_query: str
    external_label: str


def classify(message: str) -> MarketComparison | str | None:
    """Return a safe fixed plan, a concise clarification, or ``None`` for ordinary chat.

    A normal request such as "average cost of Staten Island versus New York State" is enough:
    Kash defaults to active list prices and states that assumption.  It must not fall through
    to listing search merely because a user did not say "database" or "active list price".
    """
    text = str(message or "")
    candidate = bool(_AVERAGE.search(text) and _LOCAL.search(text) and _PRICE.search(text)
                     and (_NY_STATE.search(text) or _NYC.search(text)))
    if not candidate:
        return None
    if _SALE.search(text):
        return _CLARIFY_SALES

    has_state = bool(_NY_STATE.search(text))
    has_city = bool(_NYC.search(text))
    excludes = bool(_EXCLUDING.search(text))
    if _CONTRADICTORY_NYC_STATE.search(text) or (has_state and has_city and not excludes):
        return _CLARIFY_SCOPE
    if has_state and has_city:
        return MarketComparison(
            web_query="current average active listing price New York State excluding New York City",
            external_label="New York State excluding New York City",
        )
    if has_state:
        return MarketComparison(
            web_query="current average active listing price New York State",
            external_label="New York State",
        )
    if excludes:
        return MarketComparison(
            web_query="current average active listing price New York City excluding Staten Island",
            external_label="New York City excluding Staten Island",
        )
    return MarketComparison(
        web_query="current average active listing price New York City",
        external_label="New York City",
    )


def comparable_results(plan: MarketComparison, results):
    """Keep only explicitly comparable active-listing *average* source snippets.

    This is deliberately conservative: an unavailable apples-to-apples comparison is safer
    than allowing model prose to blend a local mean listing price with an external median or
    closed-sale statistic.
    """
    accepted = []
    geography = plan.external_label.lower()
    for result in results:
        text = f"{getattr(result, 'title', '')} {getattr(result, 'snippet', '')}".lower()
        if any(word in text for word in ("median", "sale", "sold", "closed")):
            continue
        if "average" not in text or "listing" not in text:
            continue
        if "new york" not in text and "statewide" not in text:
            continue
        if "$" not in text:
            continue
        # For specific labels, the source must at least state the governing geography.
        if "new york city" in geography and "new york city" not in text and "nyc" not in text:
            continue
        accepted.append(result)
    return tuple(accepted)
