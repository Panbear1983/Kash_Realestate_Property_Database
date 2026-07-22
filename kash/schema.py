"""The Kash listing schema — the single contract every source normalizes into.

This is the ~81-column schema locked in ROADMAP.md. The pydantic model validates
every record at the boundary; the field-classification sets below drive the store
(SQLite column types) and the merge logic (which fields a source may overwrite).
"""
from __future__ import annotations

from typing import Optional, List, Any, Literal

from pydantic import BaseModel, ConfigDict, model_validator

# --- field classification (single source of truth for store + merge) -----------

BOOL_FIELDS = {"is_multifamily", "school_verified", "favorite", "contacted_agent"}
INT_FIELDS = {
    "rank", "list_price", "sqft", "price_per_sqft", "days_on_market", "zestimate",
    "target_buy_price", "arv_estimate", "brrrr_rating", "bid_estimate",
    "year_built", "lot_size_sqft", "property_tax_annual", "hoa_monthly",
    "garage_spaces", "photo_count", "original_list_price", "redfin_estimate",
    "last_sold_price", "estimated_rent_monthly", "monthly_piti",
    "times_relisted", "walk_score", "transit_score", "commute_minutes", "user_rating",
}
REAL_FIELDS = {
    "appreciation_pct", "price_drop_pct", "cap_rate", "cash_on_cash",
    "latitude", "longitude",
}
JSON_FIELDS = {"price_history"}

# Fields a data source must NEVER overwrite on merge — your judgment + workflow state.
USER_PROTECTED = {
    "investment_thesis", "analysis", "my_notes", "rank", "tier",
    "favorite", "viewing_status", "viewing_date", "offer_status",
    "user_rating", "contacted_agent",
}

# Recomputed on every write from other columns — never sourced directly.
CALC_FIELDS = {"price_per_sqft", "price_drop_pct", "cap_rate", "cash_on_cash", "monthly_piti"}


class Listing(BaseModel):
    """One property. Every field except the identity keys is optional so a source
    with partial coverage never breaks a row."""

    model_config = ConfigDict(extra="ignore")

    # --- identity / workflow (original 35) ---
    property_id: Optional[str] = None
    rank: Optional[int] = None
    status: Literal["active", "pending", "attorney_review", "off_market", "sold"] = "active"
    view_priority: Optional[Literal["now", "soon", "worth", "call", "watch", "skip"]] = None
    priority_note: Optional[str] = None
    tier: Optional[str] = None
    neighborhood: Optional[str] = None
    street_address: Optional[str] = None
    zip: Optional[str] = None
    listing_url: Optional[str] = None
    property_type: Optional[str] = None
    is_multifamily: Optional[bool] = None
    list_price: Optional[int] = None
    price_note: Optional[str] = None
    beds: Optional[str] = None          # str: source data includes ranges like "3-4"
    baths: Optional[str] = None
    bd_ba_note: Optional[str] = None
    sqft: Optional[int] = None
    price_per_sqft: Optional[int] = None
    days_on_market: Optional[int] = None
    zestimate: Optional[int] = None
    zestimate_source: Optional[str] = None
    school_name: Optional[str] = None
    school_gs_rating: Optional[str] = None
    school_verified: Optional[bool] = None
    target_buy_price: Optional[int] = None
    arv_estimate: Optional[int] = None
    appreciation_pct: Optional[float] = None
    brrrr_rating: Optional[int] = None
    bid_estimate: Optional[int] = None
    bid_note: Optional[str] = None
    investment_thesis: Optional[str] = None   # user-protected
    analysis: Optional[str] = None            # user-protected
    my_notes: Optional[str] = None            # user-protected
    last_updated: Optional[str] = None

    # --- A. core property facts ---
    year_built: Optional[int] = None
    lot_size_sqft: Optional[int] = None
    property_tax_annual: Optional[int] = None
    hoa_monthly: Optional[int] = None
    basement: Optional[str] = None
    condition: Optional[str] = None
    garage_spaces: Optional[int] = None
    heating: Optional[str] = None
    cooling: Optional[str] = None
    mls_number: Optional[str] = None
    listing_agent: Optional[str] = None
    listing_brokerage: Optional[str] = None
    photo_count: Optional[int] = None
    listing_description: Optional[str] = None

    # --- B. valuation / financial ---
    original_list_price: Optional[int] = None
    price_history: Optional[List[Any]] = None      # list of {"date","price"}
    price_drop_pct: Optional[float] = None          # calc
    redfin_estimate: Optional[int] = None
    last_sold_price: Optional[int] = None
    last_sold_date: Optional[str] = None
    estimated_rent_monthly: Optional[int] = None
    cap_rate: Optional[float] = None                # calc
    cash_on_cash: Optional[float] = None            # calc
    monthly_piti: Optional[int] = None              # calc

    # --- C. lifecycle / tracking ---
    first_seen_date: Optional[str] = None
    listing_date: Optional[str] = None
    pending_date: Optional[str] = None
    sold_date: Optional[str] = None
    times_relisted: Optional[int] = None

    # --- D1. geospatial (populated in Phase 4) ---
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    flood_zone: Optional[str] = None
    walk_score: Optional[int] = None
    transit_score: Optional[int] = None
    commute_minutes: Optional[int] = None

    # --- D2. personal / CRM (user-set) ---
    favorite: Optional[bool] = None
    viewing_status: Optional[Literal["none", "scheduled", "seen", "skip"]] = None
    viewing_date: Optional[str] = None
    offer_status: Optional[Literal["none", "considering", "offered", "rejected", "accepted"]] = None
    user_rating: Optional[int] = None
    contacted_agent: Optional[bool] = None

    # --- D3. media ---
    primary_photo_url: Optional[str] = None
    virtual_tour_url: Optional[str] = None

    # --- provenance ---
    source: Optional[str] = None
    fetched_at: Optional[str] = None
    source_url: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def _clean(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        out = {}
        for k, v in data.items():
            if isinstance(v, str):
                v = v.strip()
                if v == "":
                    v = None
            if k in BOOL_FIELDS and isinstance(v, str):
                v = v.lower() in ("true", "1", "yes", "y", "t")
            out[k] = v
        return out


# Canonical column order = model definition order.
FIELD_ORDER = list(Listing.model_fields.keys())


def sqlite_type(field: str) -> str:
    if field in INT_FIELDS or field in BOOL_FIELDS:
        return "INTEGER"
    if field in REAL_FIELDS:
        return "REAL"
    return "TEXT"  # str, date, and JSON (stored as text)
