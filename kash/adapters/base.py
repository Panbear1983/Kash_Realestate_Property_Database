"""SourceAdapter interface — the contract every data source implements.

An adapter's one job: take the preference profile, fetch matching listings from its
source, and return a list of dicts already normalized to the Kash schema. The pipeline
handles validation, dedup, merge, and calc — adapters do source-specific work only.
"""
from __future__ import annotations

import abc
from typing import Optional


class CredentialError(RuntimeError):
    """Raised when an adapter needs an API key / account that isn't configured."""


class SourceAdapter(abc.ABC):
    #: short name used in the registry and provenance (`source` column).
    name: str = "base"

    def __init__(self, config: Optional[dict] = None):
        self.config = config or {}

    @abc.abstractmethod
    def fetch(self, preferences: dict) -> list[dict]:
        """Return listings (as schema-shaped dicts) matching `preferences`.

        Must set each record's `source` and, where available, `source_url` /
        `mls_number` so downstream dedup and provenance work.
        """
        raise NotImplementedError

    def coverage(self, fetched: int) -> Optional[dict]:
        """What the last fetch() proves about listings it did NOT return, or None.

        Absence is only evidence when a query came back under its cap — see kash/lifecycle.py.
        An adapter that cannot describe its slice (RentCast pages per ZIP across all prices,
        so its window is always truncated and never conclusive) returns None and no row is
        ever aged out on its behalf.
        """
        return None

    def _stamp(self, rec: dict) -> dict:
        rec.setdefault("source", self.name)
        return rec
