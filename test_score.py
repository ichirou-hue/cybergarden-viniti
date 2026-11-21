"""Tests for score.py: matching key, tolerance, near matches, reporting.

Run:  python -X utf8 -m unittest test_score -v
"""
import contextlib
import csv
import io
import json
import pathlib
import tempfile
import unittest

import score

PASSPORT = score.load_passport()


def solar_row(**overrides):
    row = {
        "publication_id": "D001", "result_id": "R001", "device_id": "DV1",
        "measurement_id": "MS1", "material_id": "M1", "metric_name": "PCE",
        "metric_value": "20.0", "metric_unit": "%", "normalized_value": "20.0",
        "normalized_unit": "%", "extraction_status": "extracted",
    }
    row.update(overrides)
    return row


def rag_row(**overrides):
    row = {
        "publication_id": "D001", "result_id": "R001", "experiment_id": "E1",
        "config_id": "C01", "dataset_id": "DS1", "task_id": "T1",
        "metric_group": "answer quality", "metric_name": "EM",
        "metric_variant": "", "metric_value": "0.204", "metric_unit": "",
        "normalized_value": "0.204", "normalized_unit": "",
        "extraction_status": "extracted",
    }
    row.update(overrides)
    return row


def write_table(folder, area, table, rows):
    columns = PASSPORT["areas"][area]["tables"][table]["columns"]
    path = pathlib.Path(folder) / f"{table}.csv"
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({c: row.get(c, "") for c in columns})


class ScorerTest(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory(prefix="score_test_")
        self.root = pathlib.Path(self._temp.name)
        self.pred = self.root / "pred"
        self.ref = self.root / "ref"
        self.pred.mkdir()
        self.ref.mkdir()

    def tearDown(self):
        self._temp.cleanup()

    def run_area(self, area, samples=3):
        return score.score_area(
            area, score.DirSource(self.pred, PASSPORT),
            score.DirSource(self.ref, PASSPORT), PASSPORT, samples=samples)

    def results(self, area):
        return self.run_area(area)["tables"]["results"]

    # 1. perfect match -----------------------------------------------------
    def test_perfect_match_is_one(self):
        rows = [solar_row(), solar_row(result_id="R002", metric_name="Voc",
                                       metric_value="1.197",
                                       normalized_value="1.197")]
        write_table(self.pred, "solar", "results", rows)
        write_table(self.ref, "solar", "results", rows)

        entry = self.results("solar")
        self.assertEqual(entry["matched"], 2)
        self.assertEqual(entry["pred_only"], 0)
        self.assertEqual(entry["ref_only"], 0)
        self.assertEqual(entry["precision"], 1.0)
        self.assertEqual(entry["recall"], 1.0)
        self.assertEqual(entry["f1"], 1.0)

    # 2. false positive ----------------------------------------------------
    def test_false_positive_lowers_precision_only(self):
        gold = [solar_row()]
        write_table(self.ref, "solar", "results", gold)
        write_table(self.pred, "solar", "results", gold + [
            solar_row(metric_name="FF", metric_value="71.0",
                      normalized_value="71.0")])

        entry = self.results("solar")
        self.assertEqual(entry["matched"], 1)
        self.assertEqual(entry["pred_only"], 1)
        self.assertEqual(entry["ref_only"], 0)
        self.assertAlmostEqual(entry["precision"], 0.5)
        self.assertEqual(entry["recall"], 1.0)
        self.assertIn("metric_name=FF", entry["pred_only_samples"][0])

    # 3. missed row --------------------------------------------------------
    def test_missed_row_lowers_recall_only(self):
        gold = [solar_row(), solar_row(device_id="DV2", result_id="R002")]
        write_table(self.ref, "solar", "results", gold)
        write_table(self.pred, "solar", "results", [solar_row()])

        entry = self.results("solar")
        self.assertEqual(entry["matched"], 1)
        self.assertEqual(entry["pred_only"], 0)
        self.assertEqual(entry["ref_only"], 1)
        self.assertEqual(entry["precision"], 1.0)
        self.assertAlmostEqual(entry["recall"], 0.5)
        self.assertIn("device_id=DV2", entry["ref_only_samples"][0])

    # 4. near match --------------------------------------------------------
    def test_last_digit_difference_is_near_not_matched(self):
        write_table(self.ref, "solar", "results", [
            solar_row(metric_value="0.204", normalized_value="0.204")])
        write_table(self.pred, "solar", "results", [
            solar_row(metric_value="0.205", normalized_value="0.205")])

        entry = self.results("solar")
        self.assertEqual(entry["matched"], 0)
        self.assertEqual(entry["pred_only"], 1)
        self.assertEqual(entry["ref_only"], 1)
        self.assertEqual(entry["near_match"]["total"], 1)
        self.assertEqual(entry["near_match"]["last_digit"], 1)
        self.assertEqual(entry["near_match"]["unit_scale"], 0)

    def test_percent_against_fraction_is_a_unit_scale_near_match(self):
        write_table(self.ref, "solar", "results", [
            solar_row(metric_value="71.0", normalized_value="71.0")])
        write_table(self.pred, "solar", "results", [
            solar_row(metric_value="0.71", normalized_value="0.71")])

        entry = self.results("solar")
        self.assertEqual(entry["matched"], 0)
        self.assertEqual(entry["near_match"]["unit_scale"], 1)
        self.assertEqual(entry["near_match"]["last_digit"], 0)

    # tolerance: coarser reported rounding still matches -------------------
    def test_coarser_reported_rounding_matches(self):
        write_table(self.ref, "solar", "results", [
            solar_row(metric_value="0.330", normalized_value="0.330")])
        write_table(self.pred, "solar", "results", [
            solar_row(metric_value="0.33", normalized_value="0.33")])

        entry = self.results("solar")
        self.assertEqual(entry["matched"], 1)
        self.assertEqual(entry["near_match"]["total"], 0)
        # 0.331 rounded to the coarser 2 decimals is 0.33, so the rule accepts it.
        self.assertTrue(score.values_equal("0.33", "0.330"))
        self.assertTrue(score.values_equal("0.33", "0.331"))
        self.assertFalse(score.values_equal("0.33", "0.34"))

    # 5. duplicate-value trap ---------------------------------------------
    def test_same_value_under_two_configs_is_two_results(self):
        gold = [rag_row(config_id="C01"),
                rag_row(config_id="C02", result_id="R002")]
        write_table(self.ref, "rag", "results", gold)

        # A value-only scorer would call this one hit. The key must keep both.
        write_table(self.pred, "rag", "results", [rag_row(config_id="C01")])
        entry = self.results("rag")
        self.assertEqual(entry["matched"], 1)
        self.assertEqual(entry["ref_only"], 1)
        self.assertAlmostEqual(entry["recall"], 0.5)
        self.assertIn("config_id=C02", entry["ref_only_samples"][0])

        # Both configurations present, result_id renamed and reordered.
        write_table(self.pred, "rag", "results", [
            rag_row(config_id="C02", result_id="R900"),
            rag_row(config_id="C01", result_id="R901")])
        entry = self.results("rag")
        self.assertEqual(entry["matched"], 2)
        self.assertEqual(entry["pred_only"], 0)
        self.assertEqual(entry["ref_only"], 0)
        self.assertEqual(entry["f1"], 1.0)

    def test_result_id_is_never_part_of_the_key(self):
        write_table(self.ref, "solar", "results", [solar_row(result_id="R001")])
        write_table(self.pred, "solar", "results", [solar_row(result_id="R777")])
        self.assertEqual(self.results("solar")["matched"], 1)

    # multiset: identical rows count twice ---------------------------------
    def test_identical_rows_are_matched_as_a_multiset(self):
        row = rag_row()
        write_table(self.ref, "rag", "results", [row, dict(row)])
        write_table(self.pred, "rag", "results", [dict(row)])

        entry = self.results("rag")
        self.assertEqual(entry["matched"], 1)
        self.assertEqual(entry["ref_only"], 1)
        self.assertAlmostEqual(entry["recall"], 0.5)

    # non-results tables are matched by identity ---------------------------
    def test_publications_match_by_id(self):
        write_table(self.ref, "solar", "publications", [
            {"publication_id": "D001", "title": "A", "in_scope": "true"}])
        write_table(self.pred, "solar", "publications", [
            {"publication_id": "D001", "title": "B", "in_scope": "true"}])
        entry = self.run_area("solar")["tables"]["publications"]
        self.assertEqual(entry["matched"], 1)
        self.assertEqual(entry["key"], ["publication_id"])

    # reporting ------------------------------------------------------------
    def test_metric_breakdown_and_totals(self):
        gold = [solar_row(metric_name="PCE"),
                solar_row(metric_name="Jsc", metric_value="15.28",
                          normalized_value="15.28", result_id="R002")]
        write_table(self.ref, "solar", "results", gold)
        write_table(self.pred, "solar", "results", gold[:1])

        report = self.run_area("solar")
        metrics = {row["metric_name"]: row for row in report["tables"]["results"]["by_metric"]}
        self.assertEqual(metrics["PCE"]["f1"], 1.0)
        self.assertEqual(metrics["Jsc"]["recall"], 0.0)
        self.assertEqual(report["totals"]["pred_rows"], 1)
        self.assertEqual(report["totals"]["ref_rows"], 2)

    def test_cli_json_output(self):
        write_table(self.pred, "solar", "results", [solar_row()])
        write_table(self.ref, "solar", "results", [solar_row()])
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = score.main(["--area", "solar", "--pred", str(self.pred),
                               "--ref", str(self.ref), "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(buffer.getvalue())
        self.assertEqual(payload["areas"]["solar"]["tables"]["results"]["f1"], 1.0)
        self.assertIn("not_measured", payload)


@unittest.skipUnless(score.DEFAULT_DB.exists(), "data/viniti.sqlite3 не импортирован")
class GoldAgainstDatabaseTest(unittest.TestCase):
    """The organisers' gold scored against the same rows in the database."""

    def test_solar_gold_against_db(self):
        passport = PASSPORT
        gold = score.ROOT / "_corpus" / "area1" / "dev_gold"
        if not gold.is_dir():
            self.skipTest("нет _corpus/area1/dev_gold")
        report = score.score_area(
            "solar", score.DirSource(gold, passport),
            score.DbSource(score.DEFAULT_DB, passport), passport)
        self.assertEqual(report["tables"]["results"]["matched"], 659)
        self.assertEqual(report["totals"]["f1"], 1.0)

    def test_rag_gold_against_db(self):
        passport = PASSPORT
        gold = (score.ROOT / "_corpus" / "rag"
                / "RAG_ВИНИТИ_участникам_2026-11-20" / "dev_gold")
        if not gold.is_dir():
            self.skipTest("нет _corpus/rag dev_gold")
        report = score.score_area(
            "rag", score.DirSource(gold, passport),
            score.DbSource(score.DEFAULT_DB, passport), passport)
        self.assertEqual(report["tables"]["results"]["matched"], 37616)
        self.assertEqual(report["totals"]["f1"], 1.0)


if __name__ == "__main__":
    unittest.main()
