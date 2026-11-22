"""Attribution inventory is metadata, never a verdict or a rewrite.

The inventory reports which rows carry process-condition or sample-size claims.
It does not decide whether a claim is scientifically correct, it does not
suggest demoting or blanking a value, and ``state=none`` only means that this
particular field inventory is empty. Every model row stays unverified, so
``requires_review`` is always true.

The fixture comes from ``tests.test_solar`` so the metadata is checked against
the same rows the strict path exports.
"""

from copy import deepcopy
import json
import unittest

from viniti.solar_facts import (
    ATTRIBUTION_NOTE,
    ATTRIBUTION_ROW_LIMIT,
    attribution_review,
    check_consistency,
    finalize,
    normalize,
    validate_candidates,
)
from viniti.solar_schema import TABLES
from tests.test_solar import fixture


def empty_tables():
    return {table: [] for table in TABLES}


def layer_row(key, **values):
    row = dict.fromkeys(TABLES["layers"], "")
    row.update(publication_id="D001", layer_id=key, device_id="DV1")
    row.update(values)
    return row


def result_row(key, **values):
    row = dict.fromkeys(TABLES["results"], "")
    row.update(publication_id="D001", result_id=key, device_id="DV1", measurement_id="MS1")
    row.update(values)
    return row


class AttributionReviewTests(unittest.TestCase):

    def test_sensitive_claims_are_unverified_and_values_are_not_echoed(self):
        document, candidate = fixture()
        candidate["layers"][0].update(
            deposition_method="spin-coating", atmosphere="argon ambient secret",
            annealing_temperature="1550",
        )

        metadata = finalize(deepcopy(candidate), document)["extraction"]["attribution"]

        self.assertEqual(metadata["state"], "unverified")
        self.assertTrue(metadata["requires_review"])
        self.assertEqual(metadata["note"], ATTRIBUTION_NOTE)
        self.assertEqual(metadata["counts"]["rows"], 5)
        self.assertEqual(metadata["counts"]["fields"], 7)
        self.assertEqual(metadata["counts"]["by_table"]["layers"], {"rows": 1, "fields": 3})
        self.assertEqual(metadata["counts"]["by_table"]["results"], {"rows": 4, "fields": 4})
        self.assertEqual(metadata["counts"]["by_field"]["layers.atmosphere"], 1)
        self.assertEqual(metadata["counts"]["by_field"]["results.aggregation"], 4)
        self.assertEqual(metadata["counts"]["by_field"]["results.n_devices"], 0)
        self.assertEqual(metadata["counts"]["by_field"]["results.spread_value"], 0)
        self.assertFalse(metadata["truncated"])

        layer_ref = metadata["rows"][0]
        self.assertEqual(layer_ref, {
            "table": "layers", "key": "L1",
            "fields": ["annealing_temperature", "atmosphere", "deposition_method"],
        })
        for reference in metadata["rows"]:
            self.assertEqual(set(reference), {"table", "key", "fields"})
        payload = json.dumps(metadata, ensure_ascii=False, allow_nan=False)
        for value in ("1550", "argon", "secret", "spin-coating", "champion"):
            self.assertNotIn(value, payload)

    def test_empty_inventory_is_none_not_science_verified(self):
        document, candidate = fixture()
        for row in candidate["results"]:
            row["aggregation"] = ""

        result = finalize(deepcopy(candidate), document)
        metadata = result["extraction"]["attribution"]

        self.assertEqual(metadata["state"], "none")
        self.assertTrue(metadata["requires_review"])
        self.assertEqual(metadata["counts"]["rows"], 0)
        self.assertEqual(metadata["counts"]["fields"], 0)
        self.assertEqual(metadata["rows"], [])
        self.assertFalse(metadata["truncated"])
        self.assertIn("state=none", metadata["note"])
        # The scientific rows are still there: "none" is only about this inventory.
        self.assertEqual(len(result["tables"]["results"]), 4)
        self.assertTrue(all(row["raw_value"] for row in result["tables"]["results"]))

    def test_empty_tables_are_none(self):
        metadata = attribution_review(empty_tables())

        self.assertEqual(metadata["state"], "none")
        self.assertTrue(metadata["requires_review"])
        self.assertEqual(metadata["counts"]["rows"], 0)
        self.assertEqual(metadata["rows"], [])

    def test_non_sensitive_fields_are_not_inventoried(self):
        tables = empty_tables()
        tables["layers"] = [layer_row("L1", layer_thickness="500", thickness_unit="nm")]
        tables["results"] = [result_row("R1", raw_value="16", raw_unit="%")]

        metadata = attribution_review(tables)

        self.assertEqual(metadata["state"], "none")
        self.assertEqual(metadata["rows"], [])

    def test_row_cap_and_counts_stay_accurate(self):
        tables = empty_tables()
        tables["results"] = [
            result_row(f"R{index}", aggregation="mean")
            for index in range(ATTRIBUTION_ROW_LIMIT + 1)
        ]

        metadata = attribution_review(tables)

        self.assertEqual(metadata["state"], "unverified")
        self.assertEqual(metadata["counts"]["rows"], ATTRIBUTION_ROW_LIMIT + 1)
        self.assertEqual(metadata["counts"]["fields"], ATTRIBUTION_ROW_LIMIT + 1)
        self.assertEqual(len(metadata["rows"]), ATTRIBUTION_ROW_LIMIT)
        self.assertTrue(metadata["truncated"])
        self.assertEqual(metadata["rows"][-1]["key"], f"R{ATTRIBUTION_ROW_LIMIT - 1}")

        tables["results"] = tables["results"][:ATTRIBUTION_ROW_LIMIT]
        exact = attribution_review(tables)
        self.assertEqual(len(exact["rows"]), ATTRIBUTION_ROW_LIMIT)
        self.assertFalse(exact["truncated"])

    def test_review_is_pure_and_deterministic(self):
        tables = empty_tables()
        tables["layers"] = [
            layer_row("L1", annealing_time="30", atmosphere="N2"),
            layer_row("L2", deposition_method="spin-coating"),
        ]
        tables["results"] = [result_row("R1", n_devices="12", aggregation="champion")]
        before = deepcopy(tables)

        first = attribution_review(tables)
        second = attribution_review(tables)

        self.assertEqual(first, second)
        self.assertEqual(tables, before)
        self.assertEqual(
            [reference["key"] for reference in first["rows"]], ["L1", "L2", "R1"],
        )
        self.assertEqual(first["rows"][0]["fields"], ["annealing_time", "atmosphere"])

    def test_finalize_metadata_is_additive(self):
        document, candidate = fixture()
        untouched = deepcopy(candidate)

        result = finalize(deepcopy(candidate), document, model="test")
        expected, evidence = validate_candidates(deepcopy(candidate), document)
        # ``finalize`` inventories the model claims before normalization, so the
        # reference is captured at the same point of the same pipeline.
        inventory = attribution_review(expected)
        normalize(expected, evidence)
        check_consistency(expected)

        self.assertEqual(result["tables"], expected)
        self.assertEqual(result["tables"]["issues"], [])
        self.assertEqual(result["extraction"]["revision"], "solar-2")
        self.assertEqual(result["extraction"]["source_verification"], "literal")
        self.assertEqual(result["extraction"]["attribution"], inventory)
        self.assertEqual(candidate, untouched)
        json.dumps(result["extraction"]["attribution"], ensure_ascii=False, allow_nan=False)

    def test_spread_cleared_by_normalization_still_counted(self):
        document, candidate = fixture()
        # The fixture quote has no "± 1", so ``normalize`` moves the model spread
        # into ``note`` and leaves ``spread_value`` empty in the final tables.
        candidate["results"][0]["spread_value"] = "1"

        result = finalize(deepcopy(candidate), document)
        metadata = result["extraction"]["attribution"]

        final_row = result["tables"]["results"][0]
        self.assertEqual(final_row["spread_value"], "")
        self.assertIn("Original spread: 1 %", final_row["note"])

        self.assertEqual(metadata["state"], "unverified")
        self.assertEqual(metadata["counts"]["by_field"]["results.spread_value"], 1)
        self.assertEqual(metadata["counts"]["by_field"]["results.aggregation"], 4)
        self.assertEqual(metadata["counts"]["fields"], 5)
        spread_refs = [
            reference for reference in metadata["rows"]
            if "spread_value" in reference["fields"]
        ]
        self.assertEqual(spread_refs, [{
            "table": "results", "key": "R2",
            "fields": ["spread_value", "aggregation"],
        }])

        # The metadata change must not alter the scientific tables themselves.
        expected, evidence = validate_candidates(deepcopy(candidate), document)
        normalize(expected, evidence)
        check_consistency(expected)
        self.assertEqual(result["tables"], expected)
        self.assertEqual(
            [row["spread_value"] for row in result["tables"]["results"]],
            ["", "", "", ""],
        )

    def test_excluded_publication_has_empty_inventory(self):
        document, _ = fixture()
        tables = empty_tables()
        tables["publications"] = [{
            "publication_id": "D001", "in_scope": "false",
            "exclusion_reason": "Simulation only",
        }]

        result = finalize(tables, document)
        metadata = result["extraction"]["attribution"]

        self.assertEqual(metadata["state"], "none")
        self.assertTrue(metadata["requires_review"])
        self.assertEqual(metadata["rows"], [])


if __name__ == "__main__":
    unittest.main()
