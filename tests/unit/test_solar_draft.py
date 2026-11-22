"""Focused tests for advisory-only solar draft quarantine.

The fixture is a D002-style page with two independent devices. A bad entity in
one device must quarantine that device and its dependents while the other
device and its own records stay accepted. No test expects an official export or
a merge: ``validate_draft`` is only a diagnostic view.
"""

from copy import deepcopy
import json
import unittest

from viniti.solar_facts import finalize
from viniti import solar_draft
from viniti.solar_draft import empty_draft, validate_draft


SOURCE = (
    "DV1 active area 0.10 cm2. DV2 active area 0.20 cm2. "
    "DV1 achieved a PCE of 17.0 %. DV2 achieved a PCE of 18.5 %. "
    "The bandgap was 1.60 eV. The absorber thickness was 500 nm."
)
CITED_SOURCE = "A cited PCE of 19.0 % was reported."
R1_FRAGMENT = "DV1 achieved a PCE of 17.0 %."
R2_FRAGMENT = "DV2 achieved a PCE of 18.5 %."
CITED_FRAGMENT = "A cited PCE of 19.0 % was reported."


def document():
    return {
        "publication_id": "D002",
        "sha256": "deadbeef",
        "pages": 1,
        "warnings": [],
        "sources": [
            {"id": 1, "page": 1, "kind": "text", "quote": SOURCE},
            {"id": 2, "page": 1, "kind": "text", "quote": CITED_SOURCE},
        ],
    }


def device(device_id, area):
    return {
        "publication_id": "D002",
        "device_id": device_id,
        "absorber_material_id": "MAT1",
        "device_label": device_id,
        "device_role": "target",
        "device_type": "cell",
        "architecture": "n-i-p",
        "substrate_type": "rigid",
        "active_area": area,
        "area_unit": "cm2",
        "area_definition": "designated illumination",
        "source_ids": [1],
    }


def measurement(measurement_id, device_id):
    return {
        "publication_id": "D002",
        "measurement_id": measurement_id,
        "device_id": device_id,
        "measurement_type": "J-V",
        "spectrum": "AM1.5G",
        "scan_direction": "reverse",
        "mask_used": "true",
        "certified": "false",
        "source_ids": [1],
    }


def result(result_id, device_id, measurement_id, raw_value, fragment, **extra):
    row = {
        "publication_id": "D002",
        "result_id": result_id,
        "device_id": device_id,
        "measurement_id": measurement_id,
        "material_id": "MAT1",
        "metric_name": "PCE",
        "raw_value": raw_value,
        "raw_unit": "%",
        "result_origin": "primary",
        "aggregation": "champion",
        "extraction_status": "extracted",
        "source_page": "1",
        "source_fragment": fragment,
        "source_ids": [1],
    }
    row.update(extra)
    return row


def candidate():
    return {
        "publications": [{
            "publication_id": "D002",
            "title": "Two devices",
            "year": "2020",
            "DOI": "",
            "authors": "A. Author",
            "in_scope": "true",
            "exclusion_reason": "",
        }],
        "materials": [{
            "publication_id": "D002",
            "material_id": "MAT1",
            "material_name_raw": "MAPbI3",
            "formula_normalized": "CH3NH3PbI3",
            "material_class": "perovskite",
            "dimensionality": "3D",
            "bandgap_value": "1.60",
            "bandgap_unit": "eV",
            "bandgap_method": "Tauc",
            "source_ids": [1],
        }],
        "devices": [device("DV1", "0.10"), device("DV2", "0.20")],
        "layers": [{
            "publication_id": "D002",
            "layer_id": "L1",
            "device_id": "DV1",
            "layer_order": "1",
            "layer_function": "absorber",
            "layer_material": "MAPbI3",
            "layer_thickness": "500",
            "thickness_unit": "nm",
            "deposition_method": "spin-coating",
            "source_ids": [1],
        }],
        "measurements": [measurement("M1", "DV1"), measurement("M2", "DV2")],
        "results": [
            result("R1", "DV1", "M1", "17.0", R1_FRAGMENT),
            result("R2", "DV2", "M2", "18.5", R2_FRAGMENT),
        ],
        "issues": [],
    }


class SolarDraftTests(unittest.TestCase):

    def test_bad_entity_quarantines_only_bad_device_and_dependents(self):
        value = candidate()
        value["devices"][0]["active_area"] = "0.99"

        draft = validate_draft(value, document())

        self.assertEqual(draft["schema_version"], "solar-draft-1")
        self.assertEqual(draft["status"], "partial")
        self.assertEqual(draft["publication_state"], "included")
        self.assertEqual(draft["blocked_by"], [])

        bad_device = draft["tables"]["devices"][0]
        self.assertEqual(bad_device["status"], "quarantined")
        self.assertIn("active_area", bad_device["reason"])
        self.assertIn("число", bad_device["reason"])
        self.assertEqual(bad_device["row"]["active_area"], "0.99")

        self.assertEqual(draft["tables"]["devices"][1]["status"], "accepted")
        self.assertEqual(draft["tables"]["materials"][0]["status"], "accepted")
        self.assertEqual(draft["tables"]["publications"][0]["status"], "accepted")
        self.assertEqual(draft["tables"]["layers"][0]["status"], "quarantined")
        self.assertIn("в карантине", draft["tables"]["layers"][0]["reason"])
        self.assertEqual(draft["tables"]["measurements"][0]["status"], "quarantined")
        self.assertEqual(draft["tables"]["results"][0]["status"], "quarantined")
        self.assertEqual(draft["tables"]["results"][1]["status"], "accepted")
        self.assertEqual(draft["counts"]["quarantined"], 4)
        self.assertEqual(draft["counts"]["accepted"], 5)

    def test_duplicate_id_quarantines_every_copy_and_marks_ambiguity(self):
        value = candidate()
        value["devices"].append(device("DV2", "0.20"))

        draft = validate_draft(value, document())

        self.assertEqual(draft["status"], "partial")
        for index in (1, 2):
            envelope = draft["tables"]["devices"][index]
            self.assertEqual(envelope["status"], "quarantined")
            self.assertIn("повтор", envelope["reason"])
        self.assertEqual(draft["tables"]["devices"][0]["status"], "accepted")
        self.assertEqual(draft["tables"]["results"][1]["status"], "quarantined")
        self.assertIn("неоднознач", draft["tables"]["results"][1]["reason"])
        self.assertEqual(draft["tables"]["results"][0]["status"], "accepted")

    def test_invalid_publication_blocks_every_row(self):
        value = candidate()
        value["publications"][0]["in_scope"] = "maybe"

        draft = validate_draft(value, document())

        self.assertEqual(draft["status"], "blocked")
        self.assertEqual(draft["publication_state"], "blocked")
        self.assertEqual(draft["blocked_by"][0]["code"], "publication_scope")
        self.assertEqual(draft["counts"]["accepted"], 0)
        self.assertEqual(draft["counts"]["rows"], 9)
        for rows in draft["tables"].values():
            for envelope in rows:
                self.assertEqual(envelope["status"], "quarantined")

    def test_publication_gate_variants(self):
        with self.subTest("excluded without reason"):
            value = candidate()
            value["publications"][0]["in_scope"] = "false"
            draft = validate_draft(value, document())
            self.assertEqual(draft["status"], "blocked")
            self.assertEqual(draft["blocked_by"][0]["code"], "exclusion_reason")

        with self.subTest("excluded with records"):
            value = candidate()
            value["publications"][0]["in_scope"] = "false"
            value["publications"][0]["exclusion_reason"] = "review article"
            draft = validate_draft(value, document())
            self.assertEqual(draft["status"], "blocked")
            self.assertEqual(draft["blocked_by"][0]["code"], "excluded_with_records")

        with self.subTest("included with exclusion reason"):
            value = candidate()
            value["publications"][0]["exclusion_reason"] = "review article"
            draft = validate_draft(value, document())
            self.assertEqual(draft["status"], "blocked")
            self.assertEqual(draft["blocked_by"][0]["code"], "exclusion_reason")

        with self.subTest("excluded and empty"):
            value = candidate()
            value["publications"][0]["in_scope"] = "false"
            value["publications"][0]["exclusion_reason"] = "review article"
            for table in solar_draft.SCIENTIFIC_TABLES:
                value[table] = []
            draft = validate_draft(value, document())
            self.assertEqual(draft["status"], "ok")
            self.assertEqual(draft["publication_state"], "excluded")
            for table in solar_draft.SCIENTIFIC_TABLES:
                self.assertEqual(draft["tables"][table], [])

    def test_invalid_global_shape_blocks(self):
        with self.subTest("missing table"):
            value = candidate()
            del value["issues"]
            draft = validate_draft(value, document())
            self.assertEqual(draft["status"], "blocked")
            self.assertEqual(draft["blocked_by"][0]["code"], "table_set_mismatch")
            self.assertEqual(draft["counts"]["accepted"], 0)

        with self.subTest("candidate is not an object"):
            draft = validate_draft("not a draft", document())
            self.assertEqual(draft["status"], "blocked")
            self.assertEqual(draft["blocked_by"][0]["code"], "candidate_not_object")
            self.assertEqual(draft["counts"]["rows"], 0)

        with self.subTest("table is not a list"):
            value = candidate()
            value["devices"] = {}
            draft = validate_draft(value, document())
            self.assertEqual(draft["status"], "blocked")
            self.assertEqual(draft["blocked_by"][0]["code"], "table_not_list")
            self.assertEqual(draft["counts"]["accepted"], 0)

        with self.subTest("candidate is not json"):
            value = candidate()
            value["devices"][0]["bogus"] = {1, 2}
            draft = validate_draft(value, document())
            self.assertEqual(draft["status"], "blocked")
            self.assertEqual(draft["blocked_by"][0]["code"], "candidate_not_json")

    def test_global_shape_block_is_counts_only_and_bounded(self):
        value = candidate()
        del value["issues"]
        value["devices"] = [
            {"publication_id": "D002", "device_id": f"DV{index}"}
            for index in range(25000)
        ]

        draft = validate_draft(value, document())

        self.assertEqual(draft["status"], "blocked")
        self.assertEqual(draft["blocked_by"][0]["code"], "table_set_mismatch")
        for rows in draft["tables"].values():
            self.assertEqual(rows, [])
        self.assertEqual(draft["counts"]["tables"]["devices"]["rows"], 25000)
        self.assertEqual(draft["counts"]["accepted"], 0)
        json.dumps(draft, ensure_ascii=False, allow_nan=False)

    def test_cyclic_row_blocks_with_serializable_output(self):
        value = candidate()
        cyclic = {}
        cyclic["self"] = cyclic
        value["results"].append(cyclic)

        draft = validate_draft(value, document())

        self.assertEqual(draft["status"], "blocked")
        self.assertEqual(draft["blocked_by"][0]["code"], "candidate_not_json")
        for rows in draft["tables"].values():
            self.assertEqual(rows, [])
        self.assertEqual(draft["counts"]["tables"]["results"]["rows"], 3)
        json.dumps(draft, ensure_ascii=False, allow_nan=False)

    def test_cyclic_row_with_missing_table_stays_counts_only(self):
        value = candidate()
        del value["issues"]
        cyclic = {}
        cyclic["self"] = cyclic
        value["results"].append(cyclic)

        draft = validate_draft(value, document())

        self.assertEqual(draft["blocked_by"][0]["code"], "table_set_mismatch")
        for rows in draft["tables"].values():
            self.assertEqual(rows, [])
        json.dumps(draft, ensure_ascii=False, allow_nan=False)

    def test_malformed_document_is_rejected_without_leaking_body(self):
        cases = (
            ("none quote", lambda doc: doc["sources"][0].update(quote=None), "Цитата"),
            ("missing quote", lambda doc: doc["sources"][0].pop("quote"), "Цитата"),
            ("missing source id", lambda doc: doc["sources"][0].pop("id"),
             "Идентификаторы"),
            ("missing source page", lambda doc: doc["sources"][0].pop("page"),
             "недопустимую"),
            ("non-numeric pages", lambda doc: doc.update(pages="one"), "страниц"),
            ("missing pages", lambda doc: doc.pop("pages"), "страниц"),
            ("sources not list", lambda doc: doc.update(sources={}), "источник"),
        )
        for name, mutate, expected in cases:
            with self.subTest(name):
                doc = document()
                doc["sources"][0]["quote"] = "SECRET BODY TEXT"
                mutate(doc)

                draft = validate_draft(candidate(), doc)

                self.assertEqual(draft["status"], "blocked")
                self.assertEqual(draft["blocked_by"][0]["code"], "document_invalid")
                self.assertIn(expected, draft["blocked_by"][0]["reason"])
                self.assertNotIn(
                    "SECRET BODY TEXT", json.dumps(draft, ensure_ascii=False),
                )
                json.dumps(draft, ensure_ascii=False, allow_nan=False)

        with self.subTest("document is not an object"):
            draft = validate_draft(candidate(), None)
            self.assertEqual(draft["blocked_by"][0]["code"], "document_invalid")
            json.dumps(draft, ensure_ascii=False, allow_nan=False)

    def test_source_value_and_unit_failures(self):
        cases = (
            ("unknown source id", lambda value: value["results"][1].update(source_ids=[99]),
             "results", 1, "source_id"),
            ("missing source ids",
             lambda value: value["measurements"][1].pop("source_ids"),
             "measurements", 1, "source_ids"),
            ("raw value not literal",
             lambda value: value["results"][1].update(raw_value="99.9"),
             "results", 1, "исходное значение"),
            ("raw value is not a full token",
             lambda value: value["results"][1].update(raw_value="8.5"),
             "results", 1, "исходное значение"),
            ("fragment not on page",
             lambda value: value["results"][1].update(source_fragment="not in source"),
             "results", 1, "цитата"),
            ("page outside document",
             lambda value: value["results"][1].update(source_page="2"),
             "results", 1, "страница"),
            ("entity unit not literal",
             lambda value: value["devices"][1].update(area_unit="mm2"),
             "devices", 1, "единица"),
            ("vocabulary value",
             lambda value: value["devices"][1].update(device_role="hero"),
             "devices", 1, "вне словаря"),
            ("non-string field",
             lambda value: value["devices"][1].update(active_area=0.2),
             "devices", 1, "должно быть строкой"),
            ("unknown field",
             lambda value: value["devices"][1].update(bogus="x"),
             "devices", 1, "неизвестные поля"),
        )
        for name, mutate, table, index, expected in cases:
            with self.subTest(name):
                value = candidate()
                mutate(value)
                draft = validate_draft(value, document())
                envelope = draft["tables"][table][index]
                self.assertEqual(envelope["status"], "quarantined")
                self.assertIn(expected, envelope["reason"])

    def test_missing_and_quarantined_references_are_explicit(self):
        value = candidate()
        value["layers"][0]["device_id"] = "DV9"
        draft = validate_draft(value, document())
        self.assertEqual(draft["tables"]["layers"][0]["status"], "quarantined")
        self.assertIn("отсутствующую", draft["tables"]["layers"][0]["reason"])

        value = candidate()
        value["results"][1]["material_id"] = "MAT9"
        draft = validate_draft(value, document())
        self.assertEqual(draft["tables"]["results"][1]["status"], "quarantined")
        self.assertIn("отсутствующую", draft["tables"]["results"][1]["reason"])

        value = candidate()
        value["results"][1]["duplicate_of"] = "R9"
        draft = validate_draft(value, document())
        self.assertEqual(draft["tables"]["results"][1]["status"], "quarantined")
        self.assertIn("отсутствующую", draft["tables"]["results"][1]["reason"])

    def test_measurement_device_list_is_checked(self):
        value = candidate()
        value["measurements"][1]["device_id"] = "DV1; DV2"
        draft = validate_draft(value, document())
        self.assertEqual(draft["tables"]["measurements"][1]["status"], "accepted")

        value = candidate()
        value["devices"][0]["active_area"] = "0.99"
        value["measurements"][1]["device_id"] = "DV1; DV2"
        draft = validate_draft(value, document())
        self.assertEqual(draft["tables"]["measurements"][1]["status"], "quarantined")
        self.assertIn("в карантине", draft["tables"]["measurements"][1]["reason"])

    def test_non_object_row_is_quarantined(self):
        value = candidate()
        value["devices"][1] = "DV2"

        draft = validate_draft(value, document())

        self.assertEqual(draft["tables"]["devices"][1]["status"], "quarantined")
        self.assertIn("объектом", draft["tables"]["devices"][1]["reason"])

    def test_empty_optional_entity_numeric_is_permitted(self):
        value = candidate()
        value["devices"][1]["active_area"] = ""
        value["devices"][1]["area_unit"] = ""

        draft = validate_draft(value, document())

        self.assertEqual(draft["status"], "ok")
        self.assertEqual(draft["tables"]["devices"][1]["status"], "accepted")
        self.assertEqual(draft["tables"]["results"][1]["status"], "accepted")

    def test_non_extracted_result_needs_no_raw_value(self):
        value = candidate()
        value["results"][1]["extraction_status"] = "present_not_extractable"
        value["results"][1]["raw_value"] = ""

        draft = validate_draft(value, document())

        self.assertEqual(draft["status"], "ok")
        self.assertEqual(draft["tables"]["results"][1]["status"], "accepted")

    def test_cited_and_measurement_device_mismatch(self):
        value = candidate()
        value["results"].extend([
            result("RC", "", "", "19.0", CITED_FRAGMENT,
                   result_origin="cited", cited_publication_id="REF1", source_ids=[2]),
            result("RBAD", "DV2", "", "18.5", R2_FRAGMENT,
                   result_origin="cited", cited_publication_id="REF1"),
            result("RMIS", "DV1", "M2", "18.5", R2_FRAGMENT),
            result("RREF", "", "", "19.0", CITED_FRAGMENT,
                   result_origin="cited", source_ids=[2]),
        ])

        draft = validate_draft(value, document())

        cited = draft["tables"]["results"][2]
        self.assertEqual(cited["status"], "accepted")
        bad_cited = draft["tables"]["results"][3]
        self.assertEqual(bad_cited["status"], "quarantined")
        self.assertIn("cited", bad_cited["reason"])
        mismatch = draft["tables"]["results"][4]
        self.assertEqual(mismatch["status"], "quarantined")
        self.assertIn("не совпадает", mismatch["reason"])
        missing_reference = draft["tables"]["results"][5]
        self.assertEqual(missing_reference["status"], "quarantined")
        self.assertIn("cited", missing_reference["reason"])

    def test_duplicate_cycles_and_mismatch(self):
        value = candidate()
        value["results"].extend([
            result("RA", "DV2", "M2", "18.5", R2_FRAGMENT, duplicate_of="RB"),
            result("RB", "DV2", "M2", "18.5", R2_FRAGMENT, duplicate_of="RA"),
            result("RC", "DV2", "M2", "18.5", R2_FRAGMENT, duplicate_of="RA"),
            result("RD", "DV2", "M2", "17.0", R1_FRAGMENT, duplicate_of="RE"),
            result("RE", "DV2", "M2", "18.5", R2_FRAGMENT),
        ])

        draft = validate_draft(value, document())

        for index in (2, 3):
            self.assertEqual(draft["tables"]["results"][index]["status"], "quarantined")
            self.assertIn("цикл", draft["tables"]["results"][index]["reason"])
        self.assertEqual(draft["tables"]["results"][4]["status"], "quarantined")
        self.assertIn("в карантине", draft["tables"]["results"][4]["reason"])
        self.assertEqual(draft["tables"]["results"][5]["status"], "quarantined")
        self.assertIn("разные", draft["tables"]["results"][5]["reason"])
        self.assertEqual(draft["tables"]["results"][6]["status"], "accepted")

    def test_no_mutation_and_raw_rows_are_copied(self):
        value = candidate()
        before = deepcopy(value)

        draft = validate_draft(value, document())

        self.assertEqual(value, before)
        for table, rows in draft["tables"].items():
            for envelope in rows:
                self.assertIsNot(envelope["row"], value[table][envelope["index"]])
                self.assertEqual(envelope["row"], value[table][envelope["index"]])
        draft["tables"]["devices"][0]["row"]["active_area"] = "changed"
        self.assertEqual(value["devices"][0]["active_area"], "0.10")

    def test_model_issues_are_rejected(self):
        value = candidate()
        value["issues"] = [{
            "publication_id": "D002",
            "issue_id": "I1",
            "record_ids": "R2",
            "check_name": "model_guess",
            "description": "model finding",
            "source_page": "1",
            "source_fragment": R2_FRAGMENT,
        }]

        draft = validate_draft(value, document())

        self.assertEqual(draft["status"], "partial")
        self.assertEqual(draft["tables"]["issues"][0]["status"], "quarantined")
        self.assertIn("issues", draft["tables"]["issues"][0]["reason"])
        self.assertEqual(draft["tables"]["results"][1]["status"], "accepted")

    def test_reason_is_bounded(self):
        value = candidate()
        value["devices"][1]["x" * 1000] = "x"

        draft = validate_draft(value, document())

        reason = draft["tables"]["devices"][1]["reason"]
        self.assertLessEqual(len(reason), solar_draft.MAX_REASON)
        self.assertIn("x" * 1000, draft["tables"]["devices"][1]["row"])

    def test_row_limit_blocks(self):
        value = candidate()
        value["devices"] = [device("DV1", "0.10")] * (
            solar_draft.MAX_ROWS_PER_TABLE + 1
        )

        draft = validate_draft(value, document())

        self.assertEqual(draft["status"], "blocked")
        self.assertEqual(draft["blocked_by"][0]["code"], "row_limit")
        self.assertEqual(draft["counts"]["tables"]["devices"]["rows"],
                         solar_draft.MAX_ROWS_PER_TABLE + 1)
        for rows in draft["tables"].values():
            self.assertEqual(rows, [])
        json.dumps(draft, ensure_ascii=False, allow_nan=False)

    def test_finalize_still_rejects_bad_candidate(self):
        valid = candidate()
        finalize(deepcopy(valid), document())

        bad = candidate()
        bad["devices"][0]["active_area"] = "0.99"
        with self.assertRaises(ValueError) as caught:
            finalize(deepcopy(bad), document())
        self.assertIn("active_area", str(caught.exception))

    def test_empty_draft(self):
        draft = empty_draft(document())

        self.assertEqual(
            set(draft),
            {"schema_version", "publication_id", "status", "publication_state",
             "blocked_by", "tables", "counts"},
        )
        self.assertEqual(draft["schema_version"], "solar-draft-1")
        self.assertEqual(draft["publication_id"], "D002")
        self.assertEqual(draft["status"], "missing")
        self.assertEqual(draft["publication_state"], "missing")
        self.assertEqual(draft["blocked_by"][0]["code"], "candidate_absent")
        self.assertEqual(draft["counts"]["rows"], 0)
        for table in solar_draft.TABLES:
            self.assertEqual(draft["tables"][table], [])

        blocked = empty_draft(document(), status="blocked", code="provider_error")
        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(blocked["blocked_by"][0]["code"], "provider_error")

        minimal = empty_draft({"publication_id": "D002"})
        self.assertEqual(minimal["publication_id"], "D002")


if __name__ == "__main__":
    unittest.main()
