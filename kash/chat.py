"""One inbound message in, reply text out. The transport-agnostic chat surface.

Deliberately transport-agnostic: this module performs no network I/O and knows nothing about
Telegram's API. It takes a user id and a string and returns a `Reply` holding text the caller
sends. That is what `OPERATIONS.md` requires (the shared bridge owns polling; Kash stays a
read-only data core), and it is what makes the path testable — every test below runs the real
handler against a fake backend with no network and no subprocess.

The Phase 3 order of operations is the security design:

    enabled → clamp → math → command detection / route model
      ├─ general reasoning (model only)
      ├─ current web (bounded injected provider + model synthesis)
      ├─ clarify (terminal)
      └─ database → access → command or chat_policy → ReadOnlyStore → render

Three boundaries are load-bearing:

* **General and web routes never touch the database.** Routing precedes construction of
  ``Access`` or ``ReadOnlyStore`` and those branches receive no store/tool argument.
* **The model never receives listing rows.** It sees the message and database column names
  only. Web synthesis sees only capped provider results.
* **Listings are read through `ReadOnlyStore`.** The writable store is passed in for the two
  things that are genuinely writes — recording usage and recording an access request — and is
  never used to read or write listings.

Rate limiting is per-process and in-memory, so it resets if the bridge restarts. That is
acceptable because it only guards against bursts; the durable daily ceiling is
`usage.within_budget`, which is backed by the `llm_usage` table and survives a restart.
"""
from __future__ import annotations

import re
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Optional

from . import (
    brave_search,
    chat_policy,
    chat_sessions,
    chat_vocabulary,
    commands,
    llm,
    local_aggregates,
    market_comparison,
    nl,
    query,
    reasoning_contract,
    reasoning_router,
    web_research,
)
from .access import Access
from .notifications import format_listing_brief, split_text
from .readonly import ReadOnlyStore
from .schema import FIELD_ORDER

DEFAULT_RATE_PER_MINUTE = 6
MAX_MESSAGES = 4            # per reply, after splitting; a flood is not an answer


@dataclass(frozen=True)
class Reply:
    """What the caller should send. `texts` is already split to Telegram's size limit.

    `kind` is for logging and tests, not for the user. `notify_owner`, when set, is a
    separate message the caller should deliver to the owner — used only for first-contact
    access requests.
    """
    texts: tuple[str, ...] = ()
    kind: str = "ok"
    notify_owner: Optional[str] = None
    backend: Optional[str] = None
    dropped: tuple[str, ...] = field(default_factory=tuple)
    # The dashboard may refresh its local table from a policy-gated database reply.  These
    # rows are never sent to a model and ordinary transports consume only `texts`.
    rows: tuple[dict, ...] = field(default_factory=tuple)

    @property
    def text(self) -> str:
        return "\n".join(self.texts)


# --- throttle ---------------------------------------------------------------------------------

_HITS: dict[int, deque] = defaultdict(deque)


def reset_throttle() -> None:
    """Clear the in-memory window. For tests and for a config reload."""
    _HITS.clear()


def _throttled(user_id: int, per_minute: int, now: float) -> bool:
    hits = _HITS[int(user_id)]
    while hits and now - hits[0] > 60:
        hits.popleft()
    if len(hits) >= per_minute:
        return True
    hits.append(now)
    return False


# --- prompt -----------------------------------------------------------------------------------

def _prompt(message: str, allowed: list[str], *, vocabulary: str = "",
           context: str = "") -> str:
    """Compatibility wrapper for the Phase 3 route prompt."""
    return reasoning_contract.prompt(message, allowed, vocabulary=vocabulary,
                                     context=context)


def _visible_columns(access_level: str) -> list[str]:
    """Schema order, filtered to what this level may use — order matters for prompt stability."""
    allowed = chat_policy.visible_fields(access_level)
    return [f for f in FIELD_ORDER if f in allowed]


# --- reply assembly ----------------------------------------------------------------------------

def _split(text: str, kind: str, **kw) -> Reply:
    parts = split_text(text) or [""]
    if len(parts) > MAX_MESSAGES:
        parts = parts[:MAX_MESSAGES]
        parts[-1] += "\n\n(trimmed — ask something narrower for the rest)"
    return Reply(texts=tuple(parts), kind=kind, **kw)


def _render_rows(rows: list[dict], lead: str, level: str) -> str:
    shown = rows[:chat_policy.MAX_ROWS_RENDERED]
    lines = [lead] if lead else []
    lines += [format_listing_brief(r) for r in chat_policy.redact_rows(shown, level)]
    if len(rows) > len(shown):
        lines.append(f"…and {len(rows) - len(shown)} more. Narrow it down to see the rest.")
    return "\n".join(lines)


def _empty_result_reply(lead: str, diagnosis: list[tuple[dict, int]]) -> str:
    """Zero rows, explained: which single filter (if any) is the binding constraint,
    COUNT-only so this never leaks a second row-returning query's worth of content."""
    base = lead or "Nothing matches that exact combination."
    if not diagnosis:
        examples = chat_vocabulary.peek_examples()
        if examples:
            base += " Try, for example: " + "; ".join(examples) + "."
        return chat_policy.clamp_reply(base)
    lines = [base]
    for f, n in diagnosis:
        word = "home" if n == 1 else "homes"
        lines.append(f"Dropping {query.describe_filter(f)} would show {n} {word} — want me to?")
    return chat_policy.clamp_reply("\n".join(lines))


# --- access -------------------------------------------------------------------------------------

def _known(access: Access, user_id: int) -> Optional[dict]:
    """The user's access row, or None if this is first contact.

    `Access.request()` returns 'pending' both when it inserts and when the user already
    existed, so it cannot itself tell us whether to ping the owner. The table holds a handful
    of rows, so scanning it is cheaper than adding a column.
    """
    return next((r for r in access.all()
                 if int(r["telegram_user_id"]) == int(user_id)), None)


# --- Phase 3 branches ---------------------------------------------------------------------------

_FAIL_CLOSED_DROPS = (
    "field_not_available",
    "unsupported_operator",
    "null_value",
    "empty_value",       # a dropped-for-empty filter silently BROADENED the query before
    "non_scalar_value",
    "non_numeric_value",
    "filter_not_an_object",
    "filter_without_field",
)


# --- follow-up memory ---------------------------------------------------------------------
# "which of those has a backyard?" only works if "those" means something. When the message
# carries an anaphor AND a session exists, the new query is answered WITHIN the last
# result set; an ordinal ("the second one", "#3") picks a single home from it. Detection is
# deliberately narrow — a plain new question must never be silently scoped to old results.

_FOLLOWUP = re.compile(
    r"\b(those|these|them|of the above|the ones|which ones?|that list|the list)\b"
    r"|\bthe (first|second|third|fourth|fifth|last) one\b|#\d+\b",
    re.IGNORECASE)
_ORDINAL_RE = re.compile(r"\bthe (first|second|third|fourth|fifth|last) one\b|#(\d+)\b",
                         re.IGNORECASE)
_ORDINALS = {"first": 0, "second": 1, "third": 2, "fourth": 3, "fifth": 4, "last": -1}

_FOLLOWUP_CONTEXT_TTL_SECONDS = 300   # a separate, SHORTER freshness window than the
    # session's own 30-minute TTL: message adjacency alone is a weaker signal than an
    # explicit anaphor ("the second one", which still gets the full 30 minutes), so
    # replaying filters as unprompted router context stops well before the session itself
    # expires.


def _followup_context(sess, ttl_seconds: int, *, now: Optional[float] = None) -> str:
    """Render the previous message's filters as OPTIONAL context for the router — only
    while the session is fresher than _FOLLOWUP_CONTEXT_TTL_SECONDS."""
    if not sess:
        return ""
    filters = sess.get("filters")
    if not isinstance(filters, list) or not filters:
        return ""
    expires_at = sess.get("expires_at")
    if not isinstance(expires_at, (int, float)):
        return ""
    now = time.time() if now is None else now
    age = (ttl_seconds or 1800) - (expires_at - now)
    if age < 0 or age > _FOLLOWUP_CONTEXT_TTL_SECONDS:
        return ""
    parts = [f"{f.get('field')} {f.get('op')} {f.get('value')}" for f in filters
             if isinstance(f, dict) and f.get("field") and f.get("op")]
    return "; ".join(parts)


def _session_key(row: dict):
    """MUST mirror chat_sessions.save_search's key derivation, or intersections miss."""
    return (row.get("property_id") or row.get("source_url")
            or row.get("listing_url") or row.get("match_key"))


def _ordinal_index(message: str):
    m = _ORDINAL_RE.search(message or "")
    if not m:
        return None
    if m.group(1):
        return _ORDINALS[m.group(1).lower()]
    return int(m.group(2)) - 1


def _rows_for_session_keys(ro, keys) -> list[dict]:
    """Resolve saved session keys back to rows, preserving the list's order."""
    out = []
    for key in keys:
        hit = ro.execute_select(
            "SELECT * FROM listings WHERE property_id=? OR source_url=?"
            " OR listing_url=? OR match_key=? LIMIT 1", (key, key, key, key))
        if hit:
            out.append(hit[0])
    return out


def _followup_pick(user_id: int, *, store, keys, index: int,
                   session_store) -> Optional[Reply]:
    """An ordinal reference into the last list — render that one home in full."""
    access = Access(store)
    if not access.is_allowed(user_id):
        return None                      # let the normal flow run its pending/denied path
    row_meta = _known(access, user_id) or {}
    level = (row_meta.get("access_level") or "read").lower()
    rows = _rows_for_session_keys(ReadOnlyStore(store), keys)
    if not rows:
        return None
    if index == -1:
        index = len(rows) - 1
    if not 0 <= index < len(rows):
        return _split(f"Your last list has {len(rows)} home(s) — pick one of those.",
                      "clarify")
    picked = rows[index]
    if session_store is not None:
        try:
            session_store.save(user_id, filters=[], listing_keys=list(keys),
                               selected_key=_session_key(picked))
        except Exception:  # noqa: BLE001 — memory must never block an answer
            pass
    return _split(_render_rows([picked], f"From your last list, #{index + 1}:", level),
                  "query", rows=(picked,))


def _database_reply(user_id: int, name, message: str, *, store, prefs: dict,
                    spec: reasoning_contract.RouteSpec | None, backend,
                    session_store: chat_sessions.SessionStore | None = None,
                    followup_keys=None, pending_usage=None) -> Reply:
    """The only Phase 3 branch allowed to construct database-facing objects."""
    access = Access(store)
    ro = ReadOnlyStore(store)
    # Best-effort: warms the routing-prompt vocabulary cache for the NEXT message. Never
    # raises, never blocks THIS answer — see kash.chat_vocabulary's module docstring for
    # why this is the only call site allowed to do it.
    chat_vocabulary.maybe_refresh(ro, prefs)

    if not access.is_allowed(user_id):
        first_contact = _known(access, user_id) is None
        status = access.request(user_id, name, message)
        if status == "denied":
            return Reply(kind="denied")
        notify = None
        if first_contact:
            notify = (f"New Kash access request from {name or 'unknown'} (id {user_id}): "
                      f"{message[:120]}")
        return Reply(texts=("Thanks — I've passed your request to the owner for approval.",),
                     kind="pending", notify_owner=notify)

    row = _known(access, user_id) or {}
    level = (row.get("access_level") or "read").lower()
    ctx = commands.Context(user_id=user_id, access_level=level, store=store, ro=ro,
                           prefs=prefs, access=access)
    try:
        answered = commands.try_handle(message, ctx)
    except Exception as e:  # noqa: BLE001 - a broken command is not fatal
        return _split(f"That command didn't work ({type(e).__name__}).", "error")
    if answered is not None:
        return _split(answered, "command")

    if spec is None:
        return _split(chat_vocabulary.safe_clarify_with_examples(), "clarify")

    # Durable metering is intentionally limited to this database branch. General and web
    # cannot write usage without violating their no-database contract.
    if pending_usage is not None:
        # The wasted first call from a repair-retry sequence — only ever reaches here
        # (and so only ever reaches `store`) once the FINAL route is confirmed to be
        # database_query. See handle()'s routing tail for why this can't be recorded
        # any earlier.
        pending_chosen, pending_last_usage = pending_usage
        if pending_chosen is not None:
            from .usage import Usage
            Usage(store).record(user_id, pending_chosen, job="chat_invalid",
                                usage=pending_last_usage)
    nl.record_usage(store, backend, user_id, job="chat")
    chosen = getattr(backend, "chosen", None)
    safe = chat_policy.sanitize_spec({
        "filters": spec.filters,
        "sort": spec.sort,
        "order": spec.order,
        "limit": spec.limit,
    }, level)
    if any(reason.startswith(_FAIL_CLOSED_DROPS) for reason in safe.dropped):
        return _split(chat_vocabulary.safe_clarify_with_examples(), "clarify",
                      backend=chosen, dropped=safe.dropped)

    reply = chat_policy.clamp_reply(spec.reply)
    try:
        rows = query.run(ro, filters=safe.filters, sort=safe.sort,
                         order=safe.order, limit=safe.limit)
    except Exception:  # noqa: BLE001 - sanitize should prevent this
        return _split("I built a query I couldn't run. Try rephrasing?", "error",
                      backend=chosen, dropped=safe.dropped)
    if not rows:
        diagnosis = query.diagnose_empty(ro, safe.filters)
        return _split(_empty_result_reply(reply, diagnosis), "empty_result",
                      backend=chosen, dropped=safe.dropped)
    lead = reply or f"{len(rows)} match:"
    if followup_keys:
        keep = {str(k) for k in followup_keys}
        narrowed = [r for r in rows if str(_session_key(r)) in keep]
        if narrowed:
            rows = narrowed
            lead = f"Within your last list — {lead}"
        else:
            lead = f"{lead} (none of your last list matched; showing the whole pool)"
    if session_store is not None:
        try:
            session_store.save_search(user_id, filters=safe.filters, rows=rows)
        except Exception:
            # Session convenience can never block or alter a database answer.
            pass
    return _split(_render_rows(rows, lead, level), "query",
                  backend=chosen, dropped=safe.dropped, rows=tuple(rows))


def _web_reply(message: str, spec: reasoning_contract.RouteSpec, *, backend,
               provider) -> Reply:
    """Research current facts using only a bounded injected provider."""
    if not reasoning_contract.web_is_justified(message):
        return _split(chat_vocabulary.safe_clarify_with_examples(), "clarify")

    provider = provider if provider is not None else brave_search.provider_from_env()
    bundle = web_research.collect(provider, spec.web_queries or (message,))
    if not bundle.available or not bundle.results:
        return _split(web_research.UNAVAILABLE_REPLY, "web_unavailable")

    synthesis_prompt = (
        "Answer the user's question using only the bounded source packet below. "
        "Do not add unsupported facts. Be concise. Provenance is appended by code, so do not "
        "invent URLs or a Sources section.\n\n"
        f"User: {message}\n\nSources:\n{web_research.source_prompt(bundle.results)}"
    )
    try:
        answer = backend.query_spec(synthesis_prompt, reasoning_contract.WEB_ANSWER_SCHEMA)
    except Exception:  # noqa: BLE001 - no uncited fallback or provider detail leakage
        return _split(web_research.UNAVAILABLE_REPLY, "web_unavailable")
    if not isinstance(answer, dict) or set(answer) != {"reply"}:
        return _split(web_research.UNAVAILABLE_REPLY, "web_unavailable")
    reply = chat_policy.clamp_reply(answer.get("reply"))
    if not reply:
        return _split(web_research.UNAVAILABLE_REPLY, "web_unavailable")
    return _split(f"{reply}\n\n{web_research.citations(bundle.results)}",
                  "web_research", backend=getattr(backend, "chosen", None))


def _overall_price_reply(user_id: int, name, message: str, *, store) -> Reply:
    """Render the single allow-listed whole-pool aggregate without model/web access."""
    access = Access(store)
    if not access.is_allowed(user_id):
        first_contact = _known(access, user_id) is None
        status = access.request(user_id, name, message)
        if status == "denied":
            return Reply(kind="denied")
        notify = (f"New Kash access request from {name or 'unknown'} (id {user_id}): "
                  f"{message[:120]}" if first_contact else None)
        return Reply(texts=("Thanks — I've passed your request to the owner for approval.",),
                     kind="pending", notify_owner=notify)
    summary = ReadOnlyStore(store).overall_price_summary()
    count = int(summary.get("listing_count") or 0)
    average = summary.get("average_list_price")
    if not count or average is None:
        return _split("I don't have listings with a recorded list price to average yet.",
                      "empty_result")
    return _split(
        f"Across {count} listings with a recorded list price, the overall average list price is "
        f"${float(average):,.2f}.",
        "database_aggregate",
    )


def _market_comparison_reply(user_id: int, name, *, store, plan, backend, provider) -> Reply:
    """Compose a fixed local SQL aggregate with bounded, cited external research.

    This is intentionally outside the model's one-route contract.  The local statistic is
    calculated before the web synthesis and is appended by code, so the model receives source
    material only — neither rows nor private fields nor even local aggregate values.
    """
    access = Access(store)
    if not access.is_allowed(user_id):
        first_contact = _known(access, user_id) is None
        status = access.request(user_id, name, "market comparison")
        if status == "denied":
            return Reply(kind="denied")
        notify = (f"New Kash access request from {name or 'unknown'} (id {user_id}): market comparison"
                  if first_contact else None)
        return Reply(texts=("Thanks — I've passed your request to the owner for approval.",),
                     kind="pending", notify_owner=notify)

    summary = ReadOnlyStore(store).price_summary("active")
    count = int(summary.get("listing_count") or 0)
    average = summary.get("average_list_price")
    if not count or average is None:
        return _split("I don't have active local listings with list prices to compare yet.",
                      "empty_result")

    provider = provider if provider is not None else brave_search.provider_from_env()
    bundle = web_research.collect(provider, (plan.web_query,))
    if not bundle.available or not bundle.results:
        return _split(web_research.UNAVAILABLE_REPLY, "web_unavailable")
    comparable = market_comparison.comparable_results(plan, bundle.results)
    if not comparable:
        return _split(
            "I found current market sources, but not an average active-listing benchmark "
            "comparable with Kash's local average. I won't mix median or closed-sale data "
            "into that comparison.",
            "benchmark_unavailable",
        )
    prompt = (
        "State the external benchmark requested by the user using only this bounded source "
        "packet. Do not add unsupported facts, URLs, or a Sources heading; provenance is "
        "appended by code. Do not mention local listings or infer a comparison result.\n\n"
        f"Sources:\n{web_research.source_prompt(comparable)}"
    )
    try:
        answer = backend.query_spec(prompt, reasoning_contract.WEB_ANSWER_SCHEMA)
    except Exception:  # noqa: BLE001
        return _split(web_research.UNAVAILABLE_REPLY, "web_unavailable")
    if not isinstance(answer, dict) or set(answer) != {"reply"}:
        return _split(web_research.UNAVAILABLE_REPLY, "web_unavailable")
    benchmark = chat_policy.clamp_reply(answer.get("reply"))
    if not benchmark:
        return _split(web_research.UNAVAILABLE_REPLY, "web_unavailable")
    rounded_average = int(float(average) + 0.5)
    local = (f"Assumption: active listing prices (Kash's local pool does not store verified "
             f"closed-sale prices).\nLocal database: average active list price ${rounded_average:,} "
             f"across {count} active listings.")
    return _split(f"{local}\n\nExternal benchmark ({plan.external_label}): {benchmark}\n\n"
                  f"{web_research.citations(comparable)}", "market_comparison",
                  backend=getattr(backend, "chosen", None))


# --- pre-route detectors (deterministic, zero-model) --------------------------------------------

_SHOW_ALL_ACTIVE = re.compile(
    r"\b(?:show|list|produce|get|give)\b.*\b(?:all|every)\b.*\bactive\b|\b(?:all|every)\b.*\bactive\b.*\b(?:listing|listings|home|homes|property|properties)\b",
    re.IGNORECASE,
)

_AVERAGE_PRICE = re.compile(
    r"\b(?:average|mean)\b.*\b(?:price|cost|list\s+price)\b|\b(?:price|cost)\b.*\b(?:average|mean)\b",
    re.IGNORECASE,
)
# Exclude comparison-like phrases from simple average price detection
_AVERAGE_PRICE_EXCLUDE = re.compile(
    r"\b(?:versus|vs\.?|compare|comparison|versus|exclud|outside|not\s+in|minus|rest\s+of)\b",
    re.IGNORECASE,
)

_BEST_DEAL = re.compile(
    r"\bbest\s+deal\b|\bgreat\s+deal\b|\bgood\s+deal\b|\bvalue\s+(?:pick|buy|home|property)\b",
    re.IGNORECASE,
)

_RECENTLY_SOLD = re.compile(
    r"\brecently\s+sold\b|\bjust\s+sold\b|\bsold\s+recently\b|\blatest\s+sold\b",
    re.IGNORECASE,
)

_NEW_LISTINGS_RECENT = re.compile(
    r"\bnew\s+listing\b.*\b(?:last|past|recent)\b.*\b\d+\s*(?:day|days|week|weeks)\b|\b(?:last|past|recent)\b.*\b\d+\s*(?:day|days|week|weeks)\b.*\bnew\s+listing\b|\bnew\s+listings\b.*\b(?:last|past|recent)\b.*\b\d+\s*(?:day|days|week|weeks)\b|\b(?:last|past|recent)\b.*\b\d+\s*(?:day|days|week|weeks)\b.*\bnew\s+listings\b",
    re.IGNORECASE,
)


def _is_show_all_active(message: str) -> bool:
    return bool(_SHOW_ALL_ACTIVE.search(message))


def _is_average_price(message: str) -> bool:
    if _AVERAGE_PRICE_EXCLUDE.search(message):
        return False
    return bool(_AVERAGE_PRICE.search(message))


def _is_best_deal(message: str) -> bool:
    return bool(_BEST_DEAL.search(message))


def _is_recently_sold(message: str) -> bool:
    return bool(_RECENTLY_SOLD.search(message))


def _is_new_listings_recent(message: str) -> bool:
    return bool(_NEW_LISTINGS_RECENT.search(message))


def _extract_days(message: str, default: int = 7) -> int:
    """Extract number of days from 'last N days' or 'past N days' patterns."""
    match = re.search(r"\b(?:last|past|recent)\s+(\d+)\s*(?:day|days|week|weeks)\b", message, re.IGNORECASE)
    if not match:
        return default
    n = int(match.group(1))
    if "week" in match.group(0).lower():
        n *= 7
    return n


# --- the handler ----------------------------------------------------------------------------------

def handle(user_id, name, text, *, store, prefs=None, backend=None,
           web_provider=None, session_store=None, now=None) -> Reply:
    """Answer one inbound message. Never raises; every failure becomes a Reply.

    `store` is touched only by the database route. `backend` and `web_provider` are
    injectable so tests stay fully offline. When no provider is injected, the opt-in Brave
    adapter is constructed only if `BRAVE_SEARCH_API_KEY` exists; otherwise it is a safe,
    no-network unavailable provider.
    """
    prefs = prefs or {}
    cfg = prefs.get("chat") or {}
    now = time.monotonic() if now is None else now
    user_id = int(user_id)

    if not cfg.get("enabled", False):
        return Reply(kind="disabled")

    message = chat_policy.clamp_message(text)
    if not message:
        return Reply(kind="empty")

    # Math remains terminal and precedes model, database, access, and web provider objects.
    tool_answer = reasoning_router.try_answer(message)
    if tool_answer is not None:
        return _split(tool_answer.text, "math")

    if _throttled(user_id, int(cfg.get("rate_limit_per_minute", DEFAULT_RATE_PER_MINUTE)), now):
        return Reply(texts=("Easy there — give me a minute to catch up.",), kind="throttled")

    # Session is loaded ONCE, unconditionally — a cheap local disk read, not LLM-gated —
    # and feeds two independent things: (1) an anaphor ("those", "#2") triggers the
    # zero-model ordinal-pick fast path below; (2) regardless of any anaphor, a fresh
    # session's filters become OPTIONAL context for the router tail, so "actually make
    # that under 700k" works without any special phrasing. Detection failing open to a
    # normal query is the safe default in both cases.
    followup_keys = None
    followup_context = ""
    sess = None
    if session_store is not None:
        try:
            sess = session_store.load(user_id)
        except Exception:  # noqa: BLE001 — memory must never block an answer
            sess = None
        followup_context = _followup_context(sess, getattr(session_store, "ttl_seconds", 1800))

    if sess and sess.get("listing_keys") and _FOLLOWUP.search(message):
        followup_keys = tuple(sess["listing_keys"])
        index = _ordinal_index(message)
        if index is not None:
            picked = _followup_pick(user_id, store=store, keys=followup_keys,
                                    index=index, session_store=session_store)
            if picked is not None:
                return picked

    # Recognition is pure; only a confirmed command may enter the database branch.
    if commands.is_command(message):
        return _database_reply(user_id, name, message, store=store, prefs=prefs,
                               spec=None, backend=None)

    if reasoning_contract.disallowed(message):
        return _split(reasoning_contract.SAFE_CLARIFY, "clarify")

    # --- pre-routes: deterministic handlers for common intents (zero model calls) ---
    # These run BEFORE local_aggregates/market_comparison so simple phrases work.

    if _is_average_price(message):
        # Delegate to existing overall price aggregate handler
        return _overall_price_reply(user_id, name, message, store=store)

    aggregate = local_aggregates.classify(message)
    if not isinstance(aggregate, str) and aggregate is not None:
        return _overall_price_reply(user_id, name, message, store=store)

    # A fully specified local-versus-market comparison is a safe composition of two bounded
    # capabilities.  Ambiguous variants terminate here; they must not fall through to the
    # legacy-style listing route and dump rows.
    comparison = market_comparison.classify(message)
    if isinstance(comparison, str):
        return _split(comparison, "clarify")
    if isinstance(aggregate, str) and comparison is None:
        return _split(aggregate, "clarify")
    if comparison is not None:
        return _market_comparison_reply(user_id, name, store=store, plan=comparison,
                                        backend=backend, provider=web_provider)

    # --- more pre-routes (after market_comparison to avoid intercepting comparisons) ---

    if _is_show_all_active(message):
        # "All" must MEAN all: the old limit=100 fetched a quarter of the pool and the
        # renderer's "…and N more" trailer then undercounted the rest. Fetch everything;
        # the renderer still shows MAX_ROWS_RENDERED and reports the true remainder.
        spec = reasoning_contract.RouteSpec(
            route=reasoning_contract.DATABASE_QUERY,
            reply="All active listings:",
            filters=[{"field": "status", "op": "=", "value": "active"}],
            sort="rank",
            order="asc",
            limit=2000,
            web_queries=(),
            valid=True,
        )
        return _database_reply(user_id, name, message, store=store, prefs=prefs,
                               spec=spec, backend=None, session_store=session_store,
                               followup_keys=followup_keys)

    if _is_average_price(message):
        # Delegate to existing overall price aggregate handler
        return _overall_price_reply(user_id, name, message, store=store)

    if _is_best_deal(message):
        # Sort by price_per_sqft asc (best value per sqft) as proxy for "best deal"
        spec = reasoning_contract.RouteSpec(
            route=reasoning_contract.DATABASE_QUERY,
            reply="Best value homes (lowest price per sqft):",
            filters=[{"field": "status", "op": "=", "value": "active"}],
            sort="price_per_sqft",
            order="asc",
            limit=10,
            web_queries=(),
            valid=True,
        )
        return _database_reply(user_id, name, message, store=store, prefs=prefs,
                               spec=spec, backend=None, session_store=session_store,
                               followup_keys=followup_keys)

    if _is_recently_sold(message):
        # Sold listings, sorted by sold_date desc, up to 20 results
        spec = reasoning_contract.RouteSpec(
            route=reasoning_contract.DATABASE_QUERY,
            reply="Recently sold homes:",
            filters=[{"field": "status", "op": "=", "value": "sold"}],
            sort="sold_date",
            order="desc",
            limit=20,
            web_queries=(),
            valid=True,
        )
        return _database_reply(user_id, name, message, store=store, prefs=prefs,
                               spec=spec, backend=None, session_store=session_store,
                               followup_keys=followup_keys)

    if _is_new_listings_recent(message):
        # Active listings with first_seen_date genuinely in the last N days. The previous
        # spec only sorted by date while the lead text PROMISED a filtered window — a
        # wrong answer presented as a filtered one.
        days = _extract_days(message)
        from datetime import date as _date, timedelta as _timedelta
        cutoff = (_date.today() - _timedelta(days=days)).isoformat()
        spec = reasoning_contract.RouteSpec(
            route=reasoning_contract.DATABASE_QUERY,
            reply=f"New listings from the last {days} days:",
            filters=[{"field": "status", "op": "=", "value": "active"},
                     {"field": "first_seen_date", "op": ">=", "value": cutoff}],
            sort="first_seen_date",
            order="desc",
            limit=20,
            web_queries=(),
            valid=True,
        )
        return _database_reply(user_id, name, message, store=store, prefs=prefs,
                               spec=spec, backend=None, session_store=session_store,
                               followup_keys=followup_keys)

    # Deliberately omit actor/store: route selection cannot consult access, usage, or listings.
    if backend is None:
        backend = llm.route(prefs.get("llm"), job="chat")
    try:
        ok, _why = backend.available()
    except Exception:  # noqa: BLE001
        ok = False
    if not ok:
        return _split("I can't reach a model right now — try again shortly.", "unavailable")

    prompt_text = _prompt(message, _visible_columns("read"),
                          vocabulary=chat_vocabulary.peek_prompt_block(),
                          context=followup_context)
    try:
        raw = backend.query_spec(prompt_text, reasoning_contract.ROUTE_SCHEMA)
    except Exception:  # noqa: BLE001 - never leak a traceback
        return _split("I couldn't work that one out — mind rephrasing?", "error")

    spec = reasoning_contract.parse(raw)
    pending_usage = None
    if not spec.valid:
        # A malformed reply already cost real quota — but general_reasoning/web_research
        # must never touch `store` (tests prove this with store=Bomb()), and we don't yet
        # know which route the retry will land on. So the wasted call's usage is snapshotted
        # here (before the retry overwrites backend.chosen/last_usage) and only actually
        # recorded later, from _database_reply, if the FINAL route turns out to be
        # database_query — the one route already allowed to touch store. If the retry ends
        # up general_reasoning/web_research/clarify, this snapshot is simply dropped,
        # matching the pre-existing (unrelated) gap where those routes are never metered.
        pending_usage = (getattr(backend, "chosen", None), getattr(backend, "last_usage", None))
        try:
            raw = backend.query_spec(
                reasoning_contract.repair_prompt(prompt_text, raw),
                reasoning_contract.ROUTE_SCHEMA,
            )
            spec = reasoning_contract.parse(raw)
        except Exception:  # noqa: BLE001 - never leak a traceback
            spec = reasoning_contract.RouteSpec()
        if not spec.valid:
            return _split(chat_vocabulary.safe_clarify_with_examples(), "clarify")
    if spec.route == reasoning_contract.CLARIFY:
        return _split(chat_policy.clamp_reply(spec.reply)
                      or chat_vocabulary.safe_clarify_with_examples(), "clarify")
    if spec.route == reasoning_contract.GENERAL_REASONING:
        reply = chat_policy.clamp_reply(spec.reply)
        return _split(reply or chat_vocabulary.safe_clarify_with_examples(),
                      "general_reasoning", backend=getattr(backend, "chosen", None))
    if spec.route == reasoning_contract.WEB_RESEARCH:
        return _web_reply(message, spec, backend=backend, provider=web_provider)
    return _database_reply(user_id, name, message, store=store, prefs=prefs,
                           spec=spec, backend=backend, session_store=session_store,
                           followup_keys=followup_keys, pending_usage=pending_usage)
