"""Did the run actually work? — the verdict an unattended job needs in order to complain.

On the only production run on record, RentCast returned 403 and the geocoder failed on 28 of
28 rows. The owner received a normal-looking Telegram digest, launchd recorded exit 0, and
`.last_run.json` marked RentCast as having run — so its weekly cadence turned a dead
credential into a week of silence. Nothing in the system was capable of saying "that went
badly".

This module turns a run result into a verdict. `run_update` uses it for three things the old
code could not do: push an unconditional degraded-run message (independent of whether there
were listings to send), exit non-zero so launchd's LastExitStatus is meaningful, and log a
line that says plainly what happened.
"""
from __future__ import annotations

OK = "ok"
DEGRADED = "degraded"
FAILED = "failed"


def assess(result: dict, due_sources: list[str] | None = None) -> dict:
    """Classify a completed orchestrator.update() result.

    FAILED   — the cycle aborted, or every source errored: nothing was collected.
    DEGRADED — it ran, but something a human should know about went wrong.
    OK       — everything that was meant to run, ran.
    """
    problems: list[str] = []
    summaries = result.get("summaries") or []
    due_sources = due_sources or [s.get("source") for s in summaries]

    if result.get("aborted"):
        return {"verdict": FAILED, "problems": [f"run aborted: {result['aborted']}"]}

    errored = [s for s in summaries if s.get("error")]
    for s in errored:
        problems.append(f"source {s.get('source')}: {str(s.get('error'))[:120]}")

    if summaries and len(errored) == len(summaries):
        return {"verdict": FAILED, "problems": problems or ["every source failed"]}

    fetched = sum(int(s.get("fetched") or 0) for s in summaries if not s.get("error"))
    if summaries and fetched == 0 and not errored:
        problems.append("all sources returned zero listings")

    # An enrichment stage that fails on most of what it touched is not a healthy run, even
    # though each individual failure is caught and counted.
    enrich = result.get("enrich") or {}
    processed, errors = int(enrich.get("processed") or 0), int(enrich.get("errors") or 0)
    if processed and errors > processed / 2:
        problems.append(f"enrichment failed on {errors} of {processed} rows")
    if enrich.get("error"):
        problems.append(f"enrichment: {str(enrich['error'])[:120]}")
    unavailable = int(enrich.get("flood_unavailable") or 0)
    if unavailable:
        problems.append(f"flood lookup unavailable for {unavailable} rows")

    for stage in ("detail", "describe", "rank"):
        st = result.get(stage) or {}
        if st.get("error"):
            problems.append(f"{stage}: {str(st['error'])[:120]}")

    if str(result.get("backup", "")).startswith("backup failed"):
        problems.append(str(result["backup"]))

    comp = result.get("completeness") or {}
    if comp.get("alert_blocked"):
        problems.append(f"{comp['alert_blocked']} listings blocked from alerting")

    return {"verdict": DEGRADED if problems else OK, "problems": problems}


def format_alert(health: dict, db_path: str = "") -> str:
    """The message pushed to Telegram when a run did not go cleanly."""
    head = ("Kash run FAILED — no listings were collected."
            if health["verdict"] == FAILED
            else "Kash run finished with problems.")
    lines = [head] + [f"  - {p}" for p in health["problems"][:8]]
    if len(health["problems"]) > 8:
        lines.append(f"  …and {len(health['problems']) - 8} more")
    lines.append("Check logs/update.log.")
    return "\n".join(lines)
