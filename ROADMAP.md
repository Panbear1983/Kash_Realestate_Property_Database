# Kash — Roadmap

Evolving Kash from a static CSV into a live Staten Island real-estate intelligence
pipeline: a queryable data pool, a scraper that sources listings against your saved
preferences, a scheduled loop that keeps it current, and a terminal UI you can talk to.

This is a **plan**, not built code. Build order and per-phase deliverables below.

## Locked decisions

- **Sourcing: hybrid.** Lead with licensed/official data APIs; add a clearly-flagged
  Zillow adapter via a managed scraper (Apify/ScraperAPI), whose ToS risk is accepted.
  All sources normalize to the existing 35-column schema behind a `SourceAdapter`
  interface — the schema is the contract.
- **Terminal UI: NL + commands.** Deterministic commands plus a Claude-backed natural
  language mode (`claude-opus-4-8`), API cost accepted.
- **Loop host: decide in Phase 3.** Pipeline is built host-agnostic; local-on-Ubuntu vs
  Anthropic Managed Agents chosen when we reach the loop.

## Legal / ToS note

Zillow/Redfin/Realtor prohibit scraping and run active anti-bot defenses. Direct HTML
scraping is fragile and against ToS, so it lives only in the last-resort flagged adapter
behind a managed-scraper provider. Licensed APIs are the primary path. The adapter
pattern means any single source can be swapped without touching the rest of the system.

## Architecture

```
 sources (adapters)          core                         surfaces
 ─────────────────           ────                         ────────
 RentCast / ATTOM  ─┐
 RapidAPI (Realtor) ─┼─▶ normalize (Pydantic) ─▶ SQLite pool ─┬─▶ terminal UI (cmd + NL)
 Redfin bulk        ─┤       dedup / entity-res    (+ CSV      ├─▶ Telegram digest
 Zillow (flagged)   ─┘       auto-rank (BRRRR)      export)    └─▶ Word/HTML report export
                                    ▲
                          scheduled loop + subagents
```

Storage moves CSV → **SQLite** (CSV kept as an export). The 35-column schema becomes the
table definition; `preferences.yaml` (auto-derived from today's rows) drives the scraper;
`source` / `fetched_at` / `source_url` provenance columns are added.

## Model choices (grounded in current API facts)

| Job | Model | Notes |
|---|---|---|
| NL query translation + narration | `claude-opus-4-8` | SDK tool runner, adaptive thinking, read-only `query_listings` tool; schema prompt-cached (~0.1x repeat cost) |
| High-volume per-listing normalize + fuzzy dedup | `claude-haiku-4-5` | Cheap ($1/$5 per MTok), parallel; escalate ambiguous matches to opus |
| Auto-ranking / BRRRR thesis on new listings | `claude-opus-4-8` | Reproduces the tier + investment logic already encoded in the 35 rows |

## Schema additions (locked for Phase 0)

The current 35 columns are kept as-is (free-text `analysis` / `my_notes` /
`investment_thesis` untouched). The following are added. Tags: **[src]** sourced from a
provider, **[calc]** computed from other columns, **[fill]** column created now but
populated in Phase 4 enrichment, **[user]** set by you in the UI.

### A. Core property facts [src]
`year_built` (int) · `lot_size_sqft` (int) · `property_tax_annual` (int) ·
`hoa_monthly` (int) · `basement` (enum none/partial/unfinished/finished) ·
`condition` (enum move_in/tlc/gut) · `garage_spaces` (int) · `heating` (str) ·
`cooling` (str) · `mls_number` (str) · `listing_agent` (str) ·
`listing_brokerage` (str) · `photo_count` (int) · `listing_description` (text)

### B. Valuation / financial
`original_list_price` (int) [src] · `price_history` (JSON list of {date, price}) [src] ·
`price_drop_pct` (num) [calc] · `redfin_estimate` (int) [src] ·
`last_sold_price` (int) [src] · `last_sold_date` (date) [src] ·
`estimated_rent_monthly` (int) [src] · `cap_rate` (num) [calc] ·
`cash_on_cash` (num) [calc] · `monthly_piti` (int) [calc, from the 6.3%/$840K basis]

### C. Lifecycle / tracking
`first_seen_date` (date) · `listing_date` (date) [src] · `pending_date` (date) [src] ·
`sold_date` (date) [src] · `times_relisted` (int)

### D1. Geospatial [fill — Phase 4]
`latitude` (num) · `longitude` (num) · `flood_zone` (str, FEMA) ·
`walk_score` (int) · `transit_score` (int) · `commute_minutes` (int)

### D2. Personal / CRM [user]
`favorite` (bool) · `viewing_status` (enum none/scheduled/seen/skip) ·
`viewing_date` (date) · `offer_status` (enum none/considering/offered/rejected/accepted) ·
`user_rating` (int 1–5) · `contacted_agent` (bool)

### D3. Media [src]
`primary_photo_url` (url) · `virtual_tour_url` (url)

### Provenance (already planned)
`source` · `fetched_at` · `source_url`

Nullability: every added field is nullable — no source populates all of them, and
`[calc]` fields are null until their inputs exist. `[calc]` columns are recomputed on
every write (SQLite generated columns where the inputs are stored). `first_seen_date`
and `favorite`/`viewing_status` default on insert; the rest default null.

## Phases

### Phase 0 — Foundation (prerequisite)
- SQLite store + migration of current CSV; CSV export retained.
- Pydantic schema module enforcing the **expanded schema** (see *Schema additions*
  below) at every boundary.
- `preferences.yaml` derived from current data (SI ZIPs, ~$650K–$1M, property types,
  bed/bath mins, GS floor, target neighborhoods).
- Provenance columns: `source`, `fetched_at`, `source_url`.

### Phase 2 — Scraper / fetcher  — BUILT (framework + mock), live sources await keys
- `SourceAdapter` interface + registry (`kash/adapters/`). **Done.**
- Pipeline: fetch → validate → scope-filter → dedup/entity-resolution (address+ZIP) →
  merge → changelog (`kash/pipeline.py`, `kash/store.py`, `kash/dedup.py`). **Done, tested.**
- Merge protects user fields (`analysis`/`my_notes`/CRM), tracks price/status changes,
  recomputes calc fields (`kash/finance.py`). **Done, tested.**
- `MockAdapter` proves the whole path against the real 35-row seed
  (`tests/test_pipeline.py` → 37 rows, notes preserved). **Done.**
- `RentCastAdapter` + `ZillowScraperAdapter`: real code, **stubbed — need credentials**
  (`RENTCAST_API_KEY`; `APIFY_TOKEN`/`SCRAPERAPI_KEY`) in `.env` to go live.
- Note: pulled the schema contract (`kash/schema.py`, pydantic) and a minimal SQLite
  pool forward from Phase 0, since Phase 2 normalizes into and merges against them.

**To go live:** add provider keys to `.env`, then `python run_fetch.py --source rentcast`.

### Phase 1 — Terminal UI  — BUILT (command shell + visual TUI; NL via Codex, tested)
- REPL `shell.py` + safe query layer `kash/query.py` (whitelisted columns/ops,
  parameterized). **Done, tested** against the live pool.
- **Visual TUI `dashboard.py`** (Textual): header, live ranked table, stats panel, detail
  pane, and a command bar that runs filters *or* Codex `ask`. **First draft done, rendered
  + verified** (35 rows). Launch: `python3 dashboard.py`.
- Command mode: `filter` / `stats` / `show` / `changes` with `sort:` `order:` `limit:`
  tokens. **Done.**
- NL mode (`kash/nl.py` + `kash/llm.py`): **Codex/ChatGPT via OAuth** (default backend,
  no API key) translates the question into a structured query spec — read-only, isolated
  sandbox, `--output-schema` for reliable JSON — which kash.query runs locally.
  **Done, tested**: answered price and flood-risk questions against the live pool. Backend
  is pluggable (`preferences.yaml -> llm.backend`). The Phase 3 auto-rank/enrichment
  subagents will reuse this same Codex backend.

### Phase 3 — Continuous loop  — BUILT (daily self-growing routine), tested
- Update cycle `kash/orchestrator.py` + `run_update.py`: backup → fetch/merge → enrich →
  Codex auto-rank → digest → Telegram push. Resilient per stage. **Done, tested.**
- **Per-source cadence** (`kash/schedule.py` + `preferences.yaml` `sources:`): Zillow daily
  (bounded `results_limit`), RentCast weekly — stays inside free tiers. **Done, tested.**
- **Codex auto-rank** of new finds (`kash/rank.py`): assigns tier/priority/BRRRR/thesis via
  the same Codex backend; `[auto]`-marked; once per listing; never touches curated rows.
  **Done, tested.**
- **Change digest → Telegram** (`run_update.py` → `sendMessage`, no bridge needed).
  **Done, tested.**
- **Scheduler** (`scripts/install_scheduler.sh` → launchd, daily 07:00). Built; user runs
  the install command to go live.

### Phase 4 — Enrichment & extras  — STARTED (flood/geo + PITI done)
- **FEMA flood-zone flag** (`kash/enrich/flood.py`) + **geocoding** (`kash/enrich/geocode.py`,
  US Census) via `run_enrich.py`. **Done, tested** — free/no-key; flagged 2 AE-zone
  properties (New Dorp, Midland Beach) across the pool.
- **Mortgage/PITI + cap-rate + cash-on-cash** (`kash/finance.py`) — computed per row.
  **Done.**
- Comps + ARV automation — **pending** (needs RentCast active).
- Geospatial commute / school-distance — **not built** (needs a routing API key).
- Round-trip Word/HTML report export from the DB — **not built**.
- **Backup/versioning snapshot before each run** (`kash/backup.py`) — **Done** (runs at the
  top of every update cycle; keeps the last 14).

## Cross-cutting principles
- **Human notes are protected** — scraping fills machine columns only; `analysis` /
  `my_notes` are never overwritten.
- **Config-driven** — preferences, sources, thresholds all in YAML.
- **Schema is the contract** — every source conforms to it; adapters are swappable.

## Open items to resolve before/within each phase
- Which specific licensed APIs to license (cost vs coverage) — Phase 2.
- Managed-scraper provider choice for the Zillow adapter — Phase 2.
- Loop host (local vs Managed Agents) — Phase 3.
