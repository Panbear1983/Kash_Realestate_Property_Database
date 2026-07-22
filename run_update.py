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


def push_telegram(text):
    tok = os.environ.get("KASH_BOT_TOKEN")
    chat = os.environ.get("KASH_CHAT_ID")
    if not (tok and chat):
        return "skipped (no KASH_BOT_TOKEN / KASH_CHAT_ID)"
    try:
        import requests
        r = requests.get(f"https://api.telegram.org/bot{tok}/sendMessage",
                         params={"chat_id": chat, "text": text[:4000]}, timeout=20)
        return "sent" if r.json().get("ok") else r.json().get("description", "fail")
    except Exception as e:  # noqa: BLE001
        return f"error: {e}"


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

    store = Store(args.db, finance_cfg=prefs.get("finance"))
    if store.count() == 0:
        print(f"Seeded pool from CSV: {store.seed_from_csv(args.csv)} rows")

    state = schedule.load(STATE_PATH)
    requested = args.sources.split(",") if args.sources else auto_sources()
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
    print(f"  detail: {result['detail']}   enrich: {result['enrich']}   rank: {result['rank']}")
    print(f"  backup: {result['backup']}")
    print(f"  pool size: {result['pool_size']}")
    print("\n--- DIGEST ---")
    print(result["digest"])

    if not args.no_telegram and any(result["groups"].values()):
        print(f"\n[telegram] {push_telegram('Kash daily update' + chr(10) + chr(10) + result['digest'])}")

    from kash import export
    print(f"  exported {export.to_csv(store, DEFAULT_EXPORT)} rows -> pool_export.csv")

    for n in due:
        schedule.mark(n, state)
    schedule.save(STATE_PATH, state)
    store.close()


if __name__ == "__main__":
    main()
