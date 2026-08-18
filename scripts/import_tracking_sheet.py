#!/usr/bin/env python3
"""Import the friend's master tracking sheet PDF into the pool.

    python scripts/import_tracking_sheet.py <sheet.pdf>            # dry-run: parse + diff, no writes
    python scripts/import_tracking_sheet.py <sheet.pdf> --apply    # write, behind a verified backup
    python scripts/import_tracking_sheet.py <sheet.pdf> --db X.db  # target a copy

The sheet is the second edition of the document that originally seeded this database (the
35-row docx), rebuilt by a friend to 114 properties plus closed/pending comps. Merge policy,
agreed before this was written:

* NEW rows import in full via store.upsert (its insert path writes protected fields — the
  same path seed_from_csv uses).
* OVERLAPPING rows: machine fields (price, sqft, days, zestimate…) NEVER overwrite the pool —
  the scraper's values are fresher and the sheet's own "HONEST NOTES" flags OCR provenance and
  price conflicts. They fill NULLs only. Curated fields (tier, view_priority, analysis,
  my_notes, bids, schools) are the sheet's authority and DO overwrite, with every change
  printed. upsert() is deliberately NOT used for overlaps: it would clobber machine fields
  and log spurious price_drop events.
* N-tier rows (unreviewed, OCR-sourced) import with tier=None so the LLM ranker tiers them;
  their one-line sheet analysis goes to my_notes ("Sheet: …") so the ranker's [auto] text
  doesn't erase it.
* Comps import as rows with status sold / pending / attorney_review / off_market — never
  alertable (the alert gate requires status == 'active').
* Rows the parser cannot read are LISTED, never silently dropped.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Iterable

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from kash import backup, completeness, preferences  # noqa: E402
from kash.dedup import match_key                     # noqa: E402
from kash.ledger import Ledger                       # noqa: E402
from kash.store import Store                         # noqa: E402

SEE_MAP = {"NOW": "now", "SOON": "soon", "WORTH": "worth", "CALL": "call",
           "WATCH": "watch", "SKIP": "skip", "NEW": None}
TIERS = set("SABCWXN")
TYPE_MAP = [  # longest match first
    ("2-Fam Semi-Att", "2fam_semi"), ("2-Fam Detached", "2fam_detached"),
    ("Multi-Family (verify)", None), ("SF Semi-Att", "sf_semi"),
    ("SF Attached", "sf_attached"), ("SF Detached", "sf_detached"),
    ("SF (verify)", None), ("Townhouse", "sf_attached"),
]
COMP_STATUS = {"Closed": "sold", "Pending": "pending",
               "Attorney Review": "attorney_review", "Temp Off Market": "off_market"}

CURATED_FIELDS = ["tier", "view_priority", "analysis", "my_notes", "bid_estimate",
                  "bid_note", "school_name", "school_gs_rating", "school_verified",
                  "brrrr_rating", "target_buy_price", "bd_ba_note", "price_note",
                  "investment_thesis"]
MACHINE_FILL_ONLY = ["list_price", "original_list_price", "beds", "baths", "sqft",
                     "days_on_market", "zestimate", "redfin_estimate", "property_type",
                     "is_multifamily", "neighborhood", "listing_url", "last_sold_price",
                     "status"]

EXPECTED_MAIN_ROWS = tuple(range(1, 115))
EXPECTED_COMP_ROWS = tuple(range(1, 20))
PARSE_STATES = {"parsed", "manual_required", "invalid"}
MIN_SHEET_PRICE = 200_000
MAX_SHEET_PRICE = 2_000_000
MIN_SHEET_SQFT = 200
MAX_SHEET_SQFT = 20_000
PAGE_BREAK = "\n\f\n"
VALID_PROPERTY_TYPES = {mapped for _, mapped in TYPE_MAP if mapped is not None}
SOURCE_VALIDATION_FIELDS = (
    "list_price", "original_list_price", "last_sold_price", "beds", "baths",
    "sqft", "status", "property_type", "zip", "property_id",
)


@dataclass(frozen=True)
class ImportManifestEntry:
    """One explicitly accounted-for source row."""

    source_row_number: int | None
    address: str | None
    source_section: str
    parse_state: str
    match_key: str | None
    validation_errors: tuple[str, ...] = ()
    source_values: dict[str, object] = field(default_factory=dict)
    source_excerpt: str | None = None

    @property
    def source_id(self) -> str:
        number = "unknown" if self.source_row_number is None else str(self.source_row_number)
        return f"{self.source_section}:{number}"

    def repair_item(self) -> dict:
        """Return a deterministic, human-reviewable repair entry."""
        return {
            "source": {
                "section": self.source_section,
                "row_number": self.source_row_number,
                "source_id": self.source_id,
            },
            "address": self.address,
            "match_key": self.match_key,
            "parse_state": self.parse_state,
            "validation_errors": list(self.validation_errors),
            "source_values": dict(self.source_values),
            "source_excerpt": self.source_excerpt,
            "review": {
                "action": "repair_or_quarantine",
                "field_repairs": {},
                "reviewed": False,
            },
        }


@dataclass(frozen=True)
class ImportValidationResult:
    """Structured validation outcome; suitable for a CLI exit status."""

    errors: tuple[str, ...]

    @property
    def is_valid(self) -> bool:
        return not self.errors

    @property
    def exit_code(self) -> int:
        return 0 if self.is_valid else 2


@dataclass(frozen=True)
class ImportReport:
    """Pure source-integrity manifest, independent of a database or write mode."""

    manifest: tuple[ImportManifestEntry, ...]
    expected_main_rows: tuple[int, ...] = EXPECTED_MAIN_ROWS
    expected_comp_rows: tuple[int, ...] = EXPECTED_COMP_ROWS
    report_errors: tuple[str, ...] = ()

    @property
    def entries(self) -> tuple[ImportManifestEntry, ...]:
        """Readable alias for callers that call the manifest a list of entries."""
        return self.manifest

    def validate(
        self,
        *,
        strict: bool = False,
        reviewed_overrides: Iterable[tuple[str, int | None]] = (),
    ) -> ImportValidationResult:
        """Return a nonzero result for strict, unreviewed source exceptions."""
        if not strict:
            return ImportValidationResult(())

        reviewed = set(reviewed_overrides)
        errors = list(self.report_errors)
        for entry in self.manifest:
            if entry.parse_state == "parsed" and not entry.validation_errors:
                continue
            if (entry.source_section, entry.source_row_number) in reviewed:
                continue
            detail = "; ".join(entry.validation_errors) or entry.parse_state
            errors.append(f"{entry.source_id}: {detail}")
        return ImportValidationResult(tuple(errors))

    def repair_manifest(self) -> dict:
        """Return only rows that require repair, retaining their source identity."""
        repairs = [
            entry.repair_item()
            for entry in self.manifest
            if entry.parse_state != "parsed" or entry.validation_errors
        ]
        return {
            "format_version": 1,
            "kind": "tracking_sheet_repair_manifest",
            "repairs": repairs,
            "report_errors": list(self.report_errors),
        }


@dataclass(frozen=True)
class ImportResult:
    """Parsed records plus their complete source-integrity report."""

    main_records: tuple[dict, ...]
    comp_records: tuple[dict, ...]
    report: ImportReport
    quarantined_records: tuple[dict, ...] = ()

    @property
    def records(self) -> tuple[dict, ...]:
        return self.main_records + self.comp_records


def _money_k(txt):
    """'$740K' / '~$630-650K' -> (midpoint_int, raw)."""
    m = re.search(r"~?\$(\d{3,4})(?:-(\d{3,4}))?K", txt or "")
    if not m:
        return None, None
    lo = int(m.group(1)) * 1000
    hi = int(m.group(2)) * 1000 if m.group(2) else lo
    return (lo + hi) // 2, m.group(0)


def _slugify(addr: str, zip_: str) -> str:
    return re.sub(r"[^a-z0-9]", "", f"{addr} Staten Island NY {zip_}".lower())


def parse_school(txt):
    """'Est. PS 8/PS 32 GS 8/10' -> (name, rating, verified). 'Verify' alone -> Nones."""
    m = re.search(r"GS (\d+)/10", txt or "")
    if not m:
        return None, None, None
    rating = int(m.group(1))
    head = txt[:m.start()].strip()
    # Plain search, no word boundaries: `\b` after "Est\." can never match (period->space has
    # no boundary), which silently marked every "Est." school as verified.
    verified = not re.search(r"Est\.|VERIFY|Verify", head)
    name_m = re.search(r"(PS [\w /]+?)\s*$", head)
    name = name_m.group(1).strip() if name_m else (head.strip(" —-") or None)
    return name, rating, verified


def parse_bd_ba(txt):
    """'3/2 +⚠️$30K' -> ('3','2','+$30K bath addition flagged'); 'N/A/3'; bare 'N/A'."""
    txt = (txt or "").strip()
    note = None
    flag = re.search(r"\+\s*⚠?️?\s*\$(\d+)K", txt)
    if flag:
        note = f"+${flag.group(1)}K bath addition flagged on the sheet"
    m = re.match(r"(\d+|N/A)\s*/\s*(\d+|N/A)", txt)
    if not m:
        return None, None, note
    beds = None if m.group(1) == "N/A" else m.group(1)
    baths = None if m.group(2) == "N/A" else m.group(2)
    return beds, baths, note


STRICT_DOLLARS = re.compile(
    r"(?<![\d,])\$((?:\d{1,3}(?:,\d{3})+)|(?:\d{6,7}))(?![\d,])"
)


def parse_price_cell(txt):
    """First $ figure is the asking price; 'was $X' / 'from $X' is the original."""
    m = STRICT_DOLLARS.search(txt or "")
    price = int(m.group(1).replace(",", "")) if m else None
    orig = None
    om = re.search(
        r"(?:was|from)\s+" + STRICT_DOLLARS.pattern,
        txt or "",
    )
    if om:
        orig = int(om.group(1).replace(",", ""))
    note = re.sub(r"\s+", " ", (txt or "")[m.end():] if m else (txt or "")).strip(" ·")
    return price, orig, (note[:200] or None)


def parse_zest(txt):
    """'$718K' / '~$750K' / '$907K (Redfin)' / '$916K↑' / 'No Zest'.

    Returns (zestimate, redfin_estimate, chars_consumed) so the caller can advance past the
    cell — leaving it in place glued "$718K" onto "Est." ("KEst.") and broke the
    word-boundary match that detects an unverified school.
    """
    m = re.match(r"\s*(No Zest|~?\$(\d{3,4})K(\s*\(Redfin\))?↑?)", txt or "")
    if not m:
        return None, None, 0
    if m.group(1) == "No Zest":
        return None, None, m.end()
    val = int(m.group(2)) * 1000
    if m.group(3):
        return None, val, m.end()
    return val, None, m.end()


ROW_ANCHOR = re.compile(
    r"(?<!\d)(\d{1,3})\s*(NOW|SOON|WORTH|NEW|CALL|WATCH|SKIP)"
)
ADDR_IN_CHUNK = re.compile(
    r"^(.*?)(\[2-FAM\]\s*)?(\d+ [A-Z][A-Za-z'\. ]*?(?:\s+Apt\s+[A-Z])?),\s*(103\d\d)")
SQFT_DAYS = re.compile(r"([\d,]{3,5})\s*\$(\d{3})(?:(\d{1,3})d|\?d)")


def _source_pages(txt: str) -> list[str]:
    """Keep PDF pages separate; an unparsed row may not consume the next page."""
    return txt.split(PAGE_BREAK) if PAGE_BREAK in txt else [txt]


def split_rows(main_txt: str, expected_rows: Iterable[int] = EXPECTED_MAIN_ROWS):
    """Chunk the main table into rows using the sequential row numbers as truth.

    Gap-tolerant: a row whose number was mangled by table-wrap (e.g. a page boundary turning
    "80WORTH" into "0RTH") must not stall the sequence — requiring exactly-the-next number
    lost every row after the first casualty (59 of 114 on the first attempt). Accept later
    expected anchors in order and report every skipped number.
    """
    expected = tuple(expected_rows)
    expected_set = set(expected)
    picked, last = [], min(expected, default=1) - 1
    for page_number, page_txt in enumerate(_source_pages(main_txt), 1):
        cands = [(m.start(), int(m.group(1)), m.group(2), m.end())
                 for m in ROW_ANCHOR.finditer(page_txt)]
        page_picked = []
        for pos, num, see, end in cands:
            if num in expected_set and num > last:
                page_picked.append((pos, num, see, end))
                last = num
        for i, (pos, num, see, end) in enumerate(page_picked):
            stop = page_picked[i + 1][0] if i + 1 < len(page_picked) else len(page_txt)
            picked.append((page_number, pos, num, see, end, page_txt[end:stop]))
    rows = []
    for _, _, num, see, _, chunk in picked:
        chunk = re.sub(r"#SEETIER.*?MY NOTES", " ", chunk)   # strip repeated page headers
        rows.append((num, see, chunk))
    found = {row[0] for row in rows}
    return rows, [n for n in expected if n not in found]


def parse_main_row(num, see, chunk, url_by_slug):
    chunk = re.sub(r"\s+", " ", chunk).lstrip()
    tier = chunk[0] if chunk[:1] in TIERS else None
    rest = chunk[1:].lstrip() if tier else chunk
    am = ADDR_IN_CHUNK.match(rest)
    if not am:
        return None
    area, fam_tag, addr, zip_ = (am.group(1) or "").strip(), am.group(2), am.group(3).strip(), am.group(4)
    tail = rest[am.end():]

    # price cell runs until the TYPE keyword
    type_pos, ptype, type_token = len(tail), None, None
    for token, mapped in TYPE_MAP:
        p = tail.find(token)
        if p != -1 and p < type_pos:
            type_pos, ptype, type_token = p, mapped, token
    price, orig, price_note = parse_price_cell(tail[:type_pos])
    after_type = tail[type_pos + len(type_token):] if type_token else tail[type_pos:]

    sq = SQFT_DAYS.search(after_type)
    beds, baths, bd_note = parse_bd_ba(after_type[:sq.start()] if sq else after_type)
    sqft = int(sq.group(1).replace(",", "")) if sq else None
    days = int(sq.group(3)) if sq and sq.group(3) else None
    after_sq = after_type[sq.end():] if sq else after_type

    zest, redfin, consumed = parse_zest(after_sq)
    after_zest = after_sq[consumed:]
    gs = re.search(r"GS (\d+)/10", after_zest)
    school_name = school_rating = school_verified = None
    body = after_zest
    if gs:
        school_name, school_rating, school_verified = parse_school(after_zest[:gs.end()])
        body = after_zest[gs.end():]
    else:
        # N rows carry the school column's "Verify" sentinel, which otherwise glues onto the
        # body text ("Sheet: VerifyBuy $940K…") in notes the buyer reads.
        body = re.sub(r"^\s*Verify(?=[A-Z0-9$])", "", body)

    stars = max((len(s) for s in re.findall(r"★+", chunk)), default=0) or None
    buy = re.search(r"Buy \$(\d{3,4})K", body)
    target_buy = int(buy.group(1)) * 1000 if buy else None
    star_m = re.search(r"★+", body)
    after_stars = body[star_m.end():] if star_m else body
    bid, _ = _money_k(after_stars[:30])
    if bid:
        after_stars = after_stars[re.search(r"~?\$\d{3,4}(?:-\d{3,4})?K", after_stars).end():]

    # analysis / MY NOTES split: the column boundary shows up as sentence-end glued straight
    # to a capital ("…else.Verify PS 5 zone immediately.") — prose always has the space.
    blob = re.sub(r"\s+", " ", (body if not star_m else
                                body[:star_m.start()] + " " + after_stars)).strip()
    parts = re.split(r"(?<=[.!])(?=[A-Z0-9])", blob)
    my_notes = parts[-1].strip() if len(parts) > 1 else None
    analysis = " ".join(p.strip() for p in parts[:-1]) if len(parts) > 1 else blob
    analysis = (analysis or "").strip()[:500] or None
    my_notes = (my_notes or "")[:500] or None

    rec = {
        "property_id": f"SI-MT-{num:03d}",
        "street_address": addr, "zip": zip_,
        "neighborhood": None if area.lower().startswith("verify") or not area else area,
        "view_priority": SEE_MAP.get(see),
        "tier": None if tier == "N" else tier,
        "list_price": price, "original_list_price": orig, "price_note": price_note,
        "property_type": ptype, "is_multifamily": bool(fam_tag) or ptype in
                         ("2fam_detached", "2fam_semi") or None,
        "beds": beds, "baths": baths, "bd_ba_note": bd_note,
        "sqft": sqft, "days_on_market": days,
        "zestimate": zest, "redfin_estimate": redfin,
        # school_gs_rating is a TEXT column (the docx seeded ranges like "8-9/10"), so write
        # the string form — an int here reads back as "8" and re-diffs on every run.
        "school_name": school_name,
        "school_gs_rating": str(school_rating) if school_rating is not None else None,
        "school_verified": school_verified,
        "brrrr_rating": min(stars, 5) if stars else None,
        "target_buy_price": target_buy, "bid_estimate": bid,
        "listing_url": url_by_slug.get(_slugify(addr, zip_)),
        "status": "active",
    }
    if tier == "N":
        # The ranker will overwrite `analysis` on tier-None rows; keep the sheet's take.
        joined = " ".join(x for x in (analysis, my_notes) if x)
        rec["my_notes"] = f"Sheet: {joined}"[:500] if joined else None
        rec["analysis"] = None
    else:
        rec["analysis"] = analysis
        rec["my_notes"] = my_notes
    return rec


COMP_ROW = re.compile(
    r"(?<!\d)(\d{1,2})\s*([A-Z][A-Za-z ']+?)(\d+ [A-Z][A-Za-z'\. ]*?),\s*(103\d\d)\s*"
    r"(Closed|Pending|Attorney Review|Temp Off Market)(?![A-Za-z])")
COMP_MANUAL_NO_ZIP = re.compile(
    r"(?:(?<!\d)(\d{1,2})\s+)?"
    r"(\d+ [A-Z][A-Za-z' ]+? (?:St|Ave|Rd|Ln))\s+"
    r"(Pending|Attorney Review|Temp Off Market|Closed)\s*-{2,}")
COMP_LINE_START = re.compile(r"(?m)^\s*(\d{1,2})(?=\s+[A-Z0-9])")


def _comp_price(tail: str, status: str) -> tuple[int | None, int | None, tuple[str, ...]]:
    """Extract an exact comp price without guessing at corrupt or ranged values.

    A sold comp may carry ``(list $X)`` and any comp may carry an explicit
    ``from/was $X`` price cut. Those forms have one current/sold value and one
    clearly-labelled original value. An unlabelled second value is a range or
    extraction collision and is quarantined; it is never averaged or truncated.
    """
    del status  # Field placement is source-specific; token validation is the same.
    all_candidates = list(STRICT_DOLLARS.finditer(tail))
    list_match = re.search(
        r"\(\s*list\s+" + STRICT_DOLLARS.pattern + r"\s*\)", tail, re.IGNORECASE
    )
    cut_match = re.search(
        r"(?:↓\s*)?(?:from|was)\s+" + STRICT_DOLLARS.pattern,
        tail,
        re.IGNORECASE,
    )
    labelled = [match for match in (list_match, cut_match) if match is not None]
    # Translate wrapper-local spans back into the original text instead of relying on nested
    # capture-group numbers.
    labelled_value_starts = set()
    for wrapper in labelled:
        nested = STRICT_DOLLARS.search(wrapper.group(0))
        if nested:
            labelled_value_starts.add(wrapper.start() + nested.start())
    primary = [m for m in all_candidates if m.start() not in labelled_value_starts]

    errors = []
    if len(primary) != 1:
        if len(all_candidates) >= 2 and re.search(
            r"(?:-|–|—|\bto\b)",
            tail[all_candidates[0].end():all_candidates[1].start()],
            re.IGNORECASE,
        ):
            errors.append("price range is ambiguous; repair required")
        else:
            errors.append(
                "price missing or malformed after PDF extraction"
                if not primary else
                f"multiple unlabelled price candidates after PDF extraction ({len(primary)})"
            )
        return None, None, tuple(errors)

    if len(labelled) > 1 or len(all_candidates) != 1 + len(labelled):
        return None, None, ("multiple or conflicting labelled prices",)

    price = int(primary[0].group(1).replace(",", ""))
    original = None
    if labelled:
        original_match = STRICT_DOLLARS.search(labelled[0].group(0))
        original = int(original_match.group(1).replace(",", ""))
    return price, original, ()


def _parse_comps_detailed(comps_txt: str):
    out, failures, source_rows, manual_rows = [], [], [], []
    for page_txt in _source_pages(comps_txt):
        matches = list(COMP_ROW.finditer(page_txt))
        manual_matches = list(COMP_MANUAL_NO_ZIP.finditer(page_txt))
        boundaries = sorted(
            {m.start() for m in matches} |
            {m.start() for m in manual_matches} |
            {m.start() for m in COMP_LINE_START.finditer(page_txt)}
        )
        for m in matches:
            stop = next((pos for pos in boundaries if pos > m.start()), len(page_txt))
            tail = page_txt[m.end():stop]
            status = COMP_STATUS[m.group(5)]
            price, original, price_errors = _comp_price(tail, status)
            price_matches = list(STRICT_DOLLARS.finditer(tail))
            price_end = max((match.end() for match in price_matches), default=0)
            bed_tail = tail[price_end:]
            bed_tail = re.sub(r"^[^0-9N]*", "", bed_tail)
            facts_match = re.match(
                r"^[^0-9N]*(\d+|N/A)\s*/\s*(\d+(?:\.\d+)?|N/A)\s*"
                r"(\d{1,2},\d{3})(?![\d,])",
                bed_tail,
            )
            if facts_match is None:
                facts_match = re.match(
                    r"^[^0-9N]*(\d+|N/A)\s*/\s*(\d+(?:\.\d+)?|N/A)\s+"
                    r"(\d{3,5})(?!\d)",
                    bed_tail,
                )
            if facts_match:
                beds = None if facts_match.group(1) == "N/A" else facts_match.group(1)
                baths = None if facts_match.group(2) == "N/A" else facts_match.group(2)
                sqft = int(facts_match.group(3).replace(",", ""))
            else:
                beds, baths, sqft = None, None, None
            rec = {
                "property_id": f"SI-MT-C{m.group(1).zfill(2)}",
                "street_address": m.group(3).strip(), "zip": m.group(4),
                "neighborhood": m.group(2).strip(), "status": status,
                "beds": beds, "baths": baths, "sqft": sqft,
                "analysis": re.sub(r"\s+", " ", tail).strip()[:300] or None,
            }
            if status == "sold":
                rec["last_sold_price"] = price
                rec["original_list_price"] = original
            else:
                rec["list_price"] = price
                rec["original_list_price"] = original
            out.append(rec)
            source_rows.append((int(m.group(1)), rec, price_errors))

        for m in manual_matches:
            frag = m.group(0).strip()
            source_num = int(m.group(1)) if m.group(1) else None
            address = m.group(2).strip()
            failures.append(frag)
            manual_rows.append((source_num, address, frag))

        covered = [m.span() for m in matches + manual_matches]
        known_numbers = {
            int(m.group(1)) for m in matches if m.group(1)
        } | {
            int(m.group(1)) for m in manual_matches if m.group(1)
        }
        for m in COMP_LINE_START.finditer(page_txt):
            if any(start <= m.start() < stop for start, stop in covered):
                continue
            source_num = int(m.group(1))
            if source_num in known_numbers:
                continue
            stop = page_txt.find("\n", m.start())
            frag = page_txt[m.start():stop if stop != -1 else len(page_txt)].strip()
            failures.append(frag)
            manual_rows.append((source_num, None, frag))
    return out, failures, source_rows, manual_rows


def parse_comps(comps_txt: str):
    """Compatibility wrapper preserving the original records/failures return value."""
    out, failures, _, _ = _parse_comps_detailed(comps_txt)
    return out, failures


def _record_validation_errors(
    rec: dict,
    *,
    source_section: str,
    source_row_number: int | None,
    required_price_field: str = "list_price",
    parse_errors: Iterable[str] = (),
) -> tuple[str, ...]:
    """Apply the tracking-sheet contract before a record can reach merge code."""
    errors = list(parse_errors)

    if source_section not in {"main", "comp"}:
        errors.append(f"unknown source section {source_section!r}")
    if not isinstance(source_row_number, int) or source_row_number < 1:
        errors.append("source row identity missing or ambiguous")
    else:
        expected_id = (
            f"SI-MT-{source_row_number:03d}"
            if source_section == "main"
            else f"SI-MT-C{source_row_number:02d}"
        )
        if rec.get("property_id") != expected_id:
            errors.append(
                f"property_id={rec.get('property_id')!r} does not match source row "
                f"{source_section}:{source_row_number}"
            )

    address = rec.get("street_address")
    if not isinstance(address, str) or not re.fullmatch(
        r"\d+ [A-Za-z][A-Za-z'\. ]*(?: Apt [A-Z])?", address
    ):
        errors.append("address missing or ambiguous")
    zip_ = rec.get("zip")
    if not isinstance(zip_, str) or not re.fullmatch(r"103\d\d", zip_):
        errors.append("ZIP missing or ambiguous")

    allowed_statuses = {"active"} if source_section == "main" else set(COMP_STATUS.values())
    if rec.get("status") not in allowed_statuses:
        errors.append(
            f"status={rec.get('status')!r} invalid for {source_section} source row"
        )

    if rec.get(required_price_field) is None:
        errors.append(f"{required_price_field} missing or ambiguous")
    for field in ("list_price", "original_list_price", "last_sold_price"):
        value = rec.get(field)
        if value is None:
            continue
        if not isinstance(value, int) or not MIN_SHEET_PRICE <= value <= MAX_SHEET_PRICE:
            errors.append(
                f"{field}={value!r} outside "
                f"${MIN_SHEET_PRICE:,}-${MAX_SHEET_PRICE:,} source range"
            )

    for field_name in ("beds", "baths"):
        value = rec.get(field_name)
        if value is None:
            errors.append(f"{field_name} missing or unknown")
            continue
        normalized = str(value).strip()
        if not re.fullmatch(r"\d+(?:\.\d+)?(?:-\d+(?:\.\d+)?)?", normalized):
            errors.append(f"{field_name}={value!r} malformed")
            continue
        endpoints = [float(part) for part in normalized.split("-")]
        if any(number <= 0 or number > 20 for number in endpoints):
            errors.append(f"{field_name}={value!r} outside source range 0-20")

    sqft = rec.get("sqft")
    if sqft is None:
        errors.append("sqft missing or unknown")
    elif not isinstance(sqft, int) or isinstance(sqft, bool) \
            or not MIN_SHEET_SQFT <= sqft <= MAX_SHEET_SQFT:
        errors.append(
            f"sqft={sqft!r} outside {MIN_SHEET_SQFT:,}-{MAX_SHEET_SQFT:,} source range"
        )

    property_type = rec.get("property_type")
    if source_section == "main" and property_type not in VALID_PROPERTY_TYPES:
        errors.append(f"property_type={property_type!r} missing or unknown")
    elif property_type is not None and property_type not in VALID_PROPERTY_TYPES:
        errors.append(f"property_type={property_type!r} invalid")

    if not match_key(rec):
        errors.append("no match key")
    return tuple(dict.fromkeys(errors))


def _source_values(rec: dict) -> dict[str, object]:
    """Keep just the validated source fields in repair/report output."""
    return {field: rec.get(field) for field in SOURCE_VALIDATION_FIELDS}


def _source_excerpt(text: str | None, limit: int = 500) -> str | None:
    normalized = re.sub(r"\s+", " ", text or "").strip()
    return normalized[:limit] or None


def _failed_main_address(chunk: str) -> str | None:
    match = re.search(r"(\d+ [A-Z][A-Za-z'\. ]*?(?:\s+Apt\s+[A-Z])?),\s*103\d\d", chunk)
    return match.group(1).strip() if match else None


def build_import_report(
    main_txt: str,
    comps_txt: str,
    url_by_slug: dict[str, str] | None = None,
    *,
    expected_main_rows: Iterable[int] = EXPECTED_MAIN_ROWS,
    expected_comp_rows: Iterable[int] = EXPECTED_COMP_ROWS,
) -> ImportResult:
    """Parse source text and return records plus a pure, row-level integrity manifest."""
    urls = url_by_slug or {}
    expected = tuple(expected_main_rows)
    expected_set = set(expected)
    rows, _ = split_rows(main_txt, expected)
    rows_by_number = {num: (see, chunk) for num, see, chunk in rows}
    manifest, main_records, quarantined_records, report_errors = [], [], [], []
    record_by_source: dict[tuple[str, int], dict] = {}

    all_main_numbers = [
        int(match.group(1))
        for page_txt in _source_pages(main_txt)
        for match in ROW_ANCHOR.finditer(page_txt)
    ]
    duplicate_numbers = sorted(
        num for num in set(all_main_numbers) if all_main_numbers.count(num) > 1
    )
    for num in duplicate_numbers:
        report_errors.append(f"main:{num}: duplicate source row")

    for num in expected:
        source = rows_by_number.get(num)
        if source is None:
            manifest.append(ImportManifestEntry(
                source_row_number=num,
                address=None,
                source_section="main",
                parse_state="manual_required",
                match_key=None,
                validation_errors=("anchor never found (table-wrap mangling)",),
                source_values={},
                source_excerpt=None,
            ))
            continue

        see, chunk = source
        rec = parse_main_row(num, see, chunk, urls)
        if rec is None:
            manifest.append(ImportManifestEntry(
                source_row_number=num,
                address=_failed_main_address(chunk),
                source_section="main",
                parse_state="manual_required",
                match_key=None,
                validation_errors=("row anchor found but address could not be parsed",),
                source_values={},
                source_excerpt=_source_excerpt(chunk),
            ))
            continue

        errors = _record_validation_errors(
            rec,
            source_section="main",
            source_row_number=num,
            required_price_field="list_price",
        )
        key = match_key(rec)
        record_by_source[("main", num)] = rec
        if errors:
            quarantined_records.append(rec)
        else:
            main_records.append(rec)
        manifest.append(ImportManifestEntry(
            source_row_number=num,
            address=rec["street_address"],
            source_section="main",
            parse_state="invalid" if errors else "parsed",
            match_key=key,
            validation_errors=errors,
            source_values=_source_values(rec),
            source_excerpt=_source_excerpt(chunk),
        ))

    unexpected = sorted(set(all_main_numbers) - expected_set)
    for num in unexpected:
        report_errors.append(f"main:{num}: unexpected source row")

    _, _, comp_source_rows, manual_comp_rows = _parse_comps_detailed(comps_txt)
    valid_comp_records = []
    expected_comps = tuple(expected_comp_rows)
    parsed_comps_by_number: dict[int, list[tuple[dict, tuple[str, ...]]]] = {}
    for source_num, rec, parse_errors in comp_source_rows:
        parsed_comps_by_number.setdefault(source_num, []).append((rec, parse_errors))
    manual_comps_by_number: dict[int, list[tuple[str | None, str]]] = {}
    unknown_manual_comps = []
    for source_num, address, frag in manual_comp_rows:
        if source_num is None:
            unknown_manual_comps.append((address, frag))
        else:
            manual_comps_by_number.setdefault(source_num, []).append((address, frag))

    seen_comp_numbers = [row[0] for row in comp_source_rows] + [
        row[0] for row in manual_comp_rows if row[0] is not None
    ]
    for num in sorted({n for n in seen_comp_numbers if seen_comp_numbers.count(n) > 1}):
        report_errors.append(f"comp:{num}: duplicate source row")

    for num in expected_comps:
        parsed_sources = parsed_comps_by_number.get(num, [])
        manual_sources = manual_comps_by_number.get(num, [])
        if len(parsed_sources) == 1 and not manual_sources:
            rec, parse_errors = parsed_sources[0]
            record_by_source[("comp", num)] = rec
            required_price = "last_sold_price" if rec.get("status") == "sold" else "list_price"
            errors = _record_validation_errors(
                rec,
                source_section="comp",
                source_row_number=num,
                required_price_field=required_price,
                parse_errors=parse_errors,
            )
            if errors:
                quarantined_records.append(rec)
            else:
                valid_comp_records.append(rec)
            manifest.append(ImportManifestEntry(
                source_row_number=num,
                address=rec["street_address"],
                source_section="comp",
                parse_state="invalid" if errors else "parsed",
                match_key=match_key(rec),
                validation_errors=errors,
                source_values=_source_values(rec),
                source_excerpt=_source_excerpt(rec.get("analysis")),
            ))
        elif len(manual_sources) == 1 and not parsed_sources:
            address, _ = manual_sources[0]
            manifest.append(ImportManifestEntry(
                source_row_number=num,
                address=address,
                source_section="comp",
                parse_state="manual_required",
                match_key=None,
                validation_errors=(
                    "one or more ZIP/address/status fields could not be parsed unambiguously; manual entry required",
                ),
                source_values={},
                source_excerpt=_source_excerpt(manual_sources[0][1]),
            ))
        elif not parsed_sources and not manual_sources:
            manifest.append(ImportManifestEntry(
                source_row_number=num,
                address=None,
                source_section="comp",
                parse_state="manual_required",
                match_key=None,
                validation_errors=("source row anchor not found",),
                source_values={},
                source_excerpt=None,
            ))
        else:
            address = (
                parsed_sources[0][0].get("street_address") if parsed_sources
                else manual_sources[0][0]
            )
            manifest.append(ImportManifestEntry(
                source_row_number=num,
                address=address,
                source_section="comp",
                parse_state="invalid",
                match_key=None,
                validation_errors=("duplicate or conflicting source row",),
                source_values=(
                    _source_values(parsed_sources[0][0]) if parsed_sources else {}
                ),
                source_excerpt=_source_excerpt(
                    parsed_sources[0][0].get("analysis")
                    if parsed_sources else manual_sources[0][1]
                ),
            ))
            quarantined_records.extend(rec for rec, _ in parsed_sources)

    expected_comp_set = set(expected_comps)
    for num in sorted(set(seen_comp_numbers) - expected_comp_set):
        report_errors.append(f"comp:{num}: unexpected source row")
    for address, _ in unknown_manual_comps:
        manifest.append(ImportManifestEntry(
            source_row_number=None,
            address=address,
            source_section="comp",
            parse_state="manual_required",
            match_key=None,
            validation_errors=("source row number and ZIP are ambiguous; manual entry required",),
            source_values={},
            source_excerpt=_source_excerpt(_),
        ))
        report_errors.append(
            f"comp:unknown: manual row {address!r} has no source row number"
        )

    sources_by_match_key: dict[str, list[tuple[int, ImportManifestEntry]]] = {}
    for index, entry in enumerate(manifest):
        if entry.match_key:
            sources_by_match_key.setdefault(entry.match_key, []).append((index, entry))
    duplicate_record_ids = set()
    for key, indexed_entries in sources_by_match_key.items():
        if len(indexed_entries) < 2:
            continue
        source_ids = ", ".join(entry.source_id for _, entry in indexed_entries)
        report_errors.append(f"match_key:{key}: duplicate source identity ({source_ids})")
        for index, entry in indexed_entries:
            manifest[index] = replace(
                entry,
                parse_state="invalid",
                validation_errors=entry.validation_errors + (
                    f"match key shared by multiple source rows ({source_ids})",
                ),
            )
            if entry.source_row_number is not None:
                rec = record_by_source.get((entry.source_section, entry.source_row_number))
                if rec is not None:
                    duplicate_record_ids.add(id(rec))
                    quarantined_records.append(rec)
    if duplicate_record_ids:
        main_records = [rec for rec in main_records if id(rec) not in duplicate_record_ids]
        valid_comp_records = [
            rec for rec in valid_comp_records if id(rec) not in duplicate_record_ids
        ]

    # A record may already have been quarantined for field validation before an identity
    # conflict is discovered. Keep the result deterministic and list each record once.
    unique_quarantine, seen_record_ids = [], set()
    for rec in quarantined_records:
        if id(rec) not in seen_record_ids:
            unique_quarantine.append(rec)
            seen_record_ids.add(id(rec))

    report = ImportReport(
        manifest=tuple(manifest),
        expected_main_rows=expected,
        expected_comp_rows=expected_comps,
        report_errors=tuple(report_errors),
    )
    return ImportResult(
        tuple(main_records),
        tuple(valid_comp_records),
        report,
        tuple(unique_quarantine),
    )


def _load_reviewed_overrides(path: str | None) -> set[tuple[str, int | None]]:
    """Load an explicit, human-reviewed JSON exception file."""
    if not path:
        return set()
    with open(path, encoding="utf-8") as fh:
        payload = json.load(fh)
    if payload.get("reviewed") is not True or not isinstance(payload.get("rows"), list):
        raise ValueError("override file must contain reviewed=true and a rows list")

    reviewed = set()
    for item in payload["rows"]:
        section = item.get("source_section")
        number = item.get("source_row_number")
        if section not in ("main", "comp") or not isinstance(number, int):
            raise ValueError("each override row needs source_section main/comp and integer source_row_number")
        reviewed.add((section, number))
    return reviewed


def _format_import_report(report: ImportReport) -> str:
    counts = {
        state: sum(entry.parse_state == state for entry in report.manifest)
        for state in sorted(PARSE_STATES)
    }
    lines = [
        "import integrity: "
        + "  ".join(f"{state}={counts[state]}" for state in sorted(counts))
    ]
    for entry in report.manifest:
        if entry.parse_state == "parsed" and not entry.validation_errors:
            continue
        detail = "; ".join(entry.validation_errors) or entry.parse_state
        address = f" {entry.address}" if entry.address else ""
        lines.append(f"   {entry.source_id}{address}: {entry.parse_state} — {detail}")
    lines.extend(f"   report: {error}" for error in report.report_errors)
    return "\n".join(lines)


def _write_repair_manifest(report: ImportReport, path: str) -> None:
    """Write a deterministic JSON review artifact; never writes to the listing store."""
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(report.repair_manifest(), fh, indent=2, sort_keys=True)
        fh.write("\n")


def _fitz_page_text(page) -> str:
    """Rebuild visual lines from word coordinates, preserving PDF cell boundaries."""
    words = page.get_text("words", sort=True)
    if not words:
        return ""
    visual_lines: list[list[tuple]] = []
    line_y: list[float] = []
    for word in sorted(words, key=lambda item: (item[1], item[0])):
        y = (word[1] + word[3]) / 2
        target = next(
            (index for index in range(len(line_y) - 1, -1, -1)
             if abs(line_y[index] - y) <= 2.5),
            None,
        )
        if target is None:
            visual_lines.append([word])
            line_y.append(y)
        else:
            visual_lines[target].append(word)
            count = len(visual_lines[target])
            line_y[target] = ((line_y[target] * (count - 1)) + y) / count
    ordered = sorted(zip(line_y, visual_lines), key=lambda item: item[0])
    return "\n".join(
        " ".join(str(word[4]) for word in sorted(line, key=lambda item: item[0]))
        for _, line in ordered
    )


def _partition_pdf_pages(page_texts: Iterable[str]) -> tuple[str, str]:
    main_pages, comp_pages, in_comps = [], [], False
    for page_txt in page_texts:
        marker = re.search(r"\bCLOSED\s+COMPS\b", page_txt)
        if marker and not in_comps:
            main_pages.append(page_txt[:marker.start()])
            comp_pages.append(page_txt[marker.end():])
            in_comps = True
        elif in_comps:
            comp_pages.append(page_txt)
        else:
            main_pages.append(page_txt)
    return PAGE_BREAK.join(main_pages), PAGE_BREAK.join(comp_pages)


def load_pdf(path):
    """Use PyMuPDF geometry for text and pypdf only for hyperlink annotations."""
    import fitz
    import pypdf

    with fitz.open(path) as doc:
        main, comps = _partition_pdf_pages(_fitz_page_text(page) for page in doc)

    r = pypdf.PdfReader(path)
    urls = {}
    for page in r.pages:
        for a in (page.get("/Annots") or []):
            uri = (a.get_object().get("/A") or {}).get("/URI")
            if uri:
                m = re.search(r"/home(?:s|details)/([^/]+)/", str(uri))
                if m:
                    urls[re.sub(r"[^a-z0-9]", "", m.group(1).lower())] = str(uri)
    return main, comps, urls


def merge_existing(store, key, existing, rec, apply, diffs):
    """Fill machine NULLs; overwrite curated fields, recording every change."""
    fill = {f: rec[f] for f in MACHINE_FILL_ONLY
            if rec.get(f) is not None and existing.get(f) in (None, "")}
    fill.pop("status", None)                      # never touch a live row's status
    cur = {}
    for f in CURATED_FIELDS:
        v = rec.get(f)
        if v is None:
            continue                              # empty sheet cell keeps the pool's value
        old = existing.get(f)
        if old == v:
            continue
        # Never replace a human note with a shorter rebuild of itself: the sheet truncated
        # "I love the layout. If only this was in range…" to "I love the layout." — an
        # overwrite that loses words while adding none is not an update.
        if f in ("my_notes", "analysis") and isinstance(old, str) and isinstance(v, str) \
                and v.rstrip(".") and v.rstrip(".") in old:
            continue
        cur[f] = v
        diffs.append((rec["street_address"], f, old, v))
    if apply:
        if fill:
            store.update_fields(key, fill)
        if cur:
            store.update_fields(key, cur, allow_protected=True)
    return bool(fill or cur)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("pdf")
    ap.add_argument("--db", default=os.path.join(HERE, "pool.db"))
    ap.add_argument("--apply", action="store_true", help="write (default is dry-run)")
    ap.add_argument("--strict", action="store_true",
                    help="return nonzero for unreviewed manual/invalid source rows")
    ap.add_argument("--manual-overrides", metavar="FILE",
                    help="reviewed JSON exceptions for strict validation")
    ap.add_argument("--repair-manifest", metavar="FILE",
                    help="write quarantined/manual rows as reviewable JSON")
    args = ap.parse_args(argv)

    main_txt, comps_txt, urls = load_pdf(args.pdf)
    import_result = build_import_report(main_txt, comps_txt, urls)
    parsed = list(import_result.main_records)
    comps = list(import_result.comp_records)
    report = import_result.report
    try:
        reviewed_overrides = _load_reviewed_overrides(args.manual_overrides)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"invalid manual override file: {exc}", file=sys.stderr)
        return 2
    validation = report.validate(
        strict=args.strict or args.apply,
        reviewed_overrides=reviewed_overrides,
    )

    print(_format_import_report(report))
    if args.repair_manifest:
        try:
            _write_repair_manifest(report, args.repair_manifest)
        except OSError as exc:
            print(f"could not write repair manifest: {exc}", file=sys.stderr)
            return 2
        print(f"repair manifest: {args.repair_manifest}")
    if args.apply and not validation.is_valid:
        print("refusing to write: strict import integrity validation failed", file=sys.stderr)
        for error in validation.errors:
            print(f"   {error}", file=sys.stderr)
        return validation.exit_code

    if args.apply:
        snap = backup.snapshot(args.db)
        if not snap or str(snap).startswith("backup failed"):
            print(f"refusing to write: {snap}", file=sys.stderr)
            return 2
        ok, detail = backup.verify(snap)
        if not ok:
            print(f"refusing to write: backup unverified ({detail})", file=sys.stderr)
            return 2
        print(f"backup verified: {snap} ({detail})")

    store = Store(args.db, finance_cfg=(preferences.load(
        os.path.join(HERE, "preferences.yaml")).get("finance")
        if os.path.exists(os.path.join(HERE, "preferences.yaml")) else None))
    before = completeness.audit(store, Ledger(store))
    source_by_key = {
        entry.match_key: entry.source_id
        for entry in report.manifest
        if entry.parse_state == "parsed" and entry.match_key
    }
    source_by_address = {
        entry.address: entry.source_id
        for entry in report.manifest
        if entry.parse_state == "parsed" and entry.address
    }

    new_rows, overlaps, diffs = [], [], []
    for rec in parsed + comps:
        key = match_key(rec)
        if not key:
            continue
        existing = store.get(key)
        if existing is None:
            new_rows.append(rec)
            if args.apply:
                store.upsert(rec, source=f"seed:sheet:{source_by_key[key]}")
        else:
            if merge_existing(store, key, existing, rec, args.apply, diffs):
                overlaps.append(rec["street_address"])

    mode = "APPLY" if args.apply else "DRY RUN"
    print(f"=== {mode}: {os.path.basename(args.pdf)} -> {os.path.basename(args.db)} ===")
    print(f"main rows parsed : {len(parsed)}  comps: {len(comps)}")
    print(f"quarantined      : {len(import_result.quarantined_records)}")
    print(f"new rows         : {len(new_rows)}")
    print(f"overlaps changed : {len(overlaps)}")
    print(f"curated diffs    : {len(diffs)}")
    for addr, f, old, new in diffs:
        source_id = source_by_address.get(addr, "source:unknown")
        print(
            f"   [{source_id}] {addr[:26]:28s} {f:18s} "
            f"{str(old)[:40]!r} -> {str(new)[:40]!r}"
        )
    exceptions = [
        entry for entry in report.manifest
        if entry.parse_state != "parsed" or entry.validation_errors
    ]
    if exceptions or report.report_errors:
        print("\nNOT IMPORTED — needs hand entry:")
        for entry in exceptions:
            detail = "; ".join(entry.validation_errors) or entry.parse_state
            print(f"   {entry.source_id}: {entry.address or '(address unavailable)'} — {detail}")
        for error in report.report_errors:
            print(f"   {error}")
    if args.apply:
        after = completeness.audit(store, Ledger(store))
        print(f"\npool: {before['pool_size']} -> {after['pool_size']} rows")
        print(completeness.format_report(after))
    store.close()
    return validation.exit_code


if __name__ == "__main__":
    sys.exit(main())
