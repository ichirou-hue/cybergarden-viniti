"""Тесты извлечения из PDF: устойчивость к мусору и пределы.

Run:  python -X utf8 -m unittest test_pdf_source -v
"""
from io import BytesIO
import unittest

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
    def test_page_texts_are_numbered_from_one(self):
        data = ruled_pdf([600, 570, 540])
        texts = pdf_source.page_texts(data)
        self.assertEqual(sorted(texts), [1])
        self.assertIn("Table 1", texts[1])

    def test_extract_returns_page_texts_with_the_sources(self):
        document = pdf_source.extract(ruled_pdf([600, 570, 540]))
        self.assertIn("page_texts", document)
        self.assertEqual(sorted(document["page_texts"]), [1])


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
