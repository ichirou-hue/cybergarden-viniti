"""Тесты ручного этапа: профили и разбор PDF без LLM."""

from io import BytesIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from time import sleep, time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from urllib.request import build_opener
from http.cookiejar import CookieJar
import auth

from reportlab.pdfgen import canvas

from server import Handler, ThreadingHTTPServer


def get(url, client=None):
    try:
        opener = client if client is not None else build_opener()
        request = Request(url)
        with opener.open(request, timeout=30) as response:
            return response.status, response.read()
    except HTTPError as error:
        with error:
            return error.code, error.read()


def send(url, data, headers, method="POST", client=None):
    request = Request(url, data=data, headers=headers, method=method)
    try:
        with (client or build_opener()).open(request, timeout=30) as response:
            return response.status, response.read()
    except HTTPError as error:
        with error:
            return error.code, error.read()


def tiny_pdf():
    buffer = BytesIO()
    drawing = canvas.Canvas(buffer)
    drawing.drawString(72, 720, "The champion device reached a PCE of 21.5 % under reverse scan.")
    drawing.drawString(72, 700, "The reference device reached a PCE of 18.0 %.")
    drawing.save()
    return buffer.getvalue()


def custom_profile():
    return {
        "id": "test_profile",
        "version": "1",
        "description": "Тестовый профиль оборудования.",
        "tables": {
            "equipment": {
                "key": "equipment_id",
                "fields": {
                    "equipment_id": {"type": "id", "description": "Локальный идентификатор."},
                    "material": {"type": "text", "description": "Материал образца.", "required": True},
                },
            },
        },
    }


def mock_profile():
    return {
        "id": "test_mock",
        "version": "1",
        "description": "Профиль для mock-оболочки LLM.",
        "tables": {
            "equipment": {
                "key": "equipment_id",
                "description": "Оборудование с явно указанным числом.",
                "fields": {
                    "equipment_id": {"type": "id", "description": "Локальный идентификатор."},
                    "pce": {"type": "number", "description": "Явно указанная эффективность."},
                },
            },
        },
    }


class ParseApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.profiles_dir = self.root / "profiles"
        self.server.parse_root = self.root / "parses"
        self.server.auth = auth.Auth(self.root / "auth.sqlite3", iterations=auth.MIN_ITERATIONS)
        self.thread = Thread(target=self.server.serve_forever)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.client = build_opener(__import__("urllib.request").request.HTTPCookieProcessor(CookieJar()))
        request = Request(self.base + "/api/auth/register", data=json.dumps({"username": "parseuser", "password": "correct horse"}).encode(), headers={"Content-Type": "application/json"})
        with self.client.open(request, timeout=20) as response:
            self.csrf = json.load(response)["csrf_token"]

    def tearDown(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        self.tmp.cleanup()

    def wait(self, run_id, timeout=60):
        deadline = time() + timeout
        while time() < deadline:
            status, body = get(f"{self.base}/api/parse/{run_id}", self.client)
            self.assertEqual(status, 200)
            job = json.loads(body)
            if job["status"] in {"ok", "error"}:
                return job
            sleep(0.05)
        self.fail("Разбор не завершился за отведённое время.")

    def get_auth(self, url):
        return get(url, self.client)

    def send_auth(self, url, data, headers, method="POST"):
        headers = dict(headers)
        headers["X-CSRF-Token"] = self.csrf
        return send(url, data, headers, method, self.client)

    # ------------------------------------------------------------ профили
    def test_builtin_profiles_are_listed(self):
        status, body = self.get_auth(self.base + "/api/profiles")
        self.assertEqual(status, 200)
        profiles = {item["id"]: item for item in json.loads(body)["profiles"]}
        self.assertEqual(profiles["solar"]["source"], "builtin")
        self.assertEqual(len(profiles["solar"]["tables"]), 7)
        self.assertIn("custom-example", profiles)
        self.assertEqual(profiles["custom-example"]["adapter"], "fields")

    def test_user_profile_round_trip(self):
        headers = {"Content-Type": "application/json"}
        status, body = self.send_auth(self.base + "/api/profiles/test_profile",
                            json.dumps(custom_profile()).encode(), headers, method="PUT")
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["id"], "test_profile")

        status, body = self.get_auth(self.base + "/api/profiles/test_profile")
        self.assertEqual(status, 200)
        document = json.loads(body)
        self.assertEqual(document["source"], "user")
        self.assertEqual(document["profile"]["tables"]["equipment"]["key"], "equipment_id")

        status, body = self.get_auth(self.base + "/api/profiles")
        ids = {item["id"] for item in json.loads(body)["profiles"]}
        self.assertIn("test_profile", ids)

        status, body = self.send_auth(self.base + "/api/profiles/test_profile", None, {}, method="DELETE")
        self.assertEqual(status, 200)
        status, body = self.get_auth(self.base + "/api/profiles/test_profile")
        self.assertEqual(status, 404)

    def test_builtin_profile_cannot_be_overwritten_or_deleted(self):
        headers = {"Content-Type": "application/json"}
        status, _ = self.send_auth(self.base + "/api/profiles/solar",
                         json.dumps(custom_profile()).encode(), headers, method="PUT")
        self.assertEqual(status, 409)
        status, _ = self.send_auth(self.base + "/api/profiles/solar", None, {}, method="DELETE")
        self.assertEqual(status, 409)

    def test_invalid_profile_is_rejected(self):
        headers = {"Content-Type": "application/json"}
        status, _ = self.send_auth(self.base + "/api/profiles/test_profile", b"not json", headers, method="PUT")
        self.assertEqual(status, 400)
        broken = custom_profile()
        del broken["tables"]["equipment"]["key"]
        status, _ = self.send_auth(self.base + "/api/profiles/test_profile",
                         json.dumps(broken).encode(), headers, method="PUT")
        self.assertEqual(status, 422)

    def test_profile_name_cannot_escape_the_store(self):
        profile = custom_profile()
        profile["id"] = "../../evil"
        status, _ = self.send_auth(self.base + "/api/profiles/..%2F..%2Fevil",
                         json.dumps(profile).encode(),
                         {"Content-Type": "application/json"}, method="PUT")
        self.assertIn(status, {404, 422})
        self.assertFalse((self.root / "evil.json").exists())

    # ------------------------------------------------------------ разбор
    def test_parse_returns_sources_and_candidates_without_llm(self):
        headers = {
            "Content-Type": "application/pdf", "X-Profile": "solar",
            "X-Parser": "pdfplumber", "X-Filename": "D001.pdf",
        }
        status, body = self.send_auth(self.base + "/api/parse", tiny_pdf(), headers)
        self.assertEqual(status, 202, body)
        job = json.loads(body)
        self.assertEqual(job["status"], "queued")

        finished = self.wait(job["id"])
        self.assertEqual(finished["status"], "ok", finished)

        status, body = self.get_auth(f"{self.base}/api/parse/{job['id']}/result")
        self.assertEqual(status, 200)
        result = json.loads(body)
        self.assertEqual(result["profile_id"], "solar")
        self.assertEqual(result["run"]["mode"], "scripts")
        self.assertEqual(result["run"]["parser"]["name"], "pdfplumber")
        self.assertEqual(result["document"]["publication_id"], "D001")
        self.assertTrue(result["sources"])
        self.assertIn("PCE", [candidate["field"] for candidate in result["candidates"]])

        status, body = self.get_auth(f"{self.base}/api/parse/{job['id']}/file/candidates.csv")
        self.assertEqual(status, 200)
        self.assertIn(b"PCE", body)

    def test_parse_job_list_is_safe_and_single_job_contract_is_unchanged(self):
        headers = {
            "Content-Type": "application/pdf", "X-Profile": "solar",
            "X-Parser": "pdfplumber", "X-Filename": "D001.pdf",
        }
        with patch.object(Handler, "run_parse", return_value=None):
            status, body = self.send_auth(self.base + "/api/parse", tiny_pdf(), headers)
        self.assertEqual(status, 202, body)
        job = json.loads(body)

        status, body = self.get_auth(self.base + "/api/parse?active=1")
        self.assertEqual(status, 200, body)
        listing = json.loads(body)
        self.assertEqual(listing["total"], 1, listing)
        item = listing["items"][0]
        self.assertEqual(item["id"], job["id"])
        self.assertEqual(item["status"], "queued")
        self.assertIs(item["active"], True)
        for forbidden in ("sha256", "owner_user_id", "bytes"):
            self.assertNotIn(forbidden, item)

        # Old clients still receive the full stored job from the single route.
        status, body = self.get_auth(f"{self.base}/api/parse/{job['id']}")
        self.assertEqual(status, 200, body)
        stored = json.loads(body)
        self.assertIn("sha256", stored)
        self.assertEqual(stored["owner_user_id"], job["owner_user_id"])
        self.assertEqual(stored["status"], "queued")

        # The job survives a fresh read, so returning to the page restores it.
        status, body = self.get_auth(f"{self.base}/api/parse/{job['id']}")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["created_at"], job["created_at"])

    def test_parse_rejects_unknown_profile(self):
        status, _ = self.send_auth(self.base + "/api/parse", tiny_pdf(), {
            "Content-Type": "application/pdf", "X-Profile": "nosuch"})
        self.assertEqual(status, 404)

    def test_failed_parse_hides_raw_subprocess_output_in_job_error(self):
        from subprocess import CompletedProcess
        sensitive = (
            "Traceback (most recent call last):\n"
            "  File \"C:\\\\private\\\\worker.py\", line 12\n"
            "openai.AuthenticationError: invalid api key sk-live-secret"
        )
        completed = CompletedProcess([], 1, stdout="", stderr=sensitive)
        with patch("server.subprocess.run", return_value=completed):
            status, body = self.send_auth(self.base + "/api/parse", tiny_pdf(), {
                "Content-Type": "application/pdf", "X-Profile": "solar",
                "X-Parser": "pdfplumber"})
            self.assertEqual(status, 202, body)
            job = self.wait(json.loads(body)["id"])
        self.assertEqual(job["status"], "error")
        self.assertEqual(job["error"],
                         "Ошибка внешнего провайдера LLM; подробности в логе сервера.")
        self.assertNotIn("Traceback", job["error"])
        self.assertNotIn("private", job["error"])
        self.assertNotIn("sk-live", job["error"])
        self.assertNotIn("openai", job["error"].lower())
        # Details stay in the server-side log, not in the API payload.
        log_path = self.root / "parses" / job["id"] / "parse.log"
        self.assertIn("sk-live-secret", log_path.read_text(encoding="utf-8"))

    def test_failed_parse_spawn_hides_local_path_in_job_error(self):
        error = FileNotFoundError(2, "No such file", str(Path("private") / "python.exe"))
        with patch("server.subprocess.run", side_effect=error):
            status, body = self.send_auth(self.base + "/api/parse", tiny_pdf(), {
                "Content-Type": "application/pdf", "X-Profile": "solar",
                "X-Parser": "pdfplumber"})
            self.assertEqual(status, 202, body)
            job = self.wait(json.loads(body)["id"])
        self.assertEqual(job["status"], "error")
        self.assertEqual(job["error"],
                         "Не удалось запустить разбор: не найден интерпретатор или скрипт.")
        self.assertNotIn("private", job["error"])

    def test_parse_rejects_charts_without_docling(self):
        status, _ = self.send_auth(self.base + "/api/parse", tiny_pdf(), {
            "Content-Type": "application/pdf", "X-Profile": "solar",
            "X-Parser": "pdfplumber", "X-Charts": "1"})
        self.assertEqual(status, 400)

    def test_parse_file_route_refuses_traversal(self):
        status, body = self.send_auth(self.base + "/api/parse", tiny_pdf(), {
            "Content-Type": "application/pdf", "X-Profile": "solar", "X-Parser": "pdfplumber"})
        job = json.loads(body)
        self.wait(job["id"])
        status, _ = self.get_auth(f"{self.base}/api/parse/{job['id']}/file/assets/../../job.json")
        self.assertIn(status, {400, 404})

    def test_default_parse_selects_local_docling_without_api_credentials(self):
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "", "OPENROUTER_VISION_MODEL": ""}), patch.object(Handler, "run_parse"):
            status, body = self.send_auth(self.base + "/api/parse", tiny_pdf(), {
                "Content-Type": "application/pdf", "X-Profile": "solar"})
        self.assertEqual(status, 202)
        self.assertEqual(json.loads(body)["parser"], "docling")

    def test_parse_rejects_unknown_llm_mode(self):
        status, _ = self.send_auth(self.base + "/api/parse", tiny_pdf(), {
            "Content-Type": "application/pdf", "X-Profile": "solar", "X-LLM": "remote"})
        self.assertEqual(status, 400)

    def test_parse_mock_shell_writes_bounded_artifacts(self):
        status, _ = self.send_auth(self.base + "/api/profiles/test_mock",
                         json.dumps(mock_profile()).encode(),
                         {"Content-Type": "application/json"}, method="PUT")
        self.assertEqual(status, 200)
        status, body = self.send_auth(self.base + "/api/parse", tiny_pdf(), {
            "Content-Type": "application/pdf", "X-Profile": "test_mock",
            "X-Parser": "pdfplumber", "X-Filename": "S001.pdf", "X-LLM": "mock",
        })
        self.assertEqual(status, 202, body)
        job = json.loads(body)
        self.assertEqual(job["llm"], "mock")

        finished = self.wait(job["id"])
        self.assertEqual(finished["status"], "ok", finished)
        self.assertEqual(finished["summary"]["llm"]["stages"]["interpret"], "ok")

        status, body = self.get_auth(f"{self.base}/api/parse/{job['id']}/llm")
        self.assertEqual(status, 200)
        found = json.loads(body)
        self.assertTrue(found["guarantees"]["passport_unchanged"])
        self.assertFalse(found["guarantees"]["official_csv_written"])

        status, body = self.get_auth(f"{self.base}/api/parse/{job['id']}/file/llm/interpretation.json")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["tables"]["equipment"][0]["pce"], "21.5")

        status, _ = self.get_auth(f"{self.base}/api/parse/{job['id']}/file/llm/../../job.json")
        self.assertIn(status, {400, 404})


if __name__ == "__main__":
    unittest.main()
