import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from reportlab.pdfgen import canvas

from tools.parse_audit import validate_artifact, write_artifacts


class ParseAuditTests(unittest.TestCase):
    def pdf(self, directory):
        path = Path(directory) / "sample.pdf"
        pdf = canvas.Canvas(str(path))
        pdf.drawString(72, 740, "A reproducible parsing audit document.")
        pdf.drawString(72, 720, "PCE = 20.5 % and Voc = 0.70 V")
        pdf.showPage()
        pdf.drawString(72, 740, "Second page source.")
        pdf.save()
        return path

    def test_artifacts_deterministic_and_validate(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdf = self.pdf(tmp)
            first, second = Path(tmp) / "one", Path(tmp) / "two"
            write_artifacts(pdf, first, budget_chars=100, overlap=1)
            write_artifacts(pdf, second, budget_chars=100, overlap=1)
            self.assertTrue(validate_artifact(first))
            for name in ("document.json", "sources.jsonl", "windows.jsonl", "summary.json"):
                self.assertEqual((first / name).read_bytes(), (second / name).read_bytes())

    def test_malformed_link_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdf = self.pdf(tmp)
            out = Path(tmp) / "artifacts"
            write_artifacts(pdf, out)
            window_path = out / "windows.jsonl"
            rows = [json.loads(line) for line in window_path.read_text(encoding="utf-8").splitlines()]
            rows[0]["source_ids"] = [999999]
            window_path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                validate_artifact(out)

    def test_summary_malformed_row_and_tampered_items_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdf = self.pdf(tmp)
            out = Path(tmp) / "artifacts"
            write_artifacts(pdf, out, budget_chars=100)

            summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
            summary["window_hashes"][0] = "tampered"
            (out / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
            with self.assertRaises(ValueError):
                validate_artifact(out)

            write_artifacts(pdf, out, budget_chars=100)
            rows = [json.loads(line) for line in (out / "windows.jsonl").read_text(encoding="utf-8").splitlines()]
            rows[0]["items"][0]["quote"] = "tampered"
            (out / "windows.jsonl").write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                validate_artifact(out)

            write_artifacts(pdf, out, budget_chars=100)
            (out / "windows.jsonl").write_text("[]\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                validate_artifact(out)

    def test_root_script_entrypoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdf = self.pdf(tmp)
            out = Path(tmp) / "cli-artifacts"
            script = Path(__file__).resolve().parent / "tools" / "parse_audit.py"
            result = subprocess.run(
                [sys.executable, "-X", "utf8", str(script), str(pdf), "--out", str(out)],
                cwd=str(Path(__file__).resolve().parent), capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(validate_artifact(out))

    def test_tampered_quality_and_cli_validation_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdf = self.pdf(tmp)
            out = Path(tmp) / "artifacts"
            write_artifacts(pdf, out)
            document = json.loads((out / "document.json").read_text(encoding="utf-8"))
            document["page_quality"][0]["source_count"] += 1
            (out / "document.json").write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaises(ValueError):
                validate_artifact(out)
            result = subprocess.run(
                [sys.executable, "-X", "utf8", "tools/parse_audit.py", "--validate", str(out)],
                cwd=str(Path(__file__).resolve().parent), capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("error:", result.stderr)

    def test_module_entrypoint_validation_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdf = self.pdf(tmp)
            out = Path(tmp) / "artifacts"
            write_artifacts(pdf, out)
            result = subprocess.run(
                [sys.executable, "-X", "utf8", "-m", "tools.parse_audit", "--validate", str(out)],
                cwd=str(Path(__file__).resolve().parent), capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("valid", result.stdout)


if __name__ == "__main__":
    unittest.main()
