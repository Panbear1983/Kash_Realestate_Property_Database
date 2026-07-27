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
import os
import re
import sys
from datetime import date

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


def parse_price_cell(txt):
    """First $ figure is the asking price; 'was $X' / 'from $X' is the original."""
    m = re.search(r"\$([\d,]+)", txt or "")
    price = int(m.group(1).replace(",", "")) if m else None
    orig = None
    om = re.search(r"(?:was|from)\s+\$([\d,]+)", txt or "")
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


ROW_ANCHOR = re.compile(r"(\d{1,3})(NOW|SOON|WORTH|NEW|CALL|WATCH|SKIP)")
ADDR_IN_CHUNK = re.compile(
    r"^(.*?)(\[2-FAM\]\s*)?(\d+ [A-Z][A-Za-z'\. ]*?(?:\s+Apt\s+[A-Z])?),\s*(103\d\d)")
SQFT_DAYS = re.compile(r"([\d,]{3,5})\s*\$(\d{3})(?:(\d{1,3})d|\?d)")


def split_rows(main_txt: str):
    """Chunk the main table into rows using the sequential row numbers as truth.

    Gap-tolerant: a row whose number was mangled by table-wrap (e.g. a page boundary turning
    "80WORTH" into "0RTH") must not stall the sequence — requiring exactly-the-next number
    lost every row after the first casualty (59 of 114 on the first attempt). Accept the next
    candidate that continues the sequence within a small gap, and report the skipped numbers.
    """
    cands = [(m.start(), int(m.group(1)), m.group(2), m.end())
             for m in ROW_ANCHOR.finditer(main_txt)]
    picked, last = [], 0
    for pos, num, see, end in cands:
        if last < num <= min(last + 6, 114):
            picked.append((pos, num, see, end))
            last = num
    rows = []
    for i, (pos, num, see, end) in enumerate(picked):
        stop = picked[i + 1][0] if i + 1 < len(picked) else len(main_txt)
        chunk = main_txt[end:stop]
        chunk = re.sub(r"#SEETIER.*?MY NOTES", " ", chunk)   # strip repeated page headers
        rows.append((num, see, chunk))
    return rows, [n for n in range(1, 115) if n not in {p[1] for p in picked}]


def parse_main_row(num, see, chunk, url_by_slug):
    tier = chunk[0] if chunk[:1] in TIERS else None
    rest = chunk[1:] if tier else chunk
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

    beds, baths, bd_note = parse_bd_ba(after_type)
    sq = SQFT_DAYS.search(after_type)
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
    r"(\d{1,2})([A-Z][A-Za-z ']+?)(\d+ [A-Z][A-Za-z'\. ]*?),\s*(103\d\d)\s*"
    r"(Closed|Pending|Attorney Review|Temp Off Market)")


def parse_comps(comps_txt: str):
    out, failures = [], []
    matches = list(COMP_ROW.finditer(comps_txt))
    for i, m in enumerate(matches):
        stop = matches[i + 1].start() if i + 1 < len(matches) else len(comps_txt)
        tail = comps_txt[m.end():stop]
        status = COMP_STATUS[m.group(5)]
        pm = re.search(r"\$([\d,]+)", tail)
        price = int(pm.group(1).replace(",", "")) if pm else None
        lm = re.search(r"\(list \$([\d,]+)\)", tail)
        beds, baths, _ = parse_bd_ba(re.sub(r"^[^0-9N]*", "", tail[pm.end():]) if pm else tail)
        sqm = re.search(r"(\d,\d{3}|\d{3,4})(?=[^\d]|$)", tail[pm.end():] if pm else tail)
        rec = {
            "property_id": f"SI-MT-C{m.group(1).zfill(2)}",
            "street_address": m.group(3).strip(), "zip": m.group(4),
            "neighborhood": m.group(2).strip(), "status": status,
            "beds": beds, "baths": baths,
            "analysis": re.sub(r"\s+", " ", tail).strip()[:300] or None,
        }
        if status == "sold":
            rec["last_sold_price"] = price
            rec["original_list_price"] = int(lm.group(1).replace(",", "")) if lm else None
        else:
            rec["list_price"] = price
        out.append(rec)
    # comps with no parsable ZIP (e.g. "110 Bishop St Pending----")
    for frag in re.findall(r"\d+ [A-Z][A-Za-z' ]+ (?:St|Ave|Rd|Ln)\s+Pending-{2,}", comps_txt):
        failures.append(frag.strip())
    return out, failures


def load_pdf(path):
    import pypdf
    r = pypdf.PdfReader(path)
    full = "\n".join((p.extract_text() or "") for p in r.pages)
    urls = {}
    for page in r.pages:
        for a in (page.get("/Annots") or []):
            uri = (a.get_object().get("/A") or {}).get("/URI")
            if uri:
                m = re.search(r"/home(?:s|details)/([^/]+)/", str(uri))
                if m:
                    urls[re.sub(r"[^a-z0-9]", "", m.group(1).lower())] = str(uri)
    main, _, comps = full.partition("CLOSED COMPS")
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


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("pdf")
    ap.add_argument("--db", default=os.path.join(HERE, "pool.db"))
    ap.add_argument("--apply", action="store_true", help="write (default is dry-run)")
    args = ap.parse_args()

    main_txt, comps_txt, urls = load_pdf(args.pdf)
    rows, missing_nums = split_rows(main_txt)
    parsed, failed = [], []
    for num, see, chunk in rows:
        rec = parse_main_row(num, see, chunk, urls)
        (parsed if rec else failed).append(rec or (num, chunk[:80]))
    comps, comp_failures = parse_comps(comps_txt)

    store = Store(args.db, finance_cfg=(preferences.load(
        os.path.join(HERE, "preferences.yaml")).get("finance")
        if os.path.exists(os.path.join(HERE, "preferences.yaml")) else None))
    before = completeness.audit(store, Ledger(store))

    new_rows, overlaps, diffs = [], [], []
    for rec in parsed + comps:
        key = match_key(rec)
        if not key:
            failed.append((rec.get("property_id"), "no match_key"))
            continue
        existing = store.get(key)
        if existing is None:
            new_rows.append(rec)
            if args.apply:
                store.upsert(rec, source="seed:sheet")
        else:
            if merge_existing(store, key, existing, rec, args.apply, diffs):
                overlaps.append(rec["street_address"])

    mode = "APPLY" if args.apply else "DRY RUN"
    print(f"=== {mode}: {os.path.basename(args.pdf)} -> {os.path.basename(args.db)} ===")
    print(f"main rows parsed : {len(parsed)}  comps: {len(comps)}")
    print(f"new rows         : {len(new_rows)}")
    print(f"overlaps changed : {len(overlaps)}")
    print(f"curated diffs    : {len(diffs)}")
    for addr, f, old, new in diffs:
        print(f"   {addr[:26]:28s} {f:18s} {str(old)[:40]!r} -> {str(new)[:40]!r}")
    if missing_nums or failed or comp_failures:
        print("\nNOT IMPORTED — needs hand entry:")
        for n in missing_nums:
            print(f"   main row #{n}: anchor never found (table-wrap mangling)")
        for item in failed:
            print(f"   {item}")
        for frag in comp_failures:
            print(f"   comp: {frag!r} (no ZIP in the sheet)")
    if args.apply:
        after = completeness.audit(store, Ledger(store))
        print(f"\npool: {before['pool_size']} -> {after['pool_size']} rows")
        print(completeness.format_report(after))
    store.close()


if __name__ == "__main__":
    if "--apply" in sys.argv:
        db = sys.argv[sys.argv.index("--db") + 1] if "--db" in sys.argv \
            else os.path.join(HERE, "pool.db")
        snap = backup.snapshot(db)
        if not snap or str(snap).startswith("backup failed"):
            sys.exit(f"refusing to write: {snap}")
        ok, detail = backup.verify(snap)
        if not ok:
            sys.exit(f"refusing to write: backup unverified ({detail})")
        print(f"backup verified: {snap} ({detail})")
    main()
