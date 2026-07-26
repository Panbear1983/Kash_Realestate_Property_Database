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
import os

from kash.adapters import REGISTRY
from kash import orchestrator, preferences, schedule
from kash.access import Access
from kash.notifications import (
    chunk_digest, format_onboarding, listing_delivery_kind, testing_recipients,
    unsent_actionable_listings,
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

    load_dotenv(DEFAULT_ENV)
    prefs = preferences.ensure(args.prefs, args.csv)

    # Derive the export and run-state paths from the database being used. Previously both were
    # hardcoded to the repo, so `--db /tmp/copy.db` still overwrote the real pool_export.csv and
    # advanced the real .last_run.json (which rotates the sweep price band) — a test against a
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

    from kash import sweep
    lo, hi = sweep.next_slice(prefs, state)   # rotating price band; advances state
    if lo is not None:
        print(f"sweep slice: ${lo:,}-${hi:,}")

    adapters = []
    for n in due:
        cfg = dict((prefs.get("sources") or {}).get(n, {}))
        if args.limit:
            cfg["results_limit"] = args.limit
        if lo is not None:
            cfg["price_min"], cfg["price_max"] = lo, hi
        adapters.append(REGISTRY[n](config=cfg))

    result = orchestrator.update(store, prefs, adapters)

    print(f"\n=== update cycle: {', '.join(due)} ===")
    for s in result["summaries"]:
        if "error" in s:
            print(f"  [{s['source']}] SKIPPED: {s['error'][:100]}")
        else:
            print(f"  [{s['source']}] fetched {s['fetched']}  +{s['inserted']} new  "
                  f"~{s['updated']} updated  ({s['out_of_scope']} off-scope)")
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
            chunks = chunk_digest(actionable)
            # The change digest (price drops, went pending, back on market) names exactly the
            # events worth knowing about and was previously only ever printed to a log.
            preamble = [p for p in (format_onboarding() if onboarding_due else None,
                                    result["digest"] if any(result["groups"].values()) else None)
                        if p]
            sent_any, failures = 0, []
            for i, (text, rows_in_chunk) in enumerate(chunks):
                body = "\n\n".join(preamble + [text]) if i == 0 and preamble else text
                outcome = push_telegram(body, [recipient_id])[recipient_id]
                if outcome != "sent":
                    # Mark nothing for a chunk that did not arrive, so it is retried next run.
                    failures.append(outcome)
                    continue
                sent_any += 1
                if i == 0 and onboarding_due:
                    store.mark_notification_sent(recipient_id, "onboarding")
                for listing in rows_in_chunk:
                    kind = listing_delivery_kind(listing)
                    if kind:
                        store.mark_notification_sent(recipient_id, kind)
            outcomes[recipient_id] = (
                f"sent {sent_any}/{len(chunks)}" if not failures
                else f"sent {sent_any}/{len(chunks)}, failed: {'; '.join(failures[:2])}")
        if outcomes:
            print(f"\n[telegram] {outcomes}")
        elif recipients:
            print("\n[telegram] no unsent actionable listings")
        else:
            print("\n[telegram] skipped (no allowed testing recipients)")

    from kash import export
    print(f"  exported {export.to_csv(store, export_path)} rows -> {os.path.basename(export_path)}")

    for n in due:
        schedule.mark(n, state)
    schedule.save(state_path, state)
    store.close()


if __name__ == "__main__":
    main()
