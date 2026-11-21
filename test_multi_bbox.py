import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from reportlab.pdfgen import canvas

from rag.pdf import Document, Line, Page, Word
from rag_api import _anchor, _rects_for, render_anchor_png


class MultiBBoxTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.path = Path(self.tmp.name) / "wrapped.pdf"
        pdf = canvas.Canvas(str(self.path))
        pdf.drawString(72, 740, "alpha beta")
        pdf.drawString(72, 720, "gamma delta")
        pdf.save()
        self.document = Document(
            self.path,
            (Page(1, "alpha beta\ngamma delta", lines=(
                Line("alpha beta", 72, 100, 140, 110,
                     (Word("alpha", 72, 100, 100, 110), Word("beta", 105, 100, 140, 110))),
                Line("gamma delta", 72, 120, 150, 130,
                     (Word("gamma", 72, 120, 110, 130), Word("delta", 115, 120, 150, 130))),
            )),),
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_fragment_across_two_lines_returns_local_rectangles(self):
        boxes = _rects_for(self.document, 1, "alpha beta gamma delta")
        self.assertGreaterEqual(len(boxes), 2)
        self.assertLess(boxes[0][2], 300)
        self.assertLess(boxes[1][2], 300)

    def test_legacy_bbox_render_and_invalid_page(self):
        try:
            import pymupdf  # noqa: F401
        except ModuleNotFoundError:
            self.skipTest("pymupdf is not installed")
        png = render_anchor_png(self.path.read_bytes(), 1, [70, 715, 180, 750])
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
        for page in (0, 2):
            with self.assertRaises(ValueError):
                render_anchor_png(self.path.read_bytes(), page, [70, 715, 180, 750])

    def test_anchor_keeps_explicit_zero_page_non_renderable(self):
        anchor = _anchor(self.document, source_id=1, page=0, fragment="alpha")
        self.assertEqual(anchor["page"], 0)
        self.assertEqual(anchor["bboxes"], [])
        self.assertIsNone(anchor["bbox"])

    def test_anchor_out_of_range_page_is_non_renderable(self):
        anchor = _anchor(self.document, source_id=1, page=2, fragment="alpha")
        self.assertEqual(anchor["page"], 2)
        self.assertEqual(anchor["bboxes"], [])
        self.assertIsNone(anchor["bbox"])

    def test_anchor_malformed_page_is_none_and_non_renderable(self):
        for page in (None, "", "not-a-page"):
            anchor = _anchor(self.document, source_id=1, page=page, fragment="alpha")
            self.assertIsNone(anchor["page"])
            self.assertEqual(anchor["bboxes"], [])
            self.assertIsNone(anchor["bbox"])


if __name__ == "__main__":
    unittest.main()
