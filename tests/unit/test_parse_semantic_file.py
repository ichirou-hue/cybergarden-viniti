"""GET /api/parse/<id>/file/llm/semantic.json is an owner-only artifact.

The semantic layer is written next to the fixed LLM artifacts. This test pins
the HTTP contract only: the owner can read it, another signed-in user gets the
same 404 as for an unknown file, and the whitelist does not open anything else
(unknown names or path traversal). It talks to a real ``ThreadingHTTPServer``
over loopback and prepares the bundle on disk instead of running a parser, so
the artifact route is exercised without a subprocess.
"""

from http.cookiejar import CookieJar
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from urllib.error import HTTPError
from urllib.request import (
    HTTPCookieProcessor,
    Request,
    build_opener,
)

from viniti import auth, llm_shell

from viniti.server import Handler, ThreadingHTTPServer


SEMANTIC_BODY = {"schema_version": "solar-semantic-1", "records": {"layers": [], "conditions": [], "metrics": []}}


def _register(base, username):
    client = build_opener(HTTPCookieProcessor(CookieJar()))
    request = Request(
        base + "/api/auth/register",
        data=json.dumps({"username": username, "password": "correct horse"}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with client.open(request, timeout=20) as response:
        body = json.load(response)
    return client, body["user"]


def _get(url, client):
    try:
        with client.open(Request(url), timeout=20) as response:
            return response.status, response.read()
    except HTTPError as error:
        with error:
            return error.code, error.read()


class SemanticArtifactRouteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.profiles_dir = self.root / "profiles"
        self.server.parse_root = self.root / "parses"
        self.server.auth = auth.Auth(self.root / "auth.sqlite3", iterations=auth.MIN_ITERATIONS)
        self.thread = Thread(target=self.server.serve_forever)
        self.thread.start()
        self.addCleanup(self._stop_server)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.owner, self.owner_user = _register(self.base, "semantic_owner")
        self.other, _ = _register(self.base, "semantic_other")
        self.job_id = "a" * 16
        self._write_job(self.owner_user["id"])

    def _stop_server(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()

    def _write_job(self, owner_id):
        folder = self.root / "parses" / self.job_id
        bundle = folder / "bundle" / "llm"
        bundle.mkdir(parents=True)
        (folder / "job.json").write_text(
            json.dumps({
                "id": self.job_id,
                "status": "ok",
                "owner_user_id": owner_id,
                "created_at": 1.0,
                "updated_at": 1.0,
                "summary": {},
            }, ensure_ascii=False),
            encoding="utf-8",
        )
        (bundle / "semantic.json").write_text(json.dumps(SEMANTIC_BODY), encoding="utf-8")

    def test_owner_reads_semantic_artifact(self):
        status, body = _get(f"{self.base}/api/parse/{self.job_id}/file/llm/semantic.json", self.owner)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), SEMANTIC_BODY)

    def test_other_user_gets_404_for_semantic_artifact(self):
        status, _ = _get(f"{self.base}/api/parse/{self.job_id}/file/llm/semantic.json", self.other)
        self.assertEqual(status, 404)

    def test_diagnostic_artifacts_are_owner_only(self):
        folder = self.root / "parses" / self.job_id / "bundle" / "llm"
        for name in (llm_shell.RESPONSE_ARTIFACT, llm_shell.DRAFT_ARTIFACT):
            with self.subTest(name=name):
                (folder / name).write_text('{"status":"test"}', encoding="utf-8")
                url = f"{self.base}/api/parse/{self.job_id}/file/llm/{name}"
                self.assertEqual(_get(url, self.owner)[0], 200)
                self.assertEqual(_get(url, self.other)[0], 404)

    def test_unknown_llm_artifact_is_not_served(self):
        status, _ = _get(f"{self.base}/api/parse/{self.job_id}/file/llm/secret.json", self.owner)
        self.assertEqual(status, 404)

    def test_path_traversal_is_refused(self):
        for suffix in (
            "file/llm/../../job.json",
            "file/llm/../semantic.json",
            "file/assets/../../job.json",
        ):
            with self.subTest(suffix=suffix):
                status, _ = _get(f"{self.base}/api/parse/{self.job_id}/{suffix}", self.owner)
                self.assertIn(status, {400, 404})

    def test_semantic_artifact_stays_owner_scoped_after_traversal_attempt(self):
        # A traversal attempt from the other user must not leak the job either.
        status, _ = _get(f"{self.base}/api/parse/{self.job_id}/file/llm/../../job.json", self.other)
        self.assertIn(status, {400, 404})


if __name__ == "__main__":
    unittest.main()
