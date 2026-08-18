"""Injectable, bounded web-research provider contract.

No network implementation ships here. Production may inject an adapter; tests inject fakes.
The unavailable default and every malformed/raising provider fail without fabricating facts.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping, Protocol, Sequence
from urllib.parse import urlsplit

MAX_QUERY_CHARS = 240
MAX_QUERIES = 2
MAX_RESULTS_PER_QUERY = 3
MAX_TOTAL_RESULTS = 5
MAX_TITLE_CHARS = 180
MAX_URL_CHARS = 1000
MAX_SNIPPET_CHARS = 1200
MAX_FETCHED_AT_CHARS = 80

UNAVAILABLE_REPLY = (
    "Current web research isn't available right now, so I won't guess. "
    "Please try again later or ask a non-current question."
)

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True)
class WebResult:
    """One provider result with mandatory provenance."""

    title: str
    url: str
    fetched_at: str
    snippet: str = ""


class WebResearchProvider(Protocol):
    """Minimal provider surface. Implementations own any actual network behavior."""

    def available(self) -> tuple[bool, str]: ...

    def search(self, query: str, *, limit: int) -> Sequence[WebResult | Mapping]: ...


class UnavailableWebProvider:
    """Safe default: it never attempts a network call."""

    def available(self) -> tuple[bool, str]:
        return False, "no web research provider configured"

    def search(self, query: str, *, limit: int):
        raise RuntimeError("web research provider unavailable")


@dataclass(frozen=True)
class ResearchBundle:
    queries: tuple[str, ...] = ()
    results: tuple[WebResult, ...] = ()
    available: bool = False


def _clean(value, limit: int) -> str:
    return _CONTROL.sub(" ", str(value or "")).strip()[:limit].strip()


def _query(value) -> str:
    return _clean(value, MAX_QUERY_CHARS)


def _result(value) -> WebResult | None:
    if isinstance(value, WebResult):
        raw = {
            "title": value.title,
            "url": value.url,
            "fetched_at": value.fetched_at,
            "snippet": value.snippet,
        }
    elif isinstance(value, Mapping):
        raw = value
    else:
        return None

    title = _clean(raw.get("title"), MAX_TITLE_CHARS)
    url = _clean(raw.get("url"), MAX_URL_CHARS)
    fetched_at = _clean(raw.get("fetched_at"), MAX_FETCHED_AT_CHARS)
    snippet = _clean(raw.get("snippet"), MAX_SNIPPET_CHARS)
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    if not title or not fetched_at or parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    return WebResult(title=title, url=url, fetched_at=fetched_at, snippet=snippet)


def collect(provider: WebResearchProvider | None, queries) -> ResearchBundle:
    """Run bounded provider searches and normalize only provenance-complete results."""
    provider = provider or UnavailableWebProvider()
    try:
        ok, _why = provider.available()
    except Exception:  # noqa: BLE001 - provider failures are an unavailable state
        return ResearchBundle()
    if not ok:
        return ResearchBundle()

    cleaned: list[str] = []
    for raw in queries or ():
        q = _query(raw)
        if q and q not in cleaned:
            cleaned.append(q)
        if len(cleaned) >= MAX_QUERIES:
            break
    if not cleaned:
        return ResearchBundle(available=True)

    found: list[WebResult] = []
    seen_urls: set[str] = set()
    for q in cleaned:
        try:
            raw_results = provider.search(q, limit=MAX_RESULTS_PER_QUERY)
        except Exception:  # noqa: BLE001 - one failed query cannot escape the boundary
            continue
        if not isinstance(raw_results, Sequence) or isinstance(raw_results, (str, bytes)):
            continue
        for raw in list(raw_results)[:MAX_RESULTS_PER_QUERY]:
            item = _result(raw)
            if item is not None and item.url not in seen_urls:
                found.append(item)
                seen_urls.add(item.url)
            if len(found) >= MAX_TOTAL_RESULTS:
                break
        if len(found) >= MAX_TOTAL_RESULTS:
            break
    return ResearchBundle(tuple(cleaned), tuple(found), available=True)


def source_prompt(results: Sequence[WebResult]) -> str:
    """Bounded source packet for synthesis; never includes local listing data."""
    chunks = []
    for index, item in enumerate(results[:MAX_TOTAL_RESULTS], 1):
        chunks.append(
            f"[{index}] Title: {item.title}\nURL: {item.url}\n"
            f"Fetched at: {item.fetched_at}\nSnippet: {item.snippet}"
        )
    return "\n\n".join(chunks)


def citations(results: Sequence[WebResult]) -> str:
    """Deterministic provenance block; the model cannot omit or forge it."""
    lines = ["Sources:"]
    for index, item in enumerate(results[:MAX_TOTAL_RESULTS], 1):
        lines.append(f"[{index}] {item.title} — {item.url} (fetched {item.fetched_at})")
    return "\n".join(lines)
