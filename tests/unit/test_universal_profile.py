"""Universal profile is discoverable and keeps evidence constraints."""
import unittest
from tempfile import TemporaryDirectory

from viniti import profiles_store
from viniti.extraction_core import load_profile, validate_fields


class UniversalProfileTests(unittest.TestCase):
    def test_builtin_available_without_user_files_and_cannot_be_overwritten(self):
        with TemporaryDirectory() as root:
            profile, source, _ = profiles_store.load("universal", root=root)
            self.assertEqual(source, "builtin")
            self.assertEqual(profile, load_profile("universal"))
            self.assertIn("universal", [p["id"] for p in profiles_store.list_profiles(root=root)])
            with self.assertRaises(ValueError):
                profiles_store.save("universal", profile, root=root)
            with self.assertRaises(ValueError):
                profiles_store.delete("universal", root=root)

    def test_interval_preserved_with_its_evidence_without_scalar_conversion(self):
        profile = load_profile("universal")
        quote = "The mass is 62 +/- 4 solar masses in this model."
        document = {"sources": [{"id": 1, "page": 1, "quote": quote, "kind": "text"}]}
        candidate = {"studies": [], "findings": [], "results": [{
            "result_id": "R1", "metric": "mass", "value": "62 +/- 4", "unit": "solar masses",
            "field_sources": {name: {"source_id": 1, "quote": quote}
                              for name in ("metric", "value", "unit")},
        }]}
        records, tables, issues = validate_fields(candidate, document, profile)
        self.assertEqual(tables["results"][0]["value"], "62 +/- 4")
        self.assertEqual(tables["results"][0]["numeric_value"], "")
        self.assertEqual(records[0]["fields"]["value"]["evidence_refs"][0]["page"], 1)
        self.assertEqual(issues, [])
        candidate["results"][0]["value"] = "100"
        with self.assertRaises(ValueError):
            validate_fields(candidate, document, profile)

    def test_scalar_with_unknown_unit_is_not_silently_normalized(self):
        profile = load_profile("universal")
        quote = "The mass is 62 solar masses."
        document = {"sources": [{"id": 1, "page": 1, "quote": quote, "kind": "text"}]}
        candidate = {"studies": [], "findings": [], "results": [{
            "result_id": "R1", "metric": "mass", "value": "62", "numeric_value": "62",
            "field_sources": {
                "metric": {"source_id": 1, "quote": quote},
                "value": {"source_id": 1, "quote": quote},
                "numeric_value": {"source_id": 1, "quote": quote, "raw_unit": "solar masses"},
            },
        }]}
        records, tables, issues = validate_fields(candidate, document, profile)
        self.assertEqual(tables["results"][0]["numeric_value"], "")
        self.assertEqual(records[0]["fields"]["numeric_value"]["status"], "requires_review")
        self.assertTrue(any(issue["rule"] == "normalization" for issue in issues))


class TextLiteralBoundaryTests(unittest.TestCase):
    """Text evidence keeps whole phrases while allowing sentence punctuation."""

    def _document(self, quote):
        return {"sources": [{"id": 1, "page": 1, "quote": quote, "kind": "text"}]}

    def _finding_candidate(self, claim, quote):
        return {"studies": [], "results": [], "findings": [{
            "finding_id": "F1", "claim": claim,
            "field_sources": {"claim": {"source_id": 1, "quote": quote}},
        }]}

    def _result_candidate(self, quote, value, numeric_value=None):
        provenance = {
            "metric": {"source_id": 1, "quote": quote},
            "value": {"source_id": 1, "quote": quote},
        }
        row = {"result_id": "R1", "metric": "event", "value": value,
               "field_sources": provenance}
        if numeric_value is not None:
            row["numeric_value"] = numeric_value
            provenance["numeric_value"] = {"source_id": 1, "quote": quote}
        return {"studies": [], "findings": [], "results": [row]}

    def test_text_value_before_sentence_punctuation_is_literal(self):
        profile = load_profile("universal")
        quote = "We observed GW150914. The event has 65 solar masses, according to the model."
        document = self._document(quote)
        for claim in ("GW150914", "65 solar masses", "model"):
            with self.subTest(claim=claim):
                records, tables, issues = validate_fields(
                    self._finding_candidate(claim, quote), document, profile)
                self.assertEqual(tables["findings"][0]["claim"], claim)
                self.assertEqual(issues, [])

    def test_text_value_keeps_compact_whitespace_normalization(self):
        profile = load_profile("universal")
        quote = "The event has 65  solar\nmasses\t."
        records, tables, issues = validate_fields(
            self._finding_candidate("65 solar masses", quote), self._document(quote), profile)
        self.assertEqual(tables["findings"][0]["claim"], "65 solar masses")
        self.assertEqual(issues, [])

    def test_text_value_inside_a_longer_word_is_rejected(self):
        profile = load_profile("universal")
        quote = "The extrasolar planet has masses and the event GW1509140 is a different name."
        document = self._document(quote)
        for claim in ("solar", "mass", "GW150914"):
            with self.subTest(claim=claim):
                with self.assertRaises(ValueError):
                    validate_fields(self._finding_candidate(claim, quote), document, profile)

    def test_text_value_must_be_printed_whole(self):
        profile = load_profile("universal")
        quote = "The remnant has 65 solar mass in this model."
        with self.assertRaises(ValueError):
            validate_fields(self._finding_candidate("65 solar masses", quote),
                            self._document(quote), profile)

    def test_fabricated_quote_is_rejected_before_literal_match(self):
        profile = load_profile("universal")
        document = self._document("We observed GW150914.")
        with self.assertRaises(ValueError):
            validate_fields(self._finding_candidate("GW150915", "We observed GW150915."),
                            document, profile)

    def test_text_field_with_bare_number_keeps_scalar_matching(self):
        profile = load_profile("universal")
        quote = "The event has 165 solar masses."
        with self.assertRaisesRegex(ValueError, "полным токеном"):
            validate_fields(self._result_candidate(quote, "65"),
                            self._document(quote), profile)

    def test_numeric_value_keeps_strict_scalar_matching(self):
        profile = load_profile("universal")
        quote = "The event has 165 solar masses."
        document = self._document(quote)
        records, tables, issues = validate_fields(
            self._result_candidate(quote, "165 solar masses", "165"), document, profile)
        self.assertEqual(tables["results"][0]["numeric_value"], "165")
        with self.assertRaisesRegex(ValueError, "полным токеном"):
            validate_fields(self._result_candidate(quote, "165 solar masses", "65"),
                            document, profile)
