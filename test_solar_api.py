"""Tests for the Solar HTTP adapter (``solar_api``).

The adapter must publish exactly what the deterministic parser verified: the
same result rows, their literal anchors and the deterministic issues. These
tests build the mapping from a crafted parser result, and run one real ingest
over a synthetic PDF, so they need no corpus on disk.
"""

from io import BytesIO
import json
import sqlite3
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

    def test_no_results_is_done_with_a_reason(self):
        result = fake_result()
        result["tables"]["results"] = []
        result["tables"]["issues"] = []
        envelope = self.envelope(result)
        # The parser finished and found nothing: that is an answer, not a
        # half-read document. The UI shows its own "no results" notice.
        self.assertEqual(envelope["status"], "done")
        self.assertTrue(any("Результаты не найдены" in text for text in envelope["warnings"]))

    def test_unreadable_page_marks_the_document_partial(self):
        result = fake_result()
        result["warnings"] = ["Страница 3: текстовый слой пуст или отсутствует."]
        envelope = self.envelope(result)
        self.assertEqual(envelope["status"], "partial")

    def test_envelope_is_json_serializable(self):
        # The store persists the envelope as one JSON column.
        self.assertIn('"results"', json.dumps(self.envelope(), ensure_ascii=False))

    def test_excluded_paper_is_done_with_its_reason(self):
        result = fake_result()
        publication = dict(result["tables"]["publications"][0],
                           in_scope="false", exclusion_reason="моделирование.")
        result["tables"] = {table: [] for table in TABLES}
        result["tables"]["publications"] = [publication]
        envelope = self.envelope(result)
        # An excluded paper is answered, not half-parsed.
        self.assertEqual(envelope["status"], "done")
        reason = next(text for text in envelope["warnings"] if "вне области" in text)
        self.assertTrue(reason.endswith("моделирование."))
        self.assertNotIn("..", reason)
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
        stages = []
        summary = store.ingest("S1.pdf", self.pdf, progress=lambda step, label: stages.append(step))
        self.assertEqual(stages, [1, 2, 3])
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

    def test_stale_cache_with_another_name_reuses_the_card(self):
        store = self.store()
        summary = store.ingest("S1.pdf", self.pdf)
        with store._connect() as connection, connection:
            stale = dict(summary, handler_revision=solar_version.PARSER_VERSION - 1)
            connection.execute("UPDATE solar_documents SET summary = ?",
                               (json.dumps(stale, ensure_ascii=False),))
        again = store.ingest("S1-renamed.pdf", self.pdf)
        # Same bytes, new name, old revision: still one card, refreshed in place.
        self.assertEqual(again["document_id"], summary["document_id"])
        with store._connect() as connection:
            rows = connection.execute("SELECT COUNT(*) FROM solar_documents").fetchone()[0]
        self.assertEqual(rows, 1)

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
        catalog_parse = mock.patch("server.Handler.start_catalog_parse", return_value="test-docling-job")
        catalog_parse.start()
        self.addCleanup(catalog_parse.stop)
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

    def test_results_endpoint_applies_search_and_issue_filter(self):
        result = fake_result()
        result["tables"]["results"].append(
            result_row(result_id="R002", metric_name="Voc", metric_value="1.2"))
        result["evidence"]["results/R002"] = result["evidence"]["results/R001"]
        result["tables"]["issues"][0]["record_ids"] = "R002"
        base = self.store_envelope(solar_api.SolarStore._envelope(
            "solar:fake", "fake.pdf", result, 0.0))

        payload = json.loads(get(base + "results")[1])
        self.assertEqual(payload["total"], 2)

        payload = json.loads(get(base + "results?q=Voc")[1])
        self.assertEqual([row["result_id"] for row in payload["items"]], ["R002"])

        payload = json.loads(get(base + "results?issues=1")[1])
        self.assertEqual([row["result_id"] for row in payload["items"]], ["R002"])

        payload = json.loads(get(base + "results?status=extracted")[1])
        self.assertEqual(payload["total"], 2)

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


# --- anchor contract from the backend-core branch, kept on the merged parser ---

def table_pdf():
    """One page with a table TableFiller really parses (same fixture as pipeline)."""
    from reportlab.pdfgen import canvas

    buffer = BytesIO()
    drawing = canvas.Canvas(buffer)
    drawing.setFont("Helvetica", 7)
    columns = [30, 90, 150, 205, 280, 330]
    rows = [170, 145, 120, 95]
    cells = [
        ["Sample", "J SC (mA cm\u22122)", "Voc (V)", "FF", "PCE (%)"],
        ["Pristine", "15.28", "1,197", "0.71", "12.89"],
        ["Control", "14.10", "1,150", "0.68", "11.02"],
    ]
    for x in columns:
        drawing.setLineWidth(0.6)
        drawing.line(x, rows[0], x, rows[-1])
    for y in rows:
        drawing.line(columns[0], y, columns[-1], y)
    for ri, row in enumerate(cells):
        for ci, text in enumerate(row):
            drawing.drawString(columns[ci] + 3, rows[ri] - 9, text)
    drawing.showPage()
    drawing.save()
    return buffer.getvalue()


def get_typed(url):
    try:
        with urlopen(url, timeout=20) as response:
            return response.status, response.headers.get("Content-Type"), response.read()
    except HTTPError as error:
        with error:
            return error.code, error.headers.get("Content-Type"), error.read()


class SolarAnchorTest(unittest.TestCase):
    def setUp(self):
        from server import Handler, ThreadingHTTPServer

        self.tmp = TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = solar_api.SolarStore(root / "solar.sqlite3")
        self.summary = self.store.ingest("table.pdf", table_pdf())
        self.document_id = self.summary["document_id"]
        self.envelope = self.store.get(self.document_id)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.solar_store = self.store
        self.thread = Thread(target=self.server.serve_forever)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.quoted = f"{self.base}/api/documents/{self.document_id}"

    def tearDown(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        self.tmp.cleanup()

    def anchor_id(self):
        return next(iter(self.envelope["anchors"]))

    def test_envelope_anchor_has_geometry(self):
        anchors = self.envelope["anchors"]
        self.assertTrue(anchors, "Solar envelope must expose at least one anchor")
        for anchor in anchors.values():
            self.assertEqual(anchor["page"], 1)
            self.assertTrue(anchor["fragment"])
            self.assertIsInstance(anchor["bboxes"], list)
            self.assertTrue(anchor["bboxes"], anchor)
            for box in anchor["bboxes"]:
                self.assertEqual(len(box), 4)
                self.assertLess(box[0], box[2])
                self.assertLess(box[1], box[3])
            self.assertEqual(len(anchor["bbox"]), 4)
            self.assertEqual(anchor["bbox"], [
                min(box[0] for box in anchor["bboxes"]),
                min(box[1] for box in anchor["bboxes"]),
                max(box[2] for box in anchor["bboxes"]),
                max(box[3] for box in anchor["bboxes"]),
            ])

    def test_anchor_json_keeps_bbox_and_bboxes(self):
        source_id = self.anchor_id()
        status, media_type, body = get_typed(f"{self.quoted}/anchors/{source_id}.json")
        self.assertEqual(status, 200)
        self.assertEqual(media_type, "application/json; charset=utf-8")
        anchor = json.loads(body)
        self.assertEqual(str(anchor["source_id"]), source_id)
        self.assertEqual(anchor["page"], 1)
        self.assertTrue(anchor["fragment"])
        self.assertEqual(len(anchor["bbox"]), 4)
        self.assertEqual(len(anchor["bboxes"]), 1)

    def test_anchor_png_renders(self):
        try:
            import pymupdf  # noqa: F401
        except ModuleNotFoundError:
            self.skipTest("pymupdf is not installed")
        source_id = self.anchor_id()
        status, media_type, body = get_typed(f"{self.quoted}/anchors/{source_id}.png")
        self.assertEqual(status, 200)
        self.assertEqual(media_type, "image/png")
        self.assertTrue(body.startswith(b"\x89PNG\r\n\x1a\n"))

    def test_missing_anchor_is_404(self):
        for suffix in (".json", ".png"):
            status, _, body = get_typed(f"{self.quoted}/anchors/999999{suffix}")
            self.assertEqual(status, 404, suffix)
            self.assertIn("Источник не найден", json.loads(body)["error"])

    def test_non_numeric_anchor_id_is_400(self):
        status, _, body = get_typed(f"{self.quoted}/anchors/not-a-number.json")
        self.assertEqual(status, 400)
        self.assertIn("числом", json.loads(body)["error"])

    def test_invalid_anchor_page_is_a_controlled_400(self):
        try:
            import pymupdf  # noqa: F401
        except ModuleNotFoundError:
            self.skipTest("pymupdf is not installed")
        source_id = self.anchor_id()
        for bad_page in (0, None):
            with self.subTest(page=bad_page):
                self._set_anchor_page(source_id, bad_page)
                status, _, body = get_typed(f"{self.quoted}/anchors/{source_id}.png")
                self.assertEqual(status, 400)
                self.assertIn("error", json.loads(body))

    def test_existing_solar_routes_still_work(self):
        status, _, body = get_typed(f"{self.base}/api/documents?track=solar")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["total"], 1)

        status, _, body = get_typed(self.quoted)
        self.assertEqual(status, 200)
        self.assertNotIn("results", json.loads(body))

        status, _, body = get_typed(f"{self.quoted}/results?limit=2")
        self.assertEqual(status, 200)
        self.assertEqual(len(json.loads(body)["items"]), 2)

        status, _, body = get_typed(f"{self.quoted}/sources")
        self.assertEqual(status, 200)
        # Result anchors plus text sources, deduplicated: more than one per row.
        self.assertEqual(json.loads(body)["total"],
                         len(solar_api.solar_sources(self.envelope)))

        status, _, body = get_typed(f"{self.quoted}/export.csv")
        self.assertEqual(status, 200)
        self.assertIn("result_id", body.decode("utf-8-sig"))

        status, media_type, body = get_typed(f"{self.quoted}/pdf")
        self.assertEqual(status, 200)
        self.assertEqual(media_type, "application/pdf")
        self.assertTrue(body.startswith(b"%PDF-"))

    def _set_anchor_page(self, source_id, page):
        envelope = json.loads(json.dumps(self.envelope))
        envelope["anchors"][str(source_id)]["page"] = page
        conn = sqlite3.connect(self.store.path)
        try:
            with conn:
                conn.execute(
                    "UPDATE solar_documents SET envelope=? WHERE document_id=?",
                    (json.dumps(envelope), self.document_id),
                )
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
