"""Deterministic Telegram commands — the ones that must never cost a model call.

`help`, `usage`, `model` and the shell-style `filter` / `show` are all answerable from the
database and from config. Routing them through an LLM would spend quota to reformat text we
already have, add seconds of latency, and make the answer non-reproducible. So they are
matched here first and the model is never constructed.

`try_handle()` returns None when the text is not a command, which is the signal for
kash.chat to fall through to the conversational path. Everything here is read-only against
the pool: the writable store is used for the two things that are inherently writes — pinning
a model (`access.model_override`) and reading today's metering — and never for listings.

Bare words are matched conservatively. `help` is a command only when it is the entire
message; "help me find a 3-bed under 700k" is a question, and swallowing it as a help request
would be worse than a slightly narrower command set. `filter` and `show` take arguments
because they are the documented shell syntax (see README) and are unambiguous.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from . import chat_policy, llm, query
from .notifications import format_listing_brief
from .usage import Usage, format_usage

AUTO = "auto"


@dataclass
class Context:
    """What a command needs. `store` is writable (metering, model pinning); `ro` is the
    read-only view every listing read goes through."""
    user_id: int
    access_level: str
    store: object          # kash.store.Store — writes limited to access + usage
    ro: object             # kash.readonly.ReadOnlyStore
    prefs: dict
    access: object         # kash.access.Access


HELP = (
    "I can look up the Staten Island pool.\n\n"
    "Ask me anything, e.g.:\n"
    "  cheapest 3-bed homes under 750k\n"
    "  which active homes have the lowest flood risk?\n\n"
    "Commands:\n"
    "  filter tier=A list_price<=750000 sort:list_price limit:10\n"
    "  show 45 Fairlawn Loop\n"
    "  usage    — model calls you've made today\n"
    "  model    — show or pin which model answers you\n"
    "  help     — this message"
)


def _render(rows: list[dict], level: str, header: str) -> str:
    """Shared row rendering: redact, cap, reuse the existing brief formatter."""
    if not rows:
        return "Nothing matches that one — want to loosen it a bit?"
    shown = rows[:chat_policy.MAX_ROWS_RENDERED]
    lines = [header.format(n=len(rows), shown=len(shown))]
    lines += [format_listing_brief(r) for r in chat_policy.redact_rows(shown, level)]
    if len(rows) > len(shown):
        lines.append(f"…and {len(rows) - len(shown)} more. Narrow it down to see the rest.")
    return "\n".join(lines)


def _cmd_help(_args: str, ctx: Context) -> str:
    greeting = ctx.access.get_greeting(ctx.user_id)
    return f"{greeting}\n\n{HELP}" if greeting else HELP


def _cmd_usage(_args: str, ctx: Context) -> str:
    return chat_policy.clamp_reply(
        format_usage((ctx.prefs or {}).get("llm"), Usage(ctx.store), ctx.user_id))


def _cmd_model(args: str, ctx: Context) -> str:
    current = ctx.access.get_model_override(ctx.user_id)
    choices = ", ".join(sorted(llm.BACKENDS)) + f", {AUTO}"
    if not args:
        now = current or f"{AUTO} (follows the configured ladder)"
        return f"You're on: {now}\nAvailable: {choices}\nSet with: model <name>"

    wanted = args.strip().lower()
    if wanted == AUTO:
        ctx.access.set_model_override(ctx.user_id, None)
        return "Back to auto — I'll use the configured ladder."
    if wanted not in llm.BACKENDS:
        return f"I don't know '{wanted}'. Available: {choices}"
    ctx.access.set_model_override(ctx.user_id, wanted)
    return f"Pinned to {wanted}. Say 'model auto' to undo."


def _cmd_filter(args: str, ctx: Context) -> str:
    if not args:
        return "Try: filter tier=A list_price<=750000 sort:list_price limit:10"
    try:
        filters, sort, order, limit = query.parse_command(args)
    except ValueError as e:                 # shlex chokes on an unbalanced quote
        return f"I couldn't parse that filter ({e})."

    # The typed filter is a spec like any other: same allow-list, same clamps. A person who
    # types `filter my_notes~divorce` is asking the same question the model might have.
    safe = chat_policy.sanitize_spec(
        {"filters": filters, "sort": sort, "order": order, "limit": limit},
        ctx.access_level)
    if safe.dropped and not safe.filters and filters:
        return f"I can't filter on that. ({'; '.join(safe.dropped[:2])})"
    rows = query.run(ctx.ro, filters=safe.filters, sort=safe.sort,
                     order=safe.order, limit=safe.limit)
    return _render(rows, ctx.access_level, "{n} match — showing {shown}:")


def _cmd_show(args: str, ctx: Context) -> str:
    if not args:
        return "Try: show 45 Fairlawn Loop"
    safe = chat_policy.sanitize_spec(
        {"filters": [{"field": "street_address", "op": "contains", "value": args}],
         "sort": chat_policy.DEFAULT_SORT, "order": "asc", "limit": 5},
        ctx.access_level)
    rows = query.run(ctx.ro, filters=safe.filters, sort=safe.sort,
                     order=safe.order, limit=safe.limit)
    if not rows:
        return f"No listing matching '{args}'."
    return _render(rows, ctx.access_level, "{n} match:")


#: Commands taking free-form arguments. Matched on the first word.
_WITH_ARGS = {"filter": _cmd_filter, "show": _cmd_show, "model": _cmd_model}

#: Commands that must be the entire message when written without a slash.
_BARE_ONLY = {"help": _cmd_help, "start": _cmd_help, "commands": _cmd_help,
              "usage": _cmd_usage}


def _match(text: str):
    """Return ``(handler, args)`` for an unambiguous command, otherwise ``None``.

    Kept independent of ``Context`` so the Phase 3 router can preserve the zero-model-call
    command path before it constructs any database objects.
    """
    raw = (text or "").strip()
    if not raw:
        return None

    slashed = raw.startswith("/")
    if slashed:
        raw = raw[1:].strip()
    if not raw:
        return None

    head, _, args = raw.partition(" ")
    head = head.strip().lower()
    # Telegram sends /help@BotName in groups.
    head = head.split("@", 1)[0]
    args = args.strip()

    if head in _BARE_ONLY and (slashed or not args):
        return _BARE_ONLY[head], args

    if head in _WITH_ARGS:
        # Two words here are ordinary English as well as shell syntax, so the bare form is
        # matched narrowly and the slash form always wins:
        #   `model` — a command only with no argument, or one naming a backend we know.
        #             "model homes in great kills" is a question.
        #   `show`  — a command only when the argument carries a house number, which every
        #             address does ("show 45 Fairlawn Loop"). "show me something nice" is a
        #             question, and answering it with an address lookup would be nonsense.
        if not slashed and args:
            if head == "model" and args.lower() not in (set(llm.BACKENDS) | {AUTO}):
                return None
            if head == "show" and not any(c.isdigit() for c in args):
                return None
        return _WITH_ARGS[head], args

    # Backward-compatible compact filters used by the live bridge before consolidation, e.g.
    # `tier=A list_price<=750000`. They now enter the same policy-gated filter command rather
    # than preserving a second adapter-side query path.
    if not slashed and any(op in raw for op in ("=", "<", ">", "~")):
        return _cmd_filter, raw

    return None


def is_command(text: str) -> bool:
    """Whether ``text`` is a deterministic command, without touching model or database."""
    return _match(text) is not None


def try_handle(text: str, ctx: Context) -> Optional[str]:
    """Answer a deterministic command, or return None to fall through to the model."""
    matched = _match(text)
    if matched is None:
        return None
    handler, args = matched
    return handler(args, ctx)
