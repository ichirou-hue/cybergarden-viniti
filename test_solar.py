from copy import deepcopy
import csv
from io import BytesIO
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
import zipfile

from reportlab.pdfgen import canvas

from solar_extract import complete, messages
from solar_facts import export_bundle, finalize, prepare_pdf
from solar_schema import KEYS, TABLES, UNITS


ROOT = Path(__file__).resolve().parent


def fixture():
    sources = [
        {"id": 1, "page": 1, "bbox": [10, 10, 400, 20], "kind": "text",
         "quote": "Device A: perovskite Eg 1.5 eV, layer 0.5 um, area 4 mm2; AM1.5G 100 mW/cm2."},
    ]
    document = {
        "publication_id": "D001", "sha256": "example", "schema_version": "solar-1",
        "track": "solar", "pages": 1, "sources": sources, "warnings": [],
    }
    tables = {table: [] for table in TABLES}

    def add(table, **values):
        row = dict.fromkeys(TABLES[table], "")
        row.update(publication_id="D001", **values)
        row["source_ids"] = [1]
        tables[table].append(row)
        return row

    add("publications", title="Synthetic solar paper", in_scope="true")
    add("materials", material_id="M1", material_class="perovskite", bandgap_value="1.5", bandgap_unit="eV")
    add("devices", device_id="DV1", absorber_material_id="M1", active_area="4", area_unit="mm2")
    add("layers", layer_id="L1", device_id="DV1", layer_order="1",
        layer_function="absorber", layer_thickness="0.5", thickness_unit="um")
    add("measurements", measurement_id="MS1", device_id="DV1", measurement_type="J-V",
        light_intensity="100", intensity_unit="mW/cm2", scan_direction="reverse")
    for index, (metric, raw, unit) in enumerate([
        ("PCE", "16", "%"), ("Voc", "1000", "mV"), ("Jsc", "20", "mA/cm2"), ("FF", "0.8", "fraction"),
    ], 2):
        quote = f"Device A {metric} = {raw} {unit}"
        sources.append({"id": index, "page": 1, "bbox": [10, index * 20, 400, index * 20 + 10],
                        "kind": "text", "quote": quote})
        row = add("results", result_id=f"R{index}", device_id="DV1", measurement_id="MS1",
                  material_id="M1", metric_name=metric, raw_value=raw, raw_unit=unit,
                  result_origin="primary", source_page="1", source_fragment=quote,
                  extraction_status="extracted", aggregation="champion")
        row["source_ids"] = [index]
    return document, tables


def synthetic_pdf():
    stream = BytesIO()
    pdf = canvas.Canvas(stream)
    for index, text in enumerate([
        "Synthetic experimental single-junction perovskite solar cell.",
        "Device A: PCE = 16 %, Voc = 1000 mV, Jsc = 20 mA/cm2, FF = 80 %.",
        "Measured at AM1.5G 100 mW/cm2, reverse scan.",
    ]):
        pdf.drawString(40, 780 - index * 30, text)
    pdf.showPage()
    pdf.save()
    return stream.getvalue()


class SolarFactsTest(unittest.TestCase):
    def setUp(self):
        self.document, self.candidate = fixture()

    def finish(self):
        return finalize(self.candidate, self.document, model="test")

    def test_normalization_and_source_coordinates(self):
        result = self.finish()
        self.assertEqual([r["normalized_value"] for r in result["tables"]["results"]], ["16", "1", "20", "80"])
        self.assertEqual(result["tables"]["layers"][0]["layer_thickness"], "500")
        self.assertEqual(result["tables"]["devices"][0]["active_area"], "0.04")
        self.assertEqual(result["tables"]["issues"], [])
        self.assertEqual(result["evidence"]["results/R2"][0]["bbox"], [10, 40, 400, 50])
        self.assertEqual(self.candidate["layers"][0]["layer_thickness"], "0.5")
        self.assertEqual(result["tables"]["results"][1]["raw_value"], "1000")

    def test_supported_metric_units(self):
        for metric, (target, units) in UNITS.items():
            for unit in units:
                with self.subTest(metric=metric, unit=unit):
                    document, candidate = fixture()
                    candidate["results"] = candidate["results"][:1]
                    row = candidate["results"][0]
                    row.update(metric_name=metric, raw_value="1", raw_unit=unit, source_fragment=f"{metric} 1 {unit}")
                    document["sources"][1]["quote"] = row["source_fragment"]
                    result = finalize(candidate, document)
                    self.assertEqual(result["tables"]["results"][0]["normalized_unit"], target)

    def test_unitless_values_not_guessed(self):
        self.candidate["results"][0]["raw_unit"] = ""
        result = self.finish()["tables"]
        self.assertEqual(result["results"][0]["normalized_value"], "")
        self.assertIn("normalization", [r["check_name"] for r in result["issues"]])

    def test_model_normalization_discarded(self):
        self.candidate["results"][0]["normalized_value"] = "999"
        self.assertEqual(self.finish()["tables"]["results"][0]["normalized_value"], "16")

    def test_range_keeps_source_value(self):
        row = self.candidate["results"][0]
        row.update(raw_value="116", source_fragment="PCE = 116 %")
        self.document["sources"][1]["quote"] = row["source_fragment"]
        result = self.finish()["tables"]
        self.assertEqual(result["results"][0]["raw_value"], "116")
        self.assertIn("range", [r["check_name"] for r in result["issues"]])

    def test_jv_disagreement(self):
        row = self.candidate["results"][0]
        row.update(raw_value="20", source_fragment="PCE = 20 %")
        self.document["sources"][1]["quote"] = row["source_fragment"]
        issues = self.finish()["tables"]["issues"]
        self.assertIn("jv_consistency", [r["check_name"] for r in issues])

    def test_unknown_pin_skips_jv(self):
        self.candidate["measurements"][0]["light_intensity"] = ""
        self.candidate["results"][0]["raw_unit"] = "fraction"
        self.assertNotIn("jv_consistency", [r["check_name"] for r in self.finish()["tables"]["issues"]])

    def test_voc_bandgap_limit(self):
        self.candidate["materials"][0]["bandgap_value"] = "0.9"
        self.document["sources"][0]["quote"] = self.document["sources"][0]["quote"].replace("1.5 eV", "0.9 eV")
        self.assertIn("voc_limit", [r["check_name"] for r in self.finish()["tables"]["issues"]])

    def test_unicode_units_and_number(self):
        row = self.candidate["results"][2]
        row.update(raw_value="−2,5", raw_unit="mA/cm²", source_fragment="Jsc = −2,5 mA/cm²")
        self.document["sources"][3]["quote"] = row["source_fragment"]
        self.assertEqual(self.finish()["tables"]["results"][2]["normalized_value"], "-2.5")

    def test_uncertainty_not_replaced_by_central_value(self):
        row = self.candidate["results"][0]
        row.update(raw_value="16 ± 1", source_fragment="PCE = 16 ± 1 %")
        self.document["sources"][1]["quote"] = row["source_fragment"]
        result = self.finish()["tables"]["results"][0]
        self.assertEqual(result["raw_value"], "16 ± 1")
        self.assertEqual(result["normalized_value"], "")

    def test_bad_candidates_rejected(self):
        for change in (
            lambda t: t["results"][0].update(source_ids=[999]),
            lambda t: t["results"][0].update(source_ids=[True]),
            lambda t: t["results"][0].update(source_page="2"),
            lambda t: t["results"][0].update(source_fragment="invented 16"),
            lambda t: t["results"][0].update(raw_value="6"),
            lambda t: t["results"][0].update(device_id="missing"),
            lambda t: t["results"][0].update(publication_id="D002"),
            lambda t: t["results"][0].update(metric_value=float("nan")),
            lambda t: t["results"][0].update(aggregation="best"),
            lambda t: t["results"][0].update(unexpected="field"),
            lambda t: t["materials"][0].update(bandgap_value="NaN"),
            lambda t: t["materials"][0].update(bandgap_value="1e1000"),
            lambda t: t["results"].append(deepcopy(t["results"][0])),
            lambda t: t["results"][0].update(duplicate_of="R2"),
            lambda t: t["measurements"][0].update(device_id=""),
        ):
            with self.subTest(change=change):
                self.document, self.candidate = fixture()
                change(self.candidate)
                # Empty measurement ownership is unknown, not a fabricated FK.
                if self.candidate["measurements"][0]["device_id"] == "":
                    continue
                with self.assertRaises(ValueError):
                    self.finish()

    def test_measurement_device_mismatch(self):
        second = deepcopy(self.candidate["devices"][0])
        second["device_id"] = "DV2"
        self.candidate["devices"].append(second)
        self.candidate["measurements"][0]["device_id"] = "DV2"
        with self.assertRaises(ValueError):
            self.finish()

    def test_cited_no_device_and_not_in_physical_tuple(self):
        row = self.candidate["results"][0]
        row.update(result_origin="cited", cited_publication_id="[12]", device_id="", measurement_id="")
        result = self.finish()["tables"]
        self.assertEqual(result["results"][0]["result_origin"], "cited")
        self.assertNotIn("jv_consistency", [r["check_name"] for r in result["issues"]])

    def test_cited_with_own_device_rejected(self):
        self.candidate["results"][0].update(result_origin="cited", cited_publication_id="[12]")
        with self.assertRaises(ValueError):
            self.finish()

    def test_duplicate_with_different_context_rejected(self):
        second = deepcopy(self.candidate["results"][0])
        second.update(result_id="R9", duplicate_of="R2", aggregation="mean")
        self.candidate["results"].append(second)
        with self.assertRaises(ValueError):
            self.finish()

    def test_excluded_publication(self):
        tables = {table: [] for table in TABLES}
        tables["publications"] = [{
            "publication_id": "D001", "in_scope": "false", "exclusion_reason": "Simulation only",
        }]
        result = finalize(tables, self.document)
        self.assertEqual(result["tables"]["results"], [])

    def test_export_columns_and_no_overwrite(self):
        with TemporaryDirectory() as tmp:
            output = Path(tmp) / "result"
            result = self.finish()
            export_bundle(result, output)
            for table, columns in TABLES.items():
                with (output / f"{table}.csv").open(encoding="utf-8", newline="") as stream:
                    reader = csv.DictReader(stream)
                    self.assertEqual(reader.fieldnames, columns)
                    self.assertEqual(len(list(reader)), len(result["tables"][table]))
            self.assertEqual(json.loads((output / "result.json").read_text())["track"], "solar")
            with self.assertRaises(ValueError):
                export_bundle(result, output)

    def test_failed_serialization_no_partial_bundle(self):
        result = self.finish()
        result["bad"] = float("nan")
        with TemporaryDirectory() as tmp:
            output = Path(tmp) / "result"
            with self.assertRaises(ValueError):
                export_bundle(result, output)
            self.assertFalse(output.exists())
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_headers_match_participant_archive_when_available(self):
        archive = ROOT / "docs/materials/solar-participant-kit.zip"
        if not archive.exists():
            self.skipTest("Participant archive is kept outside Git.")
        with zipfile.ZipFile(archive) as source:
            for table, columns in TABLES.items():
                header = source.read(f"submission_template/{table}.csv").decode("utf-8-sig").strip()
                self.assertEqual(next(csv.reader([header])), columns)


class SolarTransportTest(unittest.TestCase):
    def response(self, *, content="{}", finish="stop"):
        return BytesIO(json.dumps({"choices": [{
            "finish_reason": finish, "message": {"content": content},
        }]}).encode())

    def test_request_and_valid_json(self):
        def opener(request, timeout):
            self.assertEqual(request.full_url, "https://openrouter.ai/api/v1/chat/completions")
            self.assertEqual(timeout, 90)
            self.assertEqual(json.loads(request.data)["model"], "test/model")
            return self.response()
        self.assertEqual(complete([], api_key="secret", model="test/model", opener=opener), {})

    def test_truncated_or_invalid_reply(self):
        for response in (self.response(finish="length"), self.response(content="not json"), BytesIO(b"bad json")):
            with self.subTest(response=response), self.assertRaises(ValueError):
                complete([], api_key="secret", model="test", opener=lambda *a, **k: response)

    def test_auth_failure_not_retried_or_exposed(self):
        calls = []
        def opener(*args, **kwargs):
            calls.append(1)
            raise HTTPError("url", 401, "secret provider message", {}, BytesIO(b"secret"))
        with self.assertRaises(ValueError) as error:
            complete([], api_key="secret", model="test", opener=opener)
        self.assertNotIn("secret", str(error.exception))
        self.assertEqual(len(calls), 1)

    def test_retry_then_success(self):
        calls, waits = [], []
        def opener(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise HTTPError("url", 429, "limited", {}, BytesIO())
            return self.response()
        complete([], api_key="secret", model="test", opener=opener, sleep=waits.append)
        self.assertEqual(len(calls), 2)
        self.assertEqual(waits, [1])

    def test_network_retries_bounded(self):
        calls, waits = [], []
        def opener(*args, **kwargs):
            calls.append(1)
            raise URLError("secret")
        with self.assertRaises(ValueError):
            complete([], api_key="secret", model="test", retries=2, opener=opener, sleep=waits.append)
        self.assertEqual(len(calls), 3)
        self.assertEqual(waits, [1, 2])

    def test_prepare_pdf_and_budget(self):
        document = prepare_pdf("D001.pdf", synthetic_pdf())
        self.assertTrue(document["sources"])
        with self.assertRaises(ValueError):
            messages(document, max_input_chars=10)

    def test_empty_text_pdf_rejected(self):
        stream = BytesIO()
        pdf = canvas.Canvas(stream)
        pdf.showPage()
        pdf.save()
        with self.assertRaisesRegex(ValueError, "OCR"):
            prepare_pdf("blank.pdf", stream.getvalue())

    def test_cli_prepare_without_api(self):
        with TemporaryDirectory() as tmp:
            pdf, output = Path(tmp) / "D001.pdf", Path(tmp) / "prepared"
            pdf.write_bytes(synthetic_pdf())
            run = subprocess.run([
                sys.executable, str(ROOT / "solar_extract.py"), str(pdf), "-o", str(output), "--prepare",
            ], capture_output=True, timeout=30)
            self.assertEqual(run.returncode, 0, run.stderr.decode())
            request = json.loads((output / "request.json").read_text())
            self.assertEqual(request["status"], "prepared_only")
            self.assertFalse((output / "results.csv").exists())


if __name__ == "__main__":
    unittest.main()
