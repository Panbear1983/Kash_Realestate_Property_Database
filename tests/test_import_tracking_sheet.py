#!/usr/bin/env python3
"""Focused, offline regressions for tracking-sheet source integrity."""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.import_tracking_sheet import (  # noqa: E402
    _load_reviewed_overrides,
    build_import_report,
    load_pdf,
)


def _main_row(number, anchor=None):
    """Small but fully parsable stand-in for one extracted main-table row."""
    anchor = anchor or f"{number}NOW"
    return (
        f"{anchor}AZone{1000 + number} Test St, 10308"
        "$700,000SF Detached3/21,500 $70030d"
        "$710KPS 1 GS 8/10Solid candidate.Note"
    )


class ImportIntegrityTests(unittest.TestCase):
    def test_wrapped_anchors_are_explicit_manifest_gaps(self):
        wrapped = {
            60: "0NOW",
            80: "0RTH",
            95: "5WORTH",
            111: "11WORTH",
        }
        text = "".join(_main_row(n, wrapped.get(n)) for n in range(1, 115))

        result = build_import_report(text, "", expected_comp_rows=())
        exceptions = {
            entry.source_row_number: entry
            for entry in result.report.entries
            if entry.source_section == "main" and entry.parse_state != "parsed"
        }

        self.assertEqual(set(exceptions), {60, 80, 95, 111})
        for number in (60, 80, 95, 111):
            entry = exceptions[number]
            self.assertEqual(entry.parse_state, "manual_required")
            self.assertIsNone(entry.match_key)
            self.assertIn("anchor never found", entry.validation_errors[0])

    def test_parse_gap_is_structured_and_fails_strict_validation(self):
        result = build_import_report(
            _main_row(1) + _main_row(3),
            "",
            expected_main_rows=(1, 2, 3),
            expected_comp_rows=(),
        )

        gap = next(
            entry for entry in result.report.manifest
            if entry.source_section == "main" and entry.source_row_number == 2
        )
        self.assertEqual(gap.parse_state, "manual_required")
        self.assertTrue(gap.validation_errors)
        self.assertNotEqual(result.report.validate(strict=True).exit_code, 0)

    def test_no_zip_bishop_comp_is_manual_required(self):
        result = build_import_report(
            "",
            "19 110 Bishop St Pending----",
            expected_main_rows=(),
            expected_comp_rows=(19,),
        )

        self.assertEqual(len(result.report.manifest), 1)
        entry = result.report.manifest[0]
        self.assertEqual(entry.source_row_number, 19)
        self.assertEqual(entry.address, "110 Bishop St")
        self.assertEqual(entry.source_section, "comp")
        self.assertEqual(entry.parse_state, "manual_required")
        self.assertIsNone(entry.match_key)
        self.assertIn("could not be parsed unambiguously", entry.validation_errors[0])

    def test_unnumbered_bishop_comp_is_not_assigned_row_19(self):
        result = build_import_report(
            "",
            "110 Bishop St Pending----",
            expected_main_rows=(),
            expected_comp_rows=(19,),
        )

        numbered = next(entry for entry in result.report.manifest
                        if entry.source_row_number == 19)
        unknown = next(entry for entry in result.report.manifest
                       if entry.source_row_number is None)
        self.assertEqual(numbered.parse_state, "manual_required")
        self.assertIsNone(numbered.address)
        self.assertEqual(unknown.address, "110 Bishop St")
        self.assertIn("source row number", unknown.validation_errors[0])

    def test_adjacent_row_number_price_corruption_is_invalid(self):
        # The next row's "4" was glued to "$775,000", producing 7,750,004.
        result = build_import_report(
            "",
            "1Tottenville123 Main St, 10307 Closed$775,0004/21,500",
            expected_main_rows=(),
            expected_comp_rows=(1,),
        )

        self.assertFalse(result.comp_records, "invalid comps must not reach the merge list")
        self.assertEqual(len(result.quarantined_records), 1)
        self.assertIsNone(result.quarantined_records[0]["last_sold_price"])
        entry = result.report.manifest[0]
        self.assertEqual(entry.parse_state, "invalid")
        self.assertIsNotNone(entry.match_key)
        self.assertTrue(any("price" in error for error in entry.validation_errors))
        self.assertNotEqual(result.report.validate(strict=True).exit_code, 0)

        repair = result.report.repair_manifest()["repairs"][0]
        self.assertEqual(repair["source"]["source_id"], "comp:1")
        self.assertEqual(repair["source_values"]["last_sold_price"], None)
        self.assertIn("$775,0004/2", repair["source_excerpt"])

    def test_comp_price_cut_is_explicit_not_a_range(self):
        result = build_import_report(
            "",
            "2Great Kills10 Test Ave, 10308 Pending"
            "$775,000 ↓ from $799,000 4/21,500",
            expected_main_rows=(),
            expected_comp_rows=(2,),
        )

        self.assertFalse(result.quarantined_records)
        rec = result.comp_records[0]
        self.assertEqual(rec["status"], "pending")
        self.assertEqual(rec["list_price"], 775_000)
        self.assertEqual(rec["original_list_price"], 799_000)
        self.assertNotIn("last_sold_price", rec)

    def test_comp_price_range_is_quarantined_not_normalized(self):
        result = build_import_report(
            "",
            "3Tottenville12 Test Rd, 10307 Closed"
            "$775,000-$825,000 4/21,500",
            expected_main_rows=(),
            expected_comp_rows=(3,),
        )

        self.assertFalse(result.comp_records)
        self.assertIsNone(result.quarantined_records[0]["last_sold_price"])
        entry = result.report.manifest[0]
        self.assertEqual(entry.parse_state, "invalid")
        self.assertIn("price range is ambiguous", " ".join(entry.validation_errors))

    def test_sold_and_pending_prices_use_source_specific_fields(self):
        result = build_import_report(
            "",
            "4Tottenville14 Test Rd, 10307 Closed"
            "$810,000 (list $825,000) 4/21,500\n"
            "5Great Kills15 Test Ave, 10308 Pending$790,000 3/21,250",
            expected_main_rows=(),
            expected_comp_rows=(4, 5),
        )

        self.assertFalse(result.quarantined_records)
        sold, pending = result.comp_records
        self.assertEqual(
            (sold["status"], sold["last_sold_price"], sold["original_list_price"]),
            ("sold", 810_000, 825_000),
        )
        self.assertNotIn("list_price", sold)
        self.assertEqual((pending["status"], pending["list_price"]), ("pending", 790_000))
        self.assertNotIn("last_sold_price", pending)

    def test_unknown_required_main_fields_are_quarantined_for_repair(self):
        unknown = _main_row(1).replace(
            "SF Detached3/21,500 $70030d",
            "SF (verify)N/A/N/A unknown sqft $70030d",
        )
        result = build_import_report(
            unknown,
            "",
            expected_main_rows=(1,),
            expected_comp_rows=(),
        )

        self.assertFalse(result.main_records)
        self.assertEqual(len(result.quarantined_records), 1)
        errors = " ".join(result.report.manifest[0].validation_errors)
        for field in ("beds", "baths", "sqft", "property_type"):
            self.assertIn(field, errors)

    def test_duplicate_match_key_quarantines_both_source_row_identities(self):
        row_two_same_property = _main_row(2).replace("1002 Test St", "1001 Test St")
        result = build_import_report(
            _main_row(1) + row_two_same_property,
            "",
            expected_main_rows=(1, 2),
            expected_comp_rows=(),
        )

        self.assertFalse(result.main_records)
        self.assertEqual(len(result.quarantined_records), 2)
        repairs = result.report.repair_manifest()["repairs"]
        self.assertEqual(
            [repair["source"]["source_id"] for repair in repairs],
            ["main:1", "main:2"],
        )
        for repair in repairs:
            self.assertIn("shared by multiple source rows", repair["validation_errors"][0])

    def test_ambiguous_comp_status_is_manual_not_inferred(self):
        result = build_import_report(
            "",
            "7 Great Kills 10 Test Ave, 10308 Maybe $700,000",
            expected_main_rows=(),
            expected_comp_rows=(7,),
        )

        self.assertFalse(result.comp_records)
        entry = result.report.manifest[0]
        self.assertEqual(entry.source_row_number, 7)
        self.assertEqual(entry.parse_state, "manual_required")
        self.assertIsNone(entry.match_key)
        self.assertIn("status", entry.validation_errors[0])

    def test_strict_validation_is_the_prewrite_gate(self):
        result = build_import_report(
            _main_row(1),
            "",
            expected_main_rows=(1, 2),
            expected_comp_rows=(),
        )

        validation = result.report.validate(strict=True)
        self.assertFalse(validation.is_valid)
        self.assertEqual(validation.exit_code, 2)
        self.assertIn("main:2", validation.errors[0])

    def test_only_an_explicit_reviewed_override_clears_a_manual_row(self):
        result = build_import_report(
            _main_row(1),
            "",
            expected_main_rows=(1, 2),
            expected_comp_rows=(),
        )
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump({
                "reviewed": True,
                "rows": [{"source_section": "main", "source_row_number": 2}],
            }, fh)
            override_path = fh.name
        try:
            reviewed = _load_reviewed_overrides(override_path)
        finally:
            os.unlink(override_path)

        self.assertTrue(
            result.report.validate(
                strict=True,
                reviewed_overrides=reviewed,
            ).is_valid
        )

    def test_fitz_geometry_recovers_all_four_wrapped_main_anchors(self):
        import fitz

        fd, path = tempfile.mkstemp(suffix=".pdf")
        os.close(fd)
        try:
            doc = fitz.open()
            page = doc.new_page(width=900, height=500)
            anchors = ((60, "NOW"), (80, "WORTH"), (95, "WORTH"), (111, "WORTH"))
            for index, (number, see) in enumerate(anchors):
                y = 45 + index * 70
                page.insert_text((20, y), str(number), fontsize=7)
                page.insert_text((42, y), see, fontsize=7)
                page.insert_text(
                    (85, y),
                    _main_row(number, anchor="")[len(f"{number}NOW"):],
                    fontsize=7,
                )
            page.insert_text((20, 360), "CLOSED", fontsize=8)
            page.insert_text((70, 360), "COMPS", fontsize=8)
            doc.save(path)
            doc.close()

            main_txt, comps_txt, _ = load_pdf(path)
            result = build_import_report(
                main_txt,
                comps_txt,
                expected_main_rows=(60, 80, 95, 111),
                expected_comp_rows=(),
            )
        finally:
            os.unlink(path)

        self.assertEqual(len(result.main_records), 4)
        self.assertEqual(
            [(entry.source_row_number, entry.parse_state) for entry in result.report.manifest],
            [(60, "parsed"), (80, "parsed"), (95, "parsed"), (111, "parsed")],
        )

    def test_fitz_cell_spacing_prevents_price_digit_contamination(self):
        import fitz

        fd, path = tempfile.mkstemp(suffix=".pdf")
        os.close(fd)
        try:
            doc = fitz.open()
            page = doc.new_page(width=900, height=300)
            page.insert_text((20, 40), "CLOSED COMPS", fontsize=8)
            page.insert_text((20, 80), "1", fontsize=7)
            page.insert_text((35, 80), "Tottenville", fontsize=7)
            page.insert_text((100, 80), "123 Main St, 10307", fontsize=7)
            page.insert_text((210, 80), "Closed", fontsize=7)
            page.insert_text((250, 80), "$775,000", fontsize=7)
            page.insert_text((310, 80), "4/2", fontsize=7)
            page.insert_text((340, 80), "1,500", fontsize=7)
            doc.save(path)
            doc.close()

            main_txt, comps_txt, _ = load_pdf(path)
            result = build_import_report(
                main_txt,
                comps_txt,
                expected_main_rows=(),
                expected_comp_rows=(1,),
            )
        finally:
            os.unlink(path)

        self.assertEqual(result.comp_records[0]["last_sold_price"], 775_000)
        self.assertEqual(result.comp_records[0]["beds"], "4")
        self.assertEqual(result.report.manifest[0].parse_state, "parsed")


if __name__ == "__main__":
    unittest.main()
