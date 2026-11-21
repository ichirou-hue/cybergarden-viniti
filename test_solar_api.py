"""Tests for the Solar HTTP adapter (``solar_api``).

The adapter must publish exactly what the deterministic parser verified: the
same result rows, their literal anchors and the deterministic issues. These
tests build the mapping from a crafted parser result, and run one real ingest
over a synthetic PDF, so they need no corpus on disk.
"""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from unittest import mock
from urllib.error import HTTPError
from urllib.request import urlopen

import solar_api
import solar_version
from solar_schema import TABLES


def get(url):
    try:
        with urlopen(url, timeout=60) as response:
            return response.status, response.read()
    except HTTPError as error:
        with error:
            return error.code, error.read()


def result_row(**overrides):
    row = {column: "" for column in TABLES["results"]}
    row.update({"publication_id": "S1", "result_id": "R001", "metric_name": "PCE",
                "metric_value": "16.5", "metric_unit": "%", "device_id": "DV1",
                "measurement_id": "MS1", "extraction_status": "extracted",
                "result_origin": "primary", "source_page": "2",
                "source_fragment": "The target device showed a PCE of 16.5%."})
    row.update(overrides)
    return row


def fake_result(**overrides):
    source = {"id": 7, "page": 2, "bbox": [10, 20, 30, 40], "kind": "text",
              "quote": "The target device showed a PCE of 16.5%."}
    tables = {table: [] for table in TABLES}
    tables["publications"] = [{
        "publication_id": "S1", "title": "Solar paper", "year": "2024",
        "DOI": "", "authors": "A. Author", "in_scope": "true", "exclusion_reason": ""}]
    tables["results"] = [result_row()]
    tables["issues"] = [{
        "publication_id": "S1", "issue_id": "I1", "record_ids": "R001;R002",
        "check_name": "normalization", "description": "Единица не подтверждена.",
        "source_page": "2", "source_fragment": "The target device showed a PCE of 16.5%."}]
    result = {
        "publication_id": "S1", "pages": 3, "sources": [source],
        "warnings": ["Проверено буквальное наличие источников."],
        "tables": tables, "evidence": {"results/R001": [source]},
    }
    result.update(overrides)
    return result


class EnvelopeTest(unittest.TestCase):
    def envelope(self, result=None):
        return solar_api.SolarStore._envelope(
            "solar:s1-abc", "s1.pdf", result or fake_result(), 0.0)

    def test_rows_keep_their_domain_links_and_literal_anchor(self):
        envelope = self.envelope()
        self.assertEqual(envelope["status"], "done")
        self.assertEqual(envelope["counts"], {"results": 1, "issues": 1})
        row = envelope["results"][0]
        self.assertEqual(row["device_id"], "DV1")
        self.assertEqual(row["measurement_id"], "MS1")
        # The shared results table reads the RAG-style keys.
        self.assertEqual(row["config_id"], "DV1")
        self.assertEqual(row["dataset_id"], "MS1")
        self.assertEqual(row["source"]["page"], 2)
        self.assertEqual(row["source"]["source_id"], 7)
        self.assertEqual(row["source"]["fragment"], "The target device showed a PCE of 16.5%.")
        self.assertEqual(envelope["anchors"]["7"]["bbox"], [10, 20, 30, 40])

    def test_issue_is_attached_to_each_record_it_names(self):
        result = fake_result()
        result["tables"]["results"].append(result_row(result_id="R002"))
        result["tables"]["results"].append(result_row(result_id="R003"))
        for record in ("R002", "R003"):
            result["evidence"][f"results/{record}"] = result["evidence"]["results/R001"]
        envelope = self.envelope(result)
        attached = {row["result_id"]: [issue["issue_id"] for issue in row["issues"]]
                    for row in envelope["results"]}
        self.assertEqual(attached["R001"], ["I1"])
        self.assertEqual(attached["R002"], ["I1"])
        # A row the issue does not name stays clean: no blanket attachment.
        self.assertEqual(attached["R003"], [])
        self.assertEqual(envelope["issues"][0]["record_ids"], ["R001", "R002"])

    def test_revision_and_warnings_come_from_the_parser(self):
        envelope = self.envelope()
        self.assertEqual(envelope["handler_revision"], solar_version.PARSER_VERSION)
        self.assertEqual(envelope["run"]["source_revision"], solar_version.PARSER_VERSION)
        self.assertTrue(envelope["warnings"])

    def test_no_results_is_partial_with_a_reason(self):
        result = fake_result()
        result["tables"]["results"] = []
        result["tables"]["issues"] = []
        envelope = self.envelope(result)
        self.assertEqual(envelope["status"], "partial")
        self.assertTrue(any("Результаты не найдены" in text for text in envelope["warnings"]))

    def test_envelope_is_json_serializable(self):
        # The store persists the envelope as one JSON column.
        self.assertIn('"results"', json.dumps(self.envelope(), ensure_ascii=False))

    def test_excluded_paper_is_done_with_its_reason(self):
        result = fake_result()
        publication = dict(result["tables"]["publications"][0],
                           in_scope="false", exclusion_reason="моделирование")
        result["tables"] = {table: [] for table in TABLES}
        result["tables"]["publications"] = [publication]
        envelope = self.envelope(result)
        # An excluded paper is answered, not half-parsed.
        self.assertEqual(envelope["status"], "done")
        self.assertTrue(any("вне области" in text for text in envelope["warnings"]))
        self.assertFalse(any("Результаты не найдены" in text for text in envelope["warnings"]))

    def test_text_sources_skip_table_rows(self):
        result = fake_result()
        result["sources"] = [
            {"id": 1, "page": 1, "bbox": None, "kind": "table", "quote": "a | b"},
            {"id": 2, "page": 1, "bbox": None, "kind": "caption", "quote": "Table 1"},
        ]
        envelope = self.envelope(result)
        self.assertEqual([item["source_id"] for item in envelope["text_sources"]], [2])


class IngestTest(unittest.TestCase):
    def setUp(self):
        from test_solar_offline import TABLE_PAGE, make_pdf

        self.pdf = make_pdf(TABLE_PAGE, table=True)
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def store(self):
        return solar_api.SolarStore(Path(self.directory.name) / "solar.sqlite3")

    def test_ingest_uses_the_deterministic_parser(self):
        store = self.store()
        summary = store.ingest("S1.pdf", self.pdf)
        envelope = store.get(summary["document_id"])
        self.assertEqual(envelope["status"], "done")
        self.assertGreater(envelope["counts"]["results"], 0)
        self.assertTrue(envelope["results"][0]["source"])
        self.assertEqual(envelope["handler_revision"], solar_version.PARSER_VERSION)

    def test_same_bytes_return_the_cached_summary(self):
        store = self.store()
        first = store.ingest("S1.pdf", self.pdf)
        second = store.ingest("S1-copy.pdf", self.pdf)
        self.assertEqual(first["document_id"], second["document_id"])
        with store._connect() as connection:
            rows = connection.execute("SELECT COUNT(*) FROM solar_documents").fetchone()[0]
        self.assertEqual(rows, 1)

    def test_cache_from_an_older_revision_is_reparsed(self):
        store = self.store()
        summary = store.ingest("S1.pdf", self.pdf)
        with store._connect() as connection, connection:
            stale = dict(summary, handler_revision=solar_version.PARSER_VERSION - 1)
            connection.execute("UPDATE solar_documents SET summary = ?",
                               (json.dumps(stale, ensure_ascii=False),))
        with mock.patch.object(solar_api.solar_offline, "extract_pdf",
                               wraps=solar_api.solar_offline.extract_pdf) as parse:
            reparsed = store.ingest("S1.pdf", self.pdf)
        self.assertTrue(parse.called)
        self.assertEqual(reparsed["handler_revision"], solar_version.PARSER_VERSION)

    def test_non_pdf_is_refused(self):
        with self.assertRaises(ValueError):
            self.store().ingest("S1.pdf", b"not a pdf")

    def test_size_limit_is_checked_before_parsing(self):
        with mock.patch.object(solar_api, "MAX_BYTES", 16), \
                mock.patch.object(solar_api.solar_offline, "extract_pdf") as parse:
            with self.assertRaises(ValueError) as raised:
                self.store().ingest("S1.pdf", b"%PDF-" + b"0" * 32)
        parse.assert_not_called()
        self.assertIn("32 МиБ", str(raised.exception))


class DocumentApiTest(unittest.TestCase):
    """The Solar document endpoints must serve what the envelope actually holds."""

    def setUp(self):
        from library import Library
        from server import Handler, ThreadingHTTPServer
        from test_solar_offline import TABLE_PAGE, make_pdf

        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.store = solar_api.SolarStore(root / "solar.sqlite3")
        self.summary = self.store.ingest("S1.pdf", make_pdf(TABLE_PAGE, table=True))
        self.document_id = self.summary["document_id"]
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.library = Library(root / "papers.sqlite3")
        self.server.solar_store = self.store
        self.thread = Thread(target=self.server.serve_forever)
        self.thread.start()
        self.addCleanup(self._stop)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def _stop(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()

    def document_url(self, resource=""):
        return f"{self.base}/api/documents/{self.document_id}/{resource}"

    def store_envelope(self, envelope):
        with self.store._connect() as connection, connection:
            connection.execute(
                "INSERT OR REPLACE INTO solar_documents VALUES (?, ?, ?, ?, ?, ?)",
                (envelope["document_id"], envelope["document_id"], "{}",
                 json.dumps(envelope, ensure_ascii=False), b"%PDF-", envelope["updated_at"]))
        return f"{self.base}/api/documents/{envelope['document_id']}/"

    def test_issues_endpoint_returns_verified_issues(self):
        # Crafted, so the assertion cannot pass on an empty synthetic result.
        base = self.store_envelope(solar_api.SolarStore._envelope(
            "solar:fake", "fake.pdf", fake_result(), 0.0))
        status, body = get(base + "issues")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["items"][0]["issue_id"], "I1")
        self.assertEqual(payload["items"][0]["record_ids"], ["R001", "R002"])

    def test_sources_endpoint_dedupes_and_keeps_kinds(self):
        result = fake_result()
        result["sources"] = [
            {"id": 7, "page": 2, "bbox": None, "kind": "text",
             "quote": "The target device showed a PCE of 16.5%."},
            {"id": 8, "page": 3, "bbox": None, "kind": "caption",
             "quote": "Table 1: photovoltaic parameters"},
        ]
        base = self.store_envelope(solar_api.SolarStore._envelope(
            "solar:fake", "fake.pdf", result, 0.0))
        status, body = get(base + "sources")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        # The result anchor and its text source are the same source: one entry.
        self.assertEqual(payload["total"], 2)
        self.assertEqual([item["kind"] for item in payload["items"]], ["text", "caption"])
        status, body = get(base + "sources?kind=caption")
        self.assertEqual(json.loads(body)["total"], 1)
        self.assertEqual(json.loads(body)["items"][0]["source_id"], 8)

    def test_bad_pagination_is_a_client_error(self):
        status, _ = get(self.document_url("results") + "?limit=abc")
        self.assertEqual(status, 400)
        status, _ = get(self.document_url("issues") + "?offset=-1")
        self.assertEqual(status, 400)

    def test_catalog_search_matches_solar_authors_string(self):
        envelope = self.store.get(self.document_id)
        summary = {**self.summary, "authors": "Vajjiravel Balamurugan"}
        with self.store._connect() as connection, connection:
            connection.execute("UPDATE solar_documents SET summary = ? WHERE document_id = ?",
                               (json.dumps(summary, ensure_ascii=False), self.document_id))
        status, body = get(self.base + "/api/documents?track=solar&q=Vajjiravel")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["total"], 1)
        self.assertEqual(envelope["track"], "solar")


if __name__ == "__main__":
    unittest.main()
