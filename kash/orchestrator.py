"""The update cycle: backup → fetch/merge each source → enrich → auto-rank → digest.

Host-agnostic: a launchd job (or any scheduler) just calls `update()`. Resilient — every
stage is guarded so one source or step failing is recorded, not fatal, and the routine
still finishes and reports.
"""
from __future__ import annotations

from . import backup
from . import completeness
from . import digest as digest_mod
from . import lifecycle
from . import pipeline
from . import rank
from . import schools
from . import urlfill
from .enrich import enrich, enrich_details, extract_descriptions
from .ledger import Ledger


def update(store, prefs: dict, adapters: list) -> dict:
    # 1. snapshot the DB before any writes. A failed backup aborts the cycle: the whole point
    # of the snapshot is that a bad scrape is recoverable, so proceeding without one trades a
    # recoverable problem for an unrecoverable one.
    try:
        snap = backup.snapshot(getattr(store, "db_path", ""))
    except Exception as e:  # noqa: BLE001
        snap = f"backup failed: {e}"
    if snap is not None and str(snap).startswith("backup failed"):
        return {"aborted": snap, "backup": snap, "summaries": [], "groups": {}, "events": [],
                "digest": "Run aborted before any write: the pre-run backup failed.",
                "pool_size": store.count()}

    start = store.conn.execute("SELECT COALESCE(MAX(id),0) FROM changelog").fetchone()[0]

    # 2. fetch + merge each source
    summaries = []
    for adapter in adapters:
        try:
            summaries.append(pipeline.run(adapter, store, prefs))
        except Exception as exc:  # noqa: BLE001 — an unattended loop must not die on one source
            summaries.append({"source": adapter.name, "error": str(exc)})

    # 2b. Listings that have quietly gone away. Runs on this cycle's coverage, so the
    # status_change events it writes land in step 5's digest alongside provider-reported ones.
    try:
        seen_keys = [k for s in summaries for k in (s.get("seen_keys") or ())]
        lifecycle_stats = lifecycle.age_listings(
            store, prefs, [s.get("coverage") for s in summaries], seen_keys)
    except Exception as e:  # noqa: BLE001
        lifecycle_stats = {"error": str(e)}

    # 2c. Listings whose source provides no URL (RentCast) get a synthesized search link so
    # they can alert; runs before completeness so the same-run audit sees the fills.
    try:
        urlfill_stats = urlfill.fill_missing_urls(store, prefs)
    except Exception as e:  # noqa: BLE001
        urlfill_stats = {"error": str(e)}

    # 3a. Zillow detail scrape: fill deep fields (year built, tax, HOA, agent, description…)
    try:
        detail_stats = enrich_details(store, limit=(prefs.get("detail") or {}).get("max_per_run", 15),
                                      prefs=prefs)
    except Exception as e:  # noqa: BLE001
        detail_stats = {"error": str(e)}

    # 3b. enrich new rows: coordinates + flood zone + neighbourhood (free, idempotent).
    # The ledger is threaded through so a failing source is recorded per row rather than
    # collapsing into an error count, and rows in backoff are skipped.
    ledger = Ledger(store)
    try:
        enrich_stats = enrich(store, limit=(prefs.get("enrich") or {}).get("max_per_run", 50),
                              ledger=ledger)
    except Exception as e:  # noqa: BLE001
        enrich_stats = {"error": str(e)}

    # 3b-ii. zoned elementary school from frozen DOE polygons. Offline and free, so it runs
    # every cycle; needs the coordinates enrich() just filled, and only ever fills blanks.
    try:
        school_stats = schools.fill_school_zones(
            store, limit=(prefs.get("schools") or {}).get("max_per_run", 200))
    except Exception as e:  # noqa: BLE001
        school_stats = {"error": str(e)}

    # 3c. read the description semantically into signal_* columns. Must run after
    # enrich_details (which fills listing_description) and before rank, which consumes it.
    try:
        describe_stats = extract_descriptions(
            store, prefs, limit=(prefs.get("describe") or {}).get("max_per_run", 15))
    except Exception as e:  # noqa: BLE001
        describe_stats = {"error": str(e)}

    # 4. auto-rank new listings via the 'rank' ladder (tier / priority / thesis)
    try:
        rank_stats = rank.rank_new(store, prefs, limit=(prefs.get("rank") or {}).get("max_per_run", 15))
    except Exception as e:  # noqa: BLE001
        rank_stats = {"error": str(e)}

    # 5. digest of listing changes this cycle
    events = [
        dict(r)
        for r in store.conn.execute(
            "SELECT event, detail, match_key FROM changelog WHERE id>? ORDER BY id",
            (start,),
        ).fetchall()
    ]
    groups = digest_mod.build(events)
    return {
        "summaries": summaries,
        "groups": groups,
        "events": events,
        "digest": digest_mod.format_text(groups),
        "pool_size": store.count(),
        "backup": snap,
        "lifecycle": lifecycle_stats,
        "urlfill": urlfill_stats,
        "schools": school_stats,
        "detail": detail_stats,
        "describe": describe_stats,
        "completeness": completeness.audit(store, ledger, prefs=prefs),
        "enrich": enrich_stats,
        "rank": rank_stats,
    }
