from copy import deepcopy
import csv
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from viniti.evaluate_extraction import evaluate, write_report
from viniti.solar_schema import TABLES


ROOT = Path(__file__).resolve().parents[1]


def fixture(prefix=""):
    tables = {name: [] for name in TABLES}

    def add(table, **fields):
        row = dict.fromkeys(TABLES[table], "")
        row.update(publication_id="D001", **fields)
        tables[table].append(row)

    add("publications", in_scope="true")
    add("devices", device_id=prefix + "DV1", device_label="control", architecture="n-i-p")
    add("measurements", measurement_id=prefix + "MS1", device_id=prefix + "DV1",
        measurement_type="J-V", scan_direction="reverse",
        light_intensity="100", intensity_unit="mW/cm2", spectrum="AM1.5G")
    add("results", result_id=prefix + "R1", device_id=prefix + "DV1",
        measurement_id=prefix + "MS1", metric_name="PCE", normalized_value="20",
        normalized_unit="%", source_page="5", extraction_status="extracted",
        result_origin="primary", aggregation="champion")
    return tables


def write_bundle(directory, tables):
    directory.mkdir(exist_ok=True)
    for table, columns in TABLES.items():
        with (directory / f"{table}.csv").open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=columns)
            writer.writeheader()
            writer.writerows(tables[table])


class EvaluateExtractionTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.pred = self.root / "pred"
        self.gold = self.root / "gold"
        self.p = fixture("different-")
        self.g = fixture()

    def run_evaluation(self, **kwargs):
        write_bundle(self.pred, self.p)
        write_bundle(self.gold, self.g)
        return evaluate(self.pred, self.gold, **kwargs)

    def test_perfect_different_ids_and_decimal_equivalence(self):
        self.p["results"][0]["normalized_value"] = "20.00"
        self.p["measurements"][0]["light_intensity"] = "100.0"
        report = self.run_evaluation()
        for metric in ("basic", "context"):
            self.assertEqual(report["micro"][metric]["f1"], 1)
        self.assertEqual(report["scope"]["matched"], 1)
        json.dumps(report, allow_nan=False)

    def test_tolerance_does_not_round_away_difference(self):
        self.p["results"][0]["normalized_value"] = "1"
        self.g["results"][0]["normalized_value"] = "-1e-40"
        report = self.run_evaluation(tolerance="1")
        self.assertEqual(report["micro"]["basic"]["tp"], 0)
        self.assertEqual(report["micro"]["basic"]["fp"], 1)

    def test_exact_tolerance_boundary(self):
        self.p["results"][0]["normalized_value"] = "1"
        self.g["results"][0]["normalized_value"] = "0"
        self.assertEqual(self.run_evaluation(tolerance="1")["micro"]["basic"]["tp"], 1)

    def test_extreme_exponent_is_clean_error_before_matching(self):
        self.p["results"][0]["normalized_value"] = "1e1000000"
        with self.assertRaises(ValueError):
            self.run_evaluation(tolerance="1")

    def test_report_write_failure_leaves_no_final_or_staging(self):
        output = self.root / "report.json"
        original = Path.write_text
        def fail_after_partial_write(path, text, **kwargs):
            original(path, text[:5], **kwargs)
            raise OSError("disk full")
        with patch.object(Path, "write_text", fail_after_partial_write), self.assertRaises(OSError):
            write_report({"report": "complete"}, output)
        self.assertFalse(output.exists())
        self.assertEqual(list(self.root.iterdir()), [])

    def test_atomic_report_never_replaces_existing_destination(self):
        output = self.root / "report.json"
        output.write_text("keep me", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            write_report({"new": "report"}, output)
        self.assertEqual(output.read_text(), "keep me")
        self.assertEqual(list(self.root.iterdir()), [output])

    def test_value_unit_page_mismatch(self):
        for field, value in (("normalized_value", "21"), ("normalized_unit", "V"),
                             ("source_page", "6")):
            with self.subTest(field=field):
                self.p = fixture("different-")
                self.p["results"][0][field] = value
                scores = self.run_evaluation()["micro"]["basic"]
                self.assertEqual((scores["tp"], scores["fp"], scores["fn"]), (0, 1, 1))

    def test_context_mismatch_does_not_affect_basic(self):
        for table, field, value in (
            ("devices", "device_label", "target"),
            ("devices", "architecture", "p-i-n"),
            ("measurements", "scan_direction", "forward"),
            ("measurements", "spectrum", ""),
            ("results", "aggregation", "mean"),
            ("results", "result_origin", "cited"),
            ("results", "time_point", "1"),
        ):
            with self.subTest(field=field):
                self.p = fixture("different-")
                self.p[table][0][field] = value
                report = self.run_evaluation()["micro"]
                self.assertEqual(report["basic"]["tp"], 1)
                self.assertEqual(report["context"]["tp"], 0)

    def test_shared_measurement_device_list(self):
        self.p["devices"].append(dict(self.p["devices"][0], device_id="other",
                                      device_label="other"))
        self.p["measurements"][0]["device_id"] += ";other"
        self.assertEqual(self.run_evaluation()["micro"]["context"]["tp"], 1)

    def test_linked_duplicates_are_mentions_without_double_matching(self):
        second = dict(self.g["results"][0], result_id="R2", duplicate_of="R1")
        self.g["results"].append(second)
        report = self.run_evaluation()["micro"]["basic"]
        self.assertEqual((report["tp"], report["fp"], report["fn"]), (1, 0, 1))
        self.p["results"].append(dict(self.p["results"][0], result_id="P2",
                                      duplicate_of="different-R1"))
        self.p["results"].append(dict(self.p["results"][0], result_id="P3"))
        report = self.run_evaluation()["micro"]["basic"]
        self.assertEqual((report["tp"], report["fp"], report["fn"]), (2, 1, 0))

    def test_empty_prediction(self):
        self.p = {table: [] for table in TABLES}
        report = self.run_evaluation()
        self.assertEqual(report["micro"]["basic"]["recall"], 0)
        self.assertEqual(report["micro"]["basic"]["fn"], 1)
        self.assertIsNone(report["scope"]["per_publication"]["D001"]["predicted"])

    def test_unnormalized_prediction_is_abstention_not_correct_claim(self):
        for value, unit in (("", ""), ("", "%"), ("20", "")):
            with self.subTest(value=value, unit=unit):
                self.p["results"][0].update(normalized_value=value, normalized_unit=unit,
                                            raw_value="20", raw_unit="unconfirmed")
                report = self.run_evaluation()
                self.assertEqual(report["excluded_results"]["predicted"], {"unnormalized": 1})
                for name in ("basic", "context"):
                    score = report["micro"][name]
                    self.assertEqual((score["tp"], score["fp"], score["fn"]), (0, 0, 1))
                    self.assertEqual((score["precision"], score["recall"], score["f1"]), (0, 0, 0))

    def test_unnormalized_gold_is_excluded(self):
        self.g["results"][0].update(normalized_value="", normalized_unit="")
        report = self.run_evaluation()
        self.assertEqual(report["excluded_results"]["gold"], {"unnormalized": 1})
        self.assertEqual(report["micro"]["basic"]["fp"], 1)
        self.assertEqual(report["micro"]["basic"]["fn"], 0)

    def test_malformed_nonempty_value_is_not_abstention(self):
        for tables in (self.p, self.g):
            for value in ("NaN", "-Infinity", "not-a-number"):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    tables["results"][0].update(normalized_value=value, normalized_unit="")
                    self.run_evaluation()
            tables["results"][0].update(normalized_value="20", normalized_unit="%")

    def test_source_page_is_ascii_positive_integer_even_for_abstention(self):
        for value, unit in (("20", "%"), ("", "")):
            for page in ("", "0", "-1", "1.0", "1e0", "١", "１", "1;2", "01"):
                with self.subTest(page=page, value=value), self.assertRaisesRegex(ValueError, "source_page"):
                    self.p["results"][0].update(normalized_value=value, normalized_unit=unit,
                                                source_page=page)
                    self.run_evaluation()

    def test_macro_equal_paper_weight_and_empty_papers_zero(self):
        # D001: three TP; D002: one FN; D003: no scored mentions.
        for tables in (self.p, self.g):
            for index in (2, 3):
                tables["results"].append(dict(tables["results"][0], result_id=f"R{index}"))
            tables["publications"].extend([
                dict(tables["publications"][0], publication_id="D002"),
                dict(tables["publications"][0], publication_id="D003"),
            ])
        self.g["results"].append(dict(self.g["results"][0], publication_id="D002",
                                     device_id="", measurement_id=""))
        report = self.run_evaluation()
        for name in ("basic", "context"):
            self.assertEqual(report["micro"][name]["recall"], 0.75)
            for field in ("precision", "recall", "f1"):
                self.assertAlmostEqual(report["macro"][name][field], 1 / 3)
            self.assertEqual(report["per_publication"]["D003"][name]["f1"], 0)
        selected = self.run_evaluation(publications=["D001"])
        self.assertEqual(selected["macro"]["basic"]["f1"], 1)

    def test_selection_isolation_and_default_gold_scope(self):
        for tables in (self.p, self.g):
            other = deepcopy(tables)
            for table in TABLES:
                for row in other[table]:
                    row["publication_id"] = "D002"
                tables[table].extend(other[table])
        self.p["results"][1]["normalized_value"] = "99"
        selected = self.run_evaluation(publications=["D001"])
        self.assertEqual(selected["micro"]["basic"]["f1"], 1)
        self.assertEqual(selected["predicted_publications_outside_selection"], ["D002"])
        self.assertEqual(selected["excluded_results"]["predicted"]["outside_selected_publications"], 1)
        self.assertEqual(self.run_evaluation()["micro"]["basic"]["fn"], 1)
        with self.assertRaises(ValueError):
            self.run_evaluation(publications=[])
        with self.assertRaises(ValueError):
            self.run_evaluation(publications=["D999"])

    def test_unknown_publication_fails(self):
        for rows in self.p.values():
            for row in rows:
                row["publication_id"] = "foreign"
        with self.assertRaisesRegex(ValueError, "absent from gold"):
            self.run_evaluation()

    def test_exclusions_and_scope_separate(self):
        self.p["publications"][0]["in_scope"] = "false"
        self.p["results"].append(dict(self.p["results"][0], result_id="unknown",
                                      metric_name="unsupported"))
        self.p["results"].append(dict(self.p["results"][0], result_id="absent",
                                      extraction_status="absent", normalized_value=""))
        report = self.run_evaluation()
        self.assertEqual(report["micro"]["basic"]["tp"], 1)
        self.assertEqual(report["scope"]["matched"], 0)
        self.assertEqual(report["excluded_results"]["predicted"],
                         {"metric:unsupported": 1, "status:absent": 1})
        self.assertEqual(self.run_evaluation(metrics=["unsupported"])["micro"]["basic"]["fp"], 1)

    def test_tolerance_one_to_one(self):
        self.g["results"][0]["normalized_value"] = "1"
        self.g["results"].append(dict(self.g["results"][0], result_id="R2",
                                      normalized_value="1.2"))
        self.p["results"][0]["normalized_value"] = "1.1"
        self.p["results"].append(dict(self.p["results"][0], result_id="P2",
                                      normalized_value="1.3"))
        self.assertEqual(self.run_evaluation()["micro"]["basic"]["tp"], 0)
        self.assertEqual(self.run_evaluation(tolerance="0.1")["micro"]["basic"]["tp"], 2)
        for tolerance in ("-1", "NaN", "Infinity", "oops"):
            with self.subTest(tolerance=tolerance), self.assertRaises(ValueError):
                self.run_evaluation(tolerance=tolerance)

    def test_bad_extracted_input(self):
        for field, value in (
            ("normalized_value", "NaN"), ("normalized_value", "Infinity"),
            ("source_page", ""),
            ("device_id", "unknown"), ("measurement_id", "unknown"),
            ("extraction_status", "unknown"), ("time_point", "NaN"),
        ):
            with self.subTest(field=field, value=value):
                self.p = fixture()
                self.p["results"][0][field] = value
                with self.assertRaises(ValueError):
                    self.run_evaluation()

    def test_missing_file_columns_and_malformed_rows(self):
        self.run_evaluation()
        path = self.pred / "results.csv"
        path.unlink()
        with self.assertRaises(ValueError):
            evaluate(self.pred, self.gold)
        path.write_text("publication_id,result_id\nD001,R1\n")
        with self.assertRaises(ValueError):
            evaluate(self.pred, self.gold)
        write_bundle(self.pred, self.p)
        with path.open("a") as stream:
            stream.write("D001,short\n")
        with self.assertRaisesRegex(ValueError, "malformed"):
            evaluate(self.pred, self.gold)

    def test_cli_and_no_overwrite_or_partial_output_on_validation_error(self):
        self.run_evaluation()
        output = self.root / "report.json"
        command = [sys.executable, "-m", "viniti.evaluate_extraction",
                   str(self.pred), str(self.gold), "--publication", "D001",
                   "-o", str(output)]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        original = output.read_bytes()
        self.assertEqual(json.loads(original)["micro"]["basic"]["tp"], 1)
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(output.read_bytes(), original)
        output.unlink()
        (self.pred / "results.csv").unlink()
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
