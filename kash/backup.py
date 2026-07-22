"""Snapshot the pool DB before an update cycle so a bad scrape is always recoverable."""
from __future__ import annotations

import glob
import os
import shutil
from datetime import datetime
from typing import Optional


def snapshot(db_path: str, keep: int = 14) -> Optional[str]:
    """Copy pool.db to backups/pool-YYYYMMDD-HHMMSS.db; prune to the last `keep`."""
    if not db_path or not os.path.exists(db_path):
        return None
    d = os.path.join(os.path.dirname(os.path.abspath(db_path)), "backups")
    os.makedirs(d, exist_ok=True)
    dest = os.path.join(d, f"pool-{datetime.now():%Y%m%d-%H%M%S}.db")
    shutil.copy2(db_path, dest)
    for old in sorted(glob.glob(os.path.join(d, "pool-*.db")))[:-keep]:
        try:
            os.remove(old)
        except OSError:
            pass
    return dest
