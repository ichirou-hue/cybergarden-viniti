"""Тесты офлайн-оболочки LLM: mock-бэкенд, артефакты и неизменность паспорта."""

from io import BytesIO
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from reportlab.pdfgen import canvas

from extraction_core import export_result, load_profile, run
from solar_facts import prepare_pdf
from solar_schema import TABLES
import llm_shell
import parse_document


PROFILE = {
    "id": "shell_test",
    "version": "1",
    "description": "Тестовый профиль оболочки.",
    "tables": {
        "sample": {
            "key": "sample_id",
            "description": "Один образец с явно указанным числом.",
            "fields": {
                "sample_id": {"type": "id", "description": "Локальный идентификатор."},
                "count": {"type": "number", "description": "Явно указанное число образцов."},
                "note": {"type": "text", "description": "Обязательное примечание.", "required": True},
            },
        },
    },
}

DOCUMENT = {
    "publication_id": "S001", "sha256": "deadbeef", "pages": 1, "warnings": [],
    "sources": [{"id": 1, "page": 1, "kind": "text", "quote": "count = 12 samples."}],
}


class BadBackend:
    """Возвращает заданный ответ на любую стадию; сеть не используется."""

    name = "bad-mock"
    network = False

    def __init__(self, response):
        self.response = response

    def complete(self, stage, payload):
        return self.response


class StagedBackend:
    """Разные ответы на interpret и audit для проверки отказа только аудита."""

    name = "staged-mock"
    network = False

    def __init__(self, interpretation, audit):
        self.interpretation = interpretation
        self.audit = audit

    def complete(self, stage, payload):
        return self.interpretation if stage == "interpret" else self.audit


def bundle(root, document=None, profile=None, name="bundle"):
    document = document if document is not None else DOCUMENT
    profile = profile if profile is not None else PROFILE
    result = run(document, profile, mode="scripts")
    result["profile"] = profile
    output = Path(root) / name
    export_result(result, output)
    return output


def manifest(bundle_path):
    return json.loads((bundle_path / "llm" / "manifest.json").read_text(encoding="utf-8"))


def stages(found):
    return {stage["name"]: stage["status"] for stage in found["stages"]}


def temperature_pdf():
    buffer = BytesIO()
    drawing = canvas.Canvas(buffer)
    drawing.drawString(72, 720, "Synthetic experimental single-junction perovskite solar cell.")
    drawing.drawString(72, 700, "Device A: PCE = 16 %, Voc = 1000 mV, Jsc = 20 mA/cm2, FF = 80 %.")
    drawing.drawString(72, 680, "Measured at AM1.5G 100 mW/cm2, reverse scan.")
    drawing.save()
    return buffer.getvalue()


def solar_document_and_candidate():
    """Minimal in-scope solar document plus a candidate finalize accepts literally."""
    sources = [
        {"id": 1, "page": 1, "kind": "text",
         "quote": "Device A: perovskite Eg 1.5 eV, layer 0.5 um, area 4 mm2; AM1.5G 100 mW/cm2."},
    ]
    document = {
        "publication_id": "D001", "sha256": "deadbeef", "pages": 1,
        "sources": sources, "warnings": [],
    }
    tables = {table: [] for table in TABLES}

    def add(table, **values):
        row = dict.fromkeys(TABLES[table], "")
        row.update(publication_id="D001", **values)
        row["source_ids"] = [1]
        tables[table].append(row)
        return row

    add("publications", title="Synthetic solar paper", in_scope="true", exclusion_reason="")
    add("materials", material_id="M1", material_class="perovskite",
        bandgap_value="1.5", bandgap_unit="eV")
    add("devices", device_id="DV1", absorber_material_id="M1",
        active_area="4", area_unit="mm2")
    add("layers", layer_id="L1", device_id="DV1", layer_order="1",
        layer_function="absorber", layer_thickness="0.5", thickness_unit="um")
    add("measurements", measurement_id="MS1", device_id="DV1", measurement_type="J-V",
        light_intensity="100", intensity_unit="mW/cm2", scan_direction="reverse")
    for index, (metric, raw, unit) in enumerate(
            [("PCE", "16", "%"), ("Voc", "1000", "mV")], 2):
        quote = f"Device A {metric} = {raw} {unit}"
        sources.append({"id": index, "page": 1, "kind": "text", "quote": quote})
        row = add("results", result_id=f"R{index}", device_id="DV1", measurement_id="MS1",
                  material_id="M1", metric_name=metric, raw_value=raw, raw_unit=unit,
                  result_origin="primary", source_page="1", source_fragment=quote,
                  extraction_status="extracted", aggregation="champion")
        row["source_ids"] = [index]
    return document, tables


def solar_bundle(root, name="solar_ok", extra_sources=()):
    document, candidate = solar_document_and_candidate()
    document["sources"].extend(extra_sources)
    profile = load_profile("solar")
    result = run(document, profile, mode="scripts")
    result["profile"] = profile
    output = Path(root) / name
    export_result(result, output)
    return output, candidate


SIMULATION_SOURCE = {
    "id": 4, "page": 1, "kind": "text",
    "quote": "Simulated stack of a perovskite solar cell; no fabricated sample.",
}


class LlmShellTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_scripts_only_bundle_has_no_llm_artifacts(self):
        output = bundle(self.root)
        self.assertFalse((output / "llm").exists())
        self.assertTrue((output / "result.json").is_file())
        self.assertTrue((output / "candidates.csv").is_file())

    def test_mock_writes_artifacts_and_keeps_passport_and_csv(self):
        output = bundle(self.root)
        before = (output / "result.json").read_bytes()
        csv_before = (output / "candidates.csv").read_bytes()

        found = llm_shell.run_bundle(output)

        self.assertEqual((output / "result.json").read_bytes(), before)
        self.assertEqual((output / "candidates.csv").read_bytes(), csv_before)
        self.assertEqual(stages(found), {
            "sources": "ok", "interpret": "ok", "passport": "ok",
            "audit": "ok", "export": "skipped",
        })
        self.assertEqual(found["counts"]["interpreted_records"], 1)
        self.assertEqual(found["counts"]["audit_findings"], 1)
        self.assertTrue(found["guarantees"]["passport_unchanged"])
        self.assertFalse(found["guarantees"]["official_csv_written"])
        self.assertFalse(found["guarantees"]["network_used"])
        self.assertEqual(found["backend"]["network"], False)

        folder = output / "llm"
        self.assertEqual(set(found["artifacts"]),
                         set(llm_shell.BASE_ARTIFACT_NAMES) - {"manifest.json"})
        self.assertFalse((folder / "results.csv").exists())
        for name in llm_shell.BASE_ARTIFACT_NAMES:
            self.assertTrue((folder / name).is_file(), name)
        self.assertFalse([item.name for item in folder.iterdir() if item.name.startswith(".")])

        interpretation = json.loads((folder / "interpretation.json").read_text(encoding="utf-8"))
        self.assertEqual(interpretation["tables"]["sample"][0]["count"], "12")
        self.assertEqual(interpretation["records"][0]["fields"]["count"]["raw"], "12")
        audit = json.loads((folder / "audit.json").read_text(encoding="utf-8"))
        self.assertEqual(audit["findings"][0]["rule"], "required")
        self.assertEqual(audit["findings"][0]["evidence"][0]["source_id"], 1)

    def test_malformed_interpretation_is_rejected_not_raised(self):
        output = bundle(self.root)
        found = llm_shell.run_bundle(output, backend=BadBackend("not json"))
        self.assertEqual(stages(found)["interpret"], "rejected")
        self.assertEqual(stages(found)["audit"], "skipped")
        interpretation = json.loads(
            (output / "llm" / "interpretation.json").read_text(encoding="utf-8"))
        self.assertEqual(interpretation["status"], "rejected")
        self.assertIn("JSON", interpretation["error"])

    def test_untrusted_quote_is_rejected(self):
        output = bundle(self.root)
        response = {"sample": [{
            "sample_id": "E1", "count": "12",
            "field_sources": {"count": {"source_id": 1, "quote": "count = 99 samples."}},
        }]}
        found = llm_shell.run_bundle(output, backend=BadBackend(response))
        self.assertEqual(stages(found)["interpret"], "rejected")
        self.assertFalse((output / "llm" / "audit.json").exists())

    def test_unknown_source_is_rejected(self):
        output = bundle(self.root)
        response = {"sample": [{
            "sample_id": "E1", "count": "12",
            "field_sources": {"count": {"source_id": 7, "quote": "count = 12 samples."}},
        }]}
        found = llm_shell.run_bundle(output, backend=BadBackend(response))
        self.assertEqual(stages(found)["interpret"], "rejected")

    def test_bad_audit_finding_rejects_only_audit(self):
        output = bundle(self.root)
        good = {"sample": [{
            "sample_id": "E1", "count": "12",
            "field_sources": {"count": {"source_id": 1, "quote": "count = 12 samples."}},
        }]}
        bad = {"findings": [{
            "table": "sample", "record_id": "E1", "field": "note",
            "rule": "required", "description": "Поле пусто.",
            "evidence": [{"source_id": 9, "page": 1}],
        }]}
        found = llm_shell.run_bundle(output, backend=StagedBackend(good, bad))
        self.assertEqual(stages(found)["interpret"], "ok")
        self.assertEqual(stages(found)["audit"], "rejected")
        self.assertEqual(found["status"], "rejected")

    def test_network_backend_is_refused(self):
        output = bundle(self.root)
        backend = BadBackend({})
        backend.network = True
        with self.assertRaises(ValueError):
            llm_shell.run_bundle(output, backend=backend)

    def test_input_budget_is_enforced_without_truncation(self):
        output = bundle(self.root)
        found = llm_shell.run_bundle(output, max_input_chars=10)
        self.assertEqual(stages(found)["interpret"], "rejected")
        interpretation = json.loads(
            (output / "llm" / "interpretation.json").read_text(encoding="utf-8"))
        self.assertIn("бюджет", interpretation["error"])

    def test_rerun_clears_stale_artifacts(self):
        output = bundle(self.root)
        llm_shell.run_bundle(output)
        self.assertTrue((output / "llm" / "audit.json").is_file())
        llm_shell.run_bundle(output, backend=BadBackend("not json"))
        self.assertFalse((output / "llm" / "audit.json").exists())
        self.assertTrue((output / "llm" / "interpretation.json").is_file())

    def test_solar_profile_records_request_and_skips_interpretation(self):
        data = temperature_pdf()
        document = prepare_pdf("D001.pdf", data)
        result = run(document, load_profile("solar"), mode="scripts")
        result["profile"] = load_profile("solar")
        output = self.root / "solar"
        export_result(result, output)

        found = llm_shell.run_bundle(output)
        self.assertEqual(stages(found)["interpret"], "skipped")
        self.assertEqual(stages(found)["audit"], "skipped")
        self.assertTrue((output / "llm" / "request.json").is_file())
        self.assertFalse((output / "llm" / "interpretation.json").exists())
        self.assertTrue(found["guarantees"]["passport_unchanged"])

    def test_bundle_must_be_a_scripts_passport(self):
        output = bundle(self.root)
        result = json.loads((output / "result.json").read_text(encoding="utf-8"))
        result["run"]["mode"] = "auto"
        (output / "result.json").write_text(json.dumps(result), encoding="utf-8")
        with self.assertRaises(ValueError):
            llm_shell.run_bundle(output)

    def test_missing_result_json_is_refused(self):
        empty = self.root / "empty"
        empty.mkdir()
        with self.assertRaises(ValueError):
            llm_shell.run_bundle(empty)


class SolarStateTest(unittest.TestCase):
    """Machine-readable solar scope/record state never invents a decision."""

    def test_skipped_interpretation_reports_no_scope_and_zero_counts(self):
        candidates = [{"field": "PCE", "raw_value": "16", "status": "candidate",
                       "context_status": "unresolved"},
                      {"field": "Voc", "raw_value": "1000", "status": "candidate",
                       "context_status": "unresolved"}]
        state = llm_shell.solar_state({"status": "skipped"}, candidates)
        self.assertEqual(state["interpretation_status"], "skipped")
        self.assertIsNone(state["in_scope"])
        self.assertIsNone(state["scope_reason"])
        self.assertEqual(state["scientific_record_counts"],
                         {table: 0 for table in llm_shell.SOLAR_RECORD_TABLES})
        self.assertEqual(state["candidate_count"], 2)
        self.assertEqual(state["unresolved_candidate_count"], 2)
        self.assertFalse(state["official_csv_written"])
        self.assertEqual(state["export_status"], "skipped")

    def test_rejected_interpretation_keeps_scope_unknown(self):
        state = llm_shell.solar_state({"status": "rejected", "error": "bad json"}, [])
        self.assertEqual(state["interpretation_status"], "rejected")
        self.assertIsNone(state["in_scope"])
        self.assertIsNone(state["scope_reason"])
        self.assertEqual(state["candidate_count"], 0)
        self.assertEqual(state["unresolved_candidate_count"], 0)

    def test_in_scope_publication_reports_record_counts(self):
        tables = {table: [] for table in TABLES}
        tables["publications"] = [
            {"publication_id": "D001", "in_scope": "true", "exclusion_reason": ""}]
        tables["materials"] = [{"material_id": "M1"}]
        tables["results"] = [{"metric_name": "PCE", "raw_value": "16"}]
        state = llm_shell.solar_state({"status": "ok", "tables": tables}, [])
        self.assertTrue(state["in_scope"])
        self.assertIsNone(state["scope_reason"])
        self.assertEqual(state["scientific_record_counts"],
                         {"materials": 1, "devices": 0, "layers": 0,
                          "measurements": 0, "results": 1})

    def test_excluded_publication_reports_its_reason(self):
        tables = {table: [] for table in TABLES}
        tables["publications"] = [{
            "publication_id": "D001", "in_scope": "false",
            "exclusion_reason": "Tandem device, not single-junction."}]
        state = llm_shell.solar_state({"status": "ok", "tables": tables}, [])
        self.assertFalse(state["in_scope"])
        self.assertEqual(state["scope_reason"], "Tandem device, not single-junction.")

    def test_scope_value_is_normalized_only_for_strings(self):
        for raw, expected in [(" True ", True), ("TRUE", True),
                              ("\tfalse\n", False), ("False", False)]:
            tables = {table: [] for table in TABLES}
            tables["publications"] = [{
                "publication_id": "D001", "in_scope": raw,
                "exclusion_reason": "  Out of scope.  " if expected is False else "",
            }]
            state = llm_shell.solar_state({"status": "ok", "tables": tables}, [])
            self.assertIs(state["in_scope"], expected, raw)
            if expected is False:
                self.assertEqual(state["scope_reason"], "Out of scope.")
            else:
                self.assertIsNone(state["scope_reason"])

    def test_non_string_scope_values_stay_unknown(self):
        for raw in (True, False, 1, 0, None):
            tables = {table: [] for table in TABLES}
            tables["publications"] = [{
                "publication_id": "D001", "in_scope": raw,
                "exclusion_reason": "Out of scope.",
            }]
            state = llm_shell.solar_state({"status": "ok", "tables": tables}, [])
            self.assertIsNone(state["in_scope"], raw)
            self.assertIsNone(state["scope_reason"], raw)

    def test_false_scope_reason_requires_a_nonempty_string(self):
        for raw in (None, 42, "", "   ", ["reason"]):
            tables = {table: [] for table in TABLES}
            tables["publications"] = [{
                "publication_id": "D001", "in_scope": "false",
                "exclusion_reason": raw,
            }]
            state = llm_shell.solar_state({"status": "ok", "tables": tables}, [])
            self.assertIs(state["in_scope"], False, raw)
            self.assertIsNone(state["scope_reason"], raw)

    def test_unresolved_candidates_are_counted_not_promoted(self):
        tables = {table: [] for table in TABLES}
        tables["publications"] = [
            {"publication_id": "D001", "in_scope": "true", "exclusion_reason": ""}]
        tables["devices"] = [{"device_id": "DV1", "active_area": "4"}]
        tables["results"] = [{"metric_name": "PCE", "raw_value": "16.0"}]
        before = deepcopy(tables)
        # The scripts pass only emits status="candidate"/context_status=
        # "unresolved"; the third item covers the branch for an explicitly
        # attributed state, which the shell itself never produces.
        candidates = [
            {"field": "PCE", "raw_value": "16", "status": "candidate",
             "context_status": "unresolved"},
            {"field": "devices.active_area", "raw_value": "4", "status": "candidate",
             "context_status": "unresolved"},
            {"field": "Voc", "raw_value": "1000", "status": "resolved",
             "context_status": "record"},
        ]
        state = llm_shell.solar_state({"status": "ok", "tables": tables}, candidates)
        self.assertEqual(state["candidate_count"], 3)
        self.assertEqual(state["unresolved_candidate_count"], 2)
        self.assertEqual(tables, before)
        self.assertNotIn("records", state)

    def test_candidates_never_become_records(self):
        tables = {table: [] for table in TABLES}
        tables["publications"] = [
            {"publication_id": "D001", "in_scope": "true", "exclusion_reason": ""}]
        before = deepcopy(tables)
        state = llm_shell.solar_state(
            {"status": "ok", "tables": tables},
            [{"field": "PCE", "raw_value": "16", "status": "candidate",
              "context_status": "unresolved"}])
        self.assertEqual(state["scientific_record_counts"],
                         {table: 0 for table in llm_shell.SOLAR_RECORD_TABLES})
        self.assertEqual(state["unresolved_candidate_count"], 1)
        self.assertEqual(tables, before)

    def test_malformed_candidates_are_unresolved_not_errors(self):
        tables = {table: [] for table in TABLES}
        state = llm_shell.solar_state(
            {"status": "ok", "tables": tables},
            [None, {"field": None}, {"field": "PCE", "raw_value": 16}])
        self.assertEqual(state["unresolved_candidate_count"], 3)

    def test_export_status_is_never_reported_as_written(self):
        state = llm_shell.solar_state({"status": "skipped"}, [], export_status="skipped")
        self.assertFalse(state["official_csv_written"])


class SolarManifestTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_manifest_and_summary_carry_solar_state(self):
        output, candidate = solar_bundle(self.root)
        before = (output / "result.json").read_bytes()
        found = llm_shell.run_bundle(
            output, mode="api", allow_network=True,
            backend=StagedBackend(candidate, {"findings": []}))
        self.assertEqual(stages(found)["interpret"], "ok")

        solar = found["solar"]
        self.assertTrue(solar["in_scope"])
        self.assertIsNone(solar["scope_reason"])
        self.assertEqual(solar["scientific_record_counts"]["results"], 2)
        self.assertEqual(solar["scientific_record_counts"]["devices"], 1)
        self.assertEqual(solar["candidate_count"],
                         len(json.loads(before)["candidates"]))
        self.assertEqual(solar["unresolved_candidate_count"], solar["candidate_count"])
        self.assertFalse(solar["official_csv_written"])
        self.assertEqual(solar["export_status"], "skipped")
        self.assertEqual(manifest(output)["solar"], solar)
        self.assertEqual(llm_shell.summarize(found)["solar"], solar)
        self.assertEqual((output / "result.json").read_bytes(), before)

    def test_mock_solar_skip_reports_unverified_scope(self):
        document, _ = solar_document_and_candidate()
        profile = load_profile("solar")
        result = run(document, profile, mode="scripts")
        result["profile"] = profile
        output = self.root / "solar_skip"
        export_result(result, output)

        found = llm_shell.run_bundle(output)
        solar = found["solar"]
        self.assertEqual(solar["interpretation_status"], "skipped")
        self.assertIsNone(solar["in_scope"])
        self.assertIsNone(solar["scope_reason"])
        self.assertEqual(solar["scientific_record_counts"],
                         {table: 0 for table in llm_shell.SOLAR_RECORD_TABLES})
        self.assertEqual(solar["unresolved_candidate_count"], solar["candidate_count"])
        self.assertFalse(solar["official_csv_written"])

    def test_fields_profile_manifest_has_no_solar_block(self):
        output = bundle(self.root)
        found = llm_shell.run_bundle(output)
        self.assertNotIn("solar", found)
        self.assertNotIn("solar", llm_shell.summarize(found))

    def test_summarize_accepts_manifest_without_solar(self):
        legacy = {"status": "ok", "stages": [{"name": "export", "status": "skipped"}],
                  "counts": {}, "mode": "mock", "backend": {"name": "x", "network": False}}
        summary = llm_shell.summarize(legacy)
        self.assertEqual(set(summary), {"status", "stages", "counts", "mode", "backend"})


class SolarAttributionMetadataTest(unittest.TestCase):
    """The attribution inventory is additive metadata, never a verdict."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def run_with(self, name, candidate=None):
        output, built = solar_bundle(self.root, name=name)
        if candidate is None:
            candidate = built
        return output, llm_shell.run_bundle(
            output, mode="api", allow_network=True,
            backend=StagedBackend(candidate, {"findings": []}))

    def interpretation(self, output):
        return json.loads(
            (output / "llm" / "interpretation.json").read_text(encoding="utf-8"))

    def test_inventory_is_additive_and_leaves_tables_unchanged(self):
        output, found = self.run_with("solar_attribution")
        interpretation = self.interpretation(output)
        self.assertIn("attribution", interpretation)
        inventory = interpretation["attribution"]
        self.assertEqual(inventory["state"], "unverified")
        self.assertTrue(inventory["requires_review"])
        self.assertEqual(inventory["counts"]["by_field"]["layers.annealing_temperature"], 0)
        self.assertIsInstance(inventory["rows"], list)

        # Every other part of the interpretation is exactly what a legacy
        # finalize (no inventory) produced.
        original = llm_shell.finalize

        def legacy(candidate, document, *, model=""):
            result = original(candidate, document, model=model)
            result["extraction"].pop("attribution", None)
            return result

        llm_shell.finalize = legacy
        try:
            plain_output, plain_found = self.run_with("solar_attribution_legacy")
        finally:
            llm_shell.finalize = original

        plain = self.interpretation(plain_output)
        self.assertNotIn("attribution", plain)
        for key in ("status", "adapter", "tables", "records", "issues", "counts"):
            self.assertEqual(interpretation[key], plain[key], key)
        self.assertEqual(found["solar"]["scientific_record_counts"],
                         plain_found["solar"]["scientific_record_counts"])
        self.assertEqual(found["counts"], plain_found["counts"])

    def test_manifest_exposes_attribution_state_and_invariants(self):
        _, found = self.run_with("solar_attribution_manifest")
        solar = found["solar"]
        self.assertEqual(solar["attribution_state"], "unverified")
        self.assertEqual(solar["attribution_counts"]["rows"],
                         len(self.interpretation(
                             self.root / "solar_attribution_manifest")["attribution"]["rows"]))
        self.assertTrue(solar["requires_review"])
        self.assertEqual(solar["scientific_binding_verification"], "not_verified")
        self.assertEqual(solar["scientific_completeness_verification"], "not_verified")
        # Persisted manifest and summarize preserve the same additive keys.
        self.assertEqual(manifest(self.root / "solar_attribution_manifest")["solar"], solar)
        self.assertEqual(llm_shell.summarize(found)["solar"], solar)

    def test_empty_inventory_is_not_a_verification(self):
        # Clear every sensitive field, including aggregation, so the inventory
        # is empty. That is still not evidence of scientific correctness.
        _, candidate = solar_document_and_candidate()
        candidate = deepcopy(candidate)
        for row in candidate["results"]:
            row["aggregation"] = ""
        _, found = self.run_with("solar_attribution_empty", candidate)
        solar = found["solar"]
        self.assertEqual(solar["attribution_state"], "none")
        self.assertTrue(solar["requires_review"])
        self.assertEqual(solar["scientific_binding_verification"], "not_verified")
        self.assertEqual(solar["scientific_completeness_verification"], "not_verified")

    def test_sensitive_fields_are_counted_without_touching_values(self):
        _, candidate = solar_document_and_candidate()
        candidate = deepcopy(candidate)
        candidate["layers"][0]["annealing_temperature"] = "150"
        candidate["layers"][0]["annealing_time"] = "30 min"
        candidate["results"][0]["n_devices"] = "20"
        candidate["results"][0]["spread_value"] = "0.4"
        output, found = self.run_with("solar_attribution_fields", candidate)

        self.assertEqual(stages(found)["interpret"], "ok")
        interpretation = self.interpretation(output)
        inventory = interpretation["attribution"]
        self.assertEqual(inventory["state"], "unverified")
        self.assertEqual(inventory["counts"]["by_field"]["layers.annealing_temperature"], 1)
        self.assertEqual(inventory["counts"]["by_field"]["layers.annealing_time"], 1)
        self.assertEqual(inventory["counts"]["by_field"]["results.n_devices"], 1)
        self.assertEqual(inventory["counts"]["by_field"]["results.aggregation"], 2)
        # The inventory is taken before normalize, so the spread claim the model
        # actually made is counted even though the normalized table no longer
        # holds it in spread_value.
        self.assertEqual(inventory["counts"]["by_field"]["results.spread_value"], 1)
        spread_rows = [
            row for row in inventory["rows"]
            if row["table"] == "results" and "spread_value" in row["fields"]
        ]
        self.assertEqual(len(spread_rows), 1)
        self.assertEqual(spread_rows[0]["key"], "R2")
        # The inventory lists field names only; the cell values stay in the
        # tables untouched, and nothing is blanked or demoted.
        self.assertNotIn("150", json.dumps(inventory, ensure_ascii=False))
        self.assertNotIn("20", json.dumps(inventory["rows"], ensure_ascii=False))
        self.assertNotIn("0.4", json.dumps(inventory, ensure_ascii=False))
        self.assertEqual(
            interpretation["tables"]["layers"][0]["annealing_temperature"], "150")
        self.assertEqual(interpretation["tables"]["results"][0]["n_devices"], "20")
        # finalize still moves the printed spread into note; this shell does not
        # restore it and does not rewrite the note the validator produced.
        self.assertEqual(interpretation["tables"]["results"][0]["spread_value"], "")
        self.assertIn("Original spread: 0.4 %",
                      interpretation["tables"]["results"][0]["note"])
        self.assertEqual(found["solar"]["attribution_state"], "unverified")
        self.assertEqual(found["solar"]["attribution_counts"]["rows"],
                         inventory["counts"]["rows"])

    def test_legacy_finalize_without_inventory_is_unknown_and_needs_review(self):
        original = llm_shell.finalize

        def legacy(candidate, document, *, model=""):
            result = original(candidate, document, model=model)
            result["extraction"].pop("attribution", None)
            return result

        llm_shell.finalize = legacy
        try:
            output, found = self.run_with("solar_attribution_no_meta")
        finally:
            llm_shell.finalize = original

        self.assertEqual(stages(found)["interpret"], "ok")
        self.assertNotIn("attribution", self.interpretation(output))
        solar = found["solar"]
        self.assertEqual(solar["attribution_state"], "unknown")
        self.assertEqual(solar["attribution_counts"], {})
        self.assertTrue(solar["requires_review"])
        self.assertEqual(solar["scientific_binding_verification"], "not_verified")
        self.assertEqual(solar["scientific_completeness_verification"], "not_verified")

    def test_rejected_interpretation_stays_rejected_with_unknown_attribution(self):
        _, candidate = solar_document_and_candidate()
        candidate = deepcopy(candidate)
        candidate["results"][0]["source_ids"] = [99]
        output, found = self.run_with("solar_attribution_rejected", candidate)

        self.assertEqual(stages(found)["interpret"], "rejected")
        self.assertNotIn("attribution", self.interpretation(output))
        solar = found["solar"]
        self.assertEqual(solar["interpretation_status"], "rejected")
        self.assertEqual(solar["attribution_state"], "unknown")
        self.assertTrue(solar["requires_review"])
        self.assertFalse(solar["official_csv_written"])
        self.assertEqual(solar["scientific_record_counts"],
                         {table: 0 for table in llm_shell.SOLAR_RECORD_TABLES})

    def test_skipped_mock_interpretation_reports_unknown_attribution(self):
        output, _ = solar_bundle(self.root, name="solar_attribution_skip")
        found = llm_shell.run_bundle(output)
        self.assertEqual(stages(found)["interpret"], "skipped")
        solar = found["solar"]
        self.assertEqual(solar["attribution_state"], "unknown")
        self.assertEqual(solar["attribution_counts"], {})
        self.assertTrue(solar["requires_review"])

    def test_attribution_summary_tolerates_malformed_inventory(self):
        self.assertEqual(llm_shell._attribution_summary(None), ("unknown", {}, True))
        self.assertEqual(llm_shell._attribution_summary("bad"), ("unknown", {}, True))
        self.assertEqual(
            llm_shell._attribution_summary({"state": "verified", "counts": []}),
            ("unknown", {}, True))
        state, counts, review = llm_shell._attribution_summary(
            {"state": "none", "counts": {"rows": 0}, "requires_review": False})
        # state "none" is forwarded, but review is never derived from it and a
        # claimed False is not trusted: the shell's binding stays unverified.
        self.assertEqual((state, counts, review), ("none", {"rows": 0}, True))


def d016_document():
    """D016-like document: a simulated stack with literal layer, condition and metric text."""
    return {
        "publication_id": "D016", "sha256": "d016", "pages": 2, "warnings": [],
        "sources": [
            {"id": 1, "page": 1, "kind": "text",
             "quote": "The stack uses a SnO2 electron transport layer of 30 nm on FTO glass."},
            {"id": 2, "page": 2, "kind": "text",
             "quote": "Simulated devices were evaluated under AM1.5G at 100 mW/cm2; PCE of 22.4 %."},
        ],
    }


def anchor(source_id, page, quote):
    return {"source_id": source_id, "page": page, "quote": quote}


def d016_semantic():
    """A semantic block where every field and claim has its own literal anchor."""
    return {
        "publication_id": "D016",
        "study_type": "simulation",
        "data_provenance": "simulated",
        "thematic_relevance": "relevant",
        "scope_reason": "Simulation of a thin-film solar cell.",
        "metadata_evidence": {
            "study_type": anchor(2, 2, "Simulated devices"),
            "data_provenance": anchor(2, 2, "Simulated devices"),
            "thematic_relevance": anchor(1, 1, "SnO2 electron transport layer"),
            "scope_reason": anchor(2, 2, "Simulated devices were evaluated"),
        },
        "records": {
            "layers": [{
                "name": "SnO2 electron transport layer", "role": "electron transport",
                "material": "SnO2", "thickness": "30", "thickness_unit": "nm",
                "field_sources": {
                    "name": anchor(1, 1, "SnO2 electron transport layer"),
                    "role": anchor(1, 1, "electron transport layer"),
                    "material": anchor(1, 1, "SnO2"),
                    "thickness": anchor(1, 1, "of 30 nm"),
                    "thickness_unit": anchor(1, 1, "of 30 nm"),
                },
            }],
            "conditions": [{
                "name": "AM1.5G", "value": "100", "unit": "mW/cm2",
                "field_sources": {
                    "name": anchor(2, 2, "under AM1.5G"),
                    "value": anchor(2, 2, "at 100 mW/cm2"),
                    "unit": anchor(2, 2, "at 100 mW/cm2"),
                },
            }],
            "metrics": [{
                "name": "PCE", "value": "22.4", "unit": "%",
                "carrier": "official", "page": 9,
                "field_sources": {
                    "name": anchor(2, 2, "PCE of 22.4 %"),
                    "value": anchor(2, 2, "PCE of 22.4 %"),
                    "unit": anchor(2, 2, "PCE of 22.4 %"),
                },
            }],
        },
    }


class SolarSemanticTest(unittest.TestCase):
    """The optional semantic block is gated on literal per-field evidence."""

    def test_simulated_semantic_record_is_accepted_with_literal_evidence(self):
        semantic = llm_shell.validate_solar_semantic(
            {"semantic": d016_semantic()}, d016_document())
        self.assertEqual(semantic["schema_version"], "solar-semantic-1")
        self.assertEqual(semantic["publication_id"], "D016")
        self.assertEqual(semantic["status"], "ok")
        self.assertEqual(semantic["study_type"], "simulation")
        self.assertEqual(semantic["data_provenance"], "simulated")
        self.assertEqual(semantic["thematic_relevance"], "relevant")
        self.assertEqual(semantic["scope_reason"], "Simulation of a thin-film solar cell.")
        self.assertEqual(semantic["errors"], [])
        self.assertEqual(set(semantic["metadata_evidence"]),
                         {"study_type", "data_provenance", "thematic_relevance", "scope_reason"})

        metric = semantic["records"]["metrics"][0]
        # carrier and page are code-owned; the model values are ignored.
        self.assertEqual(metric["carrier"], "semantic_only")
        self.assertEqual(metric["page"], 2)
        self.assertEqual(metric["field_sources"]["value"]["quote"], "PCE of 22.4 %")
        self.assertEqual(metric["field_sources"]["unit"]["source_id"], 2)
        layer = semantic["records"]["layers"][0]
        self.assertEqual(layer["carrier"], "semantic_only")
        self.assertEqual(layer["page"], 1)
        self.assertEqual(layer["field_sources"]["thickness"]["quote"], "of 30 nm")
        self.assertEqual(len(semantic["records"]["conditions"]), 1)

    def test_malformed_semantic_records_are_quarantined_not_fatal(self):
        block = d016_semantic()
        block["study_type"] = "guess"
        block["records"]["metrics"] = [
            block["records"]["metrics"][0],
            # Page does not match the cited source.
            {"name": "PCE", "value": "22.4", "unit": "%", "field_sources": {
                "name": anchor(2, 1, "PCE of 22.4 %"),
                "value": anchor(2, 1, "PCE of 22.4 %"),
                "unit": anchor(2, 1, "PCE of 22.4 %")}},
            # Quote is not a literal substring of the source.
            {"name": "PCE", "value": "31", "unit": "%", "field_sources": {
                "name": anchor(1, 1, "PCE of 31 %"),
                "value": anchor(1, 1, "PCE of 31 %"),
                "unit": anchor(1, 1, "PCE of 31 %")}},
            # Unknown source id.
            {"name": "PCE", "value": "32", "unit": "%", "field_sources": {
                "name": anchor(7, 1, "PCE of 32 %")}},
            # Empty required value.
            {"name": "PCE", "value": "", "unit": "%", "field_sources": {
                "name": anchor(2, 2, "PCE of 22.4 %")}},
            # A field without its own anchor.
            {"name": "PCE", "value": "22.4", "unit": "%", "field_sources": {
                "value": anchor(2, 2, "PCE of 22.4 %")}},
            # Not an object.
            "not-a-record",
        ]
        semantic = llm_shell.validate_solar_semantic({"semantic": block}, d016_document())
        self.assertEqual(semantic["status"], "partial")
        self.assertEqual(semantic["study_type"], "unknown")
        self.assertEqual([record["name"] for record in semantic["records"]["metrics"]],
                         ["PCE"])
        self.assertGreaterEqual(len(semantic["errors"]), 6)
        self.assertTrue(all("error" in error for error in semantic["errors"]))

    def test_value_needs_its_own_literal_token(self):
        block = d016_semantic()
        block["records"]["metrics"] = [
            # The anchor is literal, but it prints 22.4, not 224.
            {"name": "PCE", "value": "224", "unit": "%", "field_sources": {
                "name": anchor(2, 2, "PCE of 22.4 %"),
                "value": anchor(2, 2, "PCE of 22.4 %"),
                "unit": anchor(2, 2, "PCE of 22.4 %")}},
            # A unit that is not printed in its own anchor.
            {"name": "PCE", "value": "22.4", "unit": "mV", "field_sources": {
                "name": anchor(2, 2, "PCE of 22.4 %"),
                "value": anchor(2, 2, "PCE of 22.4 %"),
                "unit": anchor(2, 2, "PCE of 22.4 %")}},
            # A name that is only paraphrased, not printed.
            {"name": "power conversion efficiency", "value": "22.4", "unit": "%",
             "field_sources": {
                 "name": anchor(2, 2, "PCE of 22.4 %"),
                 "value": anchor(2, 2, "PCE of 22.4 %"),
                 "unit": anchor(2, 2, "PCE of 22.4 %")}},
        ]
        semantic = llm_shell.validate_solar_semantic({"semantic": block}, d016_document())
        self.assertEqual(semantic["status"], "partial")
        self.assertEqual(semantic["records"]["metrics"], [])
        self.assertEqual(len(semantic["errors"]), 3)

    def test_required_fields_and_their_units_are_enforced(self):
        block = d016_semantic()
        block["records"]["layers"] = [
            # A layer needs a material.
            {"name": "SnO2 electron transport layer", "role": "electron transport",
             "material": "", "thickness": "30", "thickness_unit": "nm",
             "field_sources": {
                 "name": anchor(1, 1, "SnO2 electron transport layer"),
                 "thickness": anchor(1, 1, "of 30 nm"),
                 "thickness_unit": anchor(1, 1, "of 30 nm")}},
            # A thickness without its printed unit is not accepted.
            {"name": "SnO2 electron transport layer", "material": "SnO2",
             "thickness": "30", "thickness_unit": "mm",
             "field_sources": {
                 "name": anchor(1, 1, "SnO2 electron transport layer"),
                 "material": anchor(1, 1, "SnO2"),
                 "thickness": anchor(1, 1, "of 30 nm"),
                 "thickness_unit": anchor(1, 1, "of 30 nm")}},
        ]
        block["records"]["conditions"] = [
            # A condition needs a nonempty value.
            {"name": "AM1.5G", "value": "", "unit": "mW/cm2",
             "field_sources": {"name": anchor(2, 2, "under AM1.5G")}},
        ]
        semantic = llm_shell.validate_solar_semantic({"semantic": block}, d016_document())
        self.assertEqual(semantic["records"]["layers"], [])
        self.assertEqual(semantic["records"]["conditions"], [])
        self.assertEqual(len(semantic["errors"]), 3)
        self.assertEqual(semantic["status"], "partial")

    def test_claims_without_metadata_evidence_are_unknown(self):
        block = d016_semantic()
        block.pop("metadata_evidence")
        semantic = llm_shell.validate_solar_semantic({"semantic": block}, d016_document())
        self.assertEqual(semantic["status"], "partial")
        for field in ("study_type", "data_provenance", "thematic_relevance"):
            self.assertEqual(semantic[field], "unknown", field)
        self.assertIsNone(semantic["scope_reason"])
        self.assertEqual(semantic["metadata_evidence"], {})
        self.assertEqual(len(semantic["errors"]), 4)
        # Records are independent of the claims and stay accepted.
        self.assertEqual(len(semantic["records"]["metrics"]), 1)

    def test_claim_anchor_must_support_the_value(self):
        block = d016_semantic()
        block["data_provenance"] = "measured"
        semantic = llm_shell.validate_solar_semantic({"semantic": block}, d016_document())
        self.assertEqual(semantic["study_type"], "simulation")
        self.assertEqual(semantic["data_provenance"], "unknown")
        self.assertEqual(semantic["thematic_relevance"], "relevant")
        self.assertEqual(semantic["status"], "partial")
        self.assertNotIn("data_provenance", semantic["metadata_evidence"])

    def test_explicit_unknown_needs_no_metadata_evidence(self):
        block = d016_semantic()
        block["metadata_evidence"] = {}
        block["scope_reason"] = ""
        for field in ("study_type", "data_provenance", "thematic_relevance"):
            block[field] = "unknown"
        semantic = llm_shell.validate_solar_semantic({"semantic": block}, d016_document())
        self.assertEqual(semantic["status"], "ok")
        self.assertEqual(semantic["study_type"], "unknown")
        self.assertIsNone(semantic["scope_reason"])
        self.assertEqual(semantic["errors"], [])

    def test_absent_semantic_is_missing_and_empty(self):
        for candidate in ({}, {"semantic": None}):
            semantic = llm_shell.validate_solar_semantic(candidate, d016_document())
            self.assertEqual(semantic["status"], "missing")
            self.assertEqual(semantic["records"],
                             {kind: [] for kind in llm_shell.SEMANTIC_RECORD_KINDS})
            self.assertEqual(semantic["metadata_evidence"], {})
            self.assertEqual(semantic["errors"], [])
            # No block means no claim at all, so the fields stay unset instead
            # of reporting a model "unknown".
            self.assertIsNone(semantic["study_type"])
            self.assertIsNone(semantic["data_provenance"])
            self.assertIsNone(semantic["thematic_relevance"])

    def test_unusable_semantic_block_is_rejected_without_raising(self):
        blocks = ("text", [], 5, {**d016_semantic(), "publication_id": "D999"})
        for block in blocks:
            semantic = llm_shell.validate_solar_semantic({"semantic": block}, d016_document())
            self.assertEqual(semantic["status"], "rejected", block)
            self.assertEqual(semantic["records"],
                             {kind: [] for kind in llm_shell.SEMANTIC_RECORD_KINDS})
            self.assertTrue(semantic["errors"])


class SolarSemanticBundleTest(unittest.TestCase):
    """run_bundle writes semantic.json without touching tables, export or audit."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def bundle_semantic(self):
        return {
            "publication_id": "D001", "study_type": "simulation",
            "data_provenance": "simulated", "thematic_relevance": "relevant",
            "scope_reason": "Simulated stack.",
            "metadata_evidence": {
                "study_type": anchor(4, 1, "Simulated stack"),
                "data_provenance": anchor(4, 1, "Simulated stack"),
                "thematic_relevance": anchor(1, 1, "perovskite Eg 1.5 eV"),
                "scope_reason": anchor(4, 1, "Simulated stack"),
            },
            "records": {
                "layers": [{
                    "name": "perovskite", "role": "", "material": "perovskite",
                    "thickness": "0.5", "thickness_unit": "um",
                    "field_sources": {
                        "name": anchor(1, 1, "perovskite Eg 1.5 eV"),
                        "material": anchor(1, 1, "perovskite Eg 1.5 eV"),
                        "thickness": anchor(1, 1, "layer 0.5 um"),
                        "thickness_unit": anchor(1, 1, "layer 0.5 um"),
                    },
                }],
                "conditions": [],
                "metrics": [{
                    "name": "PCE", "value": "16", "unit": "%",
                    "field_sources": {
                        "name": anchor(2, 1, "PCE = 16 %"),
                        "value": anchor(2, 1, "PCE = 16 %"),
                        "unit": anchor(2, 1, "PCE = 16 %"),
                    },
                }],
            },
        }

    def test_run_bundle_writes_semantic_artifact_outside_tables(self):
        output, candidate = solar_bundle(
            self.root, name="solar_semantic", extra_sources=[SIMULATION_SOURCE])
        candidate = deepcopy(candidate)
        candidate["semantic"] = self.bundle_semantic()
        before = (output / "result.json").read_bytes()
        found = llm_shell.run_bundle(
            output, mode="api", allow_network=True,
            backend=StagedBackend(candidate, {"findings": []}))
        self.assertEqual(stages(found)["interpret"], "ok")

        semantic = json.loads((output / "llm" / "semantic.json").read_text(encoding="utf-8"))
        self.assertEqual(semantic["status"], "ok")
        self.assertEqual(semantic["publication_id"], "D001")
        self.assertEqual(semantic["data_provenance"], "simulated")
        metric = semantic["records"]["metrics"][0]
        self.assertEqual(metric["carrier"], "semantic_only")
        self.assertEqual(metric["field_sources"]["value"]["quote"], "PCE = 16 %")
        self.assertEqual(found["solar"]["semantic_status"], "ok")
        self.assertEqual(found["solar"]["semantic_counts"],
                         {"layers": 1, "conditions": 0, "metrics": 1, "errors": 0})
        self.assertEqual(manifest(output)["solar"]["semantic_status"], "ok")
        self.assertIn("semantic.json", found["artifacts"])

        interpretation = json.loads(
            (output / "llm" / "interpretation.json").read_text(encoding="utf-8"))
        self.assertNotIn("semantic", interpretation)
        self.assertNotIn("semantic_only", json.dumps(interpretation))
        self.assertEqual(len(interpretation["tables"]["results"]), 2)
        self.assertEqual((output / "result.json").read_bytes(), before)
        self.assertFalse(found["guarantees"]["official_csv_written"])
        self.assertFalse((output / "llm" / "results.csv").exists())

    def test_missing_semantic_stays_backward_compatible(self):
        output, candidate = solar_bundle(self.root, name="solar_missing")
        found = llm_shell.run_bundle(
            output, mode="api", allow_network=True,
            backend=StagedBackend(candidate, {"findings": []}))
        semantic = json.loads((output / "llm" / "semantic.json").read_text(encoding="utf-8"))
        self.assertEqual(semantic["status"], "missing")
        self.assertEqual(semantic["records"],
                         {kind: [] for kind in llm_shell.SEMANTIC_RECORD_KINDS})
        self.assertEqual(found["solar"]["semantic_status"], "missing")
        self.assertEqual(found["solar"]["semantic_counts"],
                         {"layers": 0, "conditions": 0, "metrics": 0, "errors": 0})
        self.assertEqual(stages(found)["interpret"], "ok")

    def test_prompt_requests_optional_semantic_block(self):
        output, candidate = solar_bundle(self.root, name="solar_prompt")
        llm_shell.run_bundle(
            output, mode="api", allow_network=True,
            backend=StagedBackend(candidate, {"findings": []}))
        request = json.loads((output / "llm" / "request.json").read_text(encoding="utf-8"))
        content = "\n".join(message["content"] for message in request["messages"])
        self.assertIn("semantic", content)
        self.assertIn("semantic-only", content)
        self.assertIn("metadata_evidence", content)
        self.assertIn("field_sources", content)
        self.assertIn("MUST NOT be added to the seven official CSV", content)
        self.assertIn("as a complete numeric", content)
        self.assertIn("12 is not 112", content)


class SolarPromptContractTest(unittest.TestCase):
    """The solar prompt must ask for an unambiguous eight-key envelope."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def solar_content(self):
        output, candidate = solar_bundle(self.root, name="solar_contract")
        llm_shell.run_bundle(
            output, mode="api", allow_network=True,
            backend=StagedBackend(candidate, {"findings": []}))
        request = json.loads((output / "llm" / "request.json").read_text(encoding="utf-8"))
        return "\n".join(message["content"] for message in request["messages"])

    def test_prompt_requires_eight_keys_without_seven_only_conflict(self):
        content = self.solar_content()
        # The system prompt still opens with the base seven-table instruction,
        # but the appended contract overrides it explicitly.
        self.assertIn("Return ONLY a JSON object with the seven tables", content)
        self.assertIn("REQUIRED RESPONSE ENVELOPE", content)
        self.assertIn("supersedes any earlier instruction", content)
        self.assertIn("EIGHT top-level keys", content)
        self.assertIn("plus \"semantic\"", content)
        self.assertIn("ALWAYS present", content)

    def test_prompt_no_longer_offers_semantic_as_optional(self):
        content = self.solar_content()
        lowered = content.lower()
        # The old wording produced a real smoke run with no semantic block.
        self.assertNotIn("optional semantic layer", lowered)
        self.assertNotIn("you may add one top-level key", lowered)
        self.assertNotIn("omit \"semantic\"", lowered)
        self.assertIn("is an incomplete answer", content)

    def test_prompt_keeps_semantic_for_out_of_scope_simulations(self):
        content = self.solar_content()
        self.assertIn("out of scope for experimental solar CSV", content)
        self.assertIn("STILL required", content)
        self.assertIn("simulated layers, conditions and metrics", content)
        self.assertIn("Never fabricate rows", content)
        self.assertIn("never estimate", content)

    def test_audit_prompt_separates_scope_from_extracted_booleans(self):
        # Prompt lines wrap; compare against normalized whitespace.
        content = " ".join(llm_shell.AUDIT_SYSTEM.split())
        self.assertIn("Extracted experimental booleans", content)
        self.assertIn("measurements.mask_used", content)
        self.assertIn("publications.in_scope is NOT an extracted experimental boolean", content)
        self.assertIn("eligibility judgement", content)
        self.assertIn("Do NOT report a finding merely because the PDF does not print", content)
        # Other booleans stay under review.
        self.assertIn("Keep reviewing these booleans as before", content)


class SolarSourceFidelityTest(unittest.TestCase):
    """Sources are stored line fragments; the prompt must not rebuild them."""

    def instruction(self):
        # The instruction is appended to the solar system message; normalize
        # whitespace because the prompt text wraps.
        return " ".join(llm_shell.SEMANTIC_INSTRUCTION.split())

    def test_sources_are_line_fragments_never_reassembled(self):
        content = self.instruction()
        self.assertIn("SOURCES ARE LINE FRAGMENTS, NOT SENTENCES", content)
        self.assertIn("one stored line or fragment", content)
        self.assertIn("Never join two source ids into one quote", content)
        self.assertIn("never reorder fragments to rebuild a sentence", content)
        self.assertIn("never continue a quote across a line break or a hyphen", content)
        self.assertIn("two different stored texts", content)

    def test_stored_text_defects_are_preserved_not_repaired(self):
        content = self.instruction()
        self.assertIn("Copy the stored characters exactly", content)
        self.assertIn("OCR and font defects", content)
        self.assertIn("missing spaces, split ligatures and odd spellings", content)
        self.assertIn("Do NOT repair the text", content)
        self.assertIn("never restore a ligature", content)
        self.assertIn("never correct a spelling", content)
        self.assertIn("never merge a broken word", content)

    def test_quote_is_shortest_from_one_id_with_printed_spelling(self):
        content = self.instruction()
        self.assertIn("SHORTEST exact quote from ONE source id", content)
        self.assertIn("give that field its own anchor to its own fragment", content)
        self.assertIn("no anchor may borrow text from another id", content)
        self.assertIn("not with a normalized vocabulary term", content)
        self.assertIn("normalization is applied by code", content)

    def test_mirrored_axis_label_is_copied_or_unknown(self):
        content = self.instruction()
        self.assertIn("mirrored, garbled or unreadable axis label", content)
        self.assertIn("copy the stored characters when they are legible", content)
        self.assertIn("leave the field unknown", content)
        self.assertIn("never reconstruct a plausible label", content)
        self.assertIn("from the surrounding figure", content)

    def test_optional_fields_may_stay_empty_but_required_are_never_invented(self):
        content = self.instruction()
        self.assertIn("Optional fields (role, thickness, unit and similar) may stay empty", content)
        self.assertIn("A required field - layer material, condition name and value", content)
        self.assertIn("must never be invented", content)
        self.assertIn("skip that record instead of filling the gap", content)

    def test_instruction_has_no_document_specific_values(self):
        content = llm_shell.SEMANTIC_INSTRUCTION
        # The remedy is generic: no smoke-run identifiers or page numbers.
        self.assertNotIn("D016", content)
        self.assertNotIn("page 7", content)
        self.assertNotRegex(content, r"\bDV\d+\b")
        self.assertNotRegex(content, r"\bM\d+\b")


class SolarTableBindingTest(unittest.TestCase):
    """The shell prompt binds each official value to its printed entity."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def binding(self):
        # Prompt lines wrap; compare against normalized whitespace.
        return " ".join(llm_shell.SEMANTIC_INSTRUCTION.split())

    def test_instruction_reaches_the_solar_request(self):
        output, candidate = solar_bundle(self.root, name="solar_binding")
        llm_shell.run_bundle(
            output, mode="api", allow_network=True,
            backend=StagedBackend(candidate, {"findings": []}))
        request = json.loads((output / "llm" / "request.json").read_text(encoding="utf-8"))
        content = "\n".join(message["content"] for message in request["messages"])
        self.assertIn("SCIENTIFIC BINDING FOR THE SEVEN OFFICIAL TABLES", content)
        self.assertIn("materials.csv holds absorber materials only", content)

    def test_materials_hold_absorbers_and_transport_materials_live_in_layers(self):
        content = self.binding()
        self.assertIn("materials.csv holds absorber materials only", content)
        self.assertIn("Transport, contact, electrode, buffer, passivation and interlayer materials", content)
        self.assertIn("are not absorber rows", content)
        self.assertIn("as the printed layer_material of its own layer row", content)
        self.assertIn("never create a materials row just to give a transport layer a place", content)

    def test_result_material_references_the_absorber_of_its_device(self):
        content = self.binding()
        self.assertIn("Every results.material_id must reference the absorber of that result's device", content)
        self.assertIn("must equal devices.absorber_material_id for the same device_id", content)
        self.assertIn("leave material_id empty instead of picking a material", content)

    def test_processing_conditions_need_explicit_attribution(self):
        content = self.binding()
        self.assertIn("belong to the layer or device whose fabrication step the source names", content)
        self.assertIn("Never copy a condition from a neighbouring step", content)
        self.assertIn("from one member of a control/target pair onto the other row", content)
        self.assertIn("A condition needs explicit attribution to that entity in the source", content)
        self.assertIn("A shared process is shared only when the text says it applies to those entities", content)

    def test_group_counts_and_aggregation_are_not_propagated(self):
        content = self.binding()
        self.assertIn("n_devices, spread_value and aggregation describe one specific result", content)
        self.assertIn("is not n_devices for every row", content)
        self.assertIn("a batch size, a group size or an average over an unrelated set", content)
        self.assertIn("leave aggregation empty", content)

    def test_raw_values_are_immutable_and_normalization_is_code_only(self):
        content = self.binding()
        self.assertIn("raw_value, raw_unit and source_fragment are immutable printed text", content)
        self.assertIn("never normalize, convert or round them", content)
        self.assertIn("Normalization is applied by code afterwards", content)

    def test_dictionary_label_is_not_required_as_literal_evidence(self):
        content = self.binding()
        self.assertIn("is a controlled vocabulary term, not a quote", content)
        self.assertIn("does NOT have to appear literally in the source", content)
        self.assertIn("put the original printed wording into note of the same row", content)
        self.assertIn("the evidence quote keeps the printed wording", content)
        self.assertIn("Do not put a dictionary term into source_fragment", content)
        # The semantic block keeps its own printed-spelling rule, and the prompt
        # says so instead of contradicting the table rule.
        self.assertIn("This printed-spelling rule is specific to the semantic block", content)

    def test_table_cell_stays_bound_to_its_header_and_row(self):
        content = self.binding()
        self.assertIn("must stay bound to its own column header and row label", content)
        self.assertIn("read the cell together with its header", content)
        self.assertIn("never move a number to another column, another row or another device", content)
        self.assertIn("leave that value out instead of guessing where it belongs", content)

    def test_literature_and_duplicate_distinctions_are_retained(self):
        content = self.binding()
        self.assertIn('result_origin "cited", empty device_id and measurement_id', content)
        self.assertIn("reference marker in cited_publication_id", content)
        self.assertIn("duplicate_of pointing at the original result_id", content)
        self.assertIn("a different value under the same conditions is NOT a duplicate", content)

    def test_binding_section_has_no_document_specific_values(self):
        content = llm_shell.SEMANTIC_INSTRUCTION
        start = content.index("SCIENTIFIC BINDING FOR THE SEVEN OFFICIAL TABLES")
        section = content[start:]
        self.assertNotIn("D002", section)
        self.assertNotIn("D016", section)
        self.assertNotRegex(section, r"\bDV\d+\b")
        self.assertNotRegex(section, r"\bM\d+\b")


class SolarResponseArtifactTest(unittest.TestCase):
    """A rejected solar interpretation keeps the bounded parsed candidate."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_rejected_candidate_keeps_parsed_response(self):
        output, candidate = solar_bundle(self.root, name="solar_rejected")
        # Break the candidate so finalize rejects it: a result points to an
        # unknown source. The parsed object must still be recorded.
        candidate = deepcopy(candidate)
        candidate["results"][0]["source_ids"] = [99]
        candidate["semantic"] = {"publication_id": "D001", "study_type": "unknown",
                                 "data_provenance": "unknown",
                                 "thematic_relevance": "unknown"}
        before = (output / "result.json").read_bytes()
        found = llm_shell.run_bundle(
            output, mode="api", allow_network=True,
            backend=StagedBackend(candidate, {"findings": []}))
        self.assertEqual(stages(found)["interpret"], "rejected")
        self.assertIn("response.json", found["artifacts"])
        self.assertIn("response.json",
                      [stage for stage in found["stages"]
                       if stage["name"] == "interpret"][0]["artifacts"])

        saved = json.loads((output / "llm" / "response.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["stage"], "interpret")
        self.assertEqual(saved["publication_id"], "D001")
        self.assertEqual(saved["adapter"], "solar_csv")
        # The artifact stores the parsed candidate, including the semantic block
        # that the seven-table contract never sees.
        self.assertEqual(saved["response"]["semantic"]["study_type"], "unknown")
        self.assertEqual(saved["response"]["results"][0]["source_ids"], [99])
        # Only the parsed object: no provider or transport details.
        self.assertEqual(set(saved), {"schema_version", "stage", "publication_id",
                                      "adapter", "response"})

        self.assertEqual((output / "result.json").read_bytes(), before)
        self.assertFalse(found["guarantees"]["official_csv_written"])

    def test_stale_response_is_cleared_on_rerun(self):
        output, candidate = solar_bundle(self.root, name="solar_stale")
        llm_shell.run_bundle(
            output, mode="api", allow_network=True,
            backend=StagedBackend(candidate, {"findings": []}))
        self.assertTrue((output / "llm" / "response.json").is_file())
        llm_shell.run_bundle(output)  # mock solar run skips interpretation
        self.assertFalse((output / "llm" / "response.json").exists())
        semantic = json.loads((output / "llm" / "semantic.json").read_text(encoding="utf-8"))
        self.assertEqual(semantic["status"], "missing")

    def test_response_artifact_is_not_written_for_fields_profile(self):
        output = bundle(self.root)
        found = llm_shell.run_bundle(output)
        self.assertNotIn("response.json", found["artifacts"])
        self.assertFalse((output / "llm" / "response.json").exists())
        self.assertFalse((output / "llm" / "semantic.json").exists())
        self.assertFalse((output / "llm" / "draft.json").exists())

    def _fail_response_write(self, error):
        """Make only the ``response.json`` write fail, keeping other artifacts."""
        original = llm_shell._write_json

        def failing(path, value):
            if Path(path).name == llm_shell.RESPONSE_ARTIFACT:
                raise error
            return original(path, value)

        return original, failing

    def test_response_write_oserror_propagates_as_persistence_failure(self):
        output, candidate = solar_bundle(self.root, name="solar_io_error")
        original, failing = self._fail_response_write(
            OSError(28, "No space left on device"))
        llm_shell._write_json = failing
        try:
            with self.assertRaises(OSError):
                llm_shell.run_bundle(
                    output, mode="api", allow_network=True,
                    backend=StagedBackend(candidate, {"findings": []}))
        finally:
            llm_shell._write_json = original

        # A persistence failure is not a rejected model response.
        self.assertFalse((output / "llm" / "interpretation.json").exists())
        self.assertFalse((output / "llm" / "manifest.json").exists())
        self.assertFalse((output / "llm" / "response.json").exists())

    def test_response_encoding_valueerror_is_not_a_validation_rejection(self):
        output, candidate = solar_bundle(self.root, name="solar_nan_error")
        # json.dumps(..., allow_nan=False) raises ValueError for a non-finite
        # value; that is an artifact error, not a validator rejection.
        original, failing = self._fail_response_write(
            ValueError("Out of range float values are not JSON compliant"))
        llm_shell._write_json = failing
        try:
            with self.assertRaises(ValueError):
                llm_shell.run_bundle(
                    output, mode="api", allow_network=True,
                    backend=StagedBackend(candidate, {"findings": []}))
        finally:
            llm_shell._write_json = original

        self.assertFalse((output / "llm" / "interpretation.json").exists())
        self.assertFalse((output / "llm" / "manifest.json").exists())
        self.assertFalse((output / "llm" / "response.json").exists())


class SolarDraftArtifactTest(unittest.TestCase):
    """The advisory draft keeps acceptable rows when the strict contract fails."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def d002_like_bundle(self, name="solar_d002"):
        """A document whose device area is not printed, with independent rows."""
        document = {
            "publication_id": "D002", "sha256": "d002", "pages": 1, "warnings": [],
            "sources": [
                {"id": 1, "page": 1, "kind": "text",
                 "quote": "Device A: perovskite Eg 1.5 eV, layer 0.5 um; AM1.5G 100 mW/cm2."},
                {"id": 2, "page": 1, "kind": "text",
                 "quote": "Device A PCE = 16 %"},
            ],
        }
        tables = {table: [] for table in TABLES}

        def add(table, **values):
            row = dict.fromkeys(TABLES[table], "")
            row.update(publication_id="D002", **values)
            row["source_ids"] = [1]
            tables[table].append(row)
            return row

        add("publications", title="Synthetic D002-like paper", in_scope="true",
            exclusion_reason="")
        add("materials", material_id="M1", material_class="perovskite",
            bandgap_value="1.5", bandgap_unit="eV")
        # The printed text never states the active area, so the strict contract
        # rejects the whole response while the row itself is the only bad one.
        add("devices", device_id="DV1", absorber_material_id="M1",
            active_area="7", area_unit="mm2")
        add("layers", layer_id="L1", device_id="DV1", layer_order="1",
            layer_function="absorber", layer_thickness="0.5", thickness_unit="um")
        add("measurements", measurement_id="MS1", device_id="DV1",
            measurement_type="J-V", light_intensity="100", intensity_unit="mW/cm2")
        row = add("results", result_id="R2", device_id="DV1", measurement_id="MS1",
                  material_id="M1", metric_name="PCE", raw_value="16", raw_unit="%",
                  result_origin="primary", source_page="1", source_fragment="PCE = 16 %",
                  extraction_status="extracted", aggregation="champion")
        row["source_ids"] = [2]
        profile = load_profile("solar")
        result = run(document, profile, mode="scripts")
        result["profile"] = profile
        output = self.root / name
        export_result(result, output)
        return output, tables

    def test_bad_row_rejects_interpretation_but_keeps_partial_draft(self):
        output, candidate = self.d002_like_bundle()
        before = (output / "result.json").read_bytes()
        found = llm_shell.run_bundle(
            output, mode="api", allow_network=True,
            backend=StagedBackend(candidate, {"findings": []}))

        self.assertEqual(stages(found)["interpret"], "rejected")
        self.assertIn("draft.json", found["artifacts"])
        self.assertIn("response.json", found["artifacts"])

        draft = json.loads((output / "llm" / "draft.json").read_text(encoding="utf-8"))
        self.assertEqual(draft["schema_version"], "solar-draft-1")
        self.assertEqual(draft["publication_id"], "D002")
        self.assertEqual(draft["status"], "partial")
        self.assertEqual(set(draft["counts"]),
                         {"rows", "accepted", "quarantined", "tables"})
        self.assertGreaterEqual(draft["counts"]["accepted"], 1)
        self.assertGreaterEqual(draft["counts"]["quarantined"], 1)
        accepted = [
            (table, envelope["key"])
            for table, envelopes in draft["tables"].items()
            for envelope in envelopes if envelope["status"] == "accepted"
        ]
        quarantined = [
            (table, envelope["key"])
            for table, envelopes in draft["tables"].items()
            for envelope in envelopes if envelope["status"] == "quarantined"
        ]
        self.assertIn(("materials", "M1"), accepted)
        self.assertIn(("devices", "DV1"), quarantined)
        # Each quarantined row carries its own bounded reason.
        reason = next(
            envelope["reason"] for envelope in draft["tables"]["devices"]
            if envelope["key"] == "DV1")
        self.assertIsInstance(reason, str)
        self.assertTrue(reason.strip())

        self.assertEqual(found["solar"]["draft_status"], "partial")
        self.assertEqual(found["solar"]["draft_counts"],
                         {name: draft["counts"][name]
                          for name in ("rows", "accepted", "quarantined")})
        self.assertEqual(manifest(output)["solar"]["draft_status"], "partial")
        # The advisory draft never becomes an official record.
        self.assertEqual(found["solar"]["scientific_record_counts"],
                         {table: 0 for table in llm_shell.SOLAR_RECORD_TABLES})
        self.assertFalse(found["guarantees"]["official_csv_written"])
        self.assertEqual((output / "result.json").read_bytes(), before)

    def test_valid_interpretation_gets_ok_draft(self):
        output, candidate = solar_bundle(self.root, name="solar_draft_ok")
        found = llm_shell.run_bundle(
            output, mode="api", allow_network=True,
            backend=StagedBackend(candidate, {"findings": []}))
        self.assertEqual(stages(found)["interpret"], "ok")
        draft = json.loads((output / "llm" / "draft.json").read_text(encoding="utf-8"))
        self.assertEqual(draft["status"], "ok")
        self.assertEqual(draft["counts"]["quarantined"], 0)
        self.assertEqual(found["solar"]["draft_status"], "ok")
        self.assertEqual(found["solar"]["draft_counts"]["quarantined"], 0)

    def test_mock_skip_and_invalid_response_report_missing_draft(self):
        output, _ = solar_bundle(self.root, name="solar_draft_missing")
        found = llm_shell.run_bundle(output)  # mock solar: interpret skipped
        self.assertEqual(stages(found)["interpret"], "skipped")
        draft = json.loads((output / "llm" / "draft.json").read_text(encoding="utf-8"))
        self.assertEqual(draft["status"], "missing")
        self.assertEqual(draft["counts"]["rows"], 0)
        self.assertEqual(found["solar"]["draft_status"], "missing")

        output2, _ = solar_bundle(self.root, name="solar_draft_invalid")
        found2 = llm_shell.run_bundle(
            output2, mode="api", allow_network=True,
            backend=StagedBackend("not json", {"findings": []}))
        self.assertEqual(stages(found2)["interpret"], "rejected")
        draft2 = json.loads((output2 / "llm" / "draft.json").read_text(encoding="utf-8"))
        self.assertEqual(draft2["status"], "missing")
        self.assertEqual(found2["solar"]["draft_status"], "missing")

    def test_validator_failure_is_sanitized_and_finalize_continues(self):
        output, candidate = solar_bundle(self.root, name="solar_draft_error")
        import solar_draft
        original = solar_draft.validate_draft

        def boom(candidate_value, document_value):
            raise RuntimeError("boom token=abc123 password=hunter2")

        solar_draft.validate_draft = boom
        try:
            found = llm_shell.run_bundle(
                output, mode="api", allow_network=True,
                backend=StagedBackend(candidate, {"findings": []}))
        finally:
            solar_draft.validate_draft = original

        # The strict contract still ran and succeeded.
        self.assertEqual(stages(found)["interpret"], "ok")
        draft = json.loads((output / "llm" / "draft.json").read_text(encoding="utf-8"))
        self.assertEqual(draft["status"], "error")
        self.assertEqual(draft["publication_id"], "D001")
        self.assertEqual(found["solar"]["draft_status"], "error")
        self.assertIn("RuntimeError", draft["error"])
        self.assertNotIn("abc123", draft["error"])
        self.assertNotIn("hunter2", draft["error"])

    def test_rerun_clears_stale_draft(self):
        output, candidate = solar_bundle(self.root, name="solar_draft_stale")
        llm_shell.run_bundle(
            output, mode="api", allow_network=True,
            backend=StagedBackend(candidate, {"findings": []}))
        self.assertTrue((output / "llm" / "draft.json").is_file())
        llm_shell.run_bundle(output)  # mock solar: no model response
        draft = json.loads((output / "llm" / "draft.json").read_text(encoding="utf-8"))
        self.assertEqual(draft["status"], "missing")
        self.assertEqual(draft["counts"]["rows"], 0)

    def test_draft_artifact_name_is_whitelisted_and_cleaned(self):
        self.assertIn(llm_shell.DRAFT_ARTIFACT, llm_shell.ARTIFACT_NAMES)
        self.assertIn(llm_shell.SEMANTIC_ARTIFACT, llm_shell.ARTIFACT_NAMES)
        self.assertIn(llm_shell.RESPONSE_ARTIFACT, llm_shell.ARTIFACT_NAMES)
        self.assertNotIn(llm_shell.DRAFT_ARTIFACT, llm_shell.BASE_ARTIFACT_NAMES)


class ParseDocumentShellTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.pdf = self.root / "S001.pdf"
        self.pdf.write_bytes(temperature_pdf())

    def tearDown(self):
        self.tmp.cleanup()

    def test_cli_default_is_scripts_only(self):
        output = self.root / "off"
        code = parse_document.main([
            str(self.pdf), "--profile", "custom-example", "-o", str(output),
            "--parser", "pdfplumber"])
        self.assertEqual(code, 0)
        self.assertFalse((output / "llm").exists())

    def test_cli_mock_adds_llm_summary(self):
        output = self.root / "mock"
        code = parse_document.main([
            str(self.pdf), "--profile", "custom-example", "-o", str(output),
            "--parser", "pdfplumber", "--llm-shell", "mock"])
        self.assertEqual(code, 0)
        self.assertTrue((output / "llm" / "manifest.json").is_file())


if __name__ == "__main__":
    unittest.main()
