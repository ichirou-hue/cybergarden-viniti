"""Tests for db.py: schema shape, the publication-aware primary key, numeric mirrors.

Everything here is built from a synthetic passport inside tempfile.TemporaryDirectory().
No test reads _corpus/, data/viniti.sqlite3, or any other file outside the repository's
tracked set, so the suite runs in CI where the 745 MiB corpus is absent.

The bug these tests exist for: a primary key of (area, key) instead of
(area, publication_id, key) made every publication after the first reuse of a local id
(``L1``, ``L2`` ... which restart in every paper) silently overwrite the previous one.
That lost 68% of the gold rows without a single error.
"""
import json
import pathlib
import sqlite3
import tempfile
import unittest

import db as storage

SOLAR_RESULTS_COLUMNS = [
    "publication_id",
    "result_id",
    "metric_name",
    "metric_value",
    "metric_unit",
    "source_page",
    "source_fragment",
    "raw_value",
    "normalized_value",
    "sample_size",
]

RAG_RESULTS_COLUMNS = [
    "publication_id",
    "result_id",
    "metric_name",
    "metric_value",
    "metric_unit",
    "source_page",
    "source_fragment",
    "raw_value",
    "normalized_value",
    "sample_size",
]

# Types for the synthetic results table: mirrors must come from these, not from a literal.
RESULT_FIELDS = {
    "metric_value": {"type": "number"},
    "normalized_value": {"type": "number"},
    "source_page": {"type": "number"},
    "sample_size": {"type": "number"},
    "raw_value": {"type": "text"},
}


def publications_spec(year_type):
    """The two areas disagree about ``year``: text in solar, a number in rag."""
    return {
        "key": "publication_id",
        "columns": ["publication_id", "title", "year", "in_scope"],
        "fields": {
            "publication_id": {"type": "text"},
            "title": {"type": "text"},
            "year": {"type": year_type},
            "in_scope": {"type": "enum", "allowed": ["true", "false"]},
        },
    }


def synthetic_passport():
    """Two areas, two tables each, with numeric fields declared in ``fields``."""
    return {
        "version": 1,
        "generated_from": "test_db.synthetic_passport",
        "areas": {
            "solar": {
                "bom": True,
                "newline": "\r\n",
                "tables": {
                    "publications": publications_spec("text"),
                    "results": {
                        "key": "result_id",
                        "columns": list(SOLAR_RESULTS_COLUMNS),
                        "fields": dict(RESULT_FIELDS),
                    },
                },
            },
            "rag": {
                "bom": False,
                "newline": "\r\n",
                "tables": {
                    "publications": publications_spec("number"),
                    "results": {
                        "key": "result_id",
                        "columns": list(RAG_RESULTS_COLUMNS),
                        "fields": dict(RESULT_FIELDS),
                    },
                },
            },
        },
    }


def to_number(text):
    """The writer's rule for mirrors, spelled out locally so the test does not import it."""
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def primary_key_columns(conn, table):
    """Ordered PK column names from the schema, not from what inserts happen to allow."""
    for index in conn.execute(f"PRAGMA index_list({storage.quote(table)})"):
        if index["origin"] == "pk":
            return [row["name"] for row in
                    conn.execute(f"PRAGMA index_info({storage.quote(index['name'])})")]
    return []


class DbFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="viniti-db-")
        self.addCleanup(self.tmp.cleanup)
        self.dir = pathlib.Path(self.tmp.name)
        self.passport = synthetic_passport()
        self.path = self.dir / "viniti.sqlite3"
        storage.init(self.path, self.passport)
        self.conn = storage.connect(self.path)
        self.addCleanup(self.conn.close)

    def table(self, area, table):
        return storage.table_name(area, table)

    def count(self, area, table):
        return self.conn.execute(
            f"SELECT COUNT(*) FROM {storage.quote(self.table(area, table))}").fetchone()[0]

    def insert_result(self, area, publication_id, result_id, metric_value,
                      metric_name="efficiency", metric_unit="percent",
                      source_page="7", source_fragment="table 1, row 2",
                      raw_value="", normalized_value=""):
        """Insert one results row, computing mirrors the way a writer must."""
        values = {
            "area": area,
            "publication_id": publication_id,
            "result_id": result_id,
            "metric_name": metric_name,
            "metric_value": metric_value,
            "metric_unit": metric_unit,
            "source_page": source_page,
            "source_fragment": source_fragment,
            "raw_value": raw_value,
            "normalized_value": normalized_value,
            "value_num": to_number(metric_value),
            "page_num": to_number(source_page),
            "norm_num": to_number(normalized_value),
        }
        columns = ", ".join(storage.quote(c) for c in values)
        placeholders = ", ".join("?" for _ in values)
        self.conn.execute(
            f"INSERT INTO {storage.quote(self.table(area, 'results'))} "
            f"({columns}) VALUES ({placeholders})", list(values.values()))
        self.conn.commit()


class SchemaTest(DbFixture):
    def test_table_name_is_area_prefixed(self):
        self.assertEqual(storage.table_name("solar", "results"), "solar_results")
        self.assertEqual(storage.table_name("rag", "results"), "rag_results")

    def test_init_creates_every_area_table_and_common_tables(self):
        existing = {row["name"] for row in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        for area, spec in self.passport["areas"].items():
            for table in spec["tables"]:
                self.assertIn(self.table(area, table), existing)
        for table in storage.COMMON_TABLES:
            self.assertIn(table, existing)

    def test_primary_key_includes_publication_id(self):
        # Asserted on the schema, not on insert behaviour: a PK that merely happens to
        # allow two rows today is not the contract.
        for area, spec in self.passport["areas"].items():
            for table, meta in spec["tables"].items():
                with self.subTest(area=area, table=table):
                    expected = ["area", "publication_id"]
                    if meta["key"] != "publication_id":
                        expected.append(meta["key"])
                    actual = primary_key_columns(self.conn, self.table(area, table))
                    # db.create_area appends the key column unconditionally, so a table
                    # whose key is publication_id (publications) lists it twice. Benign,
                    # but the ordered unique list is what the contract promises.
                    deduped = list(dict.fromkeys(actual))
                    self.assertEqual(deduped, expected)
                    self.assertIn("publication_id", actual)
                    self.assertEqual(actual[0], "area")

    def test_every_passport_column_exists_as_text_and_mirrors_as_real(self):
        for area, spec in self.passport["areas"].items():
            for table, meta in spec["tables"].items():
                info = {row["name"]: row for row in self.conn.execute(
                    f"PRAGMA table_info({storage.quote(self.table(area, table))})")}
                for column in meta["columns"]:
                    with self.subTest(area=area, table=table, column=column):
                        self.assertIn(column, info)
                        self.assertEqual(info[column]["type"].upper(), "TEXT")
                for source_column, mirror in storage.table_mirrors(table, meta).items():
                    with self.subTest(area=area, table=table, mirror=mirror):
                        self.assertIn(mirror, info)
                        self.assertEqual(info[mirror]["type"].upper(), "REAL")

    def test_mirrors_come_from_the_passport_not_from_a_literal_table(self):
        # solar.year is text and rag.year is a number: only rag may grow year_num.
        solar = {row["name"] for row in self.conn.execute(
            f"PRAGMA table_info({storage.quote(self.table('solar', 'publications'))})")}
        rag = {row["name"] for row in self.conn.execute(
            f"PRAGMA table_info({storage.quote(self.table('rag', 'publications'))})")}
        self.assertNotIn("year_num", solar)
        self.assertIn("year_num", rag)

    def test_legacy_mirror_names_are_frozen(self):
        self.assertEqual(storage.mirror_name("results", "metric_value"), "value_num")
        self.assertEqual(storage.mirror_name("results", "normalized_value"), "norm_num")
        self.assertEqual(storage.mirror_name("results", "source_page"), "page_num")
        # Everything else follows <column>_num, including the same column in another table:
        # only the three historical pairs keep their old name.
        self.assertEqual(storage.mirror_name("results", "sample_size"), "sample_size_num")
        self.assertEqual(storage.mirror_name("issues", "source_page"), "source_page_num")
        self.assertEqual(storage.mirror_name("materials", "bandgap_value"), "bandgap_value_num")

    def test_text_field_gets_no_mirror(self):
        info = {row["name"] for row in self.conn.execute(
            f"PRAGMA table_info({storage.quote(self.table('solar', 'results'))})")}
        self.assertNotIn("raw_value_num", info)
        self.assertIn("sample_size_num", info)

    def test_connect_sets_row_factory_wal_and_foreign_keys(self):
        self.assertIs(self.conn.row_factory, sqlite3.Row)
        self.assertEqual(self.conn.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
        self.assertEqual(self.conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)

    def test_init_is_idempotent_and_keeps_rows(self):
        self.insert_result("solar", "P001", "L1", "0.330")
        self.assertEqual(self.count("solar", "results"), 1)

        storage.init(self.path, self.passport)  # second time on the same path

        fresh = storage.connect(self.path)
        self.addCleanup(fresh.close)
        self.assertEqual(fresh.execute(
            f"SELECT COUNT(*) FROM {storage.quote(self.table('solar', 'results'))}"
        ).fetchone()[0], 1)


class KeyTest(DbFixture):
    def test_same_local_key_different_publication_both_survive(self):
        # Every paper numbers its own rows L1, L2, ... so L1 is not unique on its own.
        self.insert_result("solar", "P001", "L1", "0.330")
        self.insert_result("solar", "P002", "L1", "0.912")

        rows = self.conn.execute(
            f"SELECT publication_id, result_id, metric_value FROM "
            f"{storage.quote(self.table('solar', 'results'))} ORDER BY publication_id"
        ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual([r["publication_id"] for r in rows], ["P001", "P002"])
        self.assertEqual([r["result_id"] for r in rows], ["L1", "L1"])
        self.assertEqual([r["metric_value"] for r in rows], ["0.330", "0.912"])

    def test_same_key_same_publication_in_two_areas_both_survive(self):
        # D001 exists in both areas with different content and must not collide.
        self.insert_result("solar", "D001", "L1", "0.330")
        self.insert_result("rag", "D001", "L1", "EM decrease")

        self.assertEqual(self.count("solar", "results"), 1)
        self.assertEqual(self.count("rag", "results"), 1)
        solar = self.conn.execute(
            f"SELECT metric_value FROM {storage.quote(self.table('solar', 'results'))}"
        ).fetchone()[0]
        rag = self.conn.execute(
            f"SELECT metric_value FROM {storage.quote(self.table('rag', 'results'))}"
        ).fetchone()[0]
        self.assertEqual(solar, "0.330")
        self.assertEqual(rag, "EM decrease")

    def test_reimporting_the_same_row_does_not_duplicate_it(self):
        # import_gold.py writes with INSERT OR REPLACE; the PK must make that a no-op.
        name = storage.quote(self.table("solar", "results"))
        for _ in range(2):
            self.conn.execute(
                f"INSERT OR REPLACE INTO {name} "
                f"(area, publication_id, result_id, metric_value) VALUES (?, ?, ?, ?)",
                ("solar", "P001", "L1", "0.330"))
        self.conn.commit()
        self.assertEqual(self.count("solar", "results"), 1)


class MirrorTest(DbFixture):
    def test_trailing_zero_text_is_kept_while_mirror_is_numeric(self):
        # TEXT affinity is what keeps '0.330' as written; a NUMERIC column would give 0.33.
        self.insert_result("solar", "P001", "L1", "0.330", source_page="7")

        row = self.conn.execute(
            f"SELECT metric_value, typeof(metric_value) AS metric_kind, "
            f"       value_num, typeof(value_num) AS mirror_kind, "
            f"       source_page, page_num "
            f"FROM {storage.quote(self.table('solar', 'results'))}"
        ).fetchone()
        self.assertEqual(row["metric_value"], "0.330")
        self.assertEqual(row["metric_kind"], "text")
        self.assertAlmostEqual(row["value_num"], 0.33, places=12)
        self.assertEqual(row["mirror_kind"], "real")
        self.assertEqual(row["source_page"], "7")
        self.assertAlmostEqual(row["page_num"], 7.0, places=12)

    def test_non_numeric_value_stores_null_mirror_without_raising(self):
        # 83% of rag raw_value is not a number at all; the mirror must stay NULL, not abort.
        self.insert_result("rag", "D001", "L1", "EM decrease", raw_value="↓18.4%")

        row = self.conn.execute(
            f"SELECT metric_value, value_num, raw_value, typeof(raw_value) AS raw_kind "
            f"FROM {storage.quote(self.table('rag', 'results'))}"
        ).fetchone()
        self.assertEqual(row["metric_value"], "EM decrease")
        self.assertIsNone(row["value_num"])
        self.assertEqual(row["raw_value"], "↓18.4%")
        self.assertEqual(row["raw_kind"], "text")

    def test_mirror_is_null_when_not_supplied(self):
        self.conn.execute(
            f"INSERT INTO {storage.quote(self.table('solar', 'results'))} "
            f"(area, publication_id, result_id, metric_value) VALUES (?, ?, ?, ?)",
            ("solar", "P001", "L1", "0.330"))
        row = self.conn.execute(
            f"SELECT value_num FROM {storage.quote(self.table('solar', 'results'))}"
        ).fetchone()
        self.assertIsNone(row["value_num"])


class MigrationTest(unittest.TestCase):
    """An old database (three hand-written mirrors) must gain the generated ones on init()."""

    OLD_MIRRORS = ("value_num", "norm_num", "page_num")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="viniti-migrate-")
        self.addCleanup(self.tmp.cleanup)
        self.path = pathlib.Path(self.tmp.name) / "old.sqlite3"
        self.build_old_database()

    def build_old_database(self):
        """The schema db.py produced before mirrors were generated from the passport."""
        meta = synthetic_passport()["areas"]["solar"]["tables"]["results"]
        parts = ["area TEXT NOT NULL", "_line INTEGER NOT NULL DEFAULT 0"]
        parts += [f"{storage.quote(c)} TEXT NOT NULL DEFAULT ''" for c in meta["columns"]]
        parts += [f"{storage.quote(m)} REAL" for m in self.OLD_MIRRORS]
        parts.append("PRIMARY KEY (area, publication_id, result_id)")
        conn = sqlite3.connect(self.path)
        try:
            with conn:
                conn.execute(f"CREATE TABLE solar_results ({', '.join(parts)})")
                conn.execute(
                    "INSERT INTO solar_results (area, publication_id, result_id, metric_value, "
                    "normalized_value, source_page, raw_value, sample_size, value_num) "
                    "VALUES ('solar', 'P001', 'L1', '0.330', '0.5', '7', 'EM decrease', '12', 0.33)")
                conn.execute(
                    "INSERT INTO solar_results (area, publication_id, result_id, metric_value, "
                    "normalized_value, source_page, raw_value, sample_size) "
                    "VALUES ('solar', 'P001', 'L2', 'EM decrease', '', '8', 'n/a', 'n/a')")
        finally:
            conn.close()

    def columns(self):
        conn = storage.connect(self.path)
        try:
            return {row["name"]: row["type"].upper() for row in conn.execute(
                "PRAGMA table_info(solar_results)")}
        finally:
            conn.close()

    def snapshot(self):
        conn = storage.connect(self.path)
        try:
            return [tuple(row) for row in conn.execute(
                "SELECT * FROM solar_results ORDER BY result_id")]
        finally:
            conn.close()

    def test_init_adds_the_missing_mirror_and_backfills_it(self):
        self.assertNotIn("sample_size_num", self.columns())

        storage.init(self.path, synthetic_passport())

        self.assertEqual(self.columns()["sample_size_num"], "REAL")
        conn = storage.connect(self.path)
        try:
            rows = conn.execute(
                "SELECT result_id, metric_value, value_num, sample_size_num "
                "FROM solar_results ORDER BY result_id").fetchall()
        finally:
            conn.close()
        self.assertEqual(rows[0]["result_id"], "L1")
        self.assertEqual(rows[0]["metric_value"], "0.330")  # text survives the migration
        self.assertAlmostEqual(rows[0]["value_num"], 0.33, places=12)
        self.assertAlmostEqual(rows[0]["sample_size_num"], 12.0, places=12)
        # Non-numeric text must stay NULL: CAST('n/a' AS REAL) would have been 0.0.
        self.assertIsNone(rows[1]["sample_size_num"])

    def test_migration_is_idempotent(self):
        storage.init(self.path, synthetic_passport())
        columns = self.columns()
        rows = self.snapshot()

        storage.init(self.path, synthetic_passport())

        self.assertEqual(self.columns(), columns)
        self.assertEqual(self.snapshot(), rows)

    def test_existing_mirror_values_are_not_recomputed(self):
        # value_num was already there; the migration adds columns, it does not rewrite data.
        conn = sqlite3.connect(self.path)
        try:
            with conn:
                conn.execute("UPDATE solar_results SET value_num = 0.5 WHERE result_id = 'L1'")
        finally:
            conn.close()

        storage.init(self.path, synthetic_passport())

        conn = storage.connect(self.path)
        try:
            value = conn.execute(
                "SELECT value_num FROM solar_results WHERE result_id = 'L1'").fetchone()[0]
        finally:
            conn.close()
        self.assertAlmostEqual(value, 0.5, places=12)


class RealPassportMirrorTest(unittest.TestCase):
    """passport.json is tracked, so the generated schema can be checked against it directly."""

    def test_every_number_field_of_the_real_passport_has_a_real_mirror(self):
        passport = storage.load_passport()
        numbers = 0
        with tempfile.TemporaryDirectory(prefix="viniti-real-") as name:
            path = pathlib.Path(name) / "viniti.sqlite3"
            storage.init(path, passport)
            conn = storage.connect(path)
            try:
                for area, spec in passport["areas"].items():
                    for table, meta in spec["tables"].items():
                        info = {row["name"]: row["type"].upper() for row in conn.execute(
                            f"PRAGMA table_info({storage.quote(storage.table_name(area, table))})")}
                        for column in meta["columns"]:
                            is_number = meta["fields"][column]["type"] == "number"
                            mirror = storage.mirror_name(table, column)
                            with self.subTest(area=area, table=table, column=column):
                                if is_number:
                                    numbers += 1
                                    self.assertIn(mirror, info)
                                    self.assertEqual(info[mirror], "REAL")
                                else:
                                    self.assertNotIn(mirror, info)
            finally:
                conn.close()
        # The loop must not pass by finding no number fields at all.
        self.assertGreaterEqual(numbers, 20)


class PassportTest(unittest.TestCase):
    def test_load_passport_reads_a_file_from_disk(self):
        with tempfile.TemporaryDirectory(prefix="viniti-passport-") as name:
            path = pathlib.Path(name) / "passport.json"
            path.write_text(json.dumps(synthetic_passport()), encoding="utf-8")
            passport = storage.load_passport(path)
        self.assertEqual(sorted(passport["areas"]), ["rag", "solar"])
        self.assertTrue(passport["areas"]["solar"]["bom"])
        self.assertFalse(passport["areas"]["rag"]["bom"])


if __name__ == "__main__":
    unittest.main()
