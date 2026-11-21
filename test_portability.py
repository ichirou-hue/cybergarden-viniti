"""Portability tests: the same result on Windows/3.14 and on ubuntu+windows/3.12.

CI runs ``python -m unittest -v`` on both ubuntu-latest and windows-latest under
Python 3.12, without ``-X utf8``. Every test here is written so that it fails on
one platform when the code is wrong and passes on both when the code is right.

Nothing here touches ``_corpus/`` (745 MiB, gitignored), ``data/viniti.sqlite3``
or ``passport.json``: CI has none of them. Every fixture is a ``tempfile``
directory, and no test calls a CLI entry point that writes into the repository.
"""
import ast
import codecs
import csv
import importlib.util
import os
import pathlib
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent

# The same rule as tools/ci_parity.py: a whole string that is a Windows path.
# Kept local so this test does not depend on the auditor.
WINDOWS_PATH = re.compile(
    r"(?:[A-Za-z]:\\)?[^\\\s()\[\]{}*+?|^$,;]+(?:\\[^\\\s()\[\]{}*+?|^$,;]{2,})+")


def load_module(relative_path, name):
    """Import tools/<file>.py by path: tools/ is not a package."""
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def backslash_path_literals(path):
    """[(line, value)] for string literals in path that are a whole Windows path."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and \
                    isinstance(body[0].value, ast.Constant) and \
                    isinstance(body[0].value.value, str):
                docstrings.add((body[0].value.lineno, body[0].value.col_offset))
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if (node.lineno, node.col_offset) in docstrings:
            continue
        if WINDOWS_PATH.fullmatch(node.value):
            found.append((node.lineno, node.value))
    return found


class PathConstructionTest(unittest.TestCase):
    """A path literal with a backslash is one file name on Linux, not a directory."""

    def assert_posix_clean(self, path, label):
        self.assertIsInstance(path, pathlib.Path, "%s must be a pathlib.Path" % label)
        self.assertNotIn("\\", path.as_posix(),
                         "%s.as_posix() carries a backslash: %s" % (label, path.as_posix()))

    def test_import_gold_gold_directories(self):
        import_gold = load_module("import_gold.py", "portability_import_gold")
        self.assertEqual(sorted(import_gold.GOLD), ["rag", "solar"])
        for area, path in import_gold.GOLD.items():
            self.assert_posix_clean(path, "import_gold.GOLD[%r]" % area)
            self.assertEqual(path.name, "dev_gold")

    def test_passport_kit_directories(self):
        passport = load_module("passport.py", "portability_passport")
        for label, path in (("PASSPORT", passport.PASSPORT),
                            ("SOLAR_KIT", passport.SOLAR_KIT),
                            ("RAG_KIT", passport.RAG_KIT)):
            self.assert_posix_clean(path, "passport.%s" % label)
        self.assertEqual(passport.RAG_KIT.name, "RAG_ВИНИТИ_участникам_2026-11-20")
        self.assertEqual(passport.PASSPORT.name, "passport.json")

    def test_diff_gold_pairs(self):
        diff_gold = load_module("tools/diff_gold.py", "portability_diff_gold")
        self.assertEqual(sorted(diff_gold.PAIRS), ["rag", "solar"])
        for area, (gold, mine) in diff_gold.PAIRS.items():
            self.assert_posix_clean(gold, "diff_gold.PAIRS[%r][0]" % area)
            self.assert_posix_clean(mine, "diff_gold.PAIRS[%r][1]" % area)
            self.assertEqual(gold.name, "dev_gold")
            self.assertNotEqual(gold.as_posix(), mine.as_posix())

    def test_export_csv_writes_where_the_path_says(self):
        export_csv = load_module("export_csv.py", "portability_export_csv")
        with tempfile.TemporaryDirectory(prefix="viniti-portability-") as name:
            target = pathlib.Path(name) / "вложенный" / "results.csv"
            self.assert_posix_clean(target, "export_csv target")
            export_csv.write_table(target, ["publication_id"], [{"publication_id": "P001"}],
                                   True, "\r\n")
            self.assertTrue(target.is_file(), "export_csv wrote somewhere else")
            # A backslash path would create a second file whose name contains "\".
            siblings = sorted(entry.name for entry in target.parent.iterdir())
            self.assertEqual(siblings, ["results.csv"])

    def test_no_backslash_path_literal_in_the_four_modules(self):
        for relative in ("import_gold.py", "export_csv.py", "passport.py",
                         "tools/diff_gold.py"):
            with self.subTest(module=relative):
                found = backslash_path_literals(ROOT / relative)
                self.assertEqual(found, [], "%s builds a path with a backslash: %r"
                                 % (relative, found))


class CsvBytesTest(unittest.TestCase):
    """The submission format is fixed bytes: solar is BOM+CRLF, rag is CRLF only."""

    COLUMNS = ["publication_id", "metric_value", "source_fragment"]

    def write(self, rows, bom):
        export_csv = load_module("export_csv.py", "portability_export_csv_bytes")
        target = pathlib.Path(self.tmp.name) / "results.csv"
        export_csv.write_table(target, self.COLUMNS, rows, bom, "\r\n")
        return target.read_bytes()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="viniti-csv-")
        self.addCleanup(self.tmp.cleanup)

    def assert_crlf_only(self, raw):
        self.assertNotIn(b"\r\r\n", raw, "double CR: the stream translated \\n again")
        self.assertNotIn(b"\r\n\r\n", raw, "blank line between records")
        self.assertEqual(raw.replace(b"\r\n", b"").count(b"\n"), 0,
                         "a bare LF reached the file: newline='' is missing")
        self.assertEqual(raw.replace(b"\r\n", b"").count(b"\r"), 0,
                         "a bare CR reached the file")
        self.assertTrue(raw.endswith(b"\r\n"), "last record is not CRLF-terminated")

    def test_solar_is_bom_and_crlf_only(self):
        raw = self.write([{"publication_id": "P001", "metric_value": "0.330",
                           "source_fragment": "page 7"}], bom=True)
        self.assertTrue(raw.startswith(codecs.BOM_UTF8), "solar must start with a BOM")
        self.assertEqual(raw.count(codecs.BOM_UTF8), 1)
        self.assert_crlf_only(raw)
        self.assertEqual(raw.count(b"\r\n"), 2)

    def test_rag_is_crlf_without_bom(self):
        raw = self.write([{"publication_id": "D001", "metric_value": "0.330",
                           "source_fragment": "table 2"}], bom=False)
        self.assertFalse(raw.startswith(codecs.BOM_UTF8), "rag must not carry a BOM")
        self.assertEqual(raw.count(codecs.BOM_UTF8), 0)
        self.assert_crlf_only(raw)

    def test_cyrillic_value_and_embedded_newline_roundtrip(self):
        value = "первая строка\nвторая строка, с запятой"
        raw = self.write([{"publication_id": "D001", "metric_value": "0.330",
                           "source_fragment": value}], bom=False)
        # newline="" keeps the embedded \n as one character; without it the file
        # would hold \r\n inside the field and the value would grow a \r.
        with (pathlib.Path(self.tmp.name) / "results.csv").open(
                encoding="utf-8", newline="") as stream:
            rows = list(csv.reader(stream))
        self.assertEqual(len(rows), 2, "the embedded newline split the record")
        self.assertEqual(rows[1][2], value)
        self.assertNotIn("\r", rows[1][2])
        self.assertIn(b'"', raw, "a value with a comma or a newline must be quoted")

    def test_cyrillic_value_is_written_as_utf8(self):
        raw = self.write([{"publication_id": "D001", "metric_value": "нет данных",
                           "source_fragment": "x"}], bom=False)
        self.assertIn("нет данных".encode("utf-8"), raw)

    def test_import_gold_read_csv_keeps_bom_crlf_and_newline(self):
        # The organisers' rag/results.csv really does hold bare LF inside quoted
        # fields while records end with CRLF (349 such fields in dev_gold).
        import_gold = load_module("import_gold.py", "portability_import_gold_csv")
        export_csv = load_module("export_csv.py", "portability_export_csv_gold")
        value = "первая строка\nвторая строка"
        original = (codecs.BOM_UTF8
                    + b"publication_id,metric_value,source_fragment\r\n"
                    + b'D001,0.330,"' + value.encode("utf-8") + b'"\r\n')
        target = pathlib.Path(self.tmp.name) / "results.csv"
        target.write_bytes(original)

        rows = import_gold.read_csv(target, self.COLUMNS)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["publication_id"], "D001")
        self.assertEqual(rows[0]["source_fragment"], value,
                         "read_csv must keep the bare LF and not add a CR")
        self.assertNotIn("\r", rows[0]["source_fragment"])

        # gold in, gold out: the same bytes must come back out of the exporter.
        again = pathlib.Path(self.tmp.name) / "again.csv"
        export_csv.write_table(again, self.COLUMNS, rows, True, "\r\n")
        self.assertEqual(again.read_bytes(), original,
                         "read then write must reproduce the organisers' bytes")


class UnicodePathTest(unittest.TestCase):
    """The rag kit lives in a Cyrillic directory; nothing may assume byte lengths."""

    NAME = "RAG_ВИНИТИ_участникам_2026-11-20"

    def test_cyrillic_path_roundtrips_through_pathlib_and_open(self):
        with tempfile.TemporaryDirectory(prefix="viniti-unicode-") as name:
            base = pathlib.Path(name) / self.NAME / "dev_gold"
            base.mkdir(parents=True)
            target = base / "results.csv"
            target.write_bytes(codecs.BOM_UTF8 + "значение\r\n".encode("utf-8"))

            self.assertTrue(target.is_file(), "the Cyrillic directory did not survive")
            self.assertNotIn("\\", target.as_posix())
            self.assertEqual(target.as_posix(),
                             base.as_posix() + "/results.csv")
            self.assertTrue(target.as_posix().endswith("/dev_gold/results.csv"))
            # The POSIX form must resolve to the same file on both platforms.
            self.assertEqual(pathlib.Path(target.as_posix()), target)
            with target.open(encoding="utf-8-sig", newline="") as stream:
                self.assertEqual(stream.read(), "значение\r\n")

    def test_cyrillic_path_is_multibyte_so_byte_slicing_is_wrong(self):
        self.assertEqual(sys.getfilesystemencoding().lower().replace("-", ""), "utf8",
                         "both CI targets must use a UTF-8 filesystem encoding")
        encoded = self.NAME.encode("utf-8")
        self.assertGreater(len(encoded), len(self.NAME),
                           "the corpus path is multibyte: len(bytes) != len(text), so "
                           "no code may slice or compare the encoded path by length")


class ConsoleEncodingTest(unittest.TestCase):
    """sys.stdout is errors='strict': one unprintable character kills the run."""

    # (script, accepted exit codes, must print non-ASCII to stdout)
    SCRIPTS = (
        ("import_gold.py", (0,), False),
        ("export_csv.py", (0,), False),
        ("db.py", (0,), False),
        ("tools/diff_gold.py", (0, 2), True),
    )

    def test_cli_help_survives_a_narrow_console(self):
        for script, accepted, expect_non_ascii in self.SCRIPTS:
            with self.subTest(script=script):
                env = dict(os.environ, PYTHONIOENCODING="cp1251")
                run = subprocess.run(
                    [sys.executable, "-X", "utf8", str(ROOT / script), "--help"],
                    capture_output=True, timeout=120, env=env, cwd=str(ROOT))
                self.assertNotIn(b"UnicodeEncodeError", run.stderr,
                                 "%s prints a character outside the console code page"
                                 % script)
                self.assertIn(run.returncode, accepted,
                              "%s --help exited %d: %r"
                              % (script, run.returncode, run.stderr[-400:]))
                if expect_non_ascii:
                    self.assertTrue(any(byte > 127 for byte in run.stdout),
                                    "%s did not reach a Cyrillic print" % script)


class SyntaxTest(unittest.TestCase):
    """CI pins Python 3.12: anything 3.13+ only is a red build on both runners."""

    def test_every_module_parses_as_python_312(self):
        paths = sorted(ROOT.glob("*.py")) + sorted((ROOT / "tools").glob("*.py"))
        self.assertGreater(len(paths), 10)
        for path in paths:
            with self.subTest(module=path.relative_to(ROOT).as_posix()):
                source = path.read_text(encoding="utf-8")
                try:
                    ast.parse(source, filename=str(path), feature_version=(3, 12))
                except SyntaxError as error:
                    self.fail("%s does not parse as Python 3.12: line %s: %s"
                              % (path.name, error.lineno, error.msg))


class OrderingTest(unittest.TestCase):
    """NTFS and ext4 return directory entries in different order."""

    def test_batch_discover_is_sorted_not_filesystem_order(self):
        import batch
        with tempfile.TemporaryDirectory(prefix="viniti-order-") as name:
            saved = batch.CORPUS_ROOTS["solar"]
            self.addCleanup(batch.CORPUS_ROOTS.__setitem__, "solar", saved)
            batch.CORPUS_ROOTS["solar"] = pathlib.Path(name)
            split = pathlib.Path(name) / "dev"
            split.mkdir()
            for filename in ("P003.pdf", "P001.pdf", "P002.pdf"):
                (split / filename).write_bytes(b"%PDF-1.4")
            found = [path.name for path in batch.discover("solar", "dev")]
            self.assertEqual(found, ["P001.pdf", "P002.pdf", "P003.pdf"],
                             "discover() must sort: creation order differs per filesystem")


class SqliteFeatureTest(unittest.TestCase):
    """Python 3.12 and 3.14 bundle different SQLite builds; the code uses both."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)

    def test_json1_functions_exist(self):
        # library.py: json_extract, json_array_length.
        self.assertEqual(
            self.conn.execute("SELECT json_extract('{\"a\": 1}', '$.a')").fetchone()[0], 1)
        self.assertEqual(
            self.conn.execute("SELECT json_array_length('[1, 2, 3]')").fetchone()[0], 3)

    def test_upsert_is_available(self):
        # batch.py: ON CONFLICT(area, publication_id) DO UPDATE.
        self.conn.execute("CREATE TABLE jobs (area TEXT, publication_id TEXT, "
                          "status TEXT, PRIMARY KEY (area, publication_id))")
        self.conn.execute("INSERT INTO jobs VALUES ('solar', 'P001', 'queued') "
                          "ON CONFLICT(area, publication_id) DO UPDATE SET status='ok'")
        self.conn.execute("INSERT INTO jobs VALUES ('solar', 'P001', 'queued') "
                          "ON CONFLICT(area, publication_id) DO UPDATE SET status='done'")
        self.assertEqual(self.conn.execute("SELECT status FROM jobs").fetchone()[0], "done")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
