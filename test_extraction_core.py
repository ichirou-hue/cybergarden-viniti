from copy import deepcopy
import csv
from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock
from urllib.error import HTTPError
from urllib.request import Request

from extraction_core import compile_profile, export_result, load_profile, response_schema, run
from solar_extract import NoRedirect
from solar_facts import finalize
from test_solar import ROOT, fixture, synthetic_pdf


def custom_fixture():
    profile = load_profile("custom-example")
    document = {
        "publication_id": "sample", "sha256": "synthetic", "pages": 1, "warnings": [],
        "sources": [{"id": 1, "page": 1, "bbox": [1, 2, 100, 20], "kind": "text",
                     "quote": "Equipment material aluminum; temperature = 20 °C"}],
    }
    candidate = {"equipment": [{
        "equipment_id": "E1", "material": "aluminum", "temperature": "20",
        "field_sources": {
            "material": {"source_id": 1, "quote": "material aluminum"},
            "temperature": {"source_id": 1, "quote": "temperature = 20 °C", "raw_unit": "°C"},
        },
    }]}
    return profile, document, candidate


class ExtractionCoreTest(unittest.TestCase):
    def test_scripts_never_call_model_and_do_not_invent_entities(self):
        profile, document, _ = custom_fixture()
        client = Mock(side_effect=AssertionError("Must not call network"))
        result = run(document, profile, mode="scripts", model_client=client)
        client.assert_not_called()
        self.assertEqual(result["tables"], {})
        self.assertEqual(result["records"], [])
        self.assertEqual(result["coverage"]["status"], "partial")
        self.assertEqual(result["candidates"][0]["raw_value"], "20")
        self.assertEqual(result["candidates"][0]["raw_unit"], "")
        self.assertEqual(result["candidates"][0]["context_status"], "unresolved")

    def test_no_matches_does_not_mean_absent(self):
        profile, document, _ = custom_fixture()
        document["sources"][0]["quote"] = "A completely different sentence."
        result = run(document, profile, mode="scripts")
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["records"], [])
        self.assertEqual(result["coverage"]["completeness"], "not_measured")
        self.assertIn("не означает", result["warnings"][-1])

    def test_auto_runs_model_even_with_script_matches(self):
        profile, document, candidate = custom_fixture()
        client = Mock(return_value=candidate)
        result = run(document, profile, mode="auto", model_client=client, model="fake")
        client.assert_called_once()
        self.assertTrue(result["candidates"])
        self.assertEqual(result["tables"]["equipment"][0]["temperature"], "20")
        observation = result["records"][0]["fields"]["temperature"]
        self.assertEqual(observation["unit"], "°C")
        self.assertEqual(observation["evidence_refs"][0]["bbox"], [1, 2, 100, 20])
        self.assertEqual(result["coverage"]["completeness"], "not_measured")

    def test_missing_context_still_calls_model(self):
        profile, document, _ = custom_fixture()
        document["sources"][0]["quote"] = "A different sentence."
        client = Mock(return_value={"equipment": []})
        result = run(document, profile, mode="auto", model_client=client)
        client.assert_called_once()
        self.assertEqual(result["records"], [])

    def test_api_failure_not_masked_as_offline_success(self):
        profile, document, _ = custom_fixture()
        with self.assertRaises(ValueError):
            run(document, profile, model_client=Mock(side_effect=ValueError("API failed")))

    def test_custom_profile_unit_and_range_rules(self):
        profile, document, candidate = custom_fixture()
        field = profile["tables"]["equipment"]["fields"]["temperature"]
        field["maximum"] = "10"
        result = run(document, profile, mode="model", model_client=lambda _: candidate)
        self.assertEqual(result["issues"][0]["rule"], "range")
        self.assertEqual(result["tables"]["equipment"][0]["temperature"], "20")

    def test_missing_required_is_unknown_not_absent(self):
        profile, document, candidate = custom_fixture()
        candidate["equipment"][0]["material"] = ""
        result = run(document, profile, mode="model", model_client=lambda _: candidate)
        self.assertEqual(result["records"][0]["fields"]["material"]["status"], "unknown")
        self.assertEqual(result["issues"][0]["rule"], "required")

    def test_invented_field_or_source_rejected(self):
        for change in (
            lambda row: row.update(material="steel"),
            lambda row: row["field_sources"]["material"].update(source_id=999),
            lambda row: row.update(invented="field"),
            lambda row: row.update(temperature="0"),
            lambda row: row["field_sources"]["temperature"].update(quote="temperature = 0 °C"),
        ):
            with self.subTest(change=change):
                profile, document, candidate = custom_fixture()
                change(candidate["equipment"][0])
                with self.assertRaises(ValueError):
                    run(document, profile, mode="model", model_client=lambda _: candidate)

    def test_wrong_unit_remains_unnormalized(self):
        profile, document, candidate = custom_fixture()
        candidate["equipment"][0]["field_sources"]["temperature"]["raw_unit"] = "K"
        result = run(document, profile, mode="model", model_client=lambda _: candidate)
        self.assertEqual(result["tables"]["equipment"][0]["temperature"], "")
        self.assertEqual(result["records"][0]["fields"]["temperature"]["raw"], "20")
        self.assertEqual(result["issues"][0]["rule"], "normalization")

    def test_separate_header_unit_is_verified(self):
        profile, document, candidate = custom_fixture()
        document["sources"][0]["quote"] = "Equipment material aluminum; temperature = 20"
        document["sources"].append({"id": 2, "page": 1, "quote": "Temperature (°C)", "kind": "text"})
        candidate["equipment"][0]["field_sources"]["temperature"].update(
            quote="temperature = 20", unit_source_id=2,
        )
        result = run(document, profile, mode="model", model_client=lambda _: candidate)
        self.assertEqual(result["tables"]["equipment"][0]["temperature"], "20")

    def test_profile_validation_and_no_code_execution(self):
        for change in (
            lambda p: p.update(adapter="execute_python"),
            lambda p: p.update(id=None),
            lambda p: p.update(adapter=[]),
            lambda p: p["tables"]["equipment"]["fields"].update(equipment_id="wrong"),
            lambda p: p["tables"]["equipment"]["fields"]["temperature"].update(type="eval"),
            lambda p: p["tables"]["equipment"]["fields"]["temperature"].update(type=[]),
            lambda p: p["tables"]["equipment"]["fields"]["temperature"].update(unit=[]),
            lambda p: p["tables"]["equipment"]["fields"]["temperature"].update(type="reference", reference=[]),
            lambda p: p["tables"]["equipment"]["fields"].update(field_sources={"type": "text", "description": "reserved"}),
            lambda p: p["tables"]["equipment"]["fields"]["temperature"].update(minimum="NaN"),
            lambda p: p["tables"]["equipment"]["fields"]["temperature"].update(minimum="40", maximum="10"),
            lambda p: p["tables"]["equipment"]["fields"]["temperature"].update(units={"°C": "-1"}),
            lambda p: p["tables"]["equipment"]["fields"]["temperature"].update(required="yes"),
        ):
            with self.subTest(change=change):
                profile, _, _ = custom_fixture()
                change(profile)
                with self.assertRaises(ValueError):
                    compile_profile(profile)

    def test_coefficient_uses_same_numeric_parser_as_compiler(self):
        profile, document, candidate = custom_fixture()
        profile["tables"]["equipment"]["fields"]["temperature"]["units"]["°C"] = "0,1"
        result = run(document, profile, mode="model", model_client=lambda _: candidate)
        self.assertEqual(result["tables"]["equipment"][0]["temperature"], "2")

    def test_custom_scripts_export_with_and_without_candidates(self):
        for quote in ("Equipment temperature = 20 °C", "No numbers here."):
            with self.subTest(quote=quote):
                profile, document, _ = custom_fixture()
                document["sources"][0]["quote"] = quote
                result = run(document, profile, mode="scripts")
                with TemporaryDirectory() as tmp:
                    out = Path(tmp) / "result"
                    export_result(result, out)
                    self.assertTrue((out / "candidates.csv").exists())
                    self.assertFalse((out / "equipment.csv").exists())

    def test_profile_compiles_response_shape(self):
        profile = load_profile("custom-example")
        schema = response_schema(profile)
        self.assertIn("equipment", schema["properties"])
        self.assertFalse(schema["additionalProperties"])

    def test_profile_changes_are_versioned_in_run(self):
        profile, document, _ = custom_fixture()
        first = run(document, profile, mode="scripts")
        profile["tables"]["equipment"]["fields"]["temperature"]["description"] += " New wording."
        second = run(document, profile, mode="scripts")
        self.assertNotEqual(first["run"]["profile_sha256"], second["run"]["profile_sha256"])

    def test_solar_adapter_exact_tables_and_offline_candidates(self):
        document, candidate = fixture()
        profile = load_profile("solar")
        offline = run(document, profile, mode="scripts")
        self.assertTrue(offline["candidates"])
        self.assertFalse(offline["tables"])
        online = run(document, profile, mode="auto", model_client=lambda _: candidate)
        self.assertEqual(online["tables"]["results"][0]["normalized_value"], "16")
        self.assertEqual(online["adapter"], "solar_csv")
        self.assertEqual(online["records"][0]["evidence_scope"], "record")

    def test_budget_failure_does_not_call_model(self):
        profile, document, _ = custom_fixture()
        client = Mock()
        with self.assertRaises(ValueError):
            run(document, profile, model_client=client, max_input_chars=1)
        client.assert_not_called()

    def test_export_generic_empty_table_has_declared_headers(self):
        profile, document, _ = custom_fixture()
        result = run(document, profile, mode="model", model_client=lambda _: {"equipment": []})
        with TemporaryDirectory() as tmp:
            out = Path(tmp) / "result"
            export_result(result, out)
            with (out / "equipment.csv").open(encoding="utf-8") as stream:
                self.assertEqual(next(csv.reader(stream)), ["equipment_id", "material", "temperature"])

    def test_cli_offline(self):
        with TemporaryDirectory() as tmp:
            pdf, out = Path(tmp) / "sample.pdf", Path(tmp) / "result"
            pdf.write_bytes(synthetic_pdf())
            env = {k: v for k, v in os.environ.items() if not k.startswith("OPENROUTER_")}
            execution = subprocess.run([
                sys.executable, str(ROOT / "extract_document.py"), str(pdf), "--profile", "solar",
                "--mode", "scripts", "-o", str(out),
            ], capture_output=True, text=True, timeout=30, env=env)
            self.assertEqual(execution.returncode, 0, execution.stderr)
            self.assertTrue((out / "candidates.csv").exists())
            self.assertFalse((out / "results.csv").exists())
            self.assertEqual(json.loads((out / "result.json").read_text())["run"]["mode"], "scripts")


class SolarReviewRegressionTest(unittest.TestCase):
    def test_short_fragment_cannot_select_suffix_of_number(self):
        document, candidate = fixture()
        document["sources"][1]["quote"] = "PCE = 116 %"
        candidate["results"][0]["source_fragment"] = "16"
        with self.assertRaises(ValueError):
            finalize(candidate, document)

    def test_scalar_cannot_select_one_end_of_range_or_uncertainty(self):
        for quote in ("PCE = 16–20 %", "PCE = 16 ± 1 %", "PCE = <16 %"):
            with self.subTest(quote=quote):
                document, candidate = fixture()
                document["sources"][1]["quote"] = quote
                candidate["results"][0]["source_fragment"] = quote
                with self.assertRaises(ValueError):
                    finalize(candidate, document)

    def test_mislabelled_unit_is_not_normalized(self):
        document, candidate = fixture()
        candidate["results"][1]["raw_unit"] = "V"
        result = finalize(candidate, document)
        self.assertEqual(result["tables"]["results"][1]["normalized_value"], "")
        self.assertTrue(any(r["check_name"] == "normalization" for r in result["tables"]["issues"]))

    def test_invented_entity_value_is_rejected(self):
        document, candidate = fixture()
        candidate["materials"][0]["bandgap_value"] = "0.9"
        with self.assertRaises(ValueError):
            finalize(candidate, document)

    def test_unknown_values_not_silently_deduplicated(self):
        document, candidate = fixture()
        first = candidate["results"][0]
        first.update(raw_value="16 ± 1", source_fragment="PCE = 16 ± 1 %")
        document["sources"][1]["quote"] = first["source_fragment"]
        second = deepcopy(first)
        second.update(result_id="R20", raw_value="20 ± 1", source_fragment="PCE = 20 ± 1 %",
                      source_ids=[20], duplicate_of="R2")
        candidate["results"].append(second)
        document["sources"].append({"id": 20, "page": 1, "quote": second["source_fragment"]})
        with self.assertRaises(ValueError):
            finalize(candidate, document)

    def test_spread_converted_with_value(self):
        document, candidate = fixture()
        row = candidate["results"][1]
        row.update(spread_value="20", source_fragment="Voc = 1000 ± 20 mV")
        document["sources"][2]["quote"] = row["source_fragment"]
        result = finalize(candidate, document)["tables"]["results"][1]
        self.assertEqual(result["normalized_value"], "1")
        self.assertEqual(result["spread_value"], "0.02")
        self.assertIn("20 mV", result["note"])

    def test_number_touching_unit_preserves_complete_numeric_token(self):
        document, candidate = fixture()
        row = candidate["results"][1]
        row["source_fragment"] = "Voc = 1000mV"
        document["sources"][2]["quote"] = row["source_fragment"]
        self.assertEqual(finalize(candidate, document)["tables"]["results"][1]["normalized_value"], "1")

    def test_relative_spread_not_misinterpreted_as_absolute(self):
        document, candidate = fixture()
        row = candidate["results"][1]
        row.update(spread_value="2", source_fragment="Voc = 1000 mV ± 2%")
        document["sources"][2]["quote"] = row["source_fragment"]
        result = finalize(candidate, document)["tables"]["results"][1]
        self.assertEqual(result["normalized_value"], "1")
        self.assertEqual(result["spread_value"], "")
        self.assertIn("2 mV", result["note"])

    def test_parenthesized_relative_spread_is_not_absolute(self):
        document, candidate = fixture()
        row = candidate["results"][1]
        row.update(spread_value="2", source_fragment="Voc = 1000 mV (±2%)")
        document["sources"][2]["quote"] = row["source_fragment"]
        self.assertEqual(finalize(candidate, document)["tables"]["results"][1]["spread_value"], "")

    def test_relative_percent_uncertainty_is_not_percentage_points(self):
        document, candidate = fixture()
        row = candidate["results"][0]
        row.update(raw_value="20", spread_value="2", source_fragment="PCE = 20 % (relative uncertainty ±2%)")
        document["sources"][1]["quote"] = row["source_fragment"]
        self.assertEqual(finalize(candidate, document)["tables"]["results"][0]["spread_value"], "")

    def test_explicit_inline_absolute_percent_spread(self):
        document, candidate = fixture()
        row = candidate["results"][0]
        row.update(raw_value="20", spread_value="2", source_fragment="PCE = 20 ± 2 %")
        document["sources"][1]["quote"] = row["source_fragment"]
        self.assertEqual(finalize(candidate, document)["tables"]["results"][0]["spread_value"], "2")

    def test_unconfirmed_time_cannot_change_comparison_group(self):
        document, candidate = fixture()
        candidate["results"][0].update(time_point="999", time_unit="h")
        with self.assertRaises(ValueError):
            finalize(candidate, document)

    def test_supported_time_with_actual_evidence(self):
        document, candidate = fixture()
        row = candidate["results"][0]
        row.update(time_point="60", time_unit="min", source_ids=[2, 20])
        document["sources"].append({"id": 20, "page": 1, "quote": "After 60 min of operation."})
        result = finalize(candidate, document)["tables"]["results"][0]
        self.assertEqual(result["time_point"], "1")
        self.assertEqual(result["time_unit"], "h")

    def test_transport_rejects_cross_host_and_downgrade(self):
        for target in ("https://other.example/", "http://openrouter.ai/"):
            with self.subTest(target=target), self.assertRaises(HTTPError) as caught:
                NoRedirect().redirect_request(
                    Request("https://openrouter.ai/api/v1/chat/completions",
                            headers={"Authorization": "Bearer never-send"}),
                    BytesIO(), 302, "Moved", {}, target,
                )
            caught.exception.close()


if __name__ == "__main__":
    unittest.main()
