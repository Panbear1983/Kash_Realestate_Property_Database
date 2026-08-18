"""Opt-in Brave Search adapter using only Brave's fixed HTTPS endpoint.

The factory is deliberately the only configuration entry point.  It returns an unavailable
provider unless ``BRAVE_SEARCH_API_KEY`` is non-empty, so creating or routing through this
module cannot cause a network request in an unconfigured deployment.
"""
from __future__ import annotations

import json
import os
import pwd
import stat
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .web_research import MAX_RESULTS_PER_QUERY, WebResult, UnavailableWebProvider, _result

BRAVE_SEARCH_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
REQUEST_TIMEOUT_SECONDS = 5.0
KEYCHAIN_SERVICE = "Kash Brave Search API"
KEYCHAIN_ACCOUNT = "brave-search"

Transport = Callable[[Request, float], object]


def _default_secret_file() -> Path:
    """Use the effective process owner's home, not launchd's optional HOME environment."""
    try:
        home = pwd.getpwuid(os.getuid()).pw_dir
    except Exception:  # noqa: BLE001 - a missing passwd entry is an unavailable secret source
        return Path("/") / ".unavailable-kash-secret"
    return Path(home) / ".config" / "kash" / "brave.env"


def _key_from_local_secret_file(path: Path | None = None) -> str:
    """Read only a single owner-only, non-symlink local secret file; never log its value."""
    path = _default_secret_file() if path is None else Path(path)
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            metadata = os.fstat(handle.fileno())
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                    or metadata.st_mode & 0o077 or metadata.st_nlink != 1):
                return ""
            lines = handle.read().splitlines()
    except Exception:  # noqa: BLE001 - missing/unsafe secret storage must remain unavailable
        return ""
    values = [line.partition("=")[2].strip() for line in lines
              if line.partition("=")[0].strip().upper() == "BRAVE_SEARCH_API_KEY"]
    return values[0] if len(values) == 1 else ""


def _key_from_keychain() -> str:
    """Read a key only into this process from macOS Keychain; never print or persist it."""
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE,
             "-a", KEYCHAIN_ACCOUNT, "-w"],
            capture_output=True, text=True, timeout=2, check=False,
        )
    except Exception:  # noqa: BLE001 - Keychain absence/denial is a safe unavailable state
        return ""
    value = result.stdout if result.returncode == 0 else ""
    return value.strip() if isinstance(value, str) else ""


class BraveSearchProvider:
    """A bounded Brave Web Search provider; transport is injectable for offline tests."""

    def __init__(self, api_key: str, *, transport: Transport | None = None,
                 timeout: float = REQUEST_TIMEOUT_SECONDS):
        self._api_key = api_key.strip()
        self._transport = transport
        # Never allow an accidentally huge caller timeout to turn this into a hung chat route.
        self._timeout = min(max(float(timeout), 0.1), REQUEST_TIMEOUT_SECONDS)

    def available(self) -> tuple[bool, str]:
        if not self._api_key:
            return False, "Brave Search API key is not configured"
        return True, ""

    def search(self, query: str, *, limit: int) -> list[WebResult]:
        """Return only well-formed Brave web results, or an empty list on any failure."""
        if not self._api_key:
            return []
        try:
            count = min(max(int(limit), 1), MAX_RESULTS_PER_QUERY)
            cleaned_query = str(query).strip()
        except Exception:  # noqa: BLE001 - provider input must not escape chat boundaries
            return []
        if not cleaned_query:
            return []

        # Endpoint is an immutable module constant, never environment/config controlled.
        params = urlencode((("count", str(count)), ("q", cleaned_query)))
        url = f"{BRAVE_SEARCH_ENDPOINT}?{params}"
        request = Request(url, headers={
            "Accept": "application/json",
            "X-Subscription-Token": self._api_key,
        }, method="GET")
        response = None
        try:
            if self._transport is None:
                response = urlopen(request, timeout=self._timeout)
            else:
                response = self._transport(request, self._timeout)
            status = getattr(response, "status", 200)
            if not isinstance(status, int) or not 200 <= status < 300:
                return []
            payload = json.loads(response.read().decode("utf-8"))
        except Exception:  # noqa: BLE001 - HTTP, decoding, and transport failures fail closed
            return []
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:  # noqa: BLE001 - close failure cannot affect a search result
                    pass

        if not isinstance(payload, Mapping):
            return []
        web = payload.get("web")
        raw_results = web.get("results") if isinstance(web, Mapping) else None
        if not isinstance(raw_results, list):
            return []

        fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        normalized: list[WebResult] = []
        for raw in raw_results[:count]:
            if not isinstance(raw, Mapping):
                continue
            item = _result({
                "title": raw.get("title"),
                "url": raw.get("url"),
                "snippet": raw.get("description"),
                "fetched_at": fetched_at,
            })
            if item is not None:
                normalized.append(item)
        return normalized


def provider_from_env(environ: Mapping[str, str] | None = None, *,
                      transport: Transport | None = None,
                      timeout: float = REQUEST_TIMEOUT_SECONDS,
                      secret_file_reader: Callable[[], str] | None = None,
                      keychain_reader: Callable[[], str] | None = None):
    """Create the opt-in provider from environment, protected local file, or Keychain.

    Production callers omit `environ`: a local file is accepted only when it is the fixed path,
    regular, owned by this user, has exactly one link, and has no group/world permissions.
    Explicit mappings keep tests hermetic.
    """
    use_default_stores = environ is None
    environ = os.environ if environ is None else environ
    api_key = environ.get("BRAVE_SEARCH_API_KEY", "")
    if not isinstance(api_key, str):
        api_key = ""
    if not api_key.strip() and (secret_file_reader is not None or use_default_stores):
        try:
            api_key = (secret_file_reader or _key_from_local_secret_file)() or ""
        except Exception:  # noqa: BLE001 - a secret-store failure must never open browsing
            api_key = ""
    if not api_key.strip() and (keychain_reader is not None or use_default_stores):
        try:
            api_key = (keychain_reader or _key_from_keychain)() or ""
        except Exception:  # noqa: BLE001 - a secret-store failure must never open browsing
            api_key = ""
    if not isinstance(api_key, str) or not api_key.strip():
        return UnavailableWebProvider()
    return BraveSearchProvider(api_key, transport=transport, timeout=timeout)
