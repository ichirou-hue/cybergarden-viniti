"""Тесты извлечения из PDF: устойчивость к мусору и пределы.

Run:  python -X utf8 -m unittest test_pdf_source -v
"""
from io import BytesIO
import unittest
from unittest import mock

import pdfplumber

import pdf_source


def ruled_pdf(rule_positions, page_size=(612, 792), texts=(), columns=None):
    """Страница с горизонтальными линиями на заданных y и подписями."""
    from reportlab.pdfgen import canvas

    buffer = BytesIO()
    drawing = canvas.Canvas(buffer, pagesize=page_size)
    for top in rule_positions:
        drawing.line(100, top, 500, top)
    drawing.drawString(72, 100, "Table 1. Parameters.")
    for top, text in texts:
        drawing.drawString(110, top, text)
    for top, cells in columns or ():
        for x, text in cells:
            drawing.drawString(x, top, text)
    drawing.save()
    return buffer.getvalue()


class FindTablesTest(unittest.TestCase):
    def test_rules_above_the_page_do_not_crash_extraction(self):
        """Линия за пределами MediaBox раньше роняла разбор всей статьи.

        pdfplumber держит объекты вне страницы, поэтому stops[0] оказывается
        отрицательным, а crop() отказывается от такого бокса с ValueError.
        """
        data = ruled_pdf([800, 820, 840])
        document = pdf_source.extract(data)
        self.assertEqual(document["pages"], 1)
        self.assertIsInstance(document["sources"], list)

    def test_normal_table_rules_still_produce_rows(self):
        data = ruled_pdf([600, 570, 540, 510], columns=[
            (585, [(110, "Pristine"), (250, "15.28"), (400, "1.197")]),
            (555, [(110, "Doped"), (250, "15.64"), (400, "1.278")]),
        ])
        document = pdf_source.extract(data)
        self.assertEqual(document["pages"], 1)
        self.assertTrue(any(source["kind"] == "table" for source in document["sources"]))


class PageTextTest(unittest.TestCase):
    def test_indices_stay_with_their_formula_in_sources(self):
        from reportlab.pdfgen import canvas
        buffer = BytesIO()
        pdf = canvas.Canvas(buffer)
        pdf.setFont("Helvetica", 12)
        pdf.drawString(60, 700, "J")
        pdf.setFont("Helvetica", 8)
        pdf.drawString(66, 696, "SC")
        pdf.setFont("Helvetica", 12)
        pdf.drawString(81, 700, " = 12 mA/cm")
        pdf.setFont("Helvetica", 8)
        pdf.drawString(152, 706, "2")
        pdf.save()
        sources = pdf_source.extract(buffer.getvalue())["sources"]
        self.assertEqual(len(sources), 1)
        self.assertIn("J_{SC}", sources[0]["display_text"])
        self.assertIn("^{2}", sources[0]["display_text"])
        self.assertEqual({span["position"] for span in sources[0]["math_spans"]}, {"subscript", "superscript"})

    def test_offset_column_baselines_still_read_column_by_column(self):
        from reportlab.pdfgen import canvas
        buffer = BytesIO()
        pdf = canvas.Canvas(buffer, pagesize=(612, 792))
        for index in range(5):
            pdf.drawString(45, 700 - index * 20, f"Left paragraph line {index} ends here.")
            pdf.drawString(335, 692 - index * 20, f"Right paragraph line {index} ends here.")
        pdf.save()
        lines = pdf_source.extract(buffer.getvalue())["sources"]
        self.assertEqual([s["column"] for s in lines], [1] * 5 + [2] * 5)

    def test_columns_are_separate_in_reading_order(self):
        from reportlab.pdfgen import canvas
        buffer = BytesIO()
        pdf = canvas.Canvas(buffer, pagesize=(612, 792))
        for index in range(5):
            pdf.drawString(45, 700 - index * 18, f"Left paragraph line {index} ends here.")
            pdf.drawString(335, 700 - index * 18, f"Right paragraph line {index} ends here.")
        pdf.save()
        document = pdf_source.extract(buffer.getvalue())
        lines = document["sources"]
        self.assertEqual(len(lines), 10)
        self.assertTrue(all(s["column_count"] == 2 for s in lines))
        self.assertEqual([s["column"] for s in lines], [1] * 5 + [2] * 5)
        self.assertTrue(all("Right" not in s["quote"] for s in lines[:5]))
        self.assertTrue(all("Left" not in s["quote"] for s in lines[5:]))
        self.assertEqual(len({s["block_id"] for s in lines}), 10)

    def test_page_texts_are_numbered_from_one(self):
        data = ruled_pdf([600, 570, 540])
        texts = pdf_source.page_texts(data)
        self.assertEqual(sorted(texts), [1])
        self.assertIn("Table 1", texts[1])

    def test_extract_returns_page_texts_with_the_sources(self):
        document = pdf_source.extract(ruled_pdf([600, 570, 540]))
        self.assertIn("page_texts", document)
        self.assertEqual(sorted(document["page_texts"]), [1])


class PyMuPDFFallbackTest(unittest.TestCase):
    def test_pymupdf_word_helper_returns_coords_on_synthetic_pdf(self):
        fake_page = mock.Mock()
        fake_page.get_text.return_value = [(10, 20, 30, 30, "Voc", 0, 0, 0), (35, 20, 50, 30, "1.2", 0, 0, 1)]
        fake_doc = mock.MagicMock()
        fake_doc.__enter__.return_value = [fake_page]
        fake_doc.__len__.return_value = 1
        fake_fitz = mock.Mock()
        fake_fitz.open.return_value = fake_doc
        with mock.patch.dict("sys.modules", {"fitz": fake_fitz}):
            words = pdf_source._pymupdf_page_words(b"%PDF-1.4 fake", 1)
        self.assertEqual(len(words), 2)
        self.assertTrue(all(set(word) >= {"x0", "top", "x1", "bottom", "text"} for word in words))
        self.assertEqual(" ".join(word["text"] for word in words), "Voc 1.2")

    def test_line_sources_from_pymupdf_words_are_structured(self):
        words = [
            {"x0": 10, "top": 10, "x1": 30, "bottom": 20, "text": "Table", "_block": 1, "_line": 1},
            {"x0": 35, "top": 10, "x1": 45, "bottom": 20, "text": "1.", "_block": 1, "_line": 1},
            {"x0": 10, "top": 30, "x1": 30, "bottom": 40, "text": "PCE", "_block": 1, "_line": 2},
            {"x0": 35, "top": 30, "x1": 55, "bottom": 40, "text": "18%", "_block": 1, "_line": 2},
        ]
        sources = pdf_source._pymupdf_line_sources(words, 3)
        self.assertEqual([s["kind"] for s in sources], ["caption", "text"])
        self.assertEqual(sources[0]["page"], 3)
        self.assertEqual(sources[0]["quote"], "Table 1.")
        self.assertEqual(len(sources[0]["bbox"]), 4)

    def test_extract_falls_back_when_pdfplumber_words_empty(self):
        data = ruled_pdf([600, 570, 540], texts=[(500, "Voc 1.2 V")])
        original = pdfplumber.page.Page.extract_words
        original_fallback = pdf_source._pymupdf_page_words

        def empty_words(self, *args, **kwargs):
            return []

        try:
            pdfplumber.page.Page.extract_words = empty_words
            pdf_source._pymupdf_page_words = lambda *args, **kwargs: [
                {"x0": 110, "top": 282, "x1": 160, "bottom": 294,
                 "text": "Voc", "_block": 1, "_line": 1},
                {"x0": 165, "top": 282, "x1": 190, "bottom": 294,
                 "text": "1.2", "_block": 1, "_line": 1},
                {"x0": 195, "top": 282, "x1": 205, "bottom": 294,
                 "text": "V", "_block": 1, "_line": 1},
            ]
            document = pdf_source.extract(data)
        finally:
            pdfplumber.page.Page.extract_words = original
            pdf_source._pymupdf_page_words = original_fallback
        self.assertFalse(any("текстовый слой пуст" in warning for warning in document["warnings"]))
        self.assertTrue(any("Voc" in source.get("quote", "") for source in document["sources"]))
        self.assertEqual(document["fallback_pages"], [1])

    def test_extract_warns_when_both_extractors_fail(self):
        data = ruled_pdf([600, 570, 540], texts=[(500, "Voc 1.2 V")])
        original_words = pdfplumber.page.Page.extract_words
        original_fallback = pdf_source._pymupdf_page_words
        try:
            pdfplumber.page.Page.extract_words = lambda self, *a, **k: []
            pdf_source._pymupdf_page_words = lambda *a, **k: []
            document = pdf_source.extract(data)
        finally:
            pdfplumber.page.Page.extract_words = original_words
            pdf_source._pymupdf_page_words = original_fallback
        self.assertTrue(any("текстовый слой пуст" in warning for warning in document["warnings"]))
        self.assertEqual(document["fallback_pages"], [])


class RenderTest(unittest.TestCase):
    def test_page_number_outside_the_document_is_an_error_not_the_last_page(self):
        """pages[-1] на нуле молча подсвечивает последнюю страницу."""
        data = ruled_pdf([600, 570, 540])
        with self.assertRaises(ValueError):
            pdf_source.render(data, 0, (0, 0, 10, 10))
        with self.assertRaises(ValueError):
            pdf_source.render(data, 2, (0, 0, 10, 10))
        self.assertTrue(pdf_source.render(data, 1, (0, 0, 10, 10)).startswith(b"\x89PNG"))


if __name__ == "__main__":
    unittest.main()
