"""Why a field is still empty — the record that makes gaps legible and retryable.

Before this, `enrich()` caught every exception into a single `stats["errors"]` integer and moved
on. Which row failed, on which field, and why were all discarded, so a source that had been down
for weeks looked identical to a quiet, healthy run. FEMA had been failing on every row of every
cycle with no signal at all.

Two jobs:

* **Legibility** — "flood_zone: 28 rows, 9 attempts each, last error SSLEOFError" instead of
  `errors: 28`.
* **Backoff** — `needs_flood` stays true forever, so a dead endpoint was re-hammered on every
  row of every run, burning the whole enrichment budget on calls that could not succeed.
  Attempts are spaced out geometrically instead.

Lives in the same `pool.db` as `access`, created with the same defensive
`CREATE TABLE IF NOT EXISTS` discipline so an existing database picks it up on open.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

# Hours to wait before attempt N+1. A permanently dead source settles at one try a day rather
# than thousands, and a transient blip still retries within the hour.
BACKOFF_HOURS = [0, 1, 4, 12, 24]
MAX_BACKOFF_HOURS = 24

PENDING = "pending"
FAILED = "failed"
RESOLVED = "resolved"
SKIPPED = "skipped"      # nothing to work with (e.g. no coordinates) — not a failure


def _now() -> datetime:
    return datetime.now()


def _parse(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


class Ledger:
    def __init__(self, store):
        self.conn = store.conn
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS enrichment_ledger (
                match_key TEXT,
                field TEXT,
                attempts INTEGER DEFAULT 0,
                first_attempt TEXT,
                last_attempt TEXT,
                last_error TEXT,
                last_status TEXT,
                resolved_at TEXT,
                PRIMARY KEY (match_key, field)
            )"""
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ledger_field ON enrichment_ledger(field, resolved_at)"
        )
        self.conn.commit()

    # --- writing ---------------------------------------------------------------------------

    def _upsert(self, key: str, field: str, status: str, error: Optional[str],
                bump: bool, resolved: bool) -> None:
        now = _now().isoformat(timespec="seconds")
        row = self.conn.execute(
            "SELECT attempts, first_attempt FROM enrichment_ledger WHERE match_key=? AND field=?",
            (key, field),
        ).fetchone()
        attempts = (row[0] if row else 0) + (1 if bump else 0)
        first = (row[1] if row and row[1] else now)
        self.conn.execute(
            """INSERT INTO enrichment_ledger
                   (match_key, field, attempts, first_attempt, last_attempt,
                    last_error, last_status, resolved_at)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(match_key, field) DO UPDATE SET
                   attempts=excluded.attempts,
                   last_attempt=excluded.last_attempt,
                   last_error=excluded.last_error,
                   last_status=excluded.last_status,
                   resolved_at=excluded.resolved_at""",
            (key, field, attempts, first, now,
             (error or "")[:300] or None, status, now if resolved else None),
        )
        self.conn.commit()

    def record_success(self, key: str, field: str) -> None:
        """Field filled. Closes the entry so it costs nothing on future runs."""
        self._upsert(key, field, RESOLVED, None, bump=True, resolved=True)

    def record_failure(self, key: str, field: str, error: str) -> None:
        self._upsert(key, field, FAILED, error, bump=True, resolved=False)

    def record_skip(self, key: str, field: str, reason: str) -> None:
        """Could not attempt — a missing precondition, not a provider failure.

        Does not bump `attempts`, so a row waiting on its coordinates is not pushed into
        backoff for something that was never its own fault.
        """
        self._upsert(key, field, SKIPPED, reason, bump=False, resolved=False)

    # --- reading ---------------------------------------------------------------------------

    def entry(self, key: str, field: str) -> Optional[dict]:
        row = self.conn.execute(
            """SELECT match_key, field, attempts, first_attempt, last_attempt,
                      last_error, last_status, resolved_at
               FROM enrichment_ledger WHERE match_key=? AND field=?""", (key, field)).fetchone()
        if not row:
            return None
        cols = ["match_key", "field", "attempts", "first_attempt", "last_attempt",
                "last_error", "last_status", "resolved_at"]
        return dict(zip(cols, row))

    def is_due(self, key: str, field: str, now: Optional[datetime] = None) -> bool:
        """Should this (row, field) be attempted right now?

        Unknown to the ledger -> yes. Already resolved -> no. Otherwise only once the
        backoff interval for its attempt count has elapsed.
        """
        e = self.entry(key, field)
        if e is None:
            return True
        if e["resolved_at"]:
            return False
        last = _parse(e["last_attempt"])
        if last is None:
            return True
        idx = min(int(e["attempts"] or 0), len(BACKOFF_HOURS) - 1)
        wait = BACKOFF_HOURS[idx] if idx < len(BACKOFF_HOURS) else MAX_BACKOFF_HOURS
        return (now or _now()) >= last + timedelta(hours=wait)

    def unresolved(self, field: Optional[str] = None) -> list[dict]:
        sql = ("SELECT match_key, field, attempts, last_attempt, last_error, last_status "
               "FROM enrichment_ledger WHERE resolved_at IS NULL")
        params: tuple = ()
        if field:
            sql += " AND field=?"
            params = (field,)
        cols = ["match_key", "field", "attempts", "last_attempt", "last_error", "last_status"]
        return [dict(zip(cols, r)) for r in self.conn.execute(sql + " ORDER BY field, match_key",
                                                              params)]

    def summary(self) -> list[dict]:
        """Per-field rollup for the completeness report."""
        rows = self.conn.execute(
            """SELECT field,
                      COUNT(*) FILTER (WHERE resolved_at IS NULL)  AS outstanding,
                      COUNT(*) FILTER (WHERE resolved_at IS NOT NULL) AS resolved,
                      MAX(attempts) AS max_attempts
               FROM enrichment_ledger GROUP BY field ORDER BY field""").fetchall()
        out = []
        for field, outstanding, resolved, max_attempts in rows:
            err = self.conn.execute(
                """SELECT last_error FROM enrichment_ledger
                   WHERE field=? AND resolved_at IS NULL AND last_error IS NOT NULL
                   ORDER BY last_attempt DESC LIMIT 1""", (field,)).fetchone()
            out.append({"field": field, "outstanding": outstanding, "resolved": resolved,
                        "max_attempts": max_attempts, "last_error": err[0] if err else None})
        return out
