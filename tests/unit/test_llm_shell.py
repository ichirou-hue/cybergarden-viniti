"""Тесты офлайн-оболочки LLM: mock-бэкенд, артефакты и неизменность паспорта."""

from io import BytesIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from reportlab.pdfgen import canvas

from extraction_core import export_result, load_profile, run
from solar_facts import prepare_pdf
import llm_shell
import parse_document


PROFILE = {
    "id": "shell_test",
    "version": "1",
    "description": "Тестовый профиль оболочки.",
    "tables": {
        "sample": {
            "key": "sample_id",
            "description": "Один образец с явно указанным числом.",
            "fields": {
                "sample_id": {"type": "id", "description": "Локальный идентификатор."},
                "count": {"type": "number", "description": "Явно указанное число образцов."},
                "note": {"type": "text", "description": "Обязательное примечание.", "required": True},
            },
        },
    },
}

DOCUMENT = {
    "publication_id": "S001", "sha256": "deadbeef", "pages": 1, "warnings": [],
    "sources": [{"id": 1, "page": 1, "kind": "text", "quote": "count = 12 samples."}],
}


class BadBackend:
    """Возвращает заданный ответ на любую стадию; сеть не используется."""

    name = "bad-mock"
    network = False

    def __init__(self, response):
        self.response = response

    def complete(self, stage, payload):
        return self.response


class StagedBackend:
    """Разные ответы на interpret и audit для проверки отказа только аудита."""

    name = "staged-mock"
    network = False

    def __init__(self, interpretation, audit):
        self.interpretation = interpretation
        self.audit = audit

    def complete(self, stage, payload):
        return self.interpretation if stage == "interpret" else self.audit


def bundle(root, document=None, profile=None, name="bundle"):
    document = document if document is not None else DOCUMENT
    profile = profile if profile is not None else PROFILE
    result = run(document, profile, mode="scripts")
    result["profile"] = profile
    output = Path(root) / name
    export_result(result, output)
    return output


def manifest(bundle_path):
    return json.loads((bundle_path / "llm" / "manifest.json").read_text(encoding="utf-8"))


def stages(found):
    return {stage["name"]: stage["status"] for stage in found["stages"]}


def temperature_pdf():
    buffer = BytesIO()
    drawing = canvas.Canvas(buffer)
    drawing.drawString(72, 720, "Synthetic experimental single-junction perovskite solar cell.")
    drawing.drawString(72, 700, "Device A: PCE = 16 %, Voc = 1000 mV, Jsc = 20 mA/cm2, FF = 80 %.")
    drawing.drawString(72, 680, "Measured at AM1.5G 100 mW/cm2, reverse scan.")
    drawing.save()
    return buffer.getvalue()


class LlmShellTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_scripts_only_bundle_has_no_llm_artifacts(self):
        output = bundle(self.root)
        self.assertFalse((output / "llm").exists())
        self.assertTrue((output / "result.json").is_file())
        self.assertTrue((output / "candidates.csv").is_file())

    def test_mock_writes_artifacts_and_keeps_passport_and_csv(self):
        output = bundle(self.root)
        before = (output / "result.json").read_bytes()
        csv_before = (output / "candidates.csv").read_bytes()

        found = llm_shell.run_bundle(output)

        self.assertEqual((output / "result.json").read_bytes(), before)
        self.assertEqual((output / "candidates.csv").read_bytes(), csv_before)
        self.assertEqual(stages(found), {
            "sources": "ok", "interpret": "ok", "passport": "ok",
            "audit": "ok", "export": "skipped",
        })
        self.assertEqual(found["counts"]["interpreted_records"], 1)
        self.assertEqual(found["counts"]["audit_findings"], 1)
        self.assertTrue(found["guarantees"]["passport_unchanged"])
        self.assertFalse(found["guarantees"]["official_csv_written"])
        self.assertFalse(found["guarantees"]["network_used"])
        self.assertEqual(found["backend"]["network"], False)

        folder = output / "llm"
        self.assertEqual(set(found["artifacts"]), set(llm_shell.ARTIFACT_NAMES) - {"manifest.json"})
        self.assertFalse((folder / "results.csv").exists())
        for name in llm_shell.ARTIFACT_NAMES:
            self.assertTrue((folder / name).is_file(), name)
        self.assertFalse([item.name for item in folder.iterdir() if item.name.startswith(".")])

        interpretation = json.loads((folder / "interpretation.json").read_text(encoding="utf-8"))
        self.assertEqual(interpretation["tables"]["sample"][0]["count"], "12")
        self.assertEqual(interpretation["records"][0]["fields"]["count"]["raw"], "12")
        audit = json.loads((folder / "audit.json").read_text(encoding="utf-8"))
        self.assertEqual(audit["findings"][0]["rule"], "required")
        self.assertEqual(audit["findings"][0]["evidence"][0]["source_id"], 1)

    def test_malformed_interpretation_is_rejected_not_raised(self):
        output = bundle(self.root)
        found = llm_shell.run_bundle(output, backend=BadBackend("not json"))
        self.assertEqual(stages(found)["interpret"], "rejected")
        self.assertEqual(stages(found)["audit"], "skipped")
        interpretation = json.loads(
            (output / "llm" / "interpretation.json").read_text(encoding="utf-8"))
        self.assertEqual(interpretation["status"], "rejected")
        self.assertIn("JSON", interpretation["error"])

    def test_untrusted_quote_is_rejected(self):
        output = bundle(self.root)
        response = {"sample": [{
            "sample_id": "E1", "count": "12",
            "field_sources": {"count": {"source_id": 1, "quote": "count = 99 samples."}},
        }]}
        found = llm_shell.run_bundle(output, backend=BadBackend(response))
        self.assertEqual(stages(found)["interpret"], "rejected")
        self.assertFalse((output / "llm" / "audit.json").exists())

    def test_unknown_source_is_rejected(self):
        output = bundle(self.root)
        response = {"sample": [{
            "sample_id": "E1", "count": "12",
            "field_sources": {"count": {"source_id": 7, "quote": "count = 12 samples."}},
        }]}
        found = llm_shell.run_bundle(output, backend=BadBackend(response))
        self.assertEqual(stages(found)["interpret"], "rejected")

    def test_bad_audit_finding_rejects_only_audit(self):
        output = bundle(self.root)
        good = {"sample": [{
            "sample_id": "E1", "count": "12",
            "field_sources": {"count": {"source_id": 1, "quote": "count = 12 samples."}},
        }]}
        bad = {"findings": [{
            "table": "sample", "record_id": "E1", "field": "note",
            "rule": "required", "description": "Поле пусто.",
            "evidence": [{"source_id": 9, "page": 1}],
        }]}
        found = llm_shell.run_bundle(output, backend=StagedBackend(good, bad))
        self.assertEqual(stages(found)["interpret"], "ok")
        self.assertEqual(stages(found)["audit"], "rejected")
        self.assertEqual(found["status"], "rejected")

    def test_network_backend_is_refused(self):
        output = bundle(self.root)
        backend = BadBackend({})
        backend.network = True
        with self.assertRaises(ValueError):
            llm_shell.run_bundle(output, backend=backend)

    def test_input_budget_is_enforced_without_truncation(self):
        output = bundle(self.root)
        found = llm_shell.run_bundle(output, max_input_chars=10)
        self.assertEqual(stages(found)["interpret"], "rejected")
        interpretation = json.loads(
            (output / "llm" / "interpretation.json").read_text(encoding="utf-8"))
        self.assertIn("бюджет", interpretation["error"])

    def test_rerun_clears_stale_artifacts(self):
        output = bundle(self.root)
        llm_shell.run_bundle(output)
        self.assertTrue((output / "llm" / "audit.json").is_file())
        llm_shell.run_bundle(output, backend=BadBackend("not json"))
        self.assertFalse((output / "llm" / "audit.json").exists())
        self.assertTrue((output / "llm" / "interpretation.json").is_file())

    def test_solar_profile_records_request_and_skips_interpretation(self):
        data = temperature_pdf()
        document = prepare_pdf("D001.pdf", data)
        result = run(document, load_profile("solar"), mode="scripts")
        result["profile"] = load_profile("solar")
        output = self.root / "solar"
        export_result(result, output)

        found = llm_shell.run_bundle(output)
        self.assertEqual(stages(found)["interpret"], "skipped")
        self.assertEqual(stages(found)["audit"], "skipped")
        self.assertTrue((output / "llm" / "request.json").is_file())
        self.assertFalse((output / "llm" / "interpretation.json").exists())
        self.assertTrue(found["guarantees"]["passport_unchanged"])

    def test_bundle_must_be_a_scripts_passport(self):
        output = bundle(self.root)
        result = json.loads((output / "result.json").read_text(encoding="utf-8"))
        result["run"]["mode"] = "auto"
        (output / "result.json").write_text(json.dumps(result), encoding="utf-8")
        with self.assertRaises(ValueError):
            llm_shell.run_bundle(output)

    def test_missing_result_json_is_refused(self):
        empty = self.root / "empty"
        empty.mkdir()
        with self.assertRaises(ValueError):
            llm_shell.run_bundle(empty)


class ParseDocumentShellTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.pdf = self.root / "S001.pdf"
        self.pdf.write_bytes(temperature_pdf())

    def tearDown(self):
        self.tmp.cleanup()

    def test_cli_default_is_scripts_only(self):
        output = self.root / "off"
        code = parse_document.main([
            str(self.pdf), "--profile", "custom-example", "-o", str(output),
            "--parser", "pdfplumber"])
        self.assertEqual(code, 0)
        self.assertFalse((output / "llm").exists())

    def test_cli_mock_adds_llm_summary(self):
        output = self.root / "mock"
        code = parse_document.main([
            str(self.pdf), "--profile", "custom-example", "-o", str(output),
            "--parser", "pdfplumber", "--llm-shell", "mock"])
        self.assertEqual(code, 0)
        self.assertTrue((output / "llm" / "manifest.json").is_file())


if __name__ == "__main__":
    unittest.main()
