"""Тесты API-конструктора: /api/passport, /api/results, /api/results.csv,
/api/issues, /api/stats и отдача статики.

Тест самодостаточен: строит пустую базу в tempfile и кладёт в неё несколько строк.
Корпус _corpus/ и data/viniti.sqlite3 не нужны - в CI их нет.
"""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from urllib.error import HTTPError
from urllib.request import urlopen

import db as storage
from library import Library
from server import Handler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parent


def get(url):
    try:
        with urlopen(url, timeout=20) as response:
            return response.status, response.read()
    except HTTPError as error:
        with error:
            return error.code, error.read()


def get_with_type(url):
    """Как get(), но вместе с Content-Type: статике он важен."""
    try:
        with urlopen(url, timeout=20) as response:
            return response.status, response.headers.get("Content-Type"), response.read()
    except HTTPError as error:
        with error:
            return error.code, error.headers.get("Content-Type"), error.read()


def insert(connection, area, table, **values):
    """Вставить строку области, оставив остальные колонки паспорта пустыми."""
    meta = storage.load_passport()["areas"][area]["tables"][table]
    columns = ["area"] + list(meta["columns"])
    row = {"area": area, **{column: "" for column in meta["columns"]}, **values}
    connection.execute(
        f"INSERT INTO {storage.quote(storage.table_name(area, table))} "
        f"({', '.join(storage.quote(column) for column in columns)}) "
        f"VALUES ({', '.join('?' for _ in columns)})",
        [row[column] for column in columns])


def build_database(path):
    """Пустая база плюс строки обеих областей для фильтров и счётчиков."""
    storage.init(path)
    conn = storage.connect(path)
    try:
        with conn:
            rows = [
                ("D001", "R1", "PCE", "21.5", "21.5", 6),
                ("D001", "R2", "PCE", "9.9", "9.9", 7),
                ("D001", "R3", "PCE", "не число", None, 8),
            ]
            for index, (publication, result, metric, value, number, page) in enumerate(rows, 1):
                conn.execute(
                    'INSERT INTO solar_results (area, _line, publication_id, result_id, '
                    'metric_name, metric_value, metric_unit, source_page, source_fragment, '
                    'value_num, page_num) '
                    'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                    ("solar", index, publication, result, metric, value, "%",
                     str(page), value, number, float(page)))

            for publication in ("D001", "D002"):
                insert(conn, "solar", "publications", publication_id=publication,
                       title=f"Solar {publication}", year="2024")
            insert(conn, "rag", "publications", publication_id="D001",
                   title="RAG D001", year="2023")

            insert(conn, "solar", "issues", publication_id="D001", issue_id="I1",
                   check_name="jv_consistency", description="PCE не сходится",
                   source_page="3", source_fragment="PCE of 12.6%")
            insert(conn, "solar", "issues", publication_id="D001", issue_id="I2",
                   check_name="jv_consistency", description="второе расхождение",
                   source_page="4", source_fragment="PCE of 9.9%")
            insert(conn, "solar", "issues", publication_id="D002", issue_id="I3",
                   check_name="unit_inconsistency", description="единица",
                   source_page="5", source_fragment="FF 0.71%")
            insert(conn, "rag", "issues", publication_id="D001", issue_id="I1",
                   check_name="normalization_mismatch", description="нормализация",
                   source_page="6", source_fragment="nDCG 0.330")

            conn.execute(
                "INSERT INTO documents (area, publication_id, parser_status, page_count, "
                "text_chars) VALUES (?, ?, ?, ?, ?)",
                ("solar", "D001", "imported", 10, 400))
            conn.execute(
                "INSERT INTO documents (area, publication_id, parser_status, page_count, "
                "text_chars) VALUES (?, ?, ?, ?, ?)",
                ("solar", "D002", "pending", 2, 100))
            conn.execute(
                "INSERT INTO documents (area, publication_id, parser_status, page_count, "
                "text_chars) VALUES (?, ?, ?, ?, ?)",
                ("rag", "D001", "imported", 7, 900))

            for area, publication, status in (
                    ("solar", "D001", "ok"), ("solar", "D002", "ok"),
                    ("solar", "D003", "error"), ("rag", "D001", "queued")):
                conn.execute(
                    "INSERT INTO jobs (area, publication_id, status) VALUES (?, ?, ?)",
                    (area, publication, status))
    finally:
        conn.close()


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.data = self.root / "viniti.sqlite3"
        build_database(self.data)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.library = Library(self.root / "papers.sqlite3")
        self.server.data = self.data
        self.thread = Thread(target=self.server.serve_forever)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        self.tmp.cleanup()

    def test_passport_for_one_area(self):
        status, body = get(self.base + "/api/passport?area=solar")
        self.assertEqual(status, 200)
        document = json.loads(body)
        self.assertEqual(document["area"], "solar")
        self.assertIn("results", document["tables"])
        self.assertIn("metric_name", document["tables"]["results"]["columns"])

    def test_passport_for_both_areas(self):
        status, body = get(self.base + "/api/passport")
        self.assertEqual(status, 200)
        self.assertEqual(sorted(json.loads(body)["areas"]), ["rag", "solar"])

    def test_results_slice_keeps_written_text(self):
        status, body = get(self.base + "/api/results?area=solar&table=results"
                                   "&where=metric_name%3DPCE&limit=2")
        self.assertEqual(status, 200)
        document = json.loads(body)
        self.assertEqual(document["count"], 2)
        self.assertEqual(document["rows"][0]["metric_value"], "21.5")
        self.assertIn("source_page", document["columns"])

    def test_numeric_filter_uses_the_mirror_not_text_order(self):
        """9.9 < 20 по числу, хотя по строке '9.9' > '20'. Фильтр обязан брать зеркало."""
        status, body = get(self.base + "/api/results?area=solar&table=results"
                                   "&where=value_num%3E20")
        self.assertEqual(status, 200)
        values = [row["metric_value"] for row in json.loads(body)["rows"]]
        self.assertEqual(values, ["21.5"])

    def test_csv_slice_keeps_area_conventions(self):
        status, body = get(self.base + "/api/results.csv?area=solar&table=results"
                                   "&where=value_num%3E20")
        self.assertEqual(status, 200)
        self.assertEqual(body[:3], b"\xef\xbb\xbf")
        self.assertIn(b"\r\n", body)
        self.assertEqual(len(body.split(b"\r\n")), 3)

    def test_unknown_field_is_a_clear_error_not_an_empty_result(self):
        status, body = get(self.base + "/api/results?area=solar&where=nosuchcol%3D1")
        self.assertEqual(status, 400)
        self.assertIn("nosuchcol", json.loads(body)["error"])

    def test_invalid_area_is_rejected(self):
        status, body = get(self.base + "/api/results?area=bogus")
        self.assertEqual(status, 400)
        self.assertIn("bogus", json.loads(body)["error"])

    def test_missing_database_answers_503(self):
        self.server.data = self.root / "нет-такой-базы.sqlite3"
        status, body = get(self.base + "/api/results?area=solar")
        self.assertEqual(status, 503)
        self.assertIn("База результатов не подключена", json.loads(body)["error"])

    # ------------------------------------------------------------- статика

    def test_index_is_served_from_static(self):
        status, media_type, body = get_with_type(self.base + "/")
        self.assertEqual(status, 200)
        self.assertEqual(media_type, "text/html; charset=utf-8")
        self.assertEqual(body, (ROOT / "static" / "index.html").read_bytes())

    def test_static_file_keeps_its_media_type(self):
        status, media_type, body = get_with_type(self.base + "/style.css")
        self.assertEqual(status, 200)
        self.assertEqual(media_type, "text/css; charset=utf-8")
        self.assertEqual(body, (ROOT / "static" / "style.css").read_bytes())

        status, media_type, body = get_with_type(self.base + "/app.js")
        self.assertEqual(status, 200)
        self.assertEqual(media_type, "text/javascript; charset=utf-8")
        self.assertEqual(body, (ROOT / "static" / "app.js").read_bytes())

    def test_missing_static_file_is_404(self):
        status, _, _ = get_with_type(self.base + "/solar.js")
        self.assertEqual(status, 404)

    def test_dot_dot_does_not_escape_static(self):
        for path in ("/../AGENTS.md", "/static/../AGENTS.md", "/..%2FAGENTS.md"):
            status, _, body = get_with_type(self.base + path)
            self.assertEqual(status, 404, path)
            self.assertNotIn(b"# AGENTS.md", body, path)

    # ------------------------------------------------------------- issues

    def test_issues_without_filter_returns_the_area_rows(self):
        status, body = get(self.base + "/api/issues?area=solar")
        self.assertEqual(status, 200)
        document = json.loads(body)
        self.assertEqual(document["area"], "solar")
        self.assertEqual(document["count"], 3)
        self.assertEqual({row["publication_id"] for row in document["issues"]}, {"D001", "D002"})
        self.assertIn("source_fragment", document["issues"][0])

        status, body = get(self.base + "/api/issues?area=rag")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["count"], 1)

    def test_issues_filter_by_check_name(self):
        status, body = get(self.base + "/api/issues?area=solar&check_name=jv_consistency")
        self.assertEqual(status, 200)
        document = json.loads(body)
        self.assertEqual(document["count"], 2)
        self.assertEqual({row["check_name"] for row in document["issues"]}, {"jv_consistency"})
        self.assertEqual({row["publication_id"] for row in document["issues"]}, {"D001"})

    def test_issues_filter_by_publication_and_limit(self):
        status, body = get(self.base + "/api/issues?area=solar&publication_id=D002&limit=1")
        self.assertEqual(status, 200)
        document = json.loads(body)
        self.assertEqual(document["count"], 1)
        self.assertEqual(document["issues"][0]["issue_id"], "I3")

    def test_issues_unknown_filter_is_rejected(self):
        status, body = get(self.base + "/api/issues?area=solar&nosuch=1")
        self.assertEqual(status, 400)
        error = json.loads(body)["error"]
        self.assertIn("nosuch", error)
        self.assertIn("check_name", error)

    def test_issues_missing_database_answers_503(self):
        self.server.data = self.root / "нет-такой-базы.sqlite3"
        status, body = get(self.base + "/api/issues?area=solar")
        self.assertEqual(status, 503)
        self.assertIn("База результатов не подключена", json.loads(body)["error"])

    # -------------------------------------------------------------- stats

    def test_stats_covers_both_areas(self):
        status, body = get(self.base + "/api/stats")
        self.assertEqual(status, 200)
        self.assertEqual(sorted(json.loads(body)["areas"]), ["rag", "solar"])

    def test_stats_counts_rows_jobs_and_documents(self):
        status, body = get(self.base + "/api/stats")
        self.assertEqual(status, 200)
        solar = json.loads(body)["areas"]["solar"]
        self.assertEqual(solar["publications"], 2)
        self.assertEqual(solar["tables"]["results"], 3)
        self.assertEqual(solar["tables"]["issues"], 3)
        self.assertEqual(solar["tables"]["materials"], 0)
        self.assertEqual(solar["jobs"], {"ok": 2, "error": 1, "skipped": 0,
                                        "running": 0, "queued": 0})
        self.assertEqual(solar["documents"]["total"], 2)
        self.assertEqual(solar["documents"]["parsed"], 1)
        self.assertEqual(solar["documents"]["pages"], 12)
        self.assertEqual(solar["documents"]["chars"], 500)
        self.assertEqual(solar["documents"]["parser_status"],
                         {"imported": 1, "pending": 1})

        rag = json.loads(body)["areas"]["rag"]
        self.assertEqual(rag["publications"], 1)
        self.assertEqual(rag["tables"]["issues"], 1)
        self.assertEqual(rag["jobs"]["queued"], 1)
        self.assertEqual(rag["documents"]["parsed"], 1)

    def test_stats_missing_database_answers_503(self):
        self.server.data = self.root / "нет-такой-базы.sqlite3"
        status, body = get(self.base + "/api/stats")
        self.assertEqual(status, 503)
        self.assertIn("База результатов не подключена", json.loads(body)["error"])


if __name__ == "__main__":
    unittest.main()
