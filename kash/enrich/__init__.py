"""Phase 4 enrichment — fill machine columns from free public data sources.

Enrichers only write non-user, non-source columns via Store.update_fields, so they
never disturb your notes, ranking, or provenance.
"""
from .detail import enrich_details
from .runner import enrich

__all__ = ["enrich", "enrich_details"]
