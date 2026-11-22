"""Тесты проверок согласованности.

Run:  python -X utf8 -m unittest test_issues -v
"""
import pathlib
import sqlite3
import tempfile
import unittest

from viniti import db as storage
from viniti import issues


def insert(connection, area, table, **values):
    """Вставить строку, оставив все остальные колонки пустыми."""
    meta = storage.load_passport()["areas"][area]["tables"][table]
    columns = ["area"] + list(meta["columns"])
    row = {"area": area, **{column: "" for column in meta["columns"]}, **values}
    connection.execute(
        f"INSERT INTO {storage.quote(storage.table_name(area, table))} "
        f"({', '.join(storage.quote(column) for column in columns)}) "
        f"VALUES ({', '.join('?' for _ in columns)})",
        [row[column] for column in columns])


class ConsistencyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="viniti-issues-")
        self.path = pathlib.Path(self.tmp.name) / "data.sqlite3"
        storage.init(self.path)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def solar_device(self, publication="D001", device="DV1", measurement="MS1",
                     jsc="15.0", voc="1.200", ff="70.0", pce="12.60",
                     origin="primary", fragment="PCE of 12.60%"):
        """Один прибор: четыре величины одного замера."""
        for index, (name, value, unit) in enumerate(
                (("Jsc", jsc, "mA/cm2"), ("Voc", voc, "V"),
                 ("FF", ff, "%"), ("PCE", pce, "%")), 1):
            insert(self.db, "solar", "results", publication_id=publication,
                   result_id=f"R{index:03d}", device_id=device,
                   measurement_id=measurement, metric_name=name,
                   metric_value=value, metric_unit=unit,
                   result_origin=origin, source_page="3",
                   source_fragment=fragment)
        self.db.commit()

    # ---------------------------------------------------------- J-V

    def test_mismatched_pce_is_found_with_all_four_record_ids(self):
        # 15.0 * 1.2 * 0.70 = 12.6; напечатано 5.0 - это уже не округление.
        self.solar_device(pce="5.0")
        found = issues.generate("solar", self.db)
        self.assertEqual(len(found), 1)
        row = found[0]
        self.assertEqual(row["check_name"], "jv_consistency")
        self.assertEqual(row["record_ids"], "R001;R002;R003;R004")
        self.assertIn("5.0", row["description"])
        self.assertIn("12.600", row["description"])

    def test_rounding_of_printed_values_is_not_an_issue(self):
        # 15.28 * 1.197 * 0.71 = 12.986 против 12.89 в статье: это округление.
        self.solar_device(jsc="15.28", voc="1.197", ff="71.0", pce="12.89")
        self.assertEqual(issues.generate("solar", self.db), [])

    def test_jsc_in_ma_and_voc_in_millivolts_are_converted(self):
        # 15.0 mA/cm2 * 1200 mV * 0.70 = 12.6 %: единицы не должны мешать.
        self.solar_device(voc="1200", jsc="15.0", ff="70.0", pce="12.6")
        for row in self.db.execute("SELECT result_id, metric_name FROM solar_results "
                                   "WHERE metric_name = 'Voc'"):
            self.db.execute("UPDATE solar_results SET metric_unit = 'mV' "
                            "WHERE result_id = ?", (row["result_id"],))
        self.db.commit()
        self.assertEqual(issues.generate("solar", self.db), [])

    def test_devices_are_not_mixed_into_one_group(self):
        """Jsc одного прибора и PCE другого - самая частая ошибка склейки."""
        self.solar_device(device="DV1", pce="12.6")
        insert(self.db, "solar", "results", publication_id="D001",
               result_id="R005", device_id="DV2", measurement_id="MS2",
               metric_name="PCE", metric_value="3.0", metric_unit="%",
               result_origin="primary", source_page="4", source_fragment="PCE of 3.0%")
        self.db.commit()
        self.assertEqual(issues.generate("solar", self.db), [])

    # ---------------------------------------------------------- единицы

    def test_ff_as_fraction_above_one_is_a_unit_error(self):
        insert(self.db, "solar", "results", publication_id="D001",
               result_id="R001", device_id="DV1", measurement_id="MS1",
               metric_name="FF", metric_value="71.0", metric_unit="fraction",
               result_origin="primary", source_page="3",
               source_fragment="FF of 71.0")
        self.db.commit()
        found = issues.generate("solar", self.db)
        self.assertEqual([row["check_name"] for row in found], ["unit_inconsistency"])
        self.assertIn("71.0", found[0]["description"])

    def test_low_efficiency_in_percent_is_not_a_unit_error(self):
        """У полупрозрачных приборов КПД 0.21 % - настоящее значение, не доля."""
        insert(self.db, "solar", "results", publication_id="D022",
               result_id="R004", device_id="DV1", measurement_id="MS1",
               metric_name="PCE", metric_value="0.21", metric_unit="%",
               result_origin="primary", source_page="3",
               source_fragment="efficiency of 0.21%")
        self.db.commit()
        self.assertEqual(issues.generate("solar", self.db), [])

    # ---------------------------------------------------------- условия

    def test_primary_result_without_conditions_is_flagged(self):
        insert(self.db, "solar", "results", publication_id="D001",
               result_id="R001", metric_name="PCE", metric_value="12.5",
               metric_unit="%", result_origin="primary", source_page="3",
               source_fragment="PCE of 12.5%")
        self.db.commit()
        found = issues.generate("solar", self.db)
        self.assertEqual([row["check_name"] for row in found], ["missing_conditions"])

    def test_cited_result_without_conditions_is_not_flagged(self):
        """У цитируемого результата условия описаны в чужой статье, не в этой."""
        insert(self.db, "solar", "results", publication_id="D001",
               result_id="R047", metric_name="PCE", metric_value="19",
               metric_unit="%", result_origin="cited", source_page="1",
               source_fragment="PCEs have reached 19%")
        self.db.commit()
        self.assertEqual(issues.generate("solar", self.db), [])

    # ---------------------------------------------------------- запись

    def test_write_keeps_imported_issues_and_replaces_computed(self):
        insert(self.db, "solar", "issues", publication_id="D001", issue_id="I01",
               check_name="contradiction", description="эталонная находка",
               source_page="3", source_fragment="PCE of 12.5%")
        self.solar_device(pce="5.0")
        rows = issues.generate("solar", self.db)
        issues.write("solar", self.db, rows)
        issues.write("solar", self.db, issues.generate("solar", self.db))
        self.db.commit()
        stored = [dict(row) for row in self.db.execute(
            "SELECT issue_id, check_name FROM solar_issues ORDER BY issue_id")]
        self.assertEqual([row["issue_id"] for row in stored], ["C01", "I01"])

    def test_every_issue_carries_a_page_and_a_fragment(self):
        """Находка без цитаты не проверяема и не имеет права попасть в issues."""
        self.solar_device(pce="5.0")
        for row in issues.generate("solar", self.db):
            self.assertTrue(row["source_page"].strip(), row)
            self.assertTrue(row["source_fragment"].strip(), row)


if __name__ == "__main__":
    unittest.main()
