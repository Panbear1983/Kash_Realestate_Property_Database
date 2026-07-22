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

    def _stamp(self, rec: dict) -> dict:
        rec.setdefault("source", self.name)
        return rec
