"""Snapshot the pool DB before an update cycle so a bad scrape is always recoverable.

This used to be `shutil.copy2(db_path, dest)`, which was correct while the database ran on a
rollback journal: every committed row lived in pool.db itself.

It stopped being correct the moment WAL was enabled. Under WAL, committed data sits in
`pool.db-wal` until a checkpoint, and a checkpoint-on-close only happens when the *last*
connection closes — which never happens here, because the Telegram bridge holds one open
permanently. A file copy therefore captured whatever happened to have been checkpointed, and
in the worst case an empty database. Reproduced: 11 committed rows, 218 KB sitting in the WAL,
a 4 KB pool.db, and a snapshot containing no `listings` table at all.

The dangerous part was that such a snapshot still passes `PRAGMA integrity_check` — it is a
perfectly valid, perfectly empty database. Restoring one would silently destroy the 35
hand-curated rows (my_notes, investment_thesis, rank, tier, analysis), which are the only
irreplaceable data in the system.

So: use SQLite's online backup API, which reads through the WAL and is safe against concurrent
writers, then verify the result before reporting success. A backup that cannot be proven good
is reported as a failure rather than trusted.
"""
from __future__ import annotations

import glob
import os
import sqlite3
from datetime import datetime
from typing import Optional

# Tables whose row counts must match the source for a snapshot to be considered good.
# listings is the one that matters; the others are cheap to check and catch partial copies.
_VERIFY_TABLES = ("listings", "changelog", "access")


def _counts(conn: sqlite3.Connection) -> dict:
    out = {}
    present = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    for t in _VERIFY_TABLES:
        if t in present:
            out[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    return out


def snapshot(db_path: str, keep: int = 14) -> Optional[str]:
    """Back up pool.db to backups/pool-YYYYMMDD-HHMMSS.db, verified; prune to the last `keep`.

    Returns the destination path on success, None if there is nothing to back up, or a string
    beginning "backup failed" if the snapshot could not be verified. Callers treat any non-path
    return as a failure — see kash/orchestrator.py.
    """
    if not db_path or not os.path.exists(db_path):
        return None
    d = os.path.join(os.path.dirname(os.path.abspath(db_path)), "backups")
    os.makedirs(d, exist_ok=True)
    dest = os.path.join(d, f"pool-{datetime.now():%Y%m%d-%H%M%S}.db")

    src = None
    try:
        # Read-only on the source: never let a backup mutate the thing it is protecting.
        src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        expected = _counts(src)
        with sqlite3.connect(dest) as out:
            src.backup(out)          # online backup API — reads through the WAL
    except sqlite3.Error as e:
        if os.path.exists(dest):
            os.remove(dest)
        return f"backup failed: {e}"
    finally:
        if src is not None:
            src.close()

    # Verify before trusting it. An empty snapshot passes integrity_check, so row counts are
    # the only check that would actually have caught the WAL bug.
    try:
        with sqlite3.connect(dest) as check:
            if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise sqlite3.DatabaseError("integrity_check failed")
            got = _counts(check)
        if got != expected:
            os.remove(dest)
            return f"backup failed: row counts differ (source {expected}, backup {got})"
    except (sqlite3.Error, OSError) as e:
        if os.path.exists(dest):
            os.remove(dest)
        return f"backup failed: {e}"

    os.chmod(dest, 0o600)            # it holds the same private data as pool.db
    for old in sorted(glob.glob(os.path.join(d, "pool-*.db")))[:-keep]:
        try:
            os.remove(old)
        except OSError:
            pass
    # A snapshot someone opened read-write leaves -wal/-shm sidecars the .db glob can
    # never prune; a restore of such a pair would replay a stale WAL over the snapshot.
    survivors = set(glob.glob(os.path.join(d, "pool-*.db")))
    for side in glob.glob(os.path.join(d, "pool-*.db-*")):
        if side.rsplit("-", 1)[0] not in survivors:
            try:
                os.remove(side)
            except OSError:
                pass
    return dest


def verify(path: str) -> tuple[bool, str]:
    """Check an existing snapshot. Used by scripts/restore.py and worth running by hand."""
    if not path or not os.path.exists(path):
        return False, "file does not exist"
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as c:
            if c.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                return False, "integrity_check failed"
            counts = _counts(c)
    except sqlite3.Error as e:
        return False, str(e)
    if not counts.get("listings"):
        return False, "contains no listings — this snapshot is empty"
    return True, ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
