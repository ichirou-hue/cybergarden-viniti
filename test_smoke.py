from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from urllib.request import Request, urlopen

from demo import demo_pdf
from library import Library
from server import Handler, ThreadingHTTPServer


ROOT = Path(__file__).resolve().parent


class SmokeTest(unittest.TestCase):
    def test_upload_export_and_evidence(self):
        pdf = demo_pdf()
        with TemporaryDirectory() as tmp, ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
            server.library = Library(Path(tmp) / "papers.sqlite3")
            thread = Thread(target=server.serve_forever)
            thread.start()
            url = f"http://127.0.0.1:{server.server_port}"
            upload = Request(url + "/api/papers", data=pdf, headers={
                "Content-Type": "application/pdf", "X-Filename": "demo.pdf",
            })
            try:
                with urlopen(upload, timeout=10) as response:
                    result = json.load(response)
                self.assertEqual(result["id"], sha256(pdf).hexdigest())
                self.assertEqual(len(result["facts"]), 8)
                flagged = [fact for fact in result["facts"] if fact["issues"]]
                self.assertEqual([(f["cutoff"], f["value"]) for f in flagged], [(5, .81), (10, .70)])
                self.assertEqual(flagged[0]["evidence"]["quote"], "81")
                base = url + "/api/papers/" + result["id"]
                with urlopen(base + "?download", timeout=10) as response:
                    self.assertEqual(json.load(response), result)
                with urlopen(f"{base}/facts/{flagged[0]['id']}.png", timeout=10) as response:
                    self.assertEqual(response.read(8), b"\x89PNG\r\n\x1a\n")
                with urlopen(upload, timeout=10) as response:
                    self.assertEqual(json.load(response), result)
                self.assertEqual(len(server.library.list()), 1)
            finally:
                server.shutdown()
                thread.join()

    def test_batch_reports_failure_and_keeps_valid_results(self):
        with TemporaryDirectory() as tmp:
            pdf, bad, output = [Path(tmp) / name for name in ("статья.pdf", "bad.pdf", "result.jsonl")]
            pdf.write_bytes(demo_pdf())
            bad.write_bytes(b"invalid")
            run = subprocess.run(
                [sys.executable, str(ROOT / "analyze_pdfs.py"), bad, pdf, "-o", output],
                capture_output=True, timeout=30,
            )
            self.assertEqual(run.returncode, 1)
            self.assertIn(b"bad.pdf", run.stderr)
            records = output.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(records), 1)
            self.assertEqual(json.loads(records[0])["name"], pdf.name)
