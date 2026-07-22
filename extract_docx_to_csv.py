#!/usr/bin/env python3
"""Extract the Staten Island active-properties Word table into a data-friendly CSV.

Re-run each time a new monthly docx arrives to regenerate the machine-readable
data layer. The Word doc stays the human-readable ranked view; this CSV is the
queryable/diffable layer underneath it.

Usage:
    python3 extract_docx_to_csv.py [input.docx] [output.csv]

Several Word cells pack a second line under the primary value (e.g. SQFT holds
"2,056" then "$367" price/sqft; SCHOOL holds the zone then "GS 7-10/10"). These
are split into their own columns below.

Heuristic columns (target_buy_price, arv_estimate, appreciation_pct,
brrrr_rating) are regex-parsed from the free-text BRRRR cell and should be
spot-checked; the full original text is preserved verbatim in investment_thesis.
"""
import csv
import os
import re
import sys
import zipfile
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_IN = os.path.join(HERE, "SI_July2026_Active_Properties.docx")
DEFAULT_OUT = "archive/SI_July2026_Active_Properties.csv"

CELL_RE = re.compile(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>", re.S)
ROW_RE = re.compile(r"<w:tr[ >].*?</w:tr>", re.S)
TC_RE = re.compile(r"<w:tc>.*?</w:tc>", re.S)
TBL_RE = re.compile(r"<w:tbl>.*?</w:tbl>", re.S)

PROPERTY_TYPE_MAP = {
    "SF Attached": "sf_attached",
    "SF Semi-Att": "sf_semi",
    "SF Detached": "sf_detached",
    "2-Fam Detached": "2fam_detached",
    "2-Fam Semi-Att": "2fam_semi",
    "2-Fam Colonial": "2fam_colonial",
}

FIELDS = [
    "property_id", "rank", "status", "view_priority", "priority_note", "tier",
    "neighborhood", "street_address", "zip", "listing_url", "property_type",
    "is_multifamily", "list_price", "price_note", "beds", "baths", "bd_ba_note",
    "sqft", "price_per_sqft", "days_on_market", "zestimate", "zestimate_source",
    "school_name", "school_gs_rating", "school_verified", "target_buy_price",
    "arv_estimate", "appreciation_pct", "brrrr_rating", "bid_estimate",
    "bid_note", "investment_thesis", "analysis", "my_notes", "last_updated",
]


def unescape(s):
    return (s.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
             .replace("&quot;", '"').replace("&apos;", "'"))


def cell_text(tc):
    return unescape("".join(CELL_RE.findall(tc))).strip()


def split_cell(raw):
    """Return (primary_line, secondary_text). Secondary = remaining lines joined."""
    parts = [p.strip() for p in raw.split("\n") if p.strip() != ""]
    if not parts:
        return "", ""
    return parts[0], " ".join(parts[1:]).strip()


def money_to_int(s):
    """'$740K' -> 740000, '907,000' -> 907000, '$1.02M' -> 1020000. First number of a range."""
    if not s:
        return None
    m = re.search(r"\$?\s*([\d,]+(?:\.\d+)?)\s*([KMkm]?)", s)
    if not m:
        return None
    num = float(m.group(1).replace(",", ""))
    unit = m.group(2).upper()
    if unit == "K":
        num *= 1_000
    elif unit == "M":
        num *= 1_000_000
    return int(round(num))


def home_price(s):
    """money_to_int, but for home prices: a bare number under 10,000 is a
    K-figure whose suffix was dropped (e.g. first number of '$850-900K')."""
    v = money_to_int(s)
    if v is not None and 0 < v < 10_000:
        v *= 1_000
    return v


def digits(s):
    return re.sub(r"[^\d]", "", s) or ""


def parse_address(raw):
    """'[2-FAM] 354 Doane Ave, 10308' -> ('354 Doane Ave', '10308')."""
    txt = re.sub(r"^\s*\[[^\]]*\]\s*", "", raw).strip()
    zm = re.search(r"\b(\d{5})\b\s*$", txt)
    zip_code = zm.group(1) if zm else ""
    street = txt[: zm.start()].rstrip(" ,") if zm else txt.rstrip(" ,")
    return street, zip_code


def parse_zest(raw):
    if not raw or "no zest" in raw.lower():
        return None, ""
    source = "redfin" if "redfin" in raw.lower() else "zillow"
    return money_to_int(raw), source


def parse_brrrr(raw):
    buy = re.search(r"Buy[:\s]*\$?\s*([\d,.]+\s*[KMkm]?)", raw)
    arv = re.search(r"ARV[^$]*\$?\s*([\d,.]+\s*[KMkm]?)", raw)
    pct = re.search(r"(\d+(?:\.\d+)?)\s*%", raw)
    star_runs = re.findall(r"⭐+", raw)
    rating = max((len(r) for r in star_runs), default=None)
    return (
        home_price(buy.group(1)) if buy else None,
        home_price(arv.group(1)) if arv else None,
        float(pct.group(1)) if pct else None,
        rating,
    )


def main():
    inp = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_IN
    out = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_OUT

    z = zipfile.ZipFile(inp)
    xml = z.read("word/document.xml").decode("utf-8", "ignore")
    rels_xml = z.read("word/_rels/document.xml.rels").decode("utf-8", "ignore")
    rels = dict(re.findall(r'Id="([^"]+)"[^>]*Target="([^"]+)"', rels_xml))

    table = TBL_RE.search(xml).group(0)
    rows = ROW_RE.findall(table)[1:]  # drop header

    out_rows = []
    today = date.today().isoformat()
    for i, row in enumerate(rows, start=1):
        cells = [cell_text(c) for c in TC_RE.findall(row)]
        if len(cells) != 16:
            print(f"WARN: row {i} has {len(cells)} cells, skipping", file=sys.stderr)
            continue
        (num, see, tier, hood, addr, price, ptype, bdba, sqft, days, zest,
         school, brrrr, bid, analysis, notes) = cells

        # split the six columns that pack a secondary line
        see_p, priority_note = split_cell(see)
        price_p, price_note = split_cell(price)
        bdba_p, bd_ba_note = split_cell(bdba)
        sqft_p, ppsf_raw = split_cell(sqft)
        school_p, gs_raw = split_cell(school)
        bid_p, bid_note = split_cell(bid)

        rid = re.search(r'<w:hyperlink[^>]*r:id="([^"]+)"', row)
        url = rels.get(rid.group(1), "") if rid else ""

        street, zip_code = parse_address(addr)
        beds, baths = "", ""
        if "/" in bdba_p:
            b, ba = bdba_p.split("/", 1)
            beds, baths = b.strip(), ba.strip()
        zestimate, zsrc = parse_zest(zest)
        buy, arv, appr, rating = parse_brrrr(brrrr)
        vp = see_p.split()[-1].lower() if see_p else ""
        gs = re.sub(r"^GS\s*", "", gs_raw).strip()          # "GS 7-10/10" -> "7-10/10"
        ppsf = digits(ppsf_raw) if "$" in ppsf_raw or ppsf_raw[:1].isdigit() else ""
        verified = not re.search(r"verify|est\.", school_p, re.I)

        out_rows.append({
            "property_id": f"SI-2026-{i:03d}",
            "rank": num.strip(),
            "status": "active",
            "view_priority": vp,
            "priority_note": priority_note,
            "tier": tier,
            "neighborhood": hood,
            "street_address": street,
            "zip": zip_code,
            "listing_url": url,
            "property_type": PROPERTY_TYPE_MAP.get(ptype, ptype),
            "is_multifamily": ptype.startswith("2-Fam") or addr.strip().startswith("[2-FAM]"),
            "list_price": money_to_int(price_p) or "",
            "price_note": price_note,
            "beds": beds,
            "baths": baths,
            "bd_ba_note": bd_ba_note,
            "sqft": digits(sqft_p),
            "price_per_sqft": ppsf,
            "days_on_market": digits(days),
            "zestimate": zestimate if zestimate is not None else "",
            "zestimate_source": zsrc,
            "school_name": school_p,
            "school_gs_rating": gs,
            "school_verified": verified,
            "target_buy_price": buy if buy is not None else "",
            "arv_estimate": arv if arv is not None else "",
            "appreciation_pct": appr if appr is not None else "",
            "brrrr_rating": rating if rating is not None else "",
            "bid_estimate": home_price(bid_p) if bid_p else "",
            "bid_note": bid_note,
            "investment_thesis": brrrr,
            "analysis": analysis,
            "my_notes": notes,
            "last_updated": today,
        })

    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(out_rows)
    print(f"{len(out_rows)} rows written -> {out}")


if __name__ == "__main__":
    main()
