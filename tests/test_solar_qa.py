"""Independent offline solar QA; failures intentionally reproduce real defects."""

from copy import deepcopy
import csv
from io import BytesIO
from importlib.util import find_spec
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from viniti.solar_extract import complete, main
from viniti.solar_facts import export_bundle, finalize, prepare_pdf
from viniti.solar_schema import TABLES
from tests.test_solar import ROOT, fixture, synthetic_pdf


class SolarIndependentQA(unittest.TestCase):
    def setUp(self):
        self.document, self.candidate = fixture()

    def result(self):
        return finalize(self.candidate, self.document)

    def replace_value(self, raw, quote):
        row = self.candidate["results"][0]
        row.update(raw_value=raw, source_fragment=quote)
        self.document["sources"][1]["quote"] = quote

    def test_negative_source_cannot_verify_unsigned_value(self):
        self.replace_value("16", "PCE = -16 %")
        with self.assertRaises(ValueError):
            self.result()

    def test_unicode_negative_source_cannot_verify_unsigned_value(self):
        self.replace_value("16", "PCE = −16 %")
        with self.assertRaises(ValueError):
            self.result()

    def test_range_preserved_without_computing_midpoint(self):
        self.replace_value("15–17", "PCE = 15–17 %")
        result = self.result()["tables"]
        self.assertEqual(result["results"][0]["raw_value"], "15–17")
        self.assertEqual(result["results"][0]["normalized_value"], "")
        self.assertIn("normalization", [i["check_name"] for i in result["issues"]])

    def test_wrong_source_id_even_when_quote_exists_elsewhere(self):
        self.candidate["results"][0]["source_ids"] = [3]
        with self.assertRaises(ValueError):
            self.result()

    def test_wrong_page_even_when_quote_exists_on_other_page(self):
        self.document["pages"] = 2
        self.candidate["results"][0]["source_page"] = "2"
        with self.assertRaises(ValueError):
            self.result()

    def test_missing_tables_and_nonobject_candidates(self):
        for candidate in (None, [], "{}", {}, {"publications": []}):
            with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                finalize(candidate, self.document)

    def test_nonstring_and_unknown_row_fields(self):
        for change in ({"raw_value": 16}, {"source_ids": "2"}, {"source_ids": [False]},
                       {"surprise": "field"}, {"extraction_status": "success"}):
            with self.subTest(change=change):
                document, candidate = fixture()
                candidate["results"][0].update(change)
                with self.assertRaises(ValueError):
                    finalize(candidate, document)

    def test_forward_and_reverse_conditions_are_not_contradiction(self):
        second = deepcopy(self.candidate["measurements"][0])
        second.update(measurement_id="MS2", scan_direction="forward")
        self.candidate["measurements"].append(second)
        row = deepcopy(self.candidate["results"][0])
        row.update(result_id="R20", measurement_id="MS2", raw_value="15",
                   source_fragment="Forward PCE = 15 %", source_ids=[20])
        self.candidate["results"].append(row)
        self.document["sources"].append(
            {"id": 20, "page": 1, "quote": row["source_fragment"]})
        self.assertNotIn("contradiction", [i["check_name"] for i in self.result()["tables"]["issues"]])

    def test_same_conditions_different_values_report_contradiction(self):
        row = deepcopy(self.candidate["results"][0])
        row.update(result_id="R20", raw_value="15", source_fragment="PCE = 15 %", source_ids=[20])
        self.candidate["results"].append(row)
        self.document["sources"].append({"id": 20, "page": 1, "quote": row["source_fragment"]})
        self.assertIn("contradiction", [i["check_name"] for i in self.result()["tables"]["issues"]])

    def test_semicolon_device_list_accepts_whitespace(self):
        device = deepcopy(self.candidate["devices"][0])
        device["device_id"] = "DV2"
        self.candidate["devices"].append(device)
        self.candidate["measurements"][0]["device_id"] = "DV1; DV2"
        self.candidate["results"][0]["device_id"] = "DV2"
        self.result()

    def test_eqe_vs_jv_difference_over_five_percent_is_flagged(self):
        row = deepcopy(self.candidate["results"][2])
        row.update(result_id="R20", metric_name="Jsc_EQE", raw_value="15",
                   source_fragment="Jsc_EQE = 15 mA/cm2", source_ids=[20])
        self.candidate["results"].append(row)
        self.document["sources"].append({"id": 20, "page": 1, "quote": row["source_fragment"]})
        issues = self.result()["tables"]["issues"]
        self.assertTrue(any({"R4", "R20"} <= set(i["record_ids"].split(";")) for i in issues),
                        "Jsc=20 vs Jsc_EQE=15 must create a linked issue (FR-4)")

    def test_malformed_provider_envelopes_fail_cleanly(self):
        for envelope in (None, [], {}, {"choices": []}, {"choices": [None]},
                         {"choices": [{"finish_reason": "stop", "message": {"content": None}}]}):
            raw = json.dumps(envelope).encode()
            with self.subTest(envelope=envelope), self.assertRaises(ValueError):
                complete([], api_key="fake", model="fake",
                         opener=lambda *a, **k: BytesIO(raw))

    def test_response_size_limit(self):
        with patch("viniti.solar_extract.MAX_RESPONSE_BYTES", 4), self.assertRaisesRegex(ValueError, "размер"):
            complete([], api_key="fake", model="fake", opener=lambda *a, **k: BytesIO(b"12345"))

    def test_missing_key_never_calls_transport(self):
        with self.assertRaises(ValueError), patch("viniti.solar_extract.urlopen") as network:
            complete([], api_key="", model="fake", opener=network)
        network.assert_not_called()

    def run_cli(self, pdf, output, *options):
        environment = {k: v for k, v in os.environ.items()
                       if k not in {"OPENROUTER_API_KEY", "OPENROUTER_MODEL"}}
        environment["OPENROUTER_API_KEY"] = ""
        environment["OPENROUTER_MODEL"] = ""
        # Present-but-empty names win over the developer's .env, so the child
        # process stays offline no matter what the local environment holds.
        environment.update({"OPENAI_API_KEY": "", "OPENAI_MODEL": "",
                            "OPENAI_BASE_URL": ""})
        return subprocess.run([sys.executable, "-X", "utf8", "-m", "viniti", "solar-extract", str(pdf),
                               "-o", str(output), *options], env=environment,
                              capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)

    def test_cli_missing_key_no_partial_export(self):
        with TemporaryDirectory() as tmp:
            pdf, output = Path(tmp) / "D001.pdf", Path(tmp) / "out"
            pdf.write_bytes(synthetic_pdf())
            run = self.run_cli(pdf, output, "--model", "fake")
            self.assertEqual(run.returncode, 1)
            self.assertIn("OPENAI_API_KEY", run.stderr)
            self.assertNotIn("Traceback", run.stderr)
            self.assertFalse(output.exists())

    def test_cli_bad_pdf_and_missing_file_no_partial_export(self):
        with TemporaryDirectory() as tmp:
            pdf, output = Path(tmp) / "bad.pdf", Path(tmp) / "out"
            for payload in (None, b"not a PDF", b"%PDF-1.7\nbroken"):
                if payload is not None:
                    pdf.write_bytes(payload)
                with self.subTest(payload=payload):
                    run = self.run_cli(pdf, output, "--prepare")
                    self.assertEqual(run.returncode, 1)
                    self.assertNotIn("Traceback", run.stderr)
                    self.assertFalse(output.exists())

    def test_cli_prepare_does_not_overwrite_existing_files(self):
        with TemporaryDirectory() as tmp:
            pdf, output = Path(tmp) / "D001.pdf", Path(tmp) / "out"
            pdf.write_bytes(synthetic_pdf())
            self.assertEqual(self.run_cli(pdf, output, "--prepare").returncode, 0)
            original = (output / "request.json").read_bytes()
            self.assertEqual(self.run_cli(pdf, output, "--prepare").returncode, 1)
            self.assertEqual((output / "request.json").read_bytes(), original)
            self.assertEqual([p.name for p in output.iterdir()], ["request.json"])

    def test_prepare_write_failure_leaves_no_partial_output(self):
        with TemporaryDirectory() as tmp:
            pdf, output = Path(tmp) / "D001.pdf", Path(tmp) / "out"
            pdf.write_bytes(synthetic_pdf())
            with patch.object(sys, "argv", ["solar-extract", str(pdf), "-o", str(output), "--prepare"]), \
                    patch("viniti.solar_extract.Path.write_text", side_effect=OSError("disk full")):
                self.assertEqual(main(), 1)
            self.assertFalse(output.exists(), "failed prepare must not block retry with an empty output directory")

    def test_csv_serialization_failure_is_atomic(self):
        result = self.result()
        result["tables"]["results"][0]["unexpected"] = "bad"
        with TemporaryDirectory() as tmp:
            output = Path(tmp) / "out"
            with self.assertRaises(ValueError):
                export_bundle(result, output)
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_headers_match_unpacked_participant_template(self):
        template = ROOT / "docs/materials/solar-participant-kit/submission_template"
        if not template.exists():
            self.skipTest("participant template not available")
        for table, columns in TABLES.items():
            with (template / f"{table}.csv").open(encoding="utf-8-sig", newline="") as stream:
                self.assertEqual(next(csv.reader(stream)), columns)

    def test_synthetic_bundle_passes_participant_checker_with_pdf(self):
        checker = ROOT / "docs/materials/solar-participant-kit/tools/check_submission.py"
        if not checker.exists():
            self.skipTest("participant checker not available")
        if find_spec("pypdf") is None:
            self.skipTest("participant quote checker requires optional pypdf")
        with TemporaryDirectory() as tmp:
            # Use the exact source quotes from the fixture in a synthetic PDF.
            from reportlab.pdfgen import canvas
            pdf = canvas.Canvas(str(Path(tmp) / "D001.pdf"))
            for index, source in enumerate(self.document["sources"]):
                pdf.drawString(20, 780 - index * 30, source["quote"])
            pdf.save()
            export_bundle(self.result(), Path(tmp) / "bundle")
            run = subprocess.run([sys.executable, str(checker), str(Path(tmp) / "bundle"), tmp],
                                 capture_output=True, text=True, timeout=30)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertIn("ошибок: 0, предупреждений: 0", run.stdout)
            self.assertIn("проверено цитат: 4, не найдено: 0", run.stdout)


if __name__ == "__main__":
    unittest.main()
