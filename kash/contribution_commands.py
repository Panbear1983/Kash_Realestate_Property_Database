"""Deterministic, constrained Telegram text templates for staged contribution proposals."""
from __future__ import annotations

from .contributions import PROPOSAL_ADD, PROPOSAL_CORRECT, PROPOSAL_RETIRE

_HELP = (
    "Contributor format:\n"
    "/propose add\nsource_url: https://...\nobserved_at: YYYY-MM-DD\n"
    "street_address: ...\nzip: ...\nlist_price: ...\nstatus: active\n\n"
    "For evidence on an existing pending proposal: attach a file with caption /attach <proposal number>."
)
_META = frozenset({"source_url", "observed_at", "match_key", "reason"})


class ContributionCommands:
    """Accept only explicitly structured proposal messages; ordinary chat stays untouched."""

    def __init__(self, contributions):
        self.contributions = contributions

    def try_handle(self, actor_id, text: str):
        raw = (text or "").strip()
        if not raw.lower().startswith("/propose"):
            return None
        lines = raw.splitlines()
        first = lines[0].split()
        if len(first) == 1 or first[1].lower() in {"help", "template"}:
            return _HELP
        if len(first) != 2 or first[1].lower() not in {PROPOSAL_ADD, PROPOSAL_CORRECT, PROPOSAL_RETIRE}:
            return "Use /propose add, /propose correct, or /propose retire."
        try:
            values = self._fields(lines[1:])
            kind = first[1].lower()
            source_url = values.pop("source_url")
            observed_at = values.pop("observed_at")
            if kind == PROPOSAL_ADD:
                proposal = self.contributions.submit_add(actor_id, values, source_url=source_url, observed_at=observed_at)
            elif kind == PROPOSAL_CORRECT:
                key = values.pop("match_key")
                proposal = self.contributions.submit_correction(actor_id, key, values, source_url=source_url,
                                                                observed_at=observed_at)
            else:
                key = values.pop("match_key")
                reason = values.pop("reason")
                if values:
                    raise ValueError("retire accepts only match_key, reason, source_url, and observed_at")
                proposal = self.contributions.submit_retire(actor_id, key, reason=reason, source_url=source_url,
                                                            observed_at=observed_at)
        except PermissionError:
            return "Contributor access is required to submit a proposal."
        except (KeyError, ValueError) as exc:
            return f"Proposal was not accepted: {exc}. Send /propose help for the template."
        return f"Proposal #{proposal['id']} submitted and pending review. It has not changed any listing."

    @staticmethod
    def _fields(lines):
        values = {}
        for line in lines:
            if not line.strip():
                continue
            if ":" not in line:
                raise ValueError("each proposal line must be key: value")
            key, value = line.split(":", 1)
            key = key.strip().lower().replace(" ", "_")
            value = value.strip()
            if not key or not value or key in values:
                raise ValueError("proposal fields must be non-empty and unique")
            values[key] = value
        if not values:
            raise ValueError("proposal fields are required")
        return values
