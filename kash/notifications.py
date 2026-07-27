"""Pure Telegram recipient and message policy; sending remains in run_update.py."""
from __future__ import annotations

from urllib.parse import urlparse

from .dedup import match_key
from .eligibility import alert_ready
from .signals import multigenerational, score_description


def _is_clickable_url(value) -> bool:
    parsed = urlparse(str(value or ""))
    return parsed.scheme == "https" and bool(parsed.netloc)


def testing_recipients(allowed_ids: list[int], prefs: dict) -> list[int]:
    """Use explicit testing recipients when configured, while honoring access control."""
    configured = prefs.get("telegram_recipient_ids")
    if not configured:
        return allowed_ids
    allowed = set(allowed_ids)
    return [int(user_id) for user_id in configured if int(user_id) in allowed]


def actionable_listings(rows: list[dict], prefs: dict) -> list[dict]:
    """Keep only active, alert-safe listings with a link Telegram can open."""
    return [
        row for row in rows
        if row.get("status") == "active" and _is_clickable_url(row.get("listing_url")) and alert_ready(row, prefs)
    ]


def listing_delivery_kind(row: dict) -> str | None:
    """Delivery receipt key for one listing.

    Includes the price, so a re-priced home counts as a new thing to tell you about. With a
    constant `listing:{key}` a price drop on a home you had already been shown produced no
    notification at all — the single most actionable event for someone tracking a shortlist.
    """
    key = match_key(row)
    if not key:
        return None
    price = row.get("list_price")
    return f"listing:{key}:{price}" if price is not None else f"listing:{key}"


def unsent_actionable_listings(store, recipient_id: int, prefs: dict) -> list[dict]:
    """Find alert-ready rows that this recipient has not already received."""
    rows = actionable_listings(store.all(), prefs)
    return [
        row for row in rows
        if (kind := listing_delivery_kind(row)) and not store.notification_sent(recipient_id, kind)
    ]


def format_onboarding() -> str:
    return (
        "Robo Kash testing is active. Robo Kash stores qualifying homes for review and sends "
        "linked alerts only for 2.5+ bath homes with a verified safe flood status. "
        "Telegram is read-only for now; search settings stay in the local dashboard. "
        "two-way Telegram interaction is planned for a later controlled update."
    )


def _price_text(value) -> str:
    return f"${value:,.0f}" if isinstance(value, (int, float)) else "price unavailable"


def format_listing_brief(row: dict) -> str:
    """Render a short, factual, lightly playful listing note without an LLM."""
    facts = []
    if row.get("beds") is not None:
        facts.append(f"{row['beds']} bd")
    if row.get("baths") is not None:
        facts.append(f"{row['baths']} ba")
    details = " · ".join(facts) or "home details pending"
    # Use the same union rank.py uses, not the raw substring matcher. The keyword path missed
    # every paraphrased mother/daughter setup, so a home the ranker had flagged TOP PRIORITY
    # arrived in Telegram saying "no extra feature claims" — which is the entire reason the
    # description extractor exists.
    is_multigen, why = multigenerational(row)
    signals = score_description(row.get("listing_description"))
    highlights = []
    if is_multigen:
        if row.get("signal_separate_entrance") or "separate entrance" in signals.reasons:
            highlights.append("separate entrance — golden-ticket setup")
        else:
            highlights.append(f"multigenerational setup — {why}" if why
                              else "multigenerational setup")
    if row.get("signal_second_kitchen") or "second kitchen" in signals.reasons:
        highlights.append("second kitchen in the listing notes")
    if "in-law suite" in signals.reasons:
        highlights.append("in-law suite noted")
    if not highlights:
        highlights.append("Robo Kash has the basics on deck; no extra feature claims, no bullshit")
    return (
        f"🤖 Robo Kash: {row.get('street_address') or 'Listing'} — {_price_text(row.get('list_price'))}\n"
        f"{details}\n"
        f"Quick take: {'; '.join(highlights)}.\n"
        f"🔗 {row['listing_url']}"
    )


def format_testing_digest(rows: list[dict]) -> str:
    """Single-message rendering. Prefer chunk_digest() for anything that actually gets sent."""
    if not rows:
        return "Robo Kash testing update: no new actionable listings today."
    return "\n".join(["Robo Kash testing: actionable listings"] +
                     [format_listing_brief(r) for r in rows])


# Telegram rejects anything over 4096 characters.
TELEGRAM_HARD_LIMIT = 4096
TELEGRAM_LIMIT = 3900          # listing chunks, with headroom for the header and part marker


def split_text(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Split arbitrary text on line boundaries so every piece fits.

    Used for the onboarding note and the change digest, which are not lists of listings and so
    cannot go through chunk_digest. Prepending them to the first listing chunk was a real bug:
    the chunk respected the limit, the combined message did not, and Telegram rejected the
    whole thing with "message is too long" — losing that chunk's listings for the run.
    """
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    out, cur = [], []
    cur_len = 0
    for line in text.split("\n"):
        # A single line longer than the limit is hard-split; nothing else can be done with it.
        while len(line) > limit:
            if cur:
                out.append("\n".join(cur))
                cur, cur_len = [], 0
            out.append(line[:limit])
            line = line[limit:]
        if cur and cur_len + len(line) + 1 > limit:
            out.append("\n".join(cur))
            cur, cur_len = [], 0
        cur.append(line)
        cur_len += len(line) + 1
    if cur:
        out.append("\n".join(cur))
    return out


def chunk_digest(rows: list[dict], limit: int = TELEGRAM_LIMIT) -> list[tuple[str, list[dict]]]:
    """Split listings into messages that fit, returning each message with the rows inside it.

    Returning the rows alongside the text is the point: the caller can then mark delivered
    only the listings in a chunk that actually sent. Previously every actionable listing was
    marked delivered whenever the single send returned 'sent', while `rows[:20]` had already
    dropped the rest and `text[:4000]` truncated what remained — so with 28 actionable rows,
    8 were never rendered, more were cut mid-message, and all 28 were recorded as delivered
    and could never be shown again.
    """
    if not rows:
        return []
    header = "Robo Kash testing: actionable listings"
    chunks: list[tuple[str, list[dict]]] = []
    cur_lines, cur_rows = [header], []
    cur_len = len(header)
    for row in rows:
        brief = format_listing_brief(row)
        if cur_rows and cur_len + len(brief) + 1 > limit:
            chunks.append(("\n".join(cur_lines), cur_rows))
            cur_lines, cur_rows, cur_len = [header], [], len(header)
        cur_lines.append(brief)
        cur_rows.append(row)
        cur_len += len(brief) + 1
    if cur_rows:
        chunks.append(("\n".join(cur_lines), cur_rows))
    if len(chunks) > 1:
        chunks = [(f"{t}\n\n({i + 1}/{len(chunks)})", r)
                  for i, (t, r) in enumerate(chunks)]
    return chunks
