"""The editable surface of preferences.yaml — what the dashboard may change, and how.

One spec (FIELDS) drives the whole feature: the Textual screen renders a widget per field,
tests iterate the same list, and validation lives here rather than in UI code. The yaml file
stays the single source of truth for every consumer (run_update, Robo Kash, the dashboard);
this module only knows how to change it safely.

Deliberately NOT editable from the UI: telegram recipient/health IDs and the bot access list
(a typo cuts off alerts or messages a stranger — those stay hand-edited), the LLM ladders and
budgets (structure, not preference), and finance assumptions.

Saving comments: yaml.safe_dump cannot preserve them, so the live file is machine-managed and
the documented reference copy is preferences.example.yaml. Every save writes a timestamped
backup first.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field as dc_field
from datetime import datetime

import yaml

# All Staten Island ZIPs. The universe merges in whatever the file currently holds, so a
# hand-added ZIP outside this list still shows up (toggled on) rather than being dropped.
SI_ZIPS = [f"103{i:02d}" for i in range(1, 15)]
PROPERTY_TYPES = ["sf_detached", "sf_semi", "sf_attached",
                  "2fam_detached", "2fam_semi", "2fam_colonial",
                  "condo", "apartment", "lot"]
FLOOD_ZONES = ["X", "X500", "A", "AE", "AO", "AH", "VE"]
LLM_BACKENDS = ["auto", "codex", "claude_cli", "agy_cli"]


@dataclass(frozen=True)
class Field:
    path: str                # dotted path into the prefs dict
    label: str
    group: str
    kind: str                # int | float | multi | choice
    lo: float | None = None
    hi: float | None = None
    universe: list = dc_field(default_factory=list)


FIELDS = [
    Field("zips", "ZIP allow-list", "Scope", "multi", universe=SI_ZIPS),
    Field("price.min", "Price min ($)", "Scope", "int", lo=50_000, hi=5_000_000),
    Field("price.max", "Price max ($)", "Scope", "int", lo=50_000, hi=5_000_000),
    Field("beds_min", "Beds minimum", "Scope", "int", lo=1, hi=8),
    Field("eligibility.store_min_baths", "Baths minimum (stored)", "Scope", "float",
          lo=1, hi=6),
    Field("property_types", "Property types", "Scope", "multi", universe=PROPERTY_TYPES),
    Field("eligibility.telegram_max_price", "Alert price cap ($)", "Alerting", "int",
          lo=50_000, hi=5_000_000),
    Field("eligibility.telegram_min_baths", "Alert baths minimum", "Alerting", "float",
          lo=1, hi=6),
    Field("eligibility.safe_flood_zones", "Safe flood zones", "Alerting", "multi",
          universe=FLOOD_ZONES),
    Field("sources.zillow.every_days", "Zillow cadence (days)", "Sources", "int",
          lo=1, hi=30),
    Field("sources.zillow.results_limit", "Zillow results per run", "Sources", "int",
          lo=1, hi=200),
    Field("sources.rentcast.every_days", "RentCast cadence (days)", "Sources", "int",
          lo=1, hi=30),
    Field("llm.backend", "LLM model pin", "LLM", "choice", universe=LLM_BACKENDS),
]

# Shown on the screen but never editable there.
READ_ONLY_PATHS = ["telegram_recipient_ids", "telegram_health_recipient_ids"]


def get_value(prefs: dict, path: str):
    node = prefs
    for part in path.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def set_value(prefs: dict, path: str, value) -> None:
    parts = path.split(".")
    node = prefs
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


def universe_for(f: Field, prefs: dict) -> list[str]:
    """The options to offer for a multi field: the known universe plus anything the file
    already holds, so a hand-added value is preserved rather than silently dropped."""
    current = [str(v) for v in (get_value(prefs, f.path) or [])]
    extras = [v for v in current if v not in f.universe]
    return list(f.universe) + sorted(extras)


def coerce(f: Field, raw):
    """Parse one field's raw UI value into its typed form, or raise ValueError."""
    if f.kind == "multi":
        vals = [str(v) for v in (raw or [])]
        if not vals:
            raise ValueError(f"{f.label}: at least one must be selected")
        return vals
    if f.kind == "choice":
        v = str(raw or "").strip()
        if v not in f.universe:
            raise ValueError(f"{f.label}: must be one of {', '.join(f.universe)}")
        return v
    text = str(raw if raw is not None else "").strip().replace(",", "").replace("$", "")
    if not text:
        raise ValueError(f"{f.label}: required")
    try:
        val = int(text) if f.kind == "int" else float(text)
    except ValueError:
        raise ValueError(f"{f.label}: not a number: {text!r}") from None
    if f.kind == "float" and val == int(val):
        val = int(val)          # keep 2.0 as 2 so the yaml stays tidy
    if f.lo is not None and val < f.lo:
        raise ValueError(f"{f.label}: minimum is {f.lo:,}")
    if f.hi is not None and val > f.hi:
        raise ValueError(f"{f.label}: maximum is {f.hi:,}")
    return val


def cross_validate(prefs: dict) -> list[str]:
    """Rules that span fields — checked on the fully-updated dict before saving."""
    errors = []
    lo, hi = get_value(prefs, "price.min"), get_value(prefs, "price.max")
    if lo and hi and lo >= hi:
        errors.append("Price min must be below price max")
    cap = get_value(prefs, "eligibility.telegram_max_price")
    if cap and lo and cap < lo:
        errors.append("Alert price cap is below price min — no listing could ever alert")
    zips = get_value(prefs, "zips") or []
    bad = [z for z in zips if not (str(z).isdigit() and len(str(z)) == 5)]
    if bad:
        errors.append(f"Not 5-digit ZIPs: {', '.join(map(str, bad))}")
    return errors


def apply(prefs: dict, values: dict[str, object]) -> tuple[dict, list[str]]:
    """values maps field path -> raw UI value. Returns (new prefs, errors); the input dict
    is never mutated, so a failed validation leaves nothing half-applied."""
    import copy
    new = copy.deepcopy(prefs)
    errors = []
    by_path = {f.path: f for f in FIELDS}
    for path, raw in values.items():
        f = by_path.get(path)
        if f is None:
            errors.append(f"Not an editable preference: {path}")
            continue
        try:
            set_value(new, path, coerce(f, raw))
        except ValueError as e:
            errors.append(str(e))
    if not errors:
        errors = cross_validate(new)
    return new, errors


def save_with_backup(prefs: dict, path: str) -> str:
    """Back up the current file, then atomically replace it. Returns the backup path
    ('' on a first save with no existing file)."""
    backup = ""
    if os.path.exists(path):
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        bdir = os.path.join(os.path.dirname(os.path.abspath(path)), "archive", "prefs_backups")
        os.makedirs(bdir, exist_ok=True)
        backup = os.path.join(bdir, f"preferences-{stamp}.yaml")
        with open(path, "rb") as src, open(backup, "wb") as dst:
            dst.write(src.read())
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("# Managed by the dashboard Preferences screen (F6).\n"
                "# Field documentation lives in preferences.example.yaml.\n")
        yaml.safe_dump(prefs, f, sort_keys=False, default_flow_style=False,
                       allow_unicode=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    return backup
