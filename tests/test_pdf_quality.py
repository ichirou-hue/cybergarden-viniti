"""The PDF readiness score is a transparent weighted heuristic."""

from unittest import TestCase

from viniti.pdf_quality import WEIGHTS, assess


def text_sources(pages, chars=1500, kind="text"):
    return [
        {"source_id": page, "page": page, "kind": kind,
         "fragment": ("word " * (chars // 5))[:chars]}
        for page in range(1, pages + 1)
    ]


class PdfQualityTest(TestCase):
    def test_good_parse_scores_all_components(self):
        envelope = {
            "pages": 10,
            "status": "ok",
            "text_sources": text_sources(10) + text_sources(1, 400, "table"),
            "results": [{"metric_name": "PCE"}],
            "issues": [],
            "warnings": [],
            "ocr_used": False,
            "scanned": False,
        }
        result = assess(envelope)
        self.assertEqual(result["status"], "ok")
        self.assertIsInstance(result["score"], int)
        self.assertEqual(set(item["key"] for item in result["components"]), set(WEIGHTS))
        self.assertAlmostEqual(sum(item["points"] for item in result["components"]),
                               result["score"], delta=1.0)
        self.assertEqual(result["inputs"]["empty_pages"], 0)

    def test_empty_ocr_document_is_insufficient(self):
        envelope = {
            "pages": 5,
            "status": "ok",
            "text_sources": [],
            "results": [],
            "issues": [],
            "warnings": [],
            "ocr_used": True,
            "scanned": True,
        }
        result = assess(envelope)
        self.assertEqual(result["status"], "insufficient")
        self.assertIsNone(result["score"])
        self.assertTrue(any("OCR" in line for line in result["limits"]))
        ocr = next(item for item in result["components"] if item["key"] == "ocr_dependency")
        self.assertLess(ocr["value"], 0.5)

    def test_missing_metadata_does_not_receive_default_points(self):
        result = assess({"pages": 2, "text_sources": text_sources(2)})
        self.assertEqual(result["status"], "insufficient")
        self.assertIsNone(result["score"])
        self.assertIsNone(result["inputs"]["ocr_used"])
        self.assertIsNone(result["inputs"]["warnings"])

    def test_done_and_ok_mean_success_without_docling_ocr_penalty(self):
        envelope = {"pages": 2, "status": "done", "text_sources": text_sources(2),
                    "warnings": [], "ocr_used": False, "recognized_sources": text_sources(2)}
        done = assess(envelope)
        envelope["status"] = "ok"
        ok = assess(envelope)
        self.assertEqual(done["status"], "ok")
        self.assertEqual(done["score"], ok["score"])
        self.assertEqual(done["inputs"]["usable_sources"], 2)
        self.assertEqual(next(c for c in done["components"] if c["key"] == "ocr_dependency")["value"], 1.0)

    def test_anchors_supply_solar_tables_and_empty_quotes_are_not_text(self):
        table = {"page": 1, "kind": "table", "fragment": "Measured signal 24 units"}
        envelope = {"pages": 2, "status": "done", "ocr_used": False, "warnings": [],
                    "text_sources": [{"page": 2, "fragment": "   "}],
                    "anchors": {"1": table}, "results": [{"source": table}]}
        result = assess(envelope)
        self.assertEqual(result["inputs"]["table_sources"], 1)
        self.assertEqual(result["inputs"]["pages_with_text"], 1)
        self.assertEqual(result["inputs"]["usable_sources"], 1)

    def test_partial_parse_never_receives_final_percentage(self):
        result = assess({"pages": 2, "status": "partial", "text_sources": text_sources(2),
                         "ocr_used": False, "warnings": []})
        self.assertEqual(result["status"], "insufficient")
        self.assertIsNone(result["score"])

    def test_missing_pages_is_insufficient_not_zero(self):
        result = assess({"status": "ok", "text_sources": []})
        self.assertEqual(result["status"], "insufficient")
        self.assertIsNone(result["score"])
        self.assertEqual(result["components"], [])

    def test_failed_parse_is_insufficient(self):
        result = assess({"pages": 3, "status": "error", "text_sources": []})
        self.assertEqual(result["status"], "insufficient")
        self.assertIsNone(result["score"])

    def test_limits_never_claim_probability(self):
        result = assess({"pages": 2, "status": "ok", "text_sources": text_sources(2),
                         "warnings": [], "issues": [], "results": []})
        joined = " ".join(result["limits"]).lower()
        self.assertIn("не вероятность", joined)
        self.assertIn("научной достоверности", joined)
