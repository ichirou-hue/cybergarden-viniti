"""Recognition blocks stay independent of scientific records and evidence."""

from copy import deepcopy
from contextlib import closing
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

from solar_api import SolarStore, solar_sources

try:  # раскладка репозитория: тесты лежат в tests/
    from tests.test_solar_api import fake_result
except ImportError:  # запуск из tests/ или с PYTHONPATH=tests
    from test_solar_api import fake_result


class SolarRecognitionTest(unittest.TestCase):
    def test_image_only_upload_keeps_card_for_docling_without_invented_facts(self):
        from PIL import Image
        from reportlab.pdfgen import canvas
        from reportlab.lib.utils import ImageReader
        stream = BytesIO()
        drawing = canvas.Canvas(stream)
        image = Image.new("RGB", (100, 100), "white")
        drawing.drawImage(ImageReader(image), 50, 50, width=100, height=100)
        drawing.showPage()
        drawing.drawImage(ImageReader(image), 50, 50, width=100, height=100)
        drawing.save()
        summary = self.store.ingest("scan.pdf", stream.getvalue())
        envelope = self.store.get(summary["document_id"])
        self.assertEqual(envelope["pages"], 2)
        self.assertEqual(envelope["status"], "partial")
        self.assertTrue(envelope["scanned"])
        self.assertEqual(envelope["results"], [])
        self.assertEqual(envelope["publication"], {})
        self.assertEqual(envelope["anchors"], {})

    def test_unrelated_parser_error_is_not_masked_by_scan_card(self):
        with mock.patch("solar_api.solar_offline.extract_pdf", side_effect=ValueError("invalid evidence")):
            with self.assertRaisesRegex(ValueError, "invalid evidence"):
                self.store.ingest("invalid.pdf", b"%PDF-distinct-test")

    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = SolarStore(Path(self.temporary.name) / "solar.sqlite3")
        self.pdf = b"%PDF-synthetic-recognition-fixture"
        self.document_id = "solar:test-recognition"
        self.original = SolarStore._envelope(self.document_id, "D001.pdf", fake_result(), 0)
        with closing(self.store._connect()) as conn, conn:
            conn.execute("INSERT INTO solar_documents VALUES (?, ?, ?, ?, ?, ?)",
                         (self.document_id, sha256(self.pdf).hexdigest(), "{}",
                          json.dumps(self.original), self.pdf, self.original["updated_at"]))

    def result(self):
        return {"document": {"pages": 3, "sha256": sha256(self.pdf).hexdigest()},
                "parser_output": {"parser": {"name": "docling", "version": "test"}},
                "warnings": ["Recognition requires review."],
                "sources": [
                    {"id": 1, "page": 2, "bbox": [10, 200, 100, 220], "quote": "Left column ends here.", "kind": "text"},
                    {"id": 2, "page": 2, "bbox": [200, 10, 300, 30], "quote": "SrCl2-doped.",
                     "display_text": "SrCl_{2}-doped.", "kind": "equation"},
                ]}

    def test_reading_order_and_math_are_visible_without_changing_results(self):
        summary = self.store.attach_recognition(self.document_id, self.result())
        envelope = self.store.get(self.document_id)
        self.assertEqual(envelope["results"], self.original["results"])
        self.assertEqual(envelope["issues"], self.original["issues"])
        self.assertEqual(envelope["text_sources"], self.original["text_sources"])
        self.assertEqual(envelope["anchors"]["7"], self.original["anchors"]["7"])
        items = solar_sources(envelope)
        self.assertEqual([item["source_id"] for item in items], [1000001, 1000002])
        self.assertEqual([item["fragment"] for item in items], ["Left column ends here.", "SrCl2-doped."])
        self.assertEqual(items[1]["display_text"], "SrCl_{2}-doped.")
        self.assertEqual(items[1]["verification"], "parser_text_only")
        self.assertTrue(items[1]["requires_review"])
        self.assertEqual(envelope["anchors"]["1000002"]["bbox"], [200, 10, 300, 30])
        self.assertNotIn("recognized_sources", summary)
        self.assertEqual(summary["recognition"]["status"], "done")
        self.assertEqual(solar_sources(envelope, kind="equation", needle="doped"), [items[1]])

    def test_wrong_document_or_invalid_block_keeps_stored_data(self):
        result = self.result()
        result["document"]["sha256"] = "wrong"
        with self.assertRaises(ValueError):
            self.store.attach_recognition(self.document_id, result)
        self.assertEqual(self.store.get(self.document_id), self.original)
        result = self.result()
        result["sources"][0]["bbox"][2] = float("nan")
        with self.assertRaises(ValueError):
            self.store.attach_recognition(self.document_id, result)
        self.assertEqual(self.store.get(self.document_id), self.original)

    def test_repeat_recognition_replaces_only_recognition_anchors(self):
        self.store.attach_recognition(self.document_id, self.result())
        result = deepcopy(self.result())
        result["sources"] = result["sources"][:1]
        self.store.attach_recognition(self.document_id, result)
        envelope = self.store.get(self.document_id)
        self.assertEqual(set(envelope["anchors"]), {"7", "1000001"})
        self.assertEqual(envelope["results"], self.original["results"])
