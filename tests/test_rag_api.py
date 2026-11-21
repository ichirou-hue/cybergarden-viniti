"""Тесты RAG-адаптера и HTTP-контракта каталога."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from urllib.request import Request, urlopen

from library import Library
from rag_api import RagStore, build_envelope, demo_pdf, export_csv
from server import Handler, ThreadingHTTPServer


class AdapterTest(unittest.TestCase):
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
        self.server.library = Library(Path(self.tmp.name) / "papers.sqlite3")
        self.server.rag_store = RagStore(Path(self.tmp.name) / "papers.sqlite3")
        self.thread = Thread(target=self.server.serve_forever)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        self.tmp.cleanup()

    def get_json(self, path):
        with urlopen(self.base + path, timeout=10) as response:
            return json.load(response)

    def test_demo_catalog_results_and_evidence(self):
        request = Request(self.base + "/api/demo", data=b"", headers={"X-Track": "rag"})
        with urlopen(request, timeout=30) as response:
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

        with urlopen(f"{self.base}/api/documents/rag%3Ademo/anchors/{source_id}.png", timeout=10) as response:
            self.assertEqual(response.read(8), b"\x89PNG\r\n\x1a\n")
        with urlopen(f"{self.base}/api/documents/rag%3Ademo/export.csv", timeout=10) as response:
            self.assertIn("result_id", response.read().decode("utf-8-sig"))
        jobs = self.get_json("/api/jobs?track=rag")
        self.assertEqual(jobs["done"], 1)


if __name__ == "__main__":
    unittest.main()
