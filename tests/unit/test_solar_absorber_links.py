"""One deterministic link invariant: a result's named material is the device absorber.

The organizer template gives a result a ``material_id`` and a device an
``absorber_material_id``. When both are named and the device names an absorber,
the two must be the same record. This test only pins that link: it does not
claim that the absorber is the scientifically correct attribution, and it never
expects the code to rewrite or blank a value. An empty side stays unknown.

The fixture is reused from ``tests.unit.test_solar_draft`` so the strict and
advisory paths are compared on exactly the same rows.
"""

from copy import deepcopy
import unittest

from solar_facts import check_links, finalize
from solar_schema import TABLES
from solar_draft import validate_draft
from tests.unit.test_solar_draft import (
    CITED_FRAGMENT,
    candidate,
    document,
    result as make_result,
)


def nio_material():
    """A second, named material: present in the table but not the absorber."""
    return {
        "publication_id": "D002",
        "material_id": "MAT2",
        "material_name_raw": "NiO",
        "formula_normalized": "NiO",
        "material_class": "other",
        "dimensionality": "3D",
        "bandgap_value": "",
        "bandgap_unit": "",
        "bandgap_method": "",
        "source_ids": [1],
    }


def mismatch_candidate():
    """R2 names NiO while its device DV2 names the MAT1 absorber."""
    value = candidate()
    value["materials"].append(nio_material())
    value["results"][1]["material_id"] = "MAT2"
    return value


def complete_tables(value):
    """Candidate rows with every template column, as ``finalize`` would build them."""
    tables = {}
    for table in TABLES:
        rows = []
        for row in value[table]:
            full = dict.fromkeys(TABLES[table], "")
            full.update({key: item for key, item in row.items() if key != "source_ids"})
            rows.append(full)
        tables[table] = rows
    return tables


class SolarAbsorberLinkTests(unittest.TestCase):

    def test_valid_absorber_pair_is_accepted(self):
        value = candidate()

        check_links(complete_tables(value))
        finalize(deepcopy(value), document())
        draft = validate_draft(value, document())

        self.assertEqual(draft["status"], "ok")
        self.assertEqual(draft["tables"]["results"][0]["status"], "accepted")
        self.assertEqual(draft["tables"]["results"][1]["status"], "accepted")

    def test_present_material_mismatch_is_rejected_strictly(self):
        value = mismatch_candidate()

        with self.assertRaises(ValueError) as caught:
            check_links(complete_tables(value))
        self.assertIn("поглотителем", str(caught.exception))

        with self.assertRaises(ValueError) as caught:
            finalize(deepcopy(value), document())
        self.assertIn("поглотителем", str(caught.exception))

    def test_mismatch_quarantines_draft_and_keeps_independent_result(self):
        value = mismatch_candidate()

        draft = validate_draft(value, document())

        self.assertEqual(draft["status"], "partial")
        self.assertEqual(draft["blocked_by"], [])
        bad = draft["tables"]["results"][1]
        self.assertEqual(bad["status"], "quarantined")
        self.assertIn("поглотителем", bad["reason"])
        # The raw value is copied, never rewritten to the absorber and never blanked.
        self.assertEqual(bad["row"]["material_id"], "MAT2")
        good = draft["tables"]["results"][0]
        self.assertEqual(good["status"], "accepted")
        self.assertEqual(good["row"]["material_id"], "MAT1")
        self.assertEqual(draft["counts"]["quarantined"], 1)
        self.assertEqual(draft["counts"]["accepted"], 9)

    def test_empty_material_stays_unknown(self):
        value = candidate()
        value["results"][1]["material_id"] = ""

        result = finalize(deepcopy(value), document())
        self.assertEqual(result["tables"]["results"][1]["material_id"], "")

        draft = validate_draft(value, document())
        self.assertEqual(draft["status"], "ok")
        self.assertEqual(draft["tables"]["results"][1]["status"], "accepted")
        self.assertEqual(draft["tables"]["results"][1]["row"]["material_id"], "")

    def test_empty_device_absorber_stays_unknown(self):
        value = candidate()
        value["devices"][1]["absorber_material_id"] = ""

        result = finalize(deepcopy(value), document())
        self.assertEqual(result["tables"]["devices"][1]["absorber_material_id"], "")

        draft = validate_draft(value, document())
        self.assertEqual(draft["status"], "ok")
        self.assertEqual(draft["tables"]["devices"][1]["status"], "accepted")
        self.assertEqual(draft["tables"]["devices"][1]["row"]["absorber_material_id"], "")
        self.assertEqual(draft["tables"]["results"][1]["status"], "accepted")

    def test_cited_result_without_device_is_unaffected(self):
        value = candidate()
        value["materials"].append(nio_material())
        value["results"].append(make_result(
            "RC", "", "", "19.0", CITED_FRAGMENT,
            result_origin="cited", cited_publication_id="REF1",
            material_id="MAT2", source_ids=[2],
        ))

        result = finalize(deepcopy(value), document())
        self.assertEqual(result["tables"]["results"][2]["material_id"], "MAT2")

        draft = validate_draft(value, document())
        self.assertEqual(draft["tables"]["results"][2]["status"], "accepted")
        self.assertEqual(draft["tables"]["results"][2]["row"]["material_id"], "MAT2")

    def test_missing_dependency_keeps_existing_reference_error(self):
        with self.subTest("missing device"):
            value = candidate()
            value["results"][1]["device_id"] = "DV9"

            with self.assertRaises(ValueError) as caught:
                check_links(complete_tables(value))
            self.assertIn("отсутствующую", str(caught.exception))
            self.assertNotIn("поглотителем", str(caught.exception))

            draft = validate_draft(value, document())
            self.assertEqual(draft["tables"]["results"][1]["status"], "quarantined")
            self.assertIn("отсутствующую", draft["tables"]["results"][1]["reason"])

        with self.subTest("missing material"):
            value = candidate()
            value["results"][1]["material_id"] = "MAT9"

            with self.assertRaises(ValueError) as caught:
                check_links(complete_tables(value))
            self.assertIn("отсутствующую", str(caught.exception))
            self.assertNotIn("поглотителем", str(caught.exception))

            draft = validate_draft(value, document())
            self.assertEqual(draft["tables"]["results"][1]["status"], "quarantined")
            self.assertIn("отсутствующую", draft["tables"]["results"][1]["reason"])

    def test_input_is_not_mutated(self):
        value = mismatch_candidate()
        before = deepcopy(value)

        validate_draft(value, document())
        self.assertEqual(value, before)

        with self.assertRaises(ValueError):
            finalize(value, document())
        self.assertEqual(value, before)


if __name__ == "__main__":
    unittest.main()
