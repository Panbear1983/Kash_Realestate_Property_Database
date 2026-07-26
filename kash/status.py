"""Read-only acquisition status for the local Kash dashboard."""
from __future__ import annotations


def acquisition_summary(store, prefs: dict) -> dict:
    source_cfg = prefs.get("sources") or {}
    sources = tuple(
        f"{name} (every {int((cfg or {}).get('every_days', 1))}d)"
        for name, cfg in sorted(source_cfg.items())
    )
    min_baths = float((prefs.get("eligibility") or {}).get("telegram_min_baths", 2.5))
    return {
        "pool_size": store.count(),
        "sources": sources,
        "policy": (
            "Kash stores qualifying homes for review; Telegram alerts require "
            f"{min_baths:g}+ baths and verified non-AE/VE flood status."
        ),
        "controls": "This screen is read-only. Search settings stay in the local dashboard.",
    }


def format_acquisition_summary(summary: dict) -> str:
    sources = ", ".join(summary["sources"]) or "no sources configured"
    return (
        "[b]Acquisition status[/b]\n"
        f"Pool: {summary['pool_size']} listings\n"
        f"Sources: {sources}\n"
        f"{summary['policy']}\n"
        f"{summary['controls']}"
    )
