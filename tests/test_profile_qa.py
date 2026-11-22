"""Independent offline profile QA. Real regressions remain failing."""

from copy import deepcopy
import csv
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from extraction_core import compile_profile, export_result, load_profile, run
from tests.test_extraction_core import custom_fixture
from tests.test_solar import ROOT, synthetic_pdf


class ProfileIndependentQA(unittest.TestCase):
    def test_solar_malformed_tables_raise_valueerror_before_pipeline(self):
        for tables in (None, [], "", 0, False):
            with self.subTest(tables=tables):
                profile = load_profile("solar")
                profile["tables"] = tables
                with self.assertRaises(ValueError):
                    compile_profile(profile)

    def test_arbitrary_json_profile_types_raise_valueerror(self):
        for location, value in (
            ("adapter", []), ("adapter", {}), ("type", []),
            ("type", {}), ("reference", []), ("unit", []), ("unit", {}),
        ):
            with self.subTest(location=location, value=value):
                profile, _, _ = custom_fixture()
                field = profile["tables"]["equipment"]["fields"]["temperature"]
                if location == "adapter":
                    profile[location] = value
                elif location == "reference":
                    field.update(type="reference", reference=value)
                else:
                    field[location] = value
                with self.assertRaises(ValueError):
                    compile_profile(profile)

    def test_cli_malformed_profile_has_no_traceback_or_partial_output(self):
        with TemporaryDirectory() as temporary:
            folder = Path(temporary)
            profile, _, _ = custom_fixture()
            profile["adapter"] = []
            profile_path = folder / "profile.json"
            profile_path.write_text(json.dumps(profile), encoding="utf-8")
            pdf = folder / "sample.pdf"
            pdf.write_bytes(synthetic_pdf())
            output = folder / "output"
            environment = {key: value for key, value in os.environ.items()
                           if not key.startswith("OPENROUTER_")}
            execution = subprocess.run(
                [sys.executable, str(ROOT / "extract_document.py"), str(pdf),
                 "--profile", str(profile_path), "--mode", "scripts", "-o", str(output)],
                env=environment, capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(execution.returncode, 1)
            self.assertFalse(output.exists())
            self.assertNotIn("Traceback", execution.stderr)
            self.assertIn("extraction:", execution.stderr)

    def test_custom_scripts_export_succeeds_without_model(self):
        profile, document, _ = custom_fixture()
        client = Mock(side_effect=AssertionError("network forbidden"))
        result = run(document, profile, mode="scripts", model_client=client)
        with TemporaryDirectory() as temporary:
            output = Path(temporary) / "output"
            export_result(result, output)
            self.assertTrue((output / "result.json").exists())
            self.assertTrue((output / "candidates.csv").exists())
        client.assert_not_called()

    def test_generic_number_cannot_drop_sign_or_select_scalar_from_range(self):
        for quote in ("temperature = -20 °C", "temperature = −20 °C",
                      "temperature = +20 °C", "temperature = 120 °C",
                      "temperature = 20–30 °C", "temperature = <20 °C",
                      "temperature = 20 ± 1 °C", "temperature = 20e3 °C"):
            with self.subTest(quote=quote):
                profile, document, candidate = custom_fixture()
                document["sources"][0]["quote"] = "material aluminum; " + quote
                candidate["equipment"][0]["field_sources"]["temperature"]["quote"] = "20"
                with self.assertRaises(ValueError):
                    run(document, profile, mode="model", model_client=lambda _: candidate)

    def test_signed_number_and_separate_header_keep_both_evidence_sources(self):
        profile, document, candidate = custom_fixture()
        document["sources"][0]["quote"] = "material aluminum; temperature = −20"
        document["sources"].append({"id": 2, "page": 1, "kind": "text", "quote": "temperature (°C)"})
        row = candidate["equipment"][0]
        row["temperature"] = "−20"
        row["field_sources"]["temperature"].update(quote="−20", unit_source_id=2)
        result = run(document, profile, mode="model", model_client=lambda _: candidate)
        observation = result["records"][0]["fields"]["temperature"]
        self.assertEqual(observation["raw"], "−20")
        self.assertEqual(observation["value"], "-20")
        self.assertEqual([source["id"] for source in observation["evidence_refs"]], [1, 2])

    def test_wrong_separate_unit_header_leaves_value_unknown(self):
        profile, document, candidate = custom_fixture()
        document["sources"][0]["quote"] = "material aluminum; temperature = 20"
        document["sources"].append({"id": 2, "page": 1, "kind": "text", "quote": "temperature (K)"})
        candidate["equipment"][0]["field_sources"]["temperature"].update(quote="20", unit_source_id=2)
        result = run(document, profile, mode="model", model_client=lambda _: candidate)
        self.assertEqual(result["tables"]["equipment"][0]["temperature"], "")
        self.assertEqual(result["issues"][0]["rule"], "normalization")

    def test_auto_model_error_propagates_after_script_match(self):
        profile, document, _ = custom_fixture()
        client = Mock(side_effect=RuntimeError("fake dependency failed"))
        with self.assertRaisesRegex(RuntimeError, "fake dependency"):
            run(document, profile, mode="auto", model_client=client)
        client.assert_called_once()

    def test_auto_validated_empty_response_does_not_promote_script_candidates(self):
        profile, document, _ = custom_fixture()
        client = Mock(return_value={"equipment": []})
        result = run(document, profile, mode="auto", model_client=client)
        client.assert_called_once()
        self.assertTrue(result["candidates"])
        self.assertEqual(result["records"], [])
        with TemporaryDirectory() as temporary:
            output = Path(temporary) / "output"
            export_result(result, output)
            with (output / "equipment.csv").open(newline="", encoding="utf-8") as stream:
                self.assertEqual(list(csv.reader(stream)), [["equipment_id", "material", "temperature"]])

    def test_export_does_not_overwrite_existing_directory(self):
        profile, document, candidate = custom_fixture()
        result = run(document, profile, mode="model", model_client=lambda _: candidate)
        with TemporaryDirectory() as temporary:
            output = Path(temporary) / "output"
            output.mkdir()
            sentinel = output / "sentinel"
            sentinel.write_bytes(b"original")
            with self.assertRaises(ValueError):
                export_result(result, output)
            self.assertEqual(sentinel.read_bytes(), b"original")
            self.assertEqual(list(output.iterdir()), [sentinel])

    def test_export_csv_failure_cleans_staging(self):
        profile, document, candidate = custom_fixture()
        result = run(document, profile, mode="model", model_client=lambda _: candidate)
        result["tables"]["equipment"][0]["unexpected"] = "field"
        with TemporaryDirectory() as temporary:
            output = Path(temporary) / "output"
            with self.assertRaises(ValueError):
                export_result(result, output)
            self.assertEqual(list(Path(temporary).iterdir()), [])

    def test_export_disk_failure_cleans_staging(self):
        profile, document, candidate = custom_fixture()
        result = run(document, profile, mode="model", model_client=lambda _: candidate)
        with TemporaryDirectory() as temporary:
            with patch("extraction_core.Path.write_text", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    export_result(result, Path(temporary) / "output")
            self.assertEqual(list(Path(temporary).iterdir()), [])

    def test_reference_forward_resolution_and_dangling_rejection(self):
        profile, document, candidate = custom_fixture()
        fields = profile["tables"]["equipment"]["fields"]
        fields["parent"] = {"type": "reference", "description": "Parent equipment",
                            "reference": "equipment"}
        first = candidate["equipment"][0]
        first["parent"] = "E2"
        first["field_sources"]["parent"] = {"source_id": 1, "quote": "Equipment"}
        second = deepcopy(first)
        second.update(equipment_id="E2", parent="")
        second["field_sources"].pop("parent")
        candidate["equipment"].append(second)
        result = run(document, profile, mode="model", model_client=lambda _: candidate)
        self.assertEqual(result["tables"]["equipment"][0]["parent"], "E2")
        self.assertEqual(result["records"][0]["fields"]["parent"]["status"], "inferred_requires_review")
        candidate["equipment"].pop()
        with self.assertRaisesRegex(ValueError, "отсутствующую"):
            run(document, profile, mode="model", model_client=lambda _: candidate)


if __name__ == "__main__":
    unittest.main()
