#!/usr/bin/env python3
"""Repair bath counts that swallowed a digit of the square footage during the PDF import.

    python scripts/repair_curated_baths.py --pdf ~/Downloads/SI_Complete_Rebuild_114_Properties.pdf
    python scripts/repair_curated_baths.py --pdf ... --apply        # writes, behind a backup

The friend's sheet packs beds/baths and sqft into one run of text, e.g.

    ...SF (verify)4/53,040 $296...          # 4 beds, 5 baths, 3,040 sqft

and the importer's bath field took the leading digit of the sqft with it: 23 Saxon Ave was
stored with `baths = 53`, 35 Daniella Ct with 42, 20 Pearl St with 22. 32 curated rows carry
an impossible bath count, and baths is not cosmetic — it gates Telegram alerts
(telegram_min_baths) and the scope filter, so a garbage value silently changes which houses
the buyer is shown.

The PDF is the source of truth here, not a heuristic: each row is matched by its sheet number
(#36 -> SI-MT-036) and the corrected value is cross-checked against the sqft already stored,
which the import got right. Anything that does not reconcile is reported and left alone.
"""
from __future__ import annotations

import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kash import backup                      # noqa: E402
from kash.store import Store                 # noqa: E402

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.path.join(HERE, "pool.db")
MAX_PLAUSIBLE_BATHS = 6

# "<row#>...<address>, <zip>$<price>...<beds-or-N/A>/<baths><sqft with comma>"
ROW_RE = re.compile(
    r"\n(?P<num>\d{1,3})(?P<flag>NEW|SOON|WORTH|NOW|CALL|WATCH|SKIP)?[A-Za-z]*"
    r"(?P<addr>[^,\n]{4,60}), (?P<zip>\d{5})"
)
BDBA_RE = re.compile(r"(?P<beds>\d+|N/A)/(?P<glued>\d{1,6})(?:,(?P<thousands>\d{3}))?")


def pdf_text(path: str) -> str:
    from pypdf import PdfReader
    reader = PdfReader(path)
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def parse_rows(text: str) -> dict[int, dict]:
    """sheet number -> {address, beds, baths, sqft} as the document states them."""
    out: dict[int, dict] = {}
    matches = list(ROW_RE.finditer(text))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        segment = text[m.end():end]
        # Prefer a match that carries the glued sqft ("4/31,810"). A bare "n/m" also matches
        # open-house dates — "Open Sun 12-3pm (7/26)" reads as 7 beds / 26 baths otherwise,
        # which is exactly how row 48 came out wrong on the first pass.
        candidates = list(BDBA_RE.finditer(segment))
        bd = next((c for c in candidates if c.group("thousands")), None)
        if bd is None:
            continue
        glued, thousands = bd.group("glued"), bd.group("thousands")
        if thousands:
            # "53,040" -> baths 5, sqft 3,040: the last digit before the comma opens the sqft.
            baths, sqft = glued[:-1], int(glued[-1] + thousands)
        else:
            baths, sqft = glued, None
        out[int(m.group("num"))] = {
            "address": m.group("addr").strip(),
            "zip": m.group("zip"),
            "beds": None if bd.group("beds") == "N/A" else bd.group("beds"),
            "baths": baths or None,
            "sqft": sqft,
        }
    return out


def plan(store, sheet: dict[int, dict]) -> tuple[list[dict], list[dict]]:
    """(repairs, unreconciled) — only rows whose stored baths are impossible are touched."""
    repairs, skipped = [], []
    for key, in store.conn.execute("SELECT match_key FROM listings").fetchall():
        row = store.get(key)
        stored = str(row.get("baths") or "")
        if not stored or (_as_float(stored) or 0) <= MAX_PLAUSIBLE_BATHS:
            continue
        pid = str(row.get("property_id") or "")
        m = re.fullmatch(r"SI-MT-(\d{3})", pid)
        if not m:
            # Comps carry C-prefixed ids and live in their own numbered section, so this
            # repair cannot address them by sheet number. Reported, never silently dropped.
            skipped.append({"key": key, "pid": pid or "(none)",
                            "address": row.get("street_address"), "stored_baths": stored,
                            "stored_sqft": row.get("sqft"),
                            "why": "not a numbered sheet row (comp or docx-era id)"})
            continue
        entry = sheet.get(int(m.group(1)))
        item = {"key": key, "pid": pid, "address": row.get("street_address"),
                "stored_baths": stored, "stored_sqft": row.get("sqft"),
                "sheet": entry}
        if not entry or not entry.get("baths"):
            item["why"] = "no matching row in the PDF"
            skipped.append(item)
            continue
        # The document and the database must agree on sqft, or the row numbers are not aligned.
        if entry.get("sqft") and row.get("sqft") and int(entry["sqft"]) != int(row["sqft"]):
            item["why"] = f"sqft disagrees (pdf {entry['sqft']} vs db {row['sqft']})"
            skipped.append(item)
            continue
        # And the corrupt value must actually be the true one with a sqft digit stuck on.
        if not stored.startswith(str(entry["baths"])):
            item["why"] = f"stored {stored} is not {entry['baths']} plus a digit"
            skipped.append(item)
            continue
        item["new_baths"] = entry["baths"]
        item["new_beds"] = entry["beds"] if not row.get("beds") else None
        repairs.append(item)
    return repairs, skipped


def _as_float(v):
    try:
        return float(str(v))
    except (TypeError, ValueError):
        return None


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--apply", action="store_true", help="write the repairs (default: dry run)")
    args = ap.parse_args()

    sheet = parse_rows(pdf_text(os.path.expanduser(args.pdf)))
    print(f"parsed {len(sheet)} rows from the sheet")

    store = Store(args.db)
    repairs, skipped = plan(store, sheet)

    print(f"\n{len(repairs)} rows to repair:")
    for r in repairs:
        beds = f"   beds {r['sheet']['beds']} (was empty)" if r.get("new_beds") else ""
        print(f"  {r['pid']}  {str(r['address'])[:28]:28s} "
              f"baths {r['stored_baths']} -> {r['new_baths']}   "
              f"(sqft {r['stored_sqft']}){beds}")
    if skipped:
        print(f"\n{len(skipped)} left alone:")
        for r in skipped:
            print(f"  {r['pid']}  {str(r['address'])[:28]:28s} baths {r['stored_baths']}"
                  f"   — {r['why']}")

    if not args.apply:
        print("\ndry run — nothing written. Re-run with --apply to write.")
        store.close()
        return

    snap = backup.snapshot(args.db)
    if str(snap).startswith("backup failed"):
        print(f"\nABORT: {snap}")
        store.close()
        sys.exit(1)
    print(f"\nbacked up to {snap}")
    for r in repairs:
        fields = {"baths": r["new_baths"]}
        if r.get("new_beds"):
            fields["beds"] = r["new_beds"]
        store.update_fields(r["key"], fields)
    print(f"repaired {len(repairs)} rows")
    store.close()


if __name__ == "__main__":
    main()
