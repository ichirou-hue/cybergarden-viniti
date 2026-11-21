from io import BytesIO
import unittest

from reportlab.pdfgen import canvas

from vision_source import normalize_page, prepare_pdf


class VisionSourceTest(unittest.TestCase):
    def test_combined_pages_keep_native_text_and_latex(self):
        stream = BytesIO()
        pdf = canvas.Canvas(stream)
        pdf.drawString(40, 700, "Left column")
        pdf.drawString(350, 700, "Right column")
        pdf.showPage()
        pdf.showPage()  # no text layer: page-image reader must still run
        pdf.save()
        calls = []

        def client(messages):
            calls.append(messages)
            self.assertTrue(messages[1]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,"))
            return {"blocks": [{"kind": "formula", "text": r"$$E=\frac{hc}{\lambda}$$",
                                "bbox": [.1, .2, .8, .3], "uncertain": False}], "warnings": []}

        doc, assets = prepare_pdf("article.pdf", stream.getvalue(), client=client, model="test/vision")
        self.assertEqual(len(calls), 2)
        self.assertEqual([s["page"] for s in doc["sources"]], [1, 2])
        self.assertEqual([s["id"] for s in doc["sources"]], [1, 2])
        self.assertIn("Left column", calls[0][1]["content"][0]["text"])
        self.assertTrue(doc["parser_output"]["native_sources"])
        self.assertEqual(doc["sources"][0]["quote"], r"$$E=\frac{hc}{\lambda}$$")
        self.assertTrue(all(s["verification"] == "visual_unverified" for s in doc["sources"]))
        self.assertEqual(assets, {})

    def test_reject_invalid_coordinates_or_missing_transcription(self):
        for box in ([0, 0, 2, 1], [0, 0, float("nan"), 1], [0, 0, 0, 1]):
            with self.assertRaises(ValueError):
                normalize_page({"blocks": [{"kind": "formula", "text": "x", "bbox": box,
                                           "uncertain": False}], "warnings": []}, 1, 600, 800)
        sources, warnings = normalize_page({"blocks": [], "warnings": []}, 1, 600, 800)
        self.assertEqual(sources, [])
        self.assertTrue(warnings)
