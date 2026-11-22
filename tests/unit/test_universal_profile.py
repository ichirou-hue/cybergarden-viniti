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
