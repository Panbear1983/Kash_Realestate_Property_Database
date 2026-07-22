"""The update cycle: backup → fetch/merge each source → enrich → auto-rank → digest.

Host-agnostic: a launchd job (or any scheduler) just calls `update()`. Resilient — every
stage is guarded so one source or step failing is recorded, not fatal, and the routine
still finishes and reports.
"""
from __future__ import annotations

from . import backup
from . import digest as digest_mod
from . import pipeline
from . import rank
from .enrich import enrich, enrich_details


def update(store, prefs: dict, adapters: list) -> dict:
    # 1. snapshot the DB before any writes
    try:
        snap = backup.snapshot(getattr(store, "db_path", ""))
    except Exception as e:  # noqa: BLE001
        snap = f"backup failed: {e}"

    start = store.conn.execute("SELECT COALESCE(MAX(id),0) FROM changelog").fetchone()[0]

    # 2. fetch + merge each source
    summaries = []
    for adapter in adapters:
        try:
            summaries.append(pipeline.run(adapter, store, prefs))
        except Exception as exc:  # noqa: BLE001 — an unattended loop must not die on one source
            summaries.append({"source": adapter.name, "error": str(exc)})

    # 3a. Zillow detail scrape: fill deep fields (year built, tax, HOA, agent, description…)
    try:
        detail_stats = enrich_details(store, limit=(prefs.get("detail") or {}).get("max_per_run", 15))
    except Exception as e:  # noqa: BLE001
        detail_stats = {"error": str(e)}

    # 3b. enrich new rows: coordinates + FEMA flood zone + neighborhood (free, idempotent)
    try:
        enrich_stats = enrich(store, limit=(prefs.get("enrich") or {}).get("max_per_run", 50))
    except Exception as e:  # noqa: BLE001
        enrich_stats = {"error": str(e)}

    # 4. auto-rank new listings via Codex (tier / priority / thesis)
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
        "digest": digest_mod.format_text(groups),
        "pool_size": store.count(),
        "backup": snap,
        "detail": detail_stats,
        "enrich": enrich_stats,
        "rank": rank_stats,
    }
