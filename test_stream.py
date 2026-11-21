"""Contract for upload progress events emitted after completed processing stages."""
import json
from threading import Thread
import unittest
from urllib.request import Request, urlopen

from server import Handler, ThreadingHTTPServer


class FakeStore:
    def ingest(self, name, data, *, progress):
        assert name == "paper.pdf"
        assert data.startswith(b"%PDF-")
        for completed, label in ((1, "read"), (2, "extract"), (3, "card")):
            progress(completed, label)
        return {"document_id": "solar:paper", "track": "solar"}


class UploadProgressTest(unittest.TestCase):
    def test_stream_reports_completed_stages_and_result(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.solar_store = FakeStore()
        thread = Thread(target=server.serve_forever)
        thread.start()
        try:
            request = Request(
                f"http://127.0.0.1:{server.server_port}/api/documents/stream",
                data=b"%PDF-1.4\n%%EOF", method="POST",
                headers={"Content-Type": "application/pdf", "X-Track": "solar",
                         "X-Filename": "paper.pdf"},
            )
            with urlopen(request, timeout=10) as response:
                self.assertEqual(response.headers.get_content_type(), "application/x-ndjson")
                events = [json.loads(line) for line in response]
            self.assertEqual([event["completed"] for event in events], [0, 1, 2, 3, 4])
            self.assertEqual(events[-1]["result"]["document_id"], "solar:paper")
        finally:
            server.shutdown()
            thread.join()
            server.server_close()
