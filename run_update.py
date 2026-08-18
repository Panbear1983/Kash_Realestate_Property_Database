#!/usr/bin/env python3
"""The daily update cycle — what the launchd scheduler runs.

    python run_update.py                     # auto: sources with creds, each on its cadence
    python run_update.py --sources zillow    # explicit source(s), bypass cadence
    python run_update.py --force             # run all due-or-not, ignore cadence
    python run_update.py --limit 40          # cap results per source this run
    python run_update.py --no-telegram       # don't push the digest

Per run: backup → fetch/merge → enrich (flood/geo) → Codex auto-rank new finds →
change digest → optional Telegram push. Per-source cadence lives in preferences.yaml
(`sources:`) and last-run dates in .last_run.json, so Zillow can run daily while RentCast
runs weekly, staying inside each provider's free tier.
"""
import argparse
import fcntl
import os
import sys
import time

from kash.adapters import REGISTRY
from kash import health, orchestrator, preferences, schedule
from kash.access import Access
from kash.notifications import (
    chunk_digest, format_onboarding, health_recipients, listing_delivery_kind, split_text,
    testing_recipients, unsent_actionable_listings,
)
from kash.store import Store

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CSV = os.path.join(HERE, "archive", "SI_July2026_Active_Properties.csv")
DEFAULT_DB = os.path.join(HERE, "pool.db")
DEFAULT_PREFS = os.path.join(HERE, "preferences.yaml")
DEFAULT_ENV = os.path.join(HERE, ".env")
STATE_PATH = os.path.join(HERE, ".last_run.json")
DEFAULT_EXPORT = os.path.join(HERE, "pool_export.csv")


def load_dotenv(path):
    if not os.path.exists(path):
        return
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("'\""))


def acquire_lock(db_path):
    """Exclusive run lock beside the DB, released when the process exits.

    A cycle now makes ~30 LLM calls and can run for minutes. Two overlapping runs would
    double-scrape, double-spend Apify credit, and interleave writes to the same rows.
    Returns the open file handle (which must stay referenced) or None if another run holds it.
    """
    path = os.path.join(os.path.dirname(os.path.abspath(db_path)), ".run.lock")
    fh = open(path, "w")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    fh.write(f"{os.getpid()}\n")
    fh.flush()
    return fh


def auto_sources():
    chosen = []
    if os.environ.get("RENTCAST_API_KEY"):
        chosen.append("rentcast")
    if os.environ.get("APIFY_TOKEN") or os.environ.get("SCRAPERAPI_KEY"):
        chosen.append("zillow")
    return chosen or ["mock"]


def push_telegram(text, chat_ids: list[int], request_get=None) -> dict[int, str]:
    tok = os.environ.get("KASH_BOT_TOKEN")
    if not tok:
        return {int(chat_id): "skipped (no KASH_BOT_TOKEN)" for chat_id in chat_ids}
    if request_get is None:
        import requests
        request_get = requests.get
    outcomes = {}
    for chat_id in chat_ids:
        try:
            # No silent truncation: chunking is the caller's job (notifications.chunk_digest),
            # and quietly cutting a message here meant listings were recorded as delivered
            # having never been shown.
            r = request_get(f"https://api.telegram.org/bot{tok}/sendMessage",
                            params={"chat_id": int(chat_id), "text": text}, timeout=20)
            outcomes[int(chat_id)] = "sent" if r.json().get("ok") else r.json().get("description", "fail")
        except Exception as e:  # noqa: BLE001
            outcomes[int(chat_id)] = f"error: {e}"
    return outcomes


def main():
    ap = argparse.ArgumentParser(description="Kash daily update cycle")
    ap.add_argument("--sources", help="comma list; default = sources with credentials")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--csv", default=DEFAULT_CSV)
    ap.add_argument("--prefs", default=DEFAULT_PREFS)
    ap.add_argument("--limit", type=int, default=None, help="cap results per source")
    ap.add_argument("--force", action="store_true", help="ignore per-source cadence")
    ap.add_argument("--no-telegram", action="store_true")
    args = ap.parse_args()

    started = time.time()
    lock = acquire_lock(args.db)
    if lock is None:
        print("another Kash run is in progress — exiting")
        return

    load_dotenv(DEFAULT_ENV)
    prefs = preferences.ensure(args.prefs, args.csv)

    # Derive the export and run-state paths from the database being used. Previously both were
    # hardcoded to the repo, so `--db /tmp/copy.db` still overwrote the real pool_export.csv and
    # advanced the real .last_run.json — a test against a
    # copy silently mutated production state.
    db_dir = os.path.dirname(os.path.abspath(args.db))
    db_stem = os.path.splitext(os.path.basename(args.db))[0]
    is_default_db = os.path.abspath(args.db) == os.path.abspath(DEFAULT_DB)
    state_path = STATE_PATH if is_default_db else os.path.join(db_dir, f".{db_stem}_run.json")
    export_path = (DEFAULT_EXPORT if is_default_db
                   else os.path.join(db_dir, f"{db_stem}_export.csv"))
    if not is_default_db:
        print(f"[non-default --db] state -> {state_path}\n"
              f"                   export -> {export_path}")

    store = Store(args.db, finance_cfg=prefs.get("finance"))
    if store.count() == 0:
        print(f"Seeded pool from CSV: {store.seed_from_csv(args.csv)} rows")

    state = schedule.load(state_path)
    requested = args.sources.split(",") if args.sources else auto_sources()
    try:
        requested = preferences.validate_source_names(requested, REGISTRY)
    except ValueError as exc:
        ap.error(str(exc))
    due = requested if (args.sources or args.force) else schedule.which_due(requested, prefs, state)
    if not due:
        print("No sources due today (per cadence). Use --force to run anyway.")
        store.close()
        return

    # No price-band sweep anymore: Zillow queries only recent listings (max_days_on_market)
    # for discovery, and the RentCast city census re-sights the whole market every other
    # run for free — kash/sweep.py stays on disk but nothing feeds its band to adapters.
    adapters = []
    for n in due:
        cfg = dict((prefs.get("sources") or {}).get(n, {}))
        # --limit caps the ZILLOW query only. Applied to rentcast it overrode the census's
        # 500 page size, forcing a 2-page truncated fetch that burned quota AND stopped
        # ageing (truncated coverage proves nothing).
        if args.limit and n == "zillow":
            cfg["results_limit"] = args.limit
        adapters.append(REGISTRY[n](config=cfg))

    result = orchestrator.update(store, prefs, adapters)

    print(f"\n=== update cycle: {', '.join(due)} ===")
    for s in result["summaries"]:
        if "error" in s:
            print(f"  [{s['source']}] SKIPPED: {s['error'][:100]}")
        else:
            print(f"  [{s['source']}] fetched {s['fetched']}  +{s['inserted']} new  "
                  f"~{s['updated']} updated  ({s['out_of_scope']} off-scope)")
    from kash import lifecycle, schools, urlfill
    print(f"  {lifecycle.format_stats(result.get('lifecycle') or {})}")
    print(f"  {urlfill.format_stats(result.get('urlfill') or {})}")
    print(f"  {schools.format_stats(result.get('schools') or {})}")
    print(f"  detail: {result['detail']}   enrich: {result['enrich']}")
    print(f"  describe: {result['describe']}   rank: {result['rank']}")
    from kash import completeness
    print(f"  {completeness.format_report(result['completeness'])}")
    print(f"  backup: {result['backup']}")
    print(f"  pool size: {result['pool_size']}")
    print("\n--- DIGEST ---")
    print(result["digest"])

    if not args.no_telegram:
        recipients = testing_recipients(Access(store).allowed_ids(), prefs)
        outcomes = {}
        for recipient_id in recipients:
            actionable = unsent_actionable_listings(store, recipient_id, prefs)
            if not actionable:
                continue
            onboarding_due = not store.notification_sent(recipient_id, "onboarding")
            # The preamble goes as its own message(s), never prepended to a listing chunk.
            # Prepending it meant the chunk respected the limit but the combined message did
            # not, and Telegram rejected the whole thing — losing that chunk's listings.
            preamble_parts = []
            if onboarding_due:
                preamble_parts += split_text(format_onboarding())
            if any(result["groups"].values()):
                preamble_parts += split_text(result["digest"])

            sent_any, failures, total = 0, [], 0
            for i, text in enumerate(preamble_parts):
                total += 1
                if push_telegram(text, [recipient_id])[recipient_id] != "sent":
                    failures.append("preamble")
                    continue
                sent_any += 1
                if i == 0 and onboarding_due:
                    store.mark_notification_sent(recipient_id, "onboarding")

            for text, rows_in_chunk in chunk_digest(actionable):
                total += 1
                outcome = push_telegram(text, [recipient_id])[recipient_id]
                if outcome != "sent":
                    # Mark nothing for a chunk that did not arrive, so it is retried next run.
                    failures.append(outcome)
                    continue
                sent_any += 1
                for listing in rows_in_chunk:
                    kind = listing_delivery_kind(listing)
                    if kind:
                        store.mark_notification_sent(recipient_id, kind)
            outcomes[recipient_id] = (
                f"sent {sent_any}/{total}" if not failures
                else f"sent {sent_any}/{total}, failed: {'; '.join(failures[:2])}")
        if outcomes:
            print(f"\n[telegram] {outcomes}")
        elif recipients:
            print("\n[telegram] no unsent actionable listings")
        else:
            print("\n[telegram] skipped (no allowed testing recipients)")

    from kash import export
    print(f"  exported {export.to_csv(store, export_path)} rows -> {os.path.basename(export_path)}")

    # Mark only sources that actually worked. Marking unconditionally is how a dead credential
    # goes quiet: RentCast 403'd, was recorded as having run, and its 7-day cadence then put
    # the next attempt a week away.
    by_source = {s.get("source"): s for s in result.get("summaries", [])}
    for n in due:
        summary = by_source.get(n) or {}
        if summary.get("error"):
            fails = schedule.mark_failure(n, state)
            print(f"  [{n}] NOT marked as run (failure #{fails})")
        else:
            schedule.mark(n, state)
    schedule.save(state_path, state)

    # Month-to-date Apify spend, printed and fed to the verdict (>=90% becomes a problem).
    # The GET is free and any failure returns None — the line is simply omitted.
    from kash import apify_budget
    budget = apify_budget.month_to_date()
    if budget:
        print(f"  {apify_budget.format_line(budget)}")
        result["apify_budget"] = budget

    health_report = health.assess(result, due)
    duration = time.time() - started
    print(f"\n=== run {health_report['verdict'].upper()} in {duration:.0f}s "
          f"(pid {os.getpid()}, db {os.path.basename(args.db)}) ===")
    for p in health_report["problems"]:
        print(f"  ! {p}")

    # A degraded run must reach the owner — but only ONCE per problem. The unconditional
    # nightly alert re-sent the same message every run (ten identical RentCast alerts);
    # alert_delta remembers what was already reported and sends new/resolved/weekly-reminder
    # changes only. Runs on OK verdicts too, so recovery produces a "Resolved:" notice.
    alert_text = health.alert_delta(health_report, state)
    schedule.save(state_path, state)          # persist the alert memory
    if alert_text:
        print(f"\n[health alert delta]\n{alert_text}")
    else:
        print("\n[health alert delta] none (nothing new to report)")
    if alert_text and not args.no_telegram:
        recipients = health_recipients(Access(store).allowed_ids(), prefs)
        if recipients:
            push_telegram(alert_text, recipients)

    # Weekly market pulse: one short push summarizing what the census saw. Rides the same
    # per-source cadence state ("pulse", every 7 days) and only marks itself run when a
    # recipient actually received it — a --no-telegram test run never consumes the slot.
    if schedule.due("pulse", {"every_days": 7}, state):
        from kash import pulse
        pulse_text = pulse.format_pulse(pulse.compute(store, prefs))
        print(f"\n--- MARKET PULSE ---\n{pulse_text}")
        if not args.no_telegram:
            recipients = testing_recipients(Access(store).allowed_ids(), prefs)
            outcomes = push_telegram(pulse_text, recipients) if recipients else {}
            if any(v == "sent" for v in outcomes.values()):
                schedule.mark("pulse", state)
                schedule.save(state_path, state)

    # Checkpoint the WAL before closing: the Telegram bridge holds a permanent connection,
    # so the close-time auto-checkpoint never runs and the WAL had grown to 4x the
    # database. TRUNCATE resets it whenever no reader is mid-transaction (best effort).
    try:
        store.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception:  # noqa: BLE001
        pass
    store.close()
    if health_report["verdict"] == health.FAILED:
        sys.exit(1)     # so launchd's LastExitStatus means something


if __name__ == "__main__":
    main()
