"""Focused tests for Solar document anchors: JSON, PNG and the 404/400 paths.

The Solar store used to serve ``anchors/*.png`` as 404 because
``solar_documents_get`` had no anchor branch, and its envelope anchors carried
``bbox=None`` with no ``bboxes``. These tests pin both: the anchor JSON has
geometry taken from the parsed source lines, the PNG renders through the same
``rag_api.render_anchor_png`` as RAG, and an unknown source or an invalid page
answers a controlled error instead of a traceback.

Run:  python -X utf8 -m unittest test_solar_api -v
"""
from io import BytesIO
import json
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from urllib.error import HTTPError
from urllib.request import urlopen

from solar_api import SolarStore
from server import Handler, ThreadingHTTPServer


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


def get(url):
    try:
        with urlopen(url, timeout=20) as response:
            return response.status, response.headers.get("Content-Type"), response.read()
    except HTTPError as error:
        with error:
            return error.code, error.headers.get("Content-Type"), error.read()


class SolarAnchorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = SolarStore(root / "solar.sqlite3")
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
        status, media_type, body = get(f"{self.quoted}/anchors/{source_id}.json")
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
        status, media_type, body = get(f"{self.quoted}/anchors/{source_id}.png")
        self.assertEqual(status, 200)
        self.assertEqual(media_type, "image/png")
        self.assertTrue(body.startswith(b"\x89PNG\r\n\x1a\n"))

    def test_missing_anchor_is_404(self):
        for suffix in (".json", ".png"):
            status, _, body = get(f"{self.quoted}/anchors/999999{suffix}")
            self.assertEqual(status, 404, suffix)
            self.assertIn("Источник не найден", json.loads(body)["error"])

    def test_non_numeric_anchor_id_is_400(self):
        status, _, body = get(f"{self.quoted}/anchors/not-a-number.json")
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
                status, _, body = get(f"{self.quoted}/anchors/{source_id}.png")
                self.assertEqual(status, 400)
                self.assertIn("error", json.loads(body))

    def test_existing_solar_routes_still_work(self):
        status, _, body = get(f"{self.base}/api/documents?track=solar")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["total"], 1)

        status, _, body = get(self.quoted)
        self.assertEqual(status, 200)
        self.assertNotIn("results", json.loads(body))

        status, _, body = get(f"{self.quoted}/results?limit=2")
        self.assertEqual(status, 200)
        self.assertEqual(len(json.loads(body)["items"]), 2)

        status, _, body = get(f"{self.quoted}/sources")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["total"], len(self.envelope["results"]))

        status, _, body = get(f"{self.quoted}/export.csv")
        self.assertEqual(status, 200)
        self.assertIn("result_id", body.decode("utf-8-sig"))

        status, media_type, body = get(f"{self.quoted}/pdf")
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
