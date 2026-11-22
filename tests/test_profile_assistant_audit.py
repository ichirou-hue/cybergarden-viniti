"""Profile assistant: duplicate/gap audit and read-only built-in profiles."""

import os
from unittest import TestCase, mock

from viniti.profile_assistant import audit_profile, suggest


class AuditTest(TestCase):
    def test_types_schema_reports_duplicates_without_requiring_types(self):
        audit = audit_profile({"types": [
            {"name": "Device", "fields": [{"name": "PCE"}, {"name": "PCE"}]},
            {"name": "device", "fields": []},
        ]})
        kinds = {item["kind"] for item in audit["duplicates"]}
        self.assertIn("duplicate_entity", kinds)
        self.assertIn("duplicate_field", kinds)
        self.assertIn("empty_entity_fields", {item["kind"] for item in audit["gaps"]})
        self.assertNotIn("missing_type", {item["kind"] for item in audit["gaps"]})

    def test_tables_schema_reports_missing_unit_and_description(self):
        audit = audit_profile({"tables": {"devices": {
            "key": "device_id",
            "fields": {
                "device_id": {"type": "id", "description": "identifier"},
                "pce": {"type": "number", "description": ""},
            },
        }}})
        kinds = {item["kind"] for item in audit["gaps"]}
        self.assertIn("missing_unit", kinds)
        self.assertIn("missing_description", kinds)
        self.assertNotIn("unknown_type", kinds)


class SuggestTest(TestCase):
    def test_audit_mode_needs_no_model_and_marks_builtin_copy(self):
        called = []

        def fake(messages, **options):
            called.append(True)
            return {"suggestions": []}

        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "", "OPENROUTER_MODEL": ""}):
            result = suggest({"question": "Проверь профиль", "mode": "audit",
                              "profile": {"types": [{"name": "Device", "fields": []}]}},
                             complete_fn=fake, profile_source="builtin")
        self.assertEqual(called, [])
        self.assertTrue(result["requires_copy"])
        self.assertEqual(result["profile_source"], "builtin")
        self.assertTrue(result["audit"]["gaps"])

    def test_create_mode_does_not_keep_unconfirmed_values(self):
        response = {"suggestions": [{
            "entity": "Device", "field": "PCE", "type": "number", "action": "add",
            "value": "21.5", "page": 2, "quote": "PCE reached 21.5",
            "explanation": "Основной показатель эффективности.",
        }]}

        def fake(messages, **options):
            return response

        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "test", "OPENROUTER_MODEL": "test"}):
            result = suggest({"question": "Создай профиль", "mode": "create",
                              "profile": {"types": []}, "sources": []}, complete_fn=fake)
        item = result["suggestions"][0]
        self.assertEqual(item["value"], "")
        self.assertEqual(item["quote"], "")
        self.assertIsNone(item["page"])
        self.assertIn("эффективности", item["explanation"])

    def test_existing_field_is_flagged_as_duplicate(self):
        response = {"suggestions": [{
            "entity": "Device", "field": "PCE", "type": "number", "action": "add",
            "value": "", "page": None, "quote": "", "explanation": "",
        }]}

        def fake(messages, **options):
            return response

        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "test", "OPENROUTER_MODEL": "test"}):
            result = suggest({"question": "Добавь поле", "mode": "suggest",
                              "profile": {"types": [{"name": "Device", "fields": [{"name": "PCE"}]}]},
                              "sources": []}, complete_fn=fake)
        self.assertTrue(result["suggestions"][0]["duplicate"])

    def test_user_profile_does_not_require_copy(self):
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "test", "OPENROUTER_MODEL": "test"}):
            result = suggest({"question": "Проверь", "mode": "audit",
                              "profile": {"types": []}}, profile_source="user")
        self.assertFalse(result["requires_copy"])
