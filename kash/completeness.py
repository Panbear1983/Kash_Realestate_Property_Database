"""What "complete" means for a listing, and how far from it the pool actually is.

One declarative table is the single source of truth: which filler owns each field, whether an
empty value blocks a Telegram alert, and whether it is worth retrying. Everything else — the
per-cycle report, the backfill runner, the dashboard line — reads from here rather than
hardcoding its own idea of what matters.

The distinction that makes the report usable is `filler`. A field with no filler is human-owned
(`my_notes`, `favorite`, `viewing_status`) and is never counted as missing — otherwise the audit
drowns in "gaps" that are simply blanks the buyer has not filled in yet. Only machine-owned
fields with a responsible filler can be genuinely incomplete.
"""
from __future__ import annotations

from typing import Optional

# filler:       which module fills this, and therefore which backfill step owns it
# blocks_alert: an empty value stops the listing reaching Telegram
# retryable:    worth another attempt later (vs. a source that will simply never have it)
FIELD_SPEC: dict[str, dict] = {
    # --- geography -------------------------------------------------------------------------
    "latitude":       {"filler": "enrich.geocode", "blocks_alert": False, "retryable": True},
    "longitude":      {"filler": "enrich.geocode", "blocks_alert": False, "retryable": True},
    "flood_zone":     {"filler": "enrich.flood",   "blocks_alert": True,  "retryable": True},
    "neighborhood":   {"filler": "geo_static",     "blocks_alert": False, "retryable": True},
    "school_name":    {"filler": "schools",        "blocks_alert": False, "retryable": True},

    # --- core listing facts (from the search adapter) ----------------------------------------
    "street_address": {"filler": "adapter",        "blocks_alert": True,  "retryable": False},
    "list_price":     {"filler": "adapter",        "blocks_alert": True,  "retryable": False},
    "listing_url":    {"filler": "adapter",        "blocks_alert": True,  "retryable": False},
    "beds":           {"filler": "adapter",        "blocks_alert": False, "retryable": False},
    "baths":          {"filler": "adapter",        "blocks_alert": True,  "retryable": False},
    "property_type":  {"filler": "adapter",        "blocks_alert": False, "retryable": False},

    # --- detail scrape -----------------------------------------------------------------------
    "year_built":          {"filler": "enrich.detail", "blocks_alert": False, "retryable": True},
    "lot_size_sqft":       {"filler": "enrich.detail", "blocks_alert": False, "retryable": True},
    "property_tax_annual": {"filler": "enrich.detail", "blocks_alert": False, "retryable": True},
    "basement":            {"filler": "enrich.detail", "blocks_alert": False, "retryable": True},
    "heating":             {"filler": "enrich.detail", "blocks_alert": False, "retryable": True},
    "cooling":             {"filler": "enrich.detail", "blocks_alert": False, "retryable": True},
    "mls_number":          {"filler": "enrich.detail", "blocks_alert": False, "retryable": True},
    "listing_description": {"filler": "enrich.detail", "blocks_alert": False, "retryable": True},

    # --- derived from the description --------------------------------------------------------
    "signal_extracted_at": {"filler": "enrich.describe", "blocks_alert": False, "retryable": True},

    # --- model judgement ----------------------------------------------------------------------
    "tier":          {"filler": "rank", "blocks_alert": False, "retryable": True},
    "view_priority": {"filler": "rank", "blocks_alert": False, "retryable": True},
    "analysis":      {"filler": "rank", "blocks_alert": False, "retryable": True},

    # --- human-owned: never counted as missing -----------------------------------------------
    "my_notes":         {"filler": None, "blocks_alert": False, "retryable": False},
    "investment_thesis": {"filler": None, "blocks_alert": False, "retryable": False},
    "favorite":         {"filler": None, "blocks_alert": False, "retryable": False},
    "viewing_status":   {"filler": None, "blocks_alert": False, "retryable": False},
    "offer_status":     {"filler": None, "blocks_alert": False, "retryable": False},
    "user_rating":      {"filler": None, "blocks_alert": False, "retryable": False},
    "property_id":      {"filler": None, "blocks_alert": False, "retryable": False},
    "rank":             {"filler": None, "blocks_alert": False, "retryable": False},
}

MACHINE_FIELDS = [f for f, s in FIELD_SPEC.items() if s["filler"]]
ALERT_FIELDS = [f for f, s in FIELD_SPEC.items() if s["blocks_alert"]]


def is_missing(row: dict, field: str) -> bool:
    return row.get(field) in (None, "")


def missing_fields(row: dict, alert_only: bool = False) -> list[str]:
    fields = ALERT_FIELDS if alert_only else MACHINE_FIELDS
    return [f for f in fields if is_missing(row, f)]


def audit(store, ledger=None, prefs=None) -> dict:
    """Per-field coverage plus the rows behind it, grouped by the filler responsible.

    Curated and scraped rows are reported separately: the curated set was hand-entered and its
    blanks mean something different from a scraper's.

    With `prefs`, rows the admission policy rejects (excluded property types, sub-minimum
    baths) are left out of the blocked-from-alerting counts: a land lot with no bath count
    is excluded by design, not blocked — counting it kept a permanent phantom problem in the
    health alerts that no filler could ever resolve.
    """
    rows = store.all()
    curated = [r for r in rows if r.get("property_id")]
    scraped = [r for r in rows if not r.get("property_id")]

    def counts_as_blocked(row) -> bool:
        if not missing_fields(row, alert_only=True):
            return False
        if prefs is not None:
            from . import eligibility
            if not eligibility.classify(row, prefs).admit:
                return False
        return True

    fields = {}
    for field in MACHINE_FIELDS:
        miss_scraped = [r for r in scraped if is_missing(r, field)]
        miss_curated = [r for r in curated if is_missing(r, field)]
        fields[field] = {
            "filler": FIELD_SPEC[field]["filler"],
            "blocks_alert": FIELD_SPEC[field]["blocks_alert"],
            "retryable": FIELD_SPEC[field]["retryable"],
            "scraped_missing": len(miss_scraped),
            "scraped_total": len(scraped),
            "curated_missing": len(miss_curated),
            "curated_total": len(curated),
            "coverage": (1 - len(miss_scraped) / len(scraped)) if scraped else 1.0,
        }

    by_filler: dict[str, int] = {}
    for field, info in fields.items():
        if info["scraped_missing"]:
            by_filler[info["filler"]] = by_filler.get(info["filler"], 0) + info["scraped_missing"]

    blocked = [r for r in scraped if counts_as_blocked(r)]
    # Curated rows are gated by the same rule, so their gaps were silently costing the buyer
    # alerts on their own hand-picked houses while the report only ever mentioned scraped rows.
    blocked_curated = [r for r in curated if counts_as_blocked(r)]

    out = {
        "pool_size": len(rows),
        "curated": len(curated),
        "scraped": len(scraped),
        "fields": fields,
        "by_filler": by_filler,
        "alert_blocked": len(blocked),
        "alert_blocked_reasons": _reasons(blocked),
        "curated_alert_blocked": len(blocked_curated),
        "curated_alert_blocked_reasons": _reasons(blocked_curated),
    }
    if ledger is not None:
        out["ledger"] = ledger.summary()
    return out


def _reasons(blocked_rows) -> dict[str, int]:
    counts: dict[str, int] = {}
    for r in blocked_rows:
        for f in missing_fields(r, alert_only=True):
            counts[f] = counts.get(f, 0) + 1
    return counts


def format_report(a: dict, verbose: bool = False) -> str:
    """Human-readable summary for run_update and run_backfill."""
    lines = [f"completeness: {a['scraped']} scraped rows, "
             f"{a['alert_blocked']} blocked from alerting"]
    if a["alert_blocked_reasons"]:
        why = ", ".join(f"{f} ({n})" for f, n in sorted(a["alert_blocked_reasons"].items()))
        lines.append(f"  alert-blocking gaps: {why}")
    if a.get("curated_alert_blocked"):
        why = ", ".join(f"{f} ({n})" for f, n
                        in sorted(a.get("curated_alert_blocked_reasons", {}).items()))
        lines.append(f"  curated: {a['curated']} rows, {a['curated_alert_blocked']} cannot "
                     f"alert — {why}")
    gaps = [(f, i) for f, i in a["fields"].items() if i["scraped_missing"]]
    if verbose and gaps:
        lines.append("  by field:")
        for f, i in sorted(gaps, key=lambda kv: -kv[1]["scraped_missing"]):
            flag = "  [blocks alerts]" if i["blocks_alert"] else ""
            lines.append(f"    {f:22s} {i['scraped_missing']:3d}/{i['scraped_total']} missing "
                         f"({i['coverage']*100:.0f}% covered)  via {i['filler']}{flag}")
    for entry in a.get("ledger", []):
        if entry["outstanding"]:
            err = f" — last error: {entry['last_error']}" if entry["last_error"] else ""
            lines.append(f"  {entry['field']}: {entry['outstanding']} outstanding, "
                         f"up to {entry['max_attempts']} attempts{err}")
    return "\n".join(lines)
