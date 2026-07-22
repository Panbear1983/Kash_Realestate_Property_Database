"""Source adapters. Each returns records normalized to the Kash schema.

Registry maps a --source name to its adapter class so `run_fetch.py` and the
Phase 3 loop can select sources by name.
"""
from .base import SourceAdapter, CredentialError
from .mock import MockAdapter
from .rentcast import RentCastAdapter
from .zillow_scraper import ZillowScraperAdapter

REGISTRY = {
    "mock": MockAdapter,
    "rentcast": RentCastAdapter,
    "zillow": ZillowScraperAdapter,
}

__all__ = [
    "SourceAdapter", "CredentialError", "MockAdapter",
    "RentCastAdapter", "ZillowScraperAdapter", "REGISTRY",
]
