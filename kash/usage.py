"""Per-user LLM usage, so the ladder can move a heavy user onto a different subscription.

Every rung here is an OAuth subscription rather than a metered API, so the scarce resource is
quota, not dollars — and quota is per provider. Rotating a user who has spent their allowance
on codex onto claude_cli spreads load across three independent pools instead of exhausting one.

**Requests, not tokens, are the unit.** Only two of the four backends report token counts:
`claude_cli` and `anthropic` do, `codex` and `agy_cli` do not — and codex is the first rung on
the chat ladder, so it answers most questions. Budgeting purely on tokens would leave the
dominant path unmetered. A request is something every rung can count truthfully.

Token counts are still recorded whenever a backend reports them, flagged by `estimated=0`, so
you get real numbers where they exist without a guess ever being presented as a measurement.

Lives in `pool.db` beside `access` and `enrichment_ledger`, created with the same defensive
`CREATE TABLE IF NOT EXISTS` discipline.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

OWNER = "owner"     # the local dashboard
SYSTEM = "system"   # batch jobs: rank, describe


class Usage:
    def __init__(self, store):
        self.conn = store.conn
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS llm_usage (
                ts TEXT,
                day TEXT,
                actor TEXT,
                backend TEXT,
                job TEXT,
                requests INTEGER DEFAULT 1,
                input_tokens INTEGER,
                output_tokens INTEGER,
                estimated INTEGER DEFAULT 1
            )"""
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_usage_day_actor ON llm_usage(day, actor)")
        self.conn.commit()

    def record(self, actor, backend: str, job: str = "chat",
               usage: Optional[dict] = None) -> None:
        """Log one call. Never raises — metering must not be able to fail a user's query."""
        try:
            now = datetime.now()
            tok = usage or {}
            has_tokens = tok.get("input_tokens") is not None or \
                tok.get("output_tokens") is not None
            self.conn.execute(
                """INSERT INTO llm_usage
                       (ts, day, actor, backend, job, requests,
                        input_tokens, output_tokens, estimated)
                   VALUES (?,?,?,?,?,1,?,?,?)""",
                (now.isoformat(timespec="seconds"), now.date().isoformat(),
                 str(actor), backend, job,
                 tok.get("input_tokens"), tok.get("output_tokens"),
                 0 if has_tokens else 1),
            )
            self.conn.commit()
        except Exception:  # noqa: BLE001 — bookkeeping is not worth breaking the product for
            pass

    def spent_today(self, actor, backend: Optional[str] = None, day: Optional[str] = None) -> dict:
        """Requests and known tokens spent by an actor today, optionally on one backend."""
        day = day or date.today().isoformat()
        sql = ("SELECT COALESCE(SUM(requests),0), COALESCE(SUM(input_tokens),0), "
               "COALESCE(SUM(output_tokens),0) FROM llm_usage WHERE day=? AND actor=?")
        params: list = [day, str(actor)]
        if backend:
            sql += " AND backend=?"
            params.append(backend)
        r = self.conn.execute(sql, params).fetchone()
        return {"requests": r[0], "input_tokens": r[1], "output_tokens": r[2]}

    def by_backend_today(self, actor, day: Optional[str] = None) -> dict:
        day = day or date.today().isoformat()
        return {b: n for b, n in self.conn.execute(
            "SELECT backend, SUM(requests) FROM llm_usage WHERE day=? AND actor=? "
            "GROUP BY backend", (day, str(actor)))}


def record_backend_call(store, backend, actor, job="chat"):
    """Log one completed backend call (moved here from the retired kash.nl). Safe to call
    with a partly-failed backend or a None one — silently a no-op."""
    if store is None or actor is None or getattr(backend, "chosen", None) is None:
        return
    Usage(store).record(actor, backend.chosen, job=job,
                        usage=getattr(backend, "last_usage", None))


def budget_for(config: Optional[dict], actor) -> dict:
    """The budget applying to this actor: a per-actor entry, else the system or default one."""
    budgets = ((config or {}).get("budgets") or {})
    per_actor = budgets.get("per_actor") or {}
    if str(actor) in per_actor:
        return per_actor[str(actor)] or {}
    if str(actor) == SYSTEM:
        return budgets.get("system") or budgets.get("default") or {}
    return budgets.get("default") or {}


def within_budget(config: Optional[dict], usage: "Usage", actor, backend: str) -> bool:
    """Has this actor got allowance left on this specific rung?

    Budgets are per rung, not global: the point is to move a heavy user onto a *different*
    subscription, which only works if each provider's allowance is tracked separately.
    """
    limit = (budget_for(config, actor) or {}).get("requests_per_day")
    if not limit:
        return True
    return usage.spent_today(actor, backend=backend)["requests"] < int(limit)


def format_usage(config: Optional[dict], usage: "Usage", actor) -> str:
    """Human-readable summary for the bot's `usage` command."""
    limit = (budget_for(config, actor) or {}).get("requests_per_day")
    per_backend = usage.by_backend_today(actor)
    total = usage.spent_today(actor)
    if not per_backend:
        cap = f" (limit {limit} per model)" if limit else ""
        return f"No model calls yet today{cap}."
    lines = [f"Today: {total['requests']} call(s)"]
    for backend, n in sorted(per_backend.items()):
        left = f" — {max(0, int(limit) - n)} left" if limit else ""
        lines.append(f"  {backend}: {n}{left}")
    if total["input_tokens"] or total["output_tokens"]:
        lines.append(f"  tokens (where reported): {total['input_tokens']} in / "
                     f"{total['output_tokens']} out")
    return "\n".join(lines)
