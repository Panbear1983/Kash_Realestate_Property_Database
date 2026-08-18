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

import re
from datetime import date

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

    for stage in ("detail", "describe", "rank", "lifecycle", "schools", "urlfill"):
        st = result.get(stage) or {}
        if st.get("error"):
            problems.append(f"{stage}: {str(st['error'])[:120]}")

    if str(result.get("backup", "")).startswith("backup failed"):
        problems.append(str(result["backup"]))

    comp = result.get("completeness") or {}
    if comp.get("alert_blocked"):
        problems.append(f"{comp['alert_blocked']} listings blocked from alerting")

    # The Apify free tier is a HARD monthly cap: past it, actor runs fail rather than bill.
    # Flag while there is still time to throttle (F6 -> zillow results per run).
    budget = result.get("apify_budget")
    from .apify_budget import WARN_AT
    if budget and budget.get("pct", 0) >= WARN_AT:
        problems.append(f"Apify credit {budget['pct'] * 100:.0f}% used "
                        f"(${budget['used']:.2f} of ${budget['cap']:.2f})")

    return {"verdict": DEGRADED if problems else OK, "problems": problems}


def signature(problem: str) -> str:
    """A stable identity for a problem, so tonight's copy matches last night's.

    Volatile fragments are normalized away: full URLs (query strings differ per run) and
    counts ("failed on 28 of 28" vs "27 of 28"). Three-digit numbers survive because HTTP
    status codes are the load-bearing part of most problems — 403 and 400 are different
    problems, 28-vs-27 rows is the same one.
    """
    sig = re.sub(r"for url: \S+", "for url: <url>", problem or "")
    sig = re.sub(r"\b\d{4,}\b", "N", sig)
    sig = re.sub(r"\b\d{1,2}\b", "N", sig)
    return " ".join(sig.split())


def alert_delta(report: dict, state: dict, remind_days: int = 7,
                today: str | None = None) -> str | None:
    """What is worth sending to a human tonight, given what they were already told.

    The unconditional nightly alert did its job — failures stopped being silent — but with
    no memory it re-sent the identical message every run: RentCast alone produced ten copies.
    This mutates ``state["health_alerts"]`` (persisted in the run-state file by the caller)
    and returns a message only when something CHANGED: a new problem appeared, a previously
    reported one resolved, or a known one has been broken for `remind_days` without a nudge.
    The full verdict still goes to the log every run — this gates only the phone.
    """
    today = today or date.today().isoformat()
    mem = state.setdefault("health_alerts", {})

    current: dict[str, str] = {}
    for p in report.get("problems", []):
        current.setdefault(signature(p), p)

    new = [text for sig, text in current.items() if sig not in mem]
    resolved = [entry.get("text", sig) for sig, entry in mem.items() if sig not in current]
    reminders = []
    for sig, text in current.items():
        entry = mem.get(sig)
        if entry and _days_between(entry.get("last_alerted"), today) >= remind_days:
            reminders.append((text, entry.get("first", today)))

    # Update the memory to reflect what the owner will now have been told.
    for sig in [s for s in mem if s not in current]:
        del mem[sig]
    for sig, text in current.items():
        if sig not in mem:
            mem[sig] = {"first": today, "last_alerted": today, "text": text}
        elif any(text == t for t, _ in reminders):
            mem[sig]["last_alerted"] = today
            mem[sig]["text"] = text

    if not (new or resolved or reminders):
        return None
    head = ("Kash run FAILED — no listings were collected."
            if report.get("verdict") == FAILED and new else "Kash health update.")
    lines = [head]
    if new:
        lines.append("New:")
        lines += [f"  - {t[:160]}" for t in new[:6]]
    if resolved:
        lines.append("Resolved:")
        lines += [f"  - {t[:120]}" for t in resolved[:6]]
    if reminders:
        lines.append("Still broken:")
        lines += [f"  - since {first}: {t[:140]}" for t, first in reminders[:6]]
    lines.append("Check logs/update.log.")
    return "\n".join(lines)


def _days_between(earlier: str | None, later: str) -> int:
    from datetime import date as _d
    try:
        a = _d.fromisoformat(earlier or "")
        b = _d.fromisoformat(later)
        return (b - a).days
    except ValueError:
        return remind_default() + 1   # unreadable stamp: err on the side of re-alerting


def remind_default() -> int:
    return 7


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
