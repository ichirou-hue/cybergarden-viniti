import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from viniti.server import Handler


class CatalogDoclingJobTest(unittest.TestCase):
    def test_successful_parse_attaches_recognition_before_marking_job_ok(self):
        with TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / "bundle").mkdir()
            result = {"sources": [{"quote": "SrCl2", "display_text": "SrCl_{2}"}], "document": {"pages": 1}}
            (folder / "bundle" / "result.json").write_text(json.dumps(result))
            handler = Handler.__new__(Handler)
            store = Mock()
            handler.server = SimpleNamespace(solar_store=store)
            handler.write_job(folder, {"document_id": "solar:example", "filename": "D001.pdf", "status": "queued"})
            completed = SimpleNamespace(returncode=0, stdout='{"sources":1}', stderr="")
            with patch("viniti.server.subprocess.run", return_value=completed) as runner:
                handler.run_parse(folder, Path("profiles/solar.json"), "docling", "auto", False)
            store.attach_recognition.assert_called_once_with("solar:example", result)
            self.assertEqual(handler.read_job(folder)["status"], "ok")
            self.assertIn("docling", runner.call_args.args[0])

    def test_failed_parse_does_not_publish_recognition(self):
        with TemporaryDirectory() as temporary:
            folder = Path(temporary)
            handler = Handler.__new__(Handler)
            store = Mock()
            handler.server = SimpleNamespace(solar_store=store)
            handler.write_job(folder, {"document_id": "solar:example", "filename": "D001.pdf"})
            completed = SimpleNamespace(returncode=1, stdout="", stderr="parse: model unavailable")
            with patch("viniti.server.subprocess.run", return_value=completed):
                handler.run_parse(folder, Path("profiles/solar.json"), "docling", "auto", False)
            store.attach_recognition.assert_not_called()
            self.assertEqual(handler.read_job(folder)["status"], "error")
