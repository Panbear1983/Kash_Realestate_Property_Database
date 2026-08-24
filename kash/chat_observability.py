"""Privacy-minimized operational signals for Kash replies.

This ledger intentionally never stores message text, reply text, user identity, listing data,
URLs, prompts, or exception strings.  It is a local aggregate-quality signal, not chat history.
"""
from __future__ import annotations

import os
import pwd
import re
import sqlite3
import time
from pathlib import Path

_ALLOWED_SURFACES = {"telegram", "dashboard"}
_ALLOWED_FEEDBACK = {"helpful", "wrong", "unclear"}
_ALLOWED_ROUTES = {
    "database_aggregate", "database_query", "market_comparison", "math",
    "general_reasoning", "web_research", "clarify", "unavailable", "error",
    "benchmark_unavailable", "empty_result", "pending", "denied", "feedback",
    # The kinds chat.handle actually emits (the list above predated it): without these,
    # every successful listing answer was ledgered as route="other" — i.e. the quality
    # ledger recorded zero successes while counting real clarifies against them.
    "query", "aggregate", "command", "throttled", "disabled", "empty",
    "web_unavailable", "query_analysis",
}
_RELEASE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def default_path() -> Path:
    """Use the effective account home, never launchd's optional HOME value."""
    home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    return home / ".local" / "state" / "kash" / "quality.sqlite"


def _surface(value: str) -> str:
    return value if value in _ALLOWED_SURFACES else "telegram"


def _route(value: str) -> str:
    return value if value in _ALLOWED_ROUTES else "other"


def _bucket(seconds) -> str:
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        return "unknown"
    if value < 1:
        return "under_1s"
    if value < 3:
        return "1_to_3s"
    if value < 10:
        return "3_to_10s"
    return "over_10s"


class QualityLedger:
    """Small local SQLite ledger that only admits an allow-listed event schema."""

    def __init__(self, path: str | os.PathLike | None = None):
        self.path = str(path or default_path())

    def _connect(self):
        target = Path(self.path)
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            target.parent.chmod(0o700)
        except OSError:
            pass
        connection = sqlite3.connect(self.path)
        connection.execute(
            "CREATE TABLE IF NOT EXISTS quality_events ("
            "occurred_at INTEGER NOT NULL, surface TEXT NOT NULL, route TEXT NOT NULL, "
            "outcome TEXT NOT NULL, latency_bucket TEXT NOT NULL, release TEXT NOT NULL)"
        )
        connection.execute("CREATE INDEX IF NOT EXISTS quality_events_time ON quality_events(occurred_at)")
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        return connection

    def _record(self, *, surface: str, route: str, outcome: str, latency_bucket: str = "unknown",
                release: str = "unknown") -> bool:
        safe_release = release if _RELEASE.fullmatch(str(release)) else "unknown"
        try:
            with self._connect() as connection:
                connection.execute(
                    "INSERT INTO quality_events "
                    "(occurred_at, surface, route, outcome, latency_bucket, release) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (int(time.time()), _surface(surface), _route(route), outcome,
                     latency_bucket, safe_release),
                )
            return True
        except (OSError, sqlite3.Error):
            return False

    def record_reply(self, *, kind: str, surface: str, elapsed_seconds=None, release: str = "unknown",
                     message=None, reply=None, user_id=None) -> bool:
        """Record metadata only. Content and identity parameters are deliberately ignored."""
        del message, reply, user_id
        route = _route(str(kind))
        outcome = "success" if route in {"database_aggregate", "database_query", "market_comparison",
                                         "math", "general_reasoning", "web_research",
                                         "query_analysis"} else route
        return self._record(surface=surface, route=route, outcome=outcome,
                            latency_bucket=_bucket(elapsed_seconds), release=release)

    def record_feedback(self, value: str, *, surface: str, message=None, user_id=None) -> bool:
        """Only accept a fixed feedback enum; user/message fields are intentionally ignored."""
        del message, user_id
        if value not in _ALLOWED_FEEDBACK:
            return False
        return self._record(surface=surface, route="feedback", outcome=value)

    def report(self, *, now: int | None = None) -> str:
        """Return an aggregate report only when an actionable threshold is reached."""
        current = int(time.time()) if now is None else int(now)
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    "SELECT route, outcome, COUNT(*) FROM quality_events "
                    "WHERE occurred_at >= ? GROUP BY route, outcome", (current - 86400,)
                ).fetchall()
        except (OSError, sqlite3.Error):
            return ""
        counts = {(route, outcome): count for route, outcome, count in rows}
        lines = []
        wrong = counts.get(("feedback", "wrong"), 0)
        if wrong >= 3:
            lines.append(f"feedback wrong: {wrong}")
        for route in ("error", "unavailable", "benchmark_unavailable", "clarify"):
            count = sum(value for (event_route, outcome), value in counts.items()
                        if event_route == route or outcome == route)
            if count >= 5:
                lines.append(f"{route}: {count}")
        return "Kash quality report — last 24h\n- " + "\n- ".join(lines) if lines else ""
