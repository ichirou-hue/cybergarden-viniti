"""Tests for the model-independent LLM filler contract."""
import json
import unittest

from viniti.llm_contract import LLMContractError, LLMJsonFiller, validate_candidates


ITEM = {"id": 1, "page": 4, "kind": "text",
        "quote": "The champion reached a PCE of 18.42 %."}
WINDOW = {"page": 4, "items": [ITEM]}
SPEC = {"columns": ["metric_name", "metric_value", "metric_unit"]}


class LLMContractTest(unittest.TestCase):
    def test_accepts_json_envelope_and_preserves_text(self):
        response = json.dumps({"candidates": [{
            "metric_name": "PCE", "metric_value": "18.42", "metric_unit": "%",
            "source_page": 4, "source_fragment": ITEM["quote"]}]})
        rows = validate_candidates(response, WINDOW, SPEC)
        self.assertEqual(rows[0]["metric_value"], "18.42")

    def test_rejects_page_hallucination(self):
        with self.assertRaises(LLMContractError):
            validate_candidates([{"metric_value": "99", "source_page": 5,
                                  "source_fragment": ITEM["quote"]}], WINDOW, SPEC)

    def test_rejects_unknown_and_identity_fields(self):
        for field in ("confidence", "publication_id"):
            with self.subTest(field=field), self.assertRaises(LLMContractError):
                validate_candidates([{field: "x", "source_page": 4,
                                      "source_fragment": ITEM["quote"]}], WINDOW, SPEC)

    def test_rejects_quote_not_in_window(self):
        with self.assertRaises(LLMContractError):
            validate_candidates([{"metric_value": "99", "source_page": 4,
                                  "source_fragment": "PCE of 99 %"}], WINDOW, SPEC)

    def test_rejects_quote_assembled_from_two_sources(self):
        window = {"page": 4, "items": [
            {"page": 4, "kind": "text", "quote": "The champion reached a PCE"},
            {"page": 4, "kind": "text", "quote": "of 18.42 %."},
        ]}
        with self.assertRaises(LLMContractError):
            validate_candidates([{"metric_value": "18.42", "source_page": 4,
                                  "source_fragment": "PCE of 18.42 %"}], window, SPEC)

    def test_adapter_calls_backend_and_returns_candidates(self):
        filler = LLMJsonFiller(lambda window, spec: [{
            "metric_name": "PCE", "metric_value": "18.42", "source_page": 4,
            "source_fragment": ITEM["quote"]}])
        self.assertEqual(filler.fill(WINDOW, SPEC)[0]["metric_name"], "PCE")


if __name__ == "__main__":
    unittest.main()
