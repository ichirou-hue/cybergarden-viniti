"""Тесты импорта эталона: потеря строк, лишние колонки, повторный импорт.

Самодостаточно: собирает маленький эталон в tempfile по образцу настоящего,
корпус _corpus/ не нужен - в CI его нет.
"""
import csv
import pathlib
from tempfile import TemporaryDirectory
import unittest

import db as storage
import import_gold


PASSPORT = {
    "version": 1,
    "areas": {
        "solar": {
            "bom": True,
            "newline": "\r\n",
            "tables": {
                "publications": {
                    "key": "publication_id",
                    "columns": ["publication_id", "title", "year", "in_scope", "exclusion_reason"],
                    "fields": {},
                },
                "devices": {
                    "key": "device_id",
                    "columns": ["publication_id", "device_id", "architecture", "active_area"],
                    "fields": {"active_area": {"type": "number"}},
                },
                "results": {
                    "key": "result_id",
                    "columns": ["publication_id", "result_id", "metric_name",
                                "metric_value", "source_page"],
                    "fields": {"metric_value": {"type": "number"},
                               "source_page": {"type": "number"}},
                },
            },
        },
    },
}


def write_csv(path, columns, rows, bom=True):
    encoding = "utf-8-sig" if bom else "utf-8"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding=encoding, newline="") as stream:
        writer = csv.writer(stream, lineterminator="\r\n")
        writer.writerow(columns)
        writer.writerows(rows)


def build_gold(root, results_rows, device_rows=None, extra_column=False):
    columns = PASSPORT["areas"]["solar"]["tables"]["results"]["columns"]
    if extra_column:
        columns = columns + ["лишняя"]
        results_rows = [row + ["x"] for row in results_rows]
    write_csv(root / "results.csv", columns, results_rows)
    write_csv(root / "publications.csv",
              PASSPORT["areas"]["solar"]["tables"]["publications"]["columns"],
              [["D001", "Статья", "2024", "true", ""]])
    write_csv(root / "devices.csv",
              PASSPORT["areas"]["solar"]["tables"]["devices"]["columns"],
              device_rows if device_rows is not None else [["D001", "DEV1", "nip", ""]])


def count(db_path, table, area="solar"):
    conn = storage.connect(db_path)
    try:
        return conn.execute(
            f"SELECT COUNT(*) FROM {storage.quote(storage.table_name(area, table))} "
            f"WHERE area = ?", (area,)).fetchone()[0]
    finally:
        conn.close()


class ImportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.gold = self.root / "gold"
        self.database = self.root / "test.sqlite3"
        storage.init(self.database, PASSPORT)

    def tearDown(self):
        self.tmp.cleanup()

    def test_values_survive_as_written(self):
        build_gold(self.gold, [["D001", "R1", "PCE", "0.330", "6"]])
        import_gold.import_area("solar", self.gold, self.database, PASSPORT)
        conn = storage.connect(self.database)
        try:
            row = conn.execute(
                "SELECT metric_value, value_num FROM solar_results").fetchone()
        finally:
            conn.close()
        self.assertEqual(row["metric_value"], "0.330")
        self.assertAlmostEqual(row["value_num"], 0.33)

    def test_import_fills_every_mirror_of_the_table(self):
        """Зеркала генерируются из паспорта: заполняться должно не только value_num."""
        build_gold(self.gold, [["D001", "R1", "PCE", "0.330", "6"]],
                   device_rows=[["D001", "DEV1", "nip", "0.25"]])
        import_gold.import_area("solar", self.gold, self.database, PASSPORT)
        conn = storage.connect(self.database)
        try:
            device = conn.execute(
                "SELECT active_area, active_area_num FROM solar_devices").fetchone()
            result = conn.execute(
                "SELECT metric_value, value_num, source_page, page_num FROM solar_results"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(device["active_area"], "0.25")
        self.assertAlmostEqual(device["active_area_num"], 0.25)
        self.assertAlmostEqual(result["value_num"], 0.33)
        self.assertAlmostEqual(result["page_num"], 6.0)

    def test_non_numeric_text_leaves_the_mirror_null(self):
        build_gold(self.gold, [["D001", "R1", "PCE", "не число", "6"]])
        import_gold.import_area("solar", self.gold, self.database, PASSPORT)
        conn = storage.connect(self.database)
        try:
            row = conn.execute(
                "SELECT metric_value, value_num FROM solar_results").fetchone()
        finally:
            conn.close()
        self.assertEqual(row["metric_value"], "не число")
        self.assertIsNone(row["value_num"])

    def test_same_key_in_two_publications_both_survive(self):
        """L1 из разных статей - разные строки. Это уже теряло 68% эталона."""
        build_gold(self.gold, [["D001", "R1", "PCE", "20.1", "6"],
                               ["D002", "R1", "PCE", "21.2", "7"]])
        import_gold.import_area("solar", self.gold, self.database, PASSPORT)
        self.assertEqual(count(self.database, "results"), 2)

    def test_duplicate_key_inside_one_publication_is_refused(self):
        build_gold(self.gold, [["D001", "R1", "PCE", "20.1", "6"],
                               ["D001", "R1", "Voc", "1.1", "6"]])
        with self.assertRaises(SystemExit) as caught:
            import_gold.import_area("solar", self.gold, self.database, PASSPORT)
        self.assertIn("повторяется", str(caught.exception))

    def test_extra_column_is_refused_not_silently_dropped(self):
        build_gold(self.gold, [["D001", "R1", "PCE", "20.1", "6"]], extra_column=True)
        with self.assertRaises(SystemExit) as caught:
            import_gold.import_area("solar", self.gold, self.database, PASSPORT)
        self.assertIn("лишние колонки", str(caught.exception))

    def test_missing_column_is_refused(self):
        self.gold.mkdir(parents=True)
        write_csv(self.gold / "results.csv", ["publication_id", "result_id"],
                  [["D001", "R1"]])
        with self.assertRaises(SystemExit) as caught:
            import_gold.import_area("solar", self.gold, self.database, PASSPORT)
        self.assertIn("нет колонок", str(caught.exception))

    def test_reimport_drops_rows_that_left_the_source(self):
        build_gold(self.gold, [["D001", "R1", "PCE", "20.1", "6"],
                               ["D001", "R2", "Voc", "1.1", "6"]])
        import_gold.import_area("solar", self.gold, self.database, PASSPORT)
        self.assertEqual(count(self.database, "results"), 2)
        build_gold(self.gold, [["D001", "R1", "PCE", "20.1", "6"]])
        import_gold.import_area("solar", self.gold, self.database, PASSPORT)
        self.assertEqual(count(self.database, "results"), 1)

    def test_missing_table_file_clears_that_table_only(self):
        build_gold(self.gold, [["D001", "R1", "PCE", "20.1", "6"]])
        import_gold.import_area("solar", self.gold, self.database, PASSPORT)
        (self.gold / "devices.csv").unlink()
        import_gold.import_area("solar", self.gold, self.database, PASSPORT)
        self.assertEqual(count(self.database, "devices"), 0)
        self.assertEqual(count(self.database, "results"), 1)

    def test_publications_reach_the_documents_envelope(self):
        build_gold(self.gold, [["D001", "R1", "PCE", "20.1", "6"]])
        import_gold.import_area("solar", self.gold, self.database, PASSPORT)
        conn = storage.connect(self.database)
        try:
            row = conn.execute(
                "SELECT title, parser_status FROM documents WHERE area = 'solar'").fetchone()
        finally:
            conn.close()
        self.assertEqual(row["title"], "Статья")
        self.assertEqual(row["parser_status"], "imported")


if __name__ == "__main__":
    unittest.main()
