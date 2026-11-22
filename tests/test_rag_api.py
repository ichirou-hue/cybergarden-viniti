"""Тесты RAG-адаптера и HTTP-контракта каталога."""

import json
from http.cookiejar import CookieJar
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from unittest import mock
from urllib.request import HTTPCookieProcessor, Request, build_opener

import auth
import rag.pdf as rag_pdf
from library import Library
from rag_api import RagStore, build_envelope, demo_pdf, export_csv, _anchor
from server import Handler, ThreadingHTTPServer


class AdapterTest(unittest.TestCase):
    def test_csv_escapes_formula_values(self):
        envelope = {"publication_id": "=pdf", "results": [{"metric_name": "@metric", "metric_value": "-1"}]}
        text = export_csv(envelope).decode("utf-8-sig")
        self.assertIn("'=pdf", text)
        self.assertIn("'@metric", text)
        self.assertIn("'-1", text)

    def test_csv_escapes_formula_values_in_source(self):
        envelope = {"publication_id": "P", "results": [{"metric_name": "M", "source": {"fragment": "=cite"}}]}
        self.assertIn("'=cite", export_csv(envelope).decode("utf-8-sig"))

    def test_demo_envelope_has_results_and_anchors(self):
        envelope = build_envelope(
            document_id="rag:demo",
            name="demo.pdf",
            pdf=demo_pdf(),
            publication_id="DEMO",
            duration_ms=1,
        )
        self.assertEqual(envelope["status"], "done")
        self.assertGreater(envelope["results_count"], 0)
        self.assertEqual(envelope["results_count"], len(envelope["results"]))
        first = envelope["results"][0]
        self.assertEqual(first["metric_name"], "Recall")
        self.assertEqual(first["metric_k"], 5)
        self.assertIsNotNone(first["source"]["bbox"])
        self.assertIn(str(first["source"]["source_id"]), envelope["anchors"])
        self.assertIn("publication_id", export_csv(envelope).decode("utf-8-sig"))

    def test_pdfplumber_fallback_builds_lines(self):
        """pdfplumber обязан отдавать Page с lines: иначе таблиц не найти."""
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "demo.pdf"
            path.write_bytes(demo_pdf())
            document = rag_pdf.load_document(path, parser="pdfplumber")
        self.assertEqual(document.parser, "pdfplumber")
        self.assertEqual(len(document.pages), 1)
        lines = document.pages[0].lines
        self.assertGreater(len(lines), 5)
        for line in lines:
            self.assertTrue(line.text.strip())
            self.assertTrue(line.words)
            self.assertLessEqual(line.x0, line.x1)
            self.assertLessEqual(line.y0, line.y1)
            self.assertGreater(line.y1, 0.0)
            for word in line.words:
                self.assertTrue(word.text.strip())
                self.assertLessEqual(word.x0, word.x1)
                self.assertLessEqual(word.y0, word.y1)

    def test_pdfplumber_fallback_keeps_results_and_anchors(self):
        """Без PyMuPDF таблица из demo_pdf всё равно даёт строки, а не ноль."""
        reference = build_envelope(
            document_id="rag:demo",
            name="demo.pdf",
            pdf=demo_pdf(),
            publication_id="DEMO",
            duration_ms=1,
        )
        with mock.patch.object(
            rag_pdf, "_load_pymupdf", side_effect=ImportError("pymupdf is not installed")
        ):
            fallback = build_envelope(
                document_id="rag:demo",
                name="demo.pdf",
                pdf=demo_pdf(),
                publication_id="DEMO",
                duration_ms=1,
            )

        self.assertEqual(fallback["status"], "done")
        self.assertGreater(fallback["results_count"], 0)
        self.assertEqual(fallback["results_count"], len(fallback["results"]))
        self.assertEqual(fallback["results_count"], reference["results_count"])
        self.assertEqual(fallback["counts"], reference["counts"])
        self.assertTrue(fallback["anchors"])
        self.assertEqual(len(fallback["anchors"]), len(reference["anchors"]))
        self.assertEqual(
            [
                (row["metric_name"], row["metric_k"], row["metric_value"],
                 row["config_id"], row["dataset_id"])
                for row in fallback["results"]
            ],
            [
                (row["metric_name"], row["metric_k"], row["metric_value"],
                 row["config_id"], row["dataset_id"])
                for row in reference["results"]
            ],
        )
        for row in fallback["results"]:
            self.assertIn(str(row["source"]["source_id"]), fallback["anchors"])
            self.assertIsNotNone(row["source"]["bbox"])

    def test_store_roundtrip_is_idempotent_by_hash(self):
        with TemporaryDirectory() as tmp:
            store = RagStore(Path(tmp) / "rag.sqlite3")
            first = store.ingest("demo.pdf", demo_pdf(), publication_id="DEMO")
            second = store.ingest("copy.pdf", demo_pdf(), publication_id="DEMO")
            self.assertEqual(first["document_id"], second["document_id"])
            self.assertEqual(store.list()[0]["document_id"], first["document_id"])
            self.assertIsNotNone(store.get(first["document_id"]))
            self.assertTrue(store.pdf(first["document_id"]).startswith(b"%PDF-"))


class HttpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.auth = auth.Auth(Path(self.tmp.name) / "auth.sqlite3",
                                     iterations=auth.MIN_ITERATIONS)
        self.server.library = Library(Path(self.tmp.name) / "papers.sqlite3")
        self.server.rag_store = RagStore(Path(self.tmp.name) / "papers.sqlite3")
        self.thread = Thread(target=self.server.serve_forever)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.client = build_opener(HTTPCookieProcessor(CookieJar()))

    def tearDown(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        self.tmp.cleanup()

    def get_json(self, path):
        with self.client.open(self.base + path, timeout=10) as response:
            return json.load(response)

    def register(self):
        request = Request(
            self.base + "/api/auth/register",
            data=json.dumps({"username": "rag_demo", "password": "correct horse"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with self.client.open(request, timeout=10) as response:
            return json.load(response)["csrf_token"]

    def test_demo_catalog_results_and_evidence(self):
        csrf = self.register()
        request = Request(self.base + "/api/demo", data=b"", headers={
            "X-Track": "rag", "X-CSRF-Token": csrf,
        })
        with self.client.open(request, timeout=30) as response:
            created = json.load(response)
        self.assertEqual(created["document_id"], "rag:demo")

        catalog = self.get_json("/api/documents?track=rag")
        self.assertEqual(catalog["total"], 1)
        self.assertEqual(catalog["items"][0]["status"], "done")

        card = self.get_json("/api/documents/rag%3Ademo")
        self.assertIn("counts", card)
        self.assertNotIn("results", card)

        results = self.get_json("/api/documents/rag%3Ademo/results?limit=2")
        self.assertEqual(len(results["items"]), 2)
        source_id = results["items"][0]["source"]["source_id"]
        anchor = self.get_json(f"/api/documents/rag%3Ademo/anchors/{source_id}.json")
        self.assertEqual(anchor["page"], 1)

        with self.client.open(f"{self.base}/api/documents/rag%3Ademo/anchors/{source_id}.png", timeout=10) as response:
            self.assertEqual(response.read(8), b"\x89PNG\r\n\x1a\n")
        with self.client.open(f"{self.base}/api/documents/rag%3Ademo/export.csv", timeout=10) as response:
            self.assertIn("result_id", response.read().decode("utf-8-sig"))
        jobs = self.get_json("/api/jobs?track=rag")
        self.assertEqual(jobs["done"], 1)


if __name__ == "__main__":
    unittest.main()
