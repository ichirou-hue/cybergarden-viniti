from io import BytesIO
import unittest

import pdfplumber
from reportlab.pdfgen import canvas

from native_math import attach_script_lines


class NativeMathTest(unittest.TestCase):
    def test_small_indices_at_multiple_font_sizes(self):
        for size in (5, 6, 7, 9, 12):
            with self.subTest(size=size):
                stream = BytesIO()
                pdf = canvas.Canvas(stream)
                pdf.setFont("Helvetica", size)
                pdf.drawString(60, 700, "J")
                pdf.setFont("Helvetica", .9 * size)
                pdf.drawString(60 + .5 * size, 700 - .2 * size, "SC")
                pdf.save()
                with pdfplumber.open(BytesIO(stream.getvalue())) as doc:
                    lines = attach_script_lines(doc.pages[0].extract_text_lines(return_chars=True))
                self.assertEqual(len(lines), 1)
                self.assertEqual(lines[0]["display_text"], "J_{SC}")

    def test_index_sharing_row_with_sidebar_moves_without_sidebar(self):
        stream = BytesIO()
        pdf = canvas.Canvas(stream)
        pdf.setFont("Helvetica", 9.96)
        pdf.drawString(270, 421.45, "SrCl")
        x = 270 + pdf.stringWidth("SrCl", "Helvetica", 9.96)
        pdf.setFont("Helvetica", 6.64)
        pdf.drawString(x, 419.81, "2")
        pdf.setFont("Helvetica", 6.97)
        pdf.drawString(120, 417.6, "Xingxing Gu,")
        pdf.save()
        with pdfplumber.open(BytesIO(stream.getvalue())) as doc:
            lines = attach_script_lines(doc.pages[0].extract_text_lines(return_chars=True))
        chemical = next(line for line in lines if "SrCl" in line["text"])
        sidebar = next(line for line in lines if "Xingxing" in line["text"])
        self.assertEqual(chemical["display_text"], "SrCl_{2}")
        self.assertNotIn("Xingxing", chemical["text"])
        self.assertNotIn("2", sidebar["text"])

    def test_nested_indices_do_not_drop_characters(self):
        def char(text, x0, x1, size, baseline):
            return {"text": text, "x0": x0, "x1": x1, "top": 800 - baseline - size,
                    "bottom": 800 - baseline, "size": size, "matrix": [1, 0, 0, 1, x0, baseline]}
        chars = [char("x", 60, 68, 12, 700), char("a", 68, 73, 7, 706), char("b", 73, 75, 4, 710)]
        line = {"text": "x a b", "chars": chars, "top": 85, "x0": 60}
        parsed = attach_script_lines([line])[0]
        self.assertEqual(parsed["text"], "xab")
        self.assertEqual(parsed["display_text"], "x^{a^{b}}")

    def test_reanalysis_clears_metadata_from_other_column(self):
        line = self.lines()[-1]
        line["display_text"] = "OTHER COLUMN"
        line["math_spans"] = [{"base": "wrong"}]
        parsed = attach_script_lines([line])[0]
        self.assertNotIn("display_text", parsed)
        self.assertNotIn("math_spans", parsed)

    def lines(self):
        buffer = BytesIO()
        pdf = canvas.Canvas(buffer)
        pdf.setFont("Helvetica", 12)
        pdf.drawString(60, 700, "J")
        pdf.setFont("Helvetica", 7)
        pdf.drawString(66, 694, "SC")
        pdf.setFont("Helvetica", 12)
        pdf.drawString(83, 700, "= 21.5 mA/cm")
        pdf.setFont("Helvetica", 7)
        pdf.drawString(162, 706, "2")
        pdf.setFont("Helvetica", 12)
        pdf.drawString(60, 675, "Next ordinary line")
        pdf.save()
        with pdfplumber.open(BytesIO(buffer.getvalue())) as document:
            return document.pages[0].extract_text_lines(return_chars=True)

    def test_rejoins_subscript_and_preserves_superscript(self):
        original = self.lines()
        lines = attach_script_lines(original)
        self.assertEqual(len(lines), 2)
        formula = lines[0]
        self.assertIn("JSC", formula["text"])
        self.assertIn("J_{SC}", formula["display_text"])
        self.assertIn("^{2}", formula["display_text"])
        self.assertEqual({span["position"] for span in formula["math_spans"]}, {"subscript", "superscript"})
        self.assertIn("SC", formula["original_lines"])
        self.assertEqual(lines[1]["text"], "Next ordinary line")
        self.assertEqual(len(original), 3)

    def test_full_size_lines_are_not_attached(self):
        original = self.lines()
        for line in original:
            for char in line["chars"]:
                char["size"] = 12
        self.assertEqual(len(attach_script_lines(original)), len(original))
