"""The buyer's own record of a house: touring, offers, notes — the write path for the
fields no data source is ever allowed to touch.

Every column here is USER_PROTECTED, which until now meant "nothing may write it": merges
skip these fields, the contribution system excludes them from PUBLIC_EDITABLE_FIELDS, and
chat_policy keeps them out of what a non-owner can even read. The result was that the one
person entitled to write them had no way to do it except editing SQLite by hand.

This module is that path, and the only place that passes `allow_protected=True` outside
ranking. It validates before it writes (a bad enum value used to be dropped silently by the
store's coercion, so a typo'd status looked accepted and changed nothing), and it records
each change in the changelog so the pool's history shows the buyer's decisions next to the
market's.

Recording intent has a side effect worth knowing: kash/lifecycle.py never ages out a house
you have favorited, booked a viewing on, made an offer on, or contacted the agent about —
so telling Kash you are touring a place also protects it from being marked off_market on a
quiet search.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

VIEWING_STATUS = ["none", "scheduled", "seen", "skip"]
OFFER_STATUS = ["none", "considering", "offered", "rejected", "accepted"]
MAX_NOTE = 2000


@dataclass(frozen=True)
class CrmField:
    name: str
    label: str
    kind: str                  # choice | date | bool | rating | text
    options: tuple = ()


FIELDS = [
    CrmField("viewing_status", "Viewing", "choice", tuple(VIEWING_STATUS)),
    CrmField("viewing_date", "Viewing date (YYYY-MM-DD)", "date"),
    CrmField("offer_status", "Offer", "choice", tuple(OFFER_STATUS)),
    CrmField("contacted_agent", "Contacted agent", "bool"),
    CrmField("favorite", "Favorite", "bool"),
    CrmField("user_rating", "My rating (1-5)", "rating"),
    CrmField("my_notes", "My notes", "text"),
]
BY_NAME = {f.name: f for f in FIELDS}


class CrmError(ValueError):
    """A value the buyer typed that cannot be stored as-is."""


def _clean(raw):
    return raw.strip() if isinstance(raw, str) else raw


def coerce(field: CrmField, raw):
    """One field's UI value -> what goes in the column. Blank clears the field (None)."""
    value = _clean(raw)
    if value is None or value == "":
        return None

    if field.kind == "choice":
        v = str(value).lower()
        if v not in field.options:
            raise CrmError(f"{field.label}: must be one of {', '.join(field.options)}")
        return None if v == "none" else v

    if field.kind == "date":
        try:
            parsed = datetime.strptime(str(value), "%Y-%m-%d").date()
        except ValueError:
            raise CrmError(f"{field.label}: use YYYY-MM-DD, got {value!r}") from None
        return parsed.isoformat()

    if field.kind == "bool":
        if isinstance(value, bool):
            return value
        v = str(value).strip().lower()
        if v in ("y", "yes", "true", "1", "on"):
            return True
        if v in ("n", "no", "false", "0", "off"):
            return False
        raise CrmError(f"{field.label}: yes or no, got {value!r}")

    if field.kind == "rating":
        try:
            n = int(str(value))
        except ValueError:
            raise CrmError(f"{field.label}: a whole number 1-5, got {value!r}") from None
        if not 1 <= n <= 5:
            raise CrmError(f"{field.label}: must be between 1 and 5")
        return n

    text = str(value)
    if len(text) > MAX_NOTE:
        raise CrmError(f"{field.label}: keep it under {MAX_NOTE} characters "
                       f"({len(text)} given)")
    return text


def implied(values: dict) -> dict:
    """The one inference worth making: a viewing date means a viewing is scheduled.

    Deliberately the only one. Guessing further (an offer implies the agent was contacted,
    a past date implies you went) would put words in the buyer's mouth in their own record.
    """
    out = dict(values)
    if out.get("viewing_date") and not out.get("viewing_status"):
        out["viewing_status"] = "scheduled"
    return out


def describe_change(field_name: str, old, new) -> str:
    """The changelog line for one field, written for a human reading history later."""
    label = BY_NAME[field_name].label
    if field_name == "my_notes":
        return "notes updated" if new is not None else "notes cleared"

    def shown(v, empty):
        return {True: "yes", False: "no", None: empty}.get(v, v)

    return f"{label}: {shown(old, 'unset')} -> {shown(new, 'cleared')}"


def apply(store, key: str, values: dict, today: str | None = None) -> dict:
    """Validate and write the buyer's fields for one listing.

    Returns {"changed": {field: new}, "notes": [human lines]}. Raises CrmError before
    writing anything if any value is bad — a half-applied CRM record is worse than none.
    """
    row = store.get(key)
    if row is None:
        raise CrmError(f"no listing with key {key}")

    typed = {}
    for name, raw in values.items():
        field = BY_NAME.get(name)
        if field is None:
            raise CrmError(f"{name} is not a field you can set here")
        typed[name] = coerce(field, raw)
    typed = implied(typed)

    changed = {n: v for n, v in typed.items() if row.get(n) != v}
    if not changed:
        return {"changed": {}, "notes": []}

    store.update_fields(key, changed, allow_protected=True)
    lines = [describe_change(n, row.get(n), v) for n, v in changed.items()]
    address = row.get("street_address") or key
    store._log(key, "crm_update", f"{address}: " + "; ".join(lines), "owner")
    store.conn.commit()
    return {"changed": changed, "notes": lines}


def summary(row: dict) -> str:
    """One line for the dashboard: what the buyer has recorded about this house."""
    bits = []
    if row.get("favorite"):
        bits.append("favorite")
    vs = row.get("viewing_status")
    if vs and vs != "none":
        when = row.get("viewing_date")
        bits.append(f"viewing {vs}" + (f" {when}" if when else ""))
    os_ = row.get("offer_status")
    if os_ and os_ != "none":
        bits.append(f"offer {os_}")
    if row.get("contacted_agent"):
        bits.append("agent contacted")
    if row.get("user_rating"):
        bits.append(f"rated {row['user_rating']}/5")
    return " · ".join(bits) or "nothing recorded yet"


def upcoming_viewings(store, today: str | None = None) -> list[dict]:
    """Scheduled viewings from today onward, soonest first — the touring calendar."""
    today = today or date.today().isoformat()
    out = []
    for key, in store.conn.execute("SELECT match_key FROM listings").fetchall():
        row = store.get(key)
        if not row or row.get("viewing_status") != "scheduled":
            continue
        when = row.get("viewing_date")
        if when and when >= today:
            out.append({**row, "match_key": key})
    return sorted(out, key=lambda r: r.get("viewing_date") or "")
