"""Contract for upload progress events emitted after completed processing stages."""
import json
from http.cookiejar import CookieJar
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from unittest import mock
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener

from viniti import auth
from viniti.server import Handler, ThreadingHTTPServer


class FakeStore:
    def ingest(self, name, data, *, progress):
        assert name == "paper.pdf"
        assert data.startswith(b"%PDF-")
        for completed, label in ((1, "read"), (2, "extract"), (3, "card")):
            progress(completed, label)
        return {"document_id": "solar:paper", "track": "solar"}


class UploadProgressTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        root = Path(self.tmp.name)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.auth = auth.Auth(root / "auth.sqlite3", iterations=auth.MIN_ITERATIONS)
        self.server.solar_store = FakeStore()
        self.server.parse_root = root / "parses"
        # Recognition runs in a background thread; the Docling subprocess is not
        # part of this test, only the job it would fill.
        runner = mock.patch.object(Handler, "run_parse", return_value=None)
        runner.start()
        self.addCleanup(runner.stop)
        self.thread = Thread(target=self.server.serve_forever)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.jar = CookieJar()
        self.client = build_opener(HTTPCookieProcessor(self.jar))
        request = Request(
            self.base + "/api/auth/register",
            data=json.dumps({"username": "streamuser", "password": "correct horse"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with self.client.open(request, timeout=20) as response:
            self.csrf = json.load(response)["csrf_token"]

    def tearDown(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        self.tmp.cleanup()

    def stream(self, csrf=True, client=None):
        headers = {"Content-Type": "application/pdf", "X-Track": "solar",
                   "X-Filename": "paper.pdf"}
        if csrf:
            headers["X-CSRF-Token"] = self.csrf
        request = Request(
            self.base + "/api/documents/stream",
            data=b"%PDF-1.4\n%%EOF", method="POST", headers=headers,
        )
        try:
            with (client or self.client).open(request, timeout=10) as response:
                return response.status, response.headers, response.read()
        except HTTPError as error:
            with error:
                return error.code, error.headers, error.read()

    def test_stream_reports_completed_stages_and_result(self):
        status, headers, body = self.stream()
        self.assertEqual(status, 200)
        self.assertEqual(headers.get_content_type(), "application/x-ndjson")
        events = [json.loads(line) for line in body.splitlines()]
        self.assertEqual([event["completed"] for event in events], [0, 1, 2, 3, 4])
        self.assertEqual(events[-1]["result"]["document_id"], "solar:paper")

    def get(self, path):
        try:
            with self.client.open(self.base + path, timeout=10) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            with error:
                return error.code, json.load(error)

    def test_stream_starts_catalog_recognition_after_claim(self):
        # The non-stream upload already starts recognition; the streamed one
        # must do the same and report the job id in the final result, otherwise
        # the wizard never learns that fragments are on the way.
        status, _, body = self.stream()
        self.assertEqual(status, 200)
        result = json.loads(body.splitlines()[-1])["result"]
        job_id = result.get("recognition_job_id")
        self.assertTrue(job_id, result)
        status, job = self.get("/api/parse/" + job_id)
        self.assertEqual(status, 200, job)
        self.assertEqual(job["document_id"], "solar:paper")
        self.assertEqual(job["status"], "queued")

    def test_repeated_stream_reuses_active_recognition_job(self):
        first = json.loads(self.stream()[2].splitlines()[-1])["result"]["recognition_job_id"]
        second = json.loads(self.stream()[2].splitlines()[-1])["result"]["recognition_job_id"]
        self.assertEqual(first, second)
        folders = [folder for folder in self.server.parse_root.iterdir() if folder.is_dir()]
        self.assertEqual(len(folders), 1, folders)

    def test_stream_requires_session_cookie(self):
        status, _, body = self.stream(client=build_opener())
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body)["error"], "Требуется вход.")

    def test_stream_requires_csrf_token(self):
        status, _, body = self.stream(csrf=False)
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body)["error"], "Неверный CSRF-токен.")
