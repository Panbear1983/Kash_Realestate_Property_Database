"""Pure Telegram recipient and message policy; sending remains in run_update.py."""
from __future__ import annotations

from urllib.parse import urlparse

from .dedup import match_key
from .eligibility import alert_ready
from .pipeline import _first_number
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


def health_recipients(allowed_ids: list[int], prefs: dict) -> list[int]:
    """Operational failure alerts go only to explicitly configured owners."""
    configured = prefs.get("telegram_health_recipient_ids") or []
    allowed = set(allowed_ids)
    return [int(user_id) for user_id in configured if int(user_id) in allowed]


def in_alert_scope(row: dict, prefs: dict) -> bool:
    """Is this listing still somewhere the buyer is actually shopping?

    `pipeline._in_scope` gates ingestion only, so rows admitted before the ZIP allow-list
    existed stayed permanently alertable — 8 such rows are in the pool today and 2 were queued
    to be pushed. Checked at read time rather than by deleting rows: `_in_scope` is a pure
    function of (row, prefs), so widening `zips` must bring them straight back, and some rows
    carry human notes.

    Deliberately checks ZIP and beds ONLY, not price. `price.max` and
    `eligibility.telegram_max_price` are independent knobs — the first decides what is worth
    storing, the second what is worth interrupting you for — and folding them together here
    would silently change the alert threshold.
    """
    zips = prefs.get("zips")
    z = row.get("zip")
    if zips and z and str(z) not in {str(v) for v in zips}:
        return False
    beds_min = prefs.get("beds_min")
    if beds_min:
        beds = _first_number(row.get("beds"))
        if beds is not None and beds < float(beds_min):
            return False
    return True


def actionable_listings(rows: list[dict], prefs: dict) -> list[dict]:
    """Keep only active, in-scope, alert-safe listings with a link Telegram can open."""
    return [
        row for row in rows
        if row.get("status") == "active"
        and _is_clickable_url(row.get("listing_url"))
        and in_alert_scope(row, prefs)
        and alert_ready(row, prefs)
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
        "linked alerts only for homes meeting the configured bath minimum with a "
        "verified safe flood status. "
        "Telegram is read-only for now; search settings stay in the local dashboard. "
        "two-way Telegram interaction is planned for a later controlled update."
    )


def _price_text(value) -> str:
    return f"${value:,.0f}" if isinstance(value, (int, float)) else "price unavailable"


ANALYSIS_CHARS = 110


def _truncate(text: str, limit: int) -> str:
    """Cut on a word boundary so a brief never ends mid-word."""
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    space = cut.rfind(" ")
    return (cut[:space] if space > limit * 0.6 else cut).rstrip(" ,.;:—-") + "…"


def _highlight(row: dict) -> str:
    """The one distinguishing feature, if there is one. Empty string if not.

    Deliberately returns nothing rather than a generic sentence: the old fallback
    ("no extra feature claims, no bullshit") fired on 18 of 18 queued listings and accounted
    for 40% of the entire message. A shorter brief beats filler.
    """
    is_multigen, why = multigenerational(row)
    signals = score_description(row.get("listing_description"))
    parts = []
    if is_multigen:
        # Always keep the "multigenerational" label. The evidence alone can be ambiguous —
        # "Live in one, rent the other" does not obviously read as a multigen signal — and this
        # is the buyer's single most important feature, so it must be unmistakable.
        if row.get("signal_separate_entrance") or "separate entrance" in signals.reasons:
            parts.append("multigenerational · separate entrance")
        elif why:
            parts.append(f"multigenerational · {_truncate(why, 50)}")
        else:
            parts.append("multigenerational")
    if row.get("signal_second_kitchen") or "second kitchen" in signals.reasons:
        parts.append("second kitchen")
    if "in-law suite" in signals.reasons:
        parts.append("in-law suite")
    return " · ".join(parts)


def format_listing_brief(row: dict) -> str:
    """Render one listing for Telegram.

    Every line has to earn its place. The previous version spent 52% of the message on text
    identical across all 18 listings — a "🤖 Robo Kash:" prefix restating the message header,
    a "Quick take:" label for a line whose position already said so, and a generic fallback
    sentence. Meanwhile the ranker's per-listing thesis (`analysis`, populated on 18 of 18)
    never reached the phone at all.
    """
    price = _price_text(row.get("list_price"))
    head = f"{row.get('street_address') or 'Listing'} — {price}"
    verdict = " · ".join(x for x in (row.get("tier"), row.get("view_priority")) if x)
    if verdict:
        head += f"  [{verdict}]"

    # Only populated fields; a missing one is omitted, never rendered as a gap. A live message
    # showed "4 ba" with no beds because the absent value still produced a separator.
    facts = []
    if row.get("beds") is not None:
        facts.append(f"{row['beds']} bd")
    if row.get("baths") is not None:
        facts.append(f"{row['baths']} ba")
    if row.get("sqft"):
        facts.append(f"{row['sqft']:,} sqft")
    if row.get("price_per_sqft"):
        facts.append(f"${row['price_per_sqft']:,}/sqft")
    if row.get("neighborhood"):
        facts.append(str(row["neighborhood"]))
    if row.get("monthly_piti"):
        facts.append(f"~${row['monthly_piti']:,}/mo")
    if row.get("days_on_market") is not None:
        facts.append(f"{row['days_on_market']}d on mkt")

    lines = [f"🤖 {head}"]
    if facts:
        lines.append(" · ".join(facts))

    highlight = _highlight(row)
    analysis = " ".join(str(row.get("analysis") or "").replace("[auto]", "").split())
    if analysis:
        lines.append(f"{highlight} — {_truncate(analysis, ANALYSIS_CHARS)}" if highlight
                     else _truncate(analysis, ANALYSIS_CHARS))
    elif highlight:
        lines.append(highlight)

    lines.append(f"🔗 {row['listing_url']}")
    return "\n".join(lines)


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
