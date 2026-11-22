"""Tests for export_csv.py: byte-level submission format, roundtrip fidelity, custom slices.

Everything is built from a synthetic passport in tempfile.TemporaryDirectory(). No test
touches _corpus/, data/viniti.sqlite3 or passport.json, so the suite runs in CI where the
corpus is absent.

What is protected here: exporting gold data reproduces the organisers' dev_gold byte for
byte. Solar is UTF-8 with BOM and CRLF, rag is UTF-8 without BOM and CRLF. Values are
written exactly as stored, because the validator compares duplicates by written precision.
"""
import codecs
import contextlib
import csv
import io
import pathlib
import sqlite3
import tempfile
import unittest

from viniti import db as storage, export_csv

RESULTS_COLUMNS = [
    "publication_id",
    "result_id",
    "metric_name",
    "metric_value",
    "metric_unit",
    "source_page",
    "source_fragment",
    "raw_value",
]

PUBLICATION_COLUMNS = ["publication_id", "title", "year", "in_scope"]


def synthetic_passport():
    """Solar keeps the BOM, rag does not; both use CRLF, exactly like the real passport."""
    def area(bom):
        return {
            "bom": bom,
            "newline": "\r\n",
            "tables": {
                "publications": {
                    "key": "publication_id",
                    "columns": list(PUBLICATION_COLUMNS),
                    "fields": {"year": {"type": "number"}},
                },
                "results": {
                    "key": "result_id",
                    "columns": list(RESULTS_COLUMNS),
                    "fields": {"metric_value": {"type": "number"},
                               "source_page": {"type": "number"}},
                },
            },
        }
    return {
        "version": 1,
        "generated_from": "test_export.synthetic_passport",
        "areas": {"solar": area(True), "rag": area(False)},
    }


def read_rows(path):
    with pathlib.Path(path).open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.reader(stream))


def read_header(path):
    return read_rows(path)[0]


class ExportFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="viniti-export-")
        self.addCleanup(self.tmp.cleanup)
        self.dir = pathlib.Path(self.tmp.name)
        self.passport = synthetic_passport()
        self.path = self.dir / "viniti.sqlite3"
        storage.init(self.path, self.passport)
        self.conn = storage.connect(self.path)
        self.addCleanup(self.conn.close)
        self.populate()

    def spec(self, area):
        return self.passport["areas"][area]

    def insert_result(self, area, publication_id, result_id, metric_name, metric_value,
                      metric_unit, source_page, source_fragment, raw_value=""):
        name = storage.quote(storage.table_name(area, "results"))
        self.conn.execute(
            f"INSERT INTO {name} (area, publication_id, result_id, metric_name, "
            f"metric_value, metric_unit, source_page, source_fragment, raw_value, "
            f"value_num, page_num) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (area, publication_id, result_id, metric_name, metric_value, metric_unit,
             source_page, source_fragment, raw_value, _number(metric_value),
             _number(source_page)))
        self.conn.commit()

    def populate(self):
        """Three rows, no embedded newlines, so line-ending checks stay meaningful."""
        self.insert_result("solar", "P001", "L1", "efficiency", "0.330", "percent", "7",
                           "table 1, row 2")
        self.insert_result("solar", "P001", "L2", "recall", "0.912", "fraction", "8",
                           'quoted "text" here')
        self.insert_result("solar", "P002", "L1", "recall", "EM decrease", "percent", "9",
                           "page 9 body")
        self.insert_result("rag", "D001", "L1", "mrr", "0.330", "percent", "3", "table 2")


def _number(text):
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


class OfficialExportTest(ExportFixture):
    def export(self, area):
        outdir = self.dir / "out" / area
        counts = export_csv.export_official(self.conn, area, self.spec(area), outdir)
        return outdir, counts

    def test_column_order_matches_passport(self):
        for area in ("solar", "rag"):
            outdir, _ = self.export(area)
            for table, meta in self.spec(area)["tables"].items():
                with self.subTest(area=area, table=table):
                    self.assertEqual(read_header(outdir / f"{table}.csv"), meta["columns"])

    def test_export_official_returns_row_counts_per_table(self):
        _, counts = self.export("solar")
        self.assertEqual(counts, {"publications": 0, "results": 3})
        _, counts = self.export("rag")
        self.assertEqual(counts, {"publications": 0, "results": 1})

    def test_solar_uses_bom_and_crlf(self):
        outdir, _ = self.export("solar")
        raw = (outdir / "results.csv").read_bytes()
        self.assertTrue(raw.startswith(codecs.BOM_UTF8), "solar must start with a UTF-8 BOM")
        self.assertEqual(raw.count(codecs.BOM_UTF8), 1)
        self.assertIn(b"\r\n", raw)
        self.assertEqual(raw.replace(b"\r\n", b"").count(b"\n"), 0, "stray LF in solar export")
        self.assertEqual(raw.replace(b"\r\n", b"").count(b"\r"), 0, "stray CR in solar export")
        self.assertTrue(raw.endswith(b"\r\n"))

    def test_rag_uses_crlf_without_bom(self):
        outdir, _ = self.export("rag")
        raw = (outdir / "results.csv").read_bytes()
        self.assertFalse(raw.startswith(codecs.BOM_UTF8), "rag must not carry a BOM")
        self.assertEqual(raw.count(codecs.BOM_UTF8), 0)
        self.assertIn(b"\r\n", raw)
        self.assertEqual(raw.replace(b"\r\n", b"").count(b"\n"), 0, "stray LF in rag export")
        self.assertTrue(raw.endswith(b"\r\n"))

    def test_official_export_keeps_written_precision(self):
        outdir, _ = self.export("solar")
        raw = (outdir / "results.csv").read_bytes()
        self.assertIn(b"0.330", raw)
        self.assertNotIn(b",0.33,", raw)


class RoundtripTest(ExportFixture):
    def populate(self):
        self.insert_result("solar", "P001", "L1", "efficiency", "0.330", "percent", "7",
                           'a, b "quoted"\nsecond line')

    def test_value_with_comma_quote_and_newline_survives_quoting(self):
        outdir = self.dir / "out"
        export_csv.export_official(self.conn, "solar", self.spec("solar"), outdir)

        rows = read_rows(outdir / "results.csv")
        header = rows[0]
        record = dict(zip(header, rows[1]))
        self.assertEqual(record["source_fragment"], 'a, b "quoted"\nsecond line')

    def test_written_value_roundtrips_as_the_same_string(self):
        outdir = self.dir / "out"
        export_csv.export_official(self.conn, "solar", self.spec("solar"), outdir)

        with (outdir / "results.csv").open(encoding="utf-8-sig", newline="") as stream:
            record = next(csv.DictReader(stream))
        self.assertEqual(record["metric_value"], "0.330")
        self.assertEqual(record["source_page"], "7")
        self.assertNotEqual(record["metric_value"], "0.33")


class CustomExportTest(ExportFixture):
    def export_custom(self, target, table="results", columns="", wheres=None,
                      order="", limit=0, area="solar"):
        return export_csv.export_custom(self.conn, area, self.spec(area), target, table,
                                        columns, wheres or [], order, limit)

    def test_custom_always_keeps_source_locators(self):
        target = self.dir / "slice.csv"
        self.export_custom(target, columns="metric_name,metric_value")

        rows = read_rows(target)
        header = rows[0]
        self.assertEqual(header[:2], ["metric_name", "metric_value"])
        self.assertIn("source_page", header)
        self.assertIn("source_fragment", header)
        self.assertEqual(len(rows) - 1, 3)
        for record in (dict(zip(header, row)) for row in rows[1:]):
            self.assertTrue(record["source_page"], "row exported without its page")
            self.assertTrue(record["source_fragment"], "row exported without its fragment")

    def test_custom_keeps_locators_when_asked_for_them_explicitly(self):
        target = self.dir / "slice.csv"
        self.export_custom(target, columns="metric_name,source_page")
        self.assertEqual(read_header(target).count("source_page"), 1)

    def test_custom_unknown_column_raises_and_writes_nothing(self):
        target = self.dir / "bad.csv"
        with self.assertRaises(SystemExit):
            self.export_custom(target, columns="metric_name,no_such_field")
        self.assertFalse(target.exists())

    def test_custom_where_returns_only_matching_rows(self):
        target = self.dir / "percent.csv"
        count = self.export_custom(target, columns="metric_name,metric_unit",
                                   wheres=["metric_unit=percent"])
        rows = read_rows(target)
        self.assertEqual(count, 2)
        self.assertEqual(len(rows) - 1, 2)
        for record in (dict(zip(rows[0], row)) for row in rows[1:]):
            self.assertEqual(record["metric_unit"], "percent")

    def test_custom_where_on_unknown_field_does_not_return_everything(self):
        target = self.dir / "bad_where.csv"
        with self.assertRaises((SystemExit, sqlite3.Error)):
            self.export_custom(target, wheres=["no_such_field=x"])
        self.assertFalse(target.exists())

    def test_custom_numeric_range_uses_the_numeric_mirror(self):
        # Documented CLI usage: --where "value_num>20". Text comparison would sort '9.9' > '10.5'.
        target = self.dir / "range.csv"
        count = self.export_custom(target, columns="metric_name,metric_value",
                                   wheres=["value_num>0.5"])
        rows = read_rows(target)
        self.assertEqual(count, 1)
        self.assertEqual(dict(zip(rows[0], rows[1]))["metric_value"], "0.912")

    def test_custom_numeric_filter_on_a_number_field_uses_its_mirror(self):
        # metric_value is a number field, so the clause must not compare the TEXT column:
        # as text '9.9' > '10' is true, as numbers it is false.
        self.insert_result("solar", "P003", "L1", "nine", "9.9", "percent", "7", "f1")
        self.insert_result("solar", "P003", "L2", "ten", "10.5", "percent", "8", "f2")

        target = self.dir / "numbers.csv"
        count = self.export_custom(target, columns="metric_name,metric_value",
                                   wheres=["metric_value>10"])

        rows = read_rows(target)
        self.assertEqual(count, 1)
        self.assertEqual(dict(zip(rows[0], rows[1]))["metric_value"], "10.5")

    def test_custom_sort_on_a_number_field_uses_its_mirror(self):
        self.insert_result("solar", "P003", "L1", "nine", "9.9", "percent", "7", "f1")
        self.insert_result("solar", "P003", "L2", "ten", "10.5", "percent", "8", "f2")

        target = self.dir / "sorted.csv"
        self.export_custom(target, columns="metric_value", order="-metric_value")

        rows = read_rows(target)
        values = [record["metric_value"]
                  for record in (dict(zip(rows[0], row)) for row in rows[1:])]
        self.assertEqual(values[:2], ["10.5", "9.9"])

    def test_numeric_filter_on_a_text_field_reports_rows_without_a_number(self):
        # raw_value is TEXT in the passport; a numeric filter drops it silently unless reported.
        target = self.dir / "raw.csv"
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.export_custom(target, wheres=["raw_value>0"])
        message = err.getvalue()
        self.assertIn("raw_value", message)
        self.assertIn("без числового значения", message)

    def test_numeric_filter_warning_names_the_field_not_just_the_mirror(self):
        # 'EM decrease' has no number: the slice is smaller than the table and must say so.
        target = self.dir / "warn.csv"
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.export_custom(target, wheres=["metric_value>0"])
        self.assertIn("metric_value", err.getvalue())

    def test_custom_defaults_to_the_full_column_list(self):
        target = self.dir / "full.csv"
        self.export_custom(target, columns="")
        self.assertEqual(read_header(target), self.spec("solar")["tables"]["results"]["columns"])

    def test_custom_keeps_rag_area_free_of_bom(self):
        target = self.dir / "rag_slice.csv"
        self.export_custom(target, columns="metric_name", area="rag")
        raw = target.read_bytes()
        self.assertFalse(raw.startswith(codecs.BOM_UTF8))
        self.assertIn(b"\r\n", raw)


if __name__ == "__main__":
    unittest.main()
