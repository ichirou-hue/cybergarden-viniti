"""Фокусные тесты api-режима: транспорт [OI]-клиента и явное включение сети."""

from io import BytesIO
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from time import sleep, time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookiejar import CookieJar
from urllib.request import HTTPCookieProcessor, build_opener

from extraction_core import export_result, load_profile, run
import auth
import llm_provider
import llm_shell
import parse_document
import server
from solar_schema import TABLES
from test_solar import fixture, synthetic_pdf


OPENAI_ENV = {
    "OPENAI_API_KEY": "sk-test-secret",
    "OPENAI_MODEL": "test/model",
    "OPENAI_BASE_URL": "https://example.invalid/v1",
}
NO_LLM_ENV = {key: value for key, value in os.environ.items()
              if not key.startswith(("OPENAI_", "OPENROUTER_"))}


def process_env(**overrides):
    """Environment for a *spawned* parse process.

    Stripping the whole environment would also remove ``SystemRoot`` and break
    Winsock in the child (``WinError 10106``), so only the LLM variables are
    replaced; the rest of the system environment is inherited as-is.
    """
    return {**NO_LLM_ENV, **overrides}


def envelope(content, finish="stop"):
    body = {"choices": [{"finish_reason": finish, "message": {"content": content}}]}
    return BytesIO(json.dumps(body).encode("utf-8"))


class ProviderConfigTest(unittest.TestCase):
    def test_default_endpoint_is_openai_chat_completions(self):
        config = llm_provider.config_from_env({
            "OPENAI_API_KEY": "k", "OPENAI_MODEL": "m"})
        self.assertEqual(config["endpoint"], "https://api.openai.com/v1/chat/completions")

    def test_base_url_override_and_full_endpoint(self):
        config = llm_provider.config_from_env({
            "OPENAI_API_KEY": "k", "OPENAI_MODEL": "m",
            "OPENAI_BASE_URL": "https://proxy.example/v1/",
        })
        self.assertEqual(config["endpoint"], "https://proxy.example/v1/chat/completions")
        config = llm_provider.config_from_env({
            "OPENAI_API_KEY": "k", "OPENAI_MODEL": "m",
            "OPENAI_BASE_URL": "https://proxy.example/custom/chat/completions",
        })
        self.assertEqual(config["endpoint"], "https://proxy.example/custom/chat/completions")

    def test_openrouter_fallback_keeps_working(self):
        config = llm_provider.config_from_env({
            "OPENROUTER_API_KEY": "k", "OPENROUTER_MODEL": "m"})
        self.assertEqual(config["endpoint"], "https://openrouter.ai/api/v1/chat/completions")
        self.assertEqual(config["source"], "openrouter")

    def test_missing_configuration_names_variables_not_values(self):
        with self.assertRaises(ValueError) as error:
            llm_provider.config_from_env({})
        self.assertIn("OPENAI_API_KEY", str(error.exception))
        self.assertIn("OPENAI_MODEL", str(error.exception))

    def test_bad_base_url_is_rejected(self):
        for base in ("not-a-url", "ftp://example/x", "/relative"):
            with self.subTest(base=base), self.assertRaises(ValueError):
                llm_provider.endpoint_for(base)

    def test_describe_never_contains_key(self):
        status = llm_provider.describe(OPENAI_ENV)
        self.assertTrue(status["configured"])
        self.assertNotIn("sk-test-secret", json.dumps(status))
        self.assertFalse(llm_provider.describe({})["configured"])

    def test_transport_options_default_when_env_is_missing(self):
        self.assertEqual(llm_provider.transport_options({}),
                         {"timeout": 90, "retries": 2})
        config = llm_provider.config_from_env({
            "OPENAI_API_KEY": "k", "OPENAI_MODEL": "m"})
        self.assertEqual((config["timeout"], config["retries"]), (90, 2))

    def test_transport_options_read_plain_integers(self):
        options = llm_provider.transport_options({
            "OPENAI_LLM_TIMEOUT": " 30 ", "OPENAI_LLM_RETRIES": "0"})
        self.assertEqual(options, {"timeout": 30, "retries": 0})
        config = llm_provider.config_from_env({
            "OPENAI_API_KEY": "k", "OPENAI_MODEL": "m",
            "OPENAI_LLM_TIMEOUT": "600", "OPENAI_LLM_RETRIES": "3",
        })
        self.assertEqual((config["timeout"], config["retries"]), (600, 3))

    def test_invalid_transport_options_keep_defaults(self):
        for value in ("", "abc", "90.0", "1e2", "90s", None, True, 90.0):
            with self.subTest(timeout=value):
                self.assertEqual(
                    llm_provider.transport_options({"OPENAI_LLM_TIMEOUT": value})["timeout"], 90)
        for value in ("", "many", "2.0", "-1", "4", "1e1", None, False, 2.0):
            with self.subTest(retries=value):
                self.assertEqual(
                    llm_provider.transport_options({"OPENAI_LLM_RETRIES": value})["retries"], 2)

    def test_out_of_range_transport_options_keep_defaults(self):
        for value in ("0", "601", "-5", "1000000"):
            with self.subTest(timeout=value):
                self.assertEqual(
                    llm_provider.transport_options({"OPENAI_LLM_TIMEOUT": value})["timeout"], 90)
        for value in ("-1", "4", "99"):
            with self.subTest(retries=value):
                self.assertEqual(
                    llm_provider.transport_options({"OPENAI_LLM_RETRIES": value})["retries"], 2)

    def test_dotenv_supplies_transport_options(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("OPENAI_LLM_TIMEOUT=45\nOPENAI_LLM_RETRIES=1\n", encoding="utf-8")
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("OPENAI_LLM_TIMEOUT", None)
                os.environ.pop("OPENAI_LLM_RETRIES", None)
                llm_provider._load_dotenv(path)
                self.assertEqual(llm_provider.transport_options(),
                                 {"timeout": 45, "retries": 1})


class ProviderTransportTest(unittest.TestCase):
    def setUp(self):
        self.config = llm_provider.config_from_env(OPENAI_ENV)
        self.messages = [{"role": "user", "content": "{}"}]

    def test_request_shape_and_parsed_reply(self):
        seen = {}

        def opener(request, timeout):
            seen["url"] = request.full_url
            seen["timeout"] = timeout
            seen["auth"] = request.get_header("Authorization")
            seen["body"] = json.loads(request.data)
            return envelope('{"ok": true}')

        self.assertEqual(
            llm_provider.chat(self.messages, self.config, opener=opener),
            {"ok": True})
        self.assertEqual(seen["url"], "https://example.invalid/v1/chat/completions")
        self.assertEqual(seen["timeout"], 90)
        self.assertEqual(seen["auth"], "Bearer sk-test-secret")
        self.assertEqual(seen["body"]["model"], "test/model")
        self.assertEqual(seen["body"]["temperature"], 0)
        self.assertEqual(seen["body"]["response_format"], {"type": "json_object"})

    def test_truncated_or_non_object_reply_is_rejected(self):
        for reply in (envelope("{}", finish="length"), envelope("not json"),
                      envelope("[]"), BytesIO(b"bad json")):
            with self.subTest(reply=reply), self.assertRaises(ValueError):
                llm_provider.chat(self.messages, self.config,
                                  opener=lambda *a, **k: reply)

    def test_auth_error_is_sanitized_and_not_retried(self):
        calls = []

        def opener(*args, **kwargs):
            calls.append(1)
            raise HTTPError("url", 401, "provider secret body", {}, BytesIO(b"secret"))

        with self.assertRaises(ValueError) as error:
            llm_provider.chat(self.messages, self.config, opener=opener)
        self.assertNotIn("sk-test-secret", str(error.exception))
        self.assertNotIn("provider secret body", str(error.exception))
        self.assertEqual(len(calls), 1)

    def test_transient_errors_retry_then_fail_bounded(self):
        calls, waits = [], []

        def opener(*args, **kwargs):
            calls.append(1)
            raise URLError("secret")

        with self.assertRaises(ValueError):
            llm_provider.chat(self.messages, self.config, retries=2,
                              opener=opener, sleep=waits.append)
        self.assertEqual(len(calls), 3)
        self.assertEqual(waits, [1, 2])

    def test_retry_then_success(self):
        calls = []

        def opener(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise HTTPError("url", 429, "limited", {}, BytesIO())
            return envelope('{"ok": 1}')

        self.assertEqual(
            llm_provider.chat(self.messages, self.config, opener=opener,
                              sleep=lambda _: None),
            {"ok": 1})
        self.assertEqual(len(calls), 2)

    def test_response_size_is_bounded(self):
        with patch.object(llm_provider, "MAX_RESPONSE_BYTES", 4), \
                self.assertRaisesRegex(ValueError, "размер"):
            llm_provider.chat(self.messages, self.config,
                              opener=lambda *a, **k: BytesIO(b"12345"))

    def test_empty_key_never_reaches_transport(self):
        config = dict(self.config, api_key="")
        with patch.object(llm_provider, "urlopen") as network, self.assertRaises(ValueError):
            llm_provider.chat(self.messages, config)
        network.assert_not_called()

    def test_redirect_is_refused(self):
        handler = llm_provider.NoRedirect()
        request = llm_provider.Request("https://example.invalid/v1/chat/completions")
        with self.assertRaises(HTTPError):
            handler.redirect_request(request, None, 302, "", {}, "https://evil.invalid")


class ApiBackendOptionsTest(unittest.TestCase):
    """The shell backend forwards env-backed timeout/retries into the transport."""

    def complete(self, backend):
        messages = [{"role": "user", "content": "{}"}]
        return backend.complete("interpret", {"messages": messages})

    def test_backend_uses_environment_options(self):
        seen = {}

        def fake_chat(messages, config, **options):
            seen.update(options)
            return {"ok": True}

        env = {**OPENAI_ENV, "OPENAI_LLM_TIMEOUT": "45", "OPENAI_LLM_RETRIES": "1"}
        with patch.dict(os.environ, env, clear=True), \
                patch.object(llm_provider, "chat", side_effect=fake_chat):
            self.assertEqual(self.complete(llm_shell.ApiBackend()), {"ok": True})
        self.assertEqual(seen, {"timeout": 45, "retries": 1})

    def test_backend_defaults_when_env_is_absent(self):
        seen = {}

        def fake_chat(messages, config, **options):
            seen.update(options)
            return {"ok": True}

        with patch.dict(os.environ, OPENAI_ENV, clear=True), \
                patch.object(llm_provider, "chat", side_effect=fake_chat):
            self.complete(llm_shell.ApiBackend())
        self.assertEqual(seen, {"timeout": 90, "retries": 2})

    def test_explicit_options_override_environment(self):
        seen = {}

        def fake_chat(messages, config, **options):
            seen.update(options)
            return {"ok": True}

        env = {**OPENAI_ENV, "OPENAI_LLM_TIMEOUT": "45", "OPENAI_LLM_RETRIES": "1"}
        with patch.dict(os.environ, env, clear=True), \
                patch.object(llm_provider, "chat", side_effect=fake_chat):
            self.complete(llm_shell.ApiBackend(timeout=5, retries=0))
        self.assertEqual(seen, {"timeout": 5, "retries": 0})

    def test_invalid_environment_options_fall_back_to_defaults(self):
        seen = {}

        def fake_chat(messages, config, **options):
            seen.update(options)
            return {"ok": True}

        env = {**OPENAI_ENV, "OPENAI_LLM_TIMEOUT": "soon", "OPENAI_LLM_RETRIES": "9"}
        with patch.dict(os.environ, env, clear=True), \
                patch.object(llm_provider, "chat", side_effect=fake_chat):
            self.complete(llm_shell.ApiBackend())
        self.assertEqual(seen, {"timeout": 90, "retries": 2})


class ShellApiOptInTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.bundle = self.make_bundle()

    def tearDown(self):
        self.tmp.cleanup()

    def make_bundle(self):
        document, candidate = fixture()
        result = run(document, load_profile("solar"), mode="scripts")
        result["profile"] = load_profile("solar")
        output = self.root / "bundle"
        export_result(result, output)
        return output

    def stages(self, manifest):
        return {stage["name"]: stage["status"] for stage in manifest["stages"]}

    def test_api_requires_explicit_allow_network(self):
        with patch.dict(os.environ, OPENAI_ENV, clear=True), self.assertRaises(ValueError):
            llm_shell.run_bundle(self.bundle, mode="api")

    def test_api_requires_environment_configuration(self):
        with patch.dict(os.environ, NO_LLM_ENV, clear=True), self.assertRaises(ValueError) as error:
            llm_shell.run_bundle(self.bundle, mode="api", allow_network=True)
        self.assertIn("OPENAI_API_KEY", str(error.exception))

    def test_unknown_mode_is_refused(self):
        with self.assertRaises(ValueError):
            llm_shell.run_bundle(self.bundle, mode="remote", allow_network=True)

    def test_api_solar_bundle_uses_real_validation(self):
        _, candidate = fixture()
        calls = []

        class StubBackend:
            name = "api"
            network = True
            model = "test/model"

            def complete(self, stage, payload):
                calls.append(stage)
                self.assert_messages = payload.get("messages")
                return candidate if stage == "interpret" else {"findings": []}

        backend = StubBackend()
        found = llm_shell.run_bundle(
            self.bundle, mode="api", allow_network=True, backend=backend)

        self.assertEqual(calls, ["interpret", "audit"])
        self.assertEqual(self.stages(found)["interpret"], "ok", found)
        self.assertEqual(self.stages(found)["audit"], "ok", found)
        self.assertEqual(found["backend"], {"name": "api", "network": True, "model": "test/model"})
        self.assertTrue(found["guarantees"]["network_used"])
        self.assertTrue(found["guarantees"]["passport_unchanged"])
        self.assertFalse(found["guarantees"]["official_csv_written"])
        self.assertEqual(found["counts"]["interpreted_records"], 9)

        interpretation = json.loads(
            (self.bundle / "llm" / "interpretation.json").read_text(encoding="utf-8"))
        self.assertEqual(interpretation["adapter"], "solar_csv")
        self.assertEqual(interpretation["tables"]["results"][0]["metric_name"], "PCE")

    def test_api_key_never_appears_in_artifacts(self):
        _, candidate = fixture()

        class StubBackend:
            name = "api"
            network = True
            model = "test/model"

            def complete(self, stage, payload):
                return candidate if stage == "interpret" else {"findings": []}

        llm_shell.run_bundle(self.bundle, mode="api", allow_network=True, backend=StubBackend())
        for path in (self.bundle / "llm").iterdir():
            self.assertNotIn("sk-test-secret", path.read_text(encoding="utf-8"), path.name)

    def test_mock_still_skips_solar_and_stays_offline(self):
        found = llm_shell.run_bundle(self.bundle)
        self.assertEqual(self.stages(found)["interpret"], "skipped")
        self.assertFalse(found["guarantees"]["network_used"])


class ShellApiEndToEndTest(unittest.TestCase):
    """Real transport through a local [OI]-compatible server, no external call."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.requests = []

        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0") or 0)
                body = json.loads(self.rfile.read(length))
                outer.requests.append({"path": self.path, "body": body,
                                       "auth": self.headers.get("Authorization")})
                if self.path.endswith("/chat/completions"):
                    system = body["messages"][0]["content"]
                    reply = {"findings": []} if system.startswith("Audit") else outer.reply
                    content = json.dumps(reply)
                    payload = json.dumps({"choices": [{
                        "finish_reason": "stop", "message": {"content": content}}]}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                self.send_response(404)
                self.end_headers()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = Thread(target=self.server.serve_forever)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}/v1"

        document, self.candidate = fixture()
        self.reply = self.candidate
        result = run(document, load_profile("solar"), mode="scripts")
        result["profile"] = load_profile("solar")
        self.bundle = self.root / "bundle"
        export_result(result, self.bundle)

    def tearDown(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        self.tmp.cleanup()

    def test_api_mode_calls_local_endpoint_and_validates(self):
        env = {"OPENAI_API_KEY": "sk-local-secret", "OPENAI_MODEL": "test/model",
               "OPENAI_BASE_URL": self.base}
        with patch.dict(os.environ, env, clear=True):
            found = llm_shell.run_bundle(
                self.bundle, mode="api", allow_network=True,
                max_response_bytes=4 * 1024 * 1024)

        stages = {stage["name"]: stage["status"] for stage in found["stages"]}
        self.assertEqual(stages["interpret"], "ok", found)
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.requests[0]["path"], "/v1/chat/completions")
        self.assertEqual(self.requests[0]["auth"], "Bearer sk-local-secret")
        self.assertEqual(self.requests[0]["body"]["model"], "test/model")
        self.assertEqual(found["backend"]["name"], "api")
        self.assertTrue(found["guarantees"]["network_used"])
        self.assertNotIn("sk-local-secret", json.dumps(found))
        self.assertTrue(found["guarantees"]["passport_unchanged"])


class ParseDocumentApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.pdf = self.root / "D001.pdf"
        self.pdf.write_bytes(synthetic_pdf())

    def tearDown(self):
        self.tmp.cleanup()

    def test_cli_api_without_environment_fails_before_export(self):
        output = self.root / "api"
        with patch.dict(os.environ, NO_LLM_ENV, clear=True):
            code = parse_document.main([
                str(self.pdf), "--profile", "solar", "-o", str(output),
                "--llm-shell", "api"])
        self.assertEqual(code, 1)
        self.assertFalse(output.exists())


class ServerApiOptInTest(unittest.TestCase):
    def status(self):
        return server.llm_api_status()

    def test_status_hides_key_and_requires_opt_in(self):
        with patch.dict(os.environ, OPENAI_ENV, clear=True), \
                patch.object(server, "PARSE_LLM_NETWORK", False):
            status = self.status()
        self.assertFalse(status["enabled"])
        self.assertFalse(status["available"])
        self.assertTrue(status["configured"])
        self.assertNotIn("sk-test-secret", json.dumps(status))

        with patch.dict(os.environ, OPENAI_ENV, clear=True), \
                patch.object(server, "PARSE_LLM_NETWORK", True):
            status = self.status()
        self.assertTrue(status["available"])

    def test_status_reports_missing_configuration(self):
        with patch.dict(os.environ, NO_LLM_ENV, clear=True), \
                patch.object(server, "PARSE_LLM_NETWORK", True):
            status = self.status()
        self.assertFalse(status["configured"])
        self.assertFalse(status["available"])

    def test_job_llm_modes_are_limited(self):
        self.assertEqual(server.PARSE_LLM_MODES, {"off", "mock", "api"})


class ServerParseApiHttpTest(unittest.TestCase):
    """POST /api/parse with X-LLM: api must be refused without deployment opt-in."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.server = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.server.profiles_dir = self.root / "profiles"
        self.server.parse_root = self.root / "parses"
        self.server.auth = auth.Auth(self.root / "auth.sqlite3",
                                     iterations=auth.MIN_ITERATIONS)
        self.thread = Thread(target=self.server.serve_forever)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.client = build_opener(HTTPCookieProcessor(CookieJar()))
        request = Request(
            self.base + "/api/auth/register",
            data=json.dumps({"username": "llmuser", "password": "correct horse"}).encode(),
            headers={"Content-Type": "application/json"})
        with self.client.open(request, timeout=20) as response:
            self.csrf = json.load(response)["csrf_token"]

    def tearDown(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        self.tmp.cleanup()

    def post(self):
        request = Request(self.base + "/api/parse", data=synthetic_pdf(), headers={
            "Content-Type": "application/pdf", "X-Profile": "solar", "X-LLM": "api",
            "X-CSRF-Token": self.csrf})
        try:
            with self.client.open(request, timeout=30) as response:
                return response.status
        except HTTPError as error:
            with error:
                return error.code

    def test_api_mode_disabled_is_403(self):
        with patch.dict(os.environ, OPENAI_ENV, clear=True), \
                patch.object(server, "PARSE_LLM_NETWORK", False):
            self.assertEqual(self.post(), 403)

    def test_api_mode_without_key_is_503(self):
        with patch.dict(os.environ, NO_LLM_ENV, clear=True), \
                patch.object(server, "PARSE_LLM_NETWORK", True):
            self.assertEqual(self.post(), 503)

    def test_api_mode_enabled_and_configured_is_accepted(self):
        calls = []

        def no_parse(handler, folder, profile_path, parser, ocr, charts, llm="off"):
            calls.append({"llm": llm, "args": list(getattr(handler, "last_command", []))})

        with patch.dict(os.environ, OPENAI_ENV, clear=True), \
                patch.object(server, "PARSE_LLM_NETWORK", True), \
                patch.object(server.Handler, "run_parse", no_parse):
            self.assertEqual(self.post(), 202)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["llm"], "api")
        jobs = list((self.root / "parses").glob("*/job.json"))
        self.assertEqual(len(jobs), 1)
        job = json.loads(jobs[0].read_text(encoding="utf-8"))
        self.assertEqual(job["llm"], "api")
        self.assertNotIn("sk-test-secret", json.dumps(job))


class ServerApiRealSubprocessTest(unittest.TestCase):
    """The full user-facing path: HTTP -> subprocess -> model endpoint -> artifacts.

    A local [OI]-compatible stub plays the provider, so nothing external is
    contacted while the real transport, argv, environment and artifact chain run.
    """

    def setUp(self):
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0") or 0)
                body = json.loads(self.rfile.read(length))
                outer.requests.append({"path": self.path, "body": body,
                                       "auth": self.headers.get("Authorization")})
                system = body["messages"][0]["content"]
                if system.startswith("Audit"):
                    reply = {"findings": []}
                else:
                    # A grounded answer for the synthetic PDF: only a
                    # publications row, quoting nothing that is not there.
                    row = dict.fromkeys(TABLES["publications"], "")
                    row.update(publication_id="article",
                               title="Synthetic experimental single-junction perovskite solar cell.",
                               in_scope="true")
                    row["source_ids"] = []
                    reply = {table: [] for table in TABLES}
                    reply["publications"] = [row]
                payload = json.dumps({"choices": [{
                    "finish_reason": "stop",
                    "message": {"content": json.dumps(reply)}}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.provider = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.provider_thread = Thread(target=self.provider.serve_forever)
        self.provider_thread.start()
        self.provider_base = f"http://127.0.0.1:{self.provider.server_port}/v1"

        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.server = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.server.profiles_dir = self.root / "profiles"
        self.server.parse_root = self.root / "parses"
        self.server.parse_slots = None
        self.server.auth = auth.Auth(self.root / "auth.sqlite3",
                                     iterations=auth.MIN_ITERATIONS)
        self.thread = Thread(target=self.server.serve_forever)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.client = build_opener(HTTPCookieProcessor(CookieJar()))
        request = Request(
            self.base + "/api/auth/register",
            data=json.dumps({"username": "llmreal", "password": "correct horse"}).encode(),
            headers={"Content-Type": "application/json"})
        with self.client.open(request, timeout=20) as response:
            self.csrf = json.load(response)["csrf_token"]

    def tearDown(self):
        for item, thread in ((self.server, self.thread), (self.provider, self.provider_thread)):
            item.shutdown()
            thread.join()
            item.server_close()
        self.tmp.cleanup()

    def test_parse_with_api_mode_produces_model_artifacts(self):
        env = process_env(
            OPENAI_API_KEY="sk-local-secret", OPENAI_MODEL="test/model",
            OPENAI_BASE_URL=self.provider_base, VINITI_PARSE_LLM="api")
        request = Request(self.base + "/api/parse", data=synthetic_pdf(), headers={
            "Content-Type": "application/pdf", "X-Profile": "solar", "X-LLM": "api",
            "X-Parser": "pdfplumber", "X-CSRF-Token": self.csrf})
        # The parse subprocess inherits os.environ when it is spawned, so the
        # patch must stay active until the job has actually finished.
        with patch.dict(os.environ, env, clear=True), \
                patch.object(server, "PARSE_LLM_NETWORK", True), \
                patch.object(server, "PARSE_TIMEOUT", 120):
            with self.client.open(request, timeout=60) as response:
                self.assertEqual(response.status, 202)
                job_id = json.loads(response.read())["id"]
            folder = self.root / "parses" / job_id
            deadline = time() + 120
            while time() < deadline:
                job = json.loads((folder / "job.json").read_text(encoding="utf-8"))
                if job.get("status") not in {"queued", "running"}:
                    break
                sleep(0.2)
        self.assertEqual(job["status"], "ok", job)
        self.assertEqual(job["llm"], "api")
        # The parser is a child process, so its provider handler cannot mutate
        # this process' in-memory request list.  Transport shape and auth are
        # covered by ShellApiEndToEndTest; here the persisted manifest and
        # validated artifacts prove that the child completed both calls.

        manifest = json.loads((folder / "bundle" / "llm" / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["mode"], "api")
        self.assertEqual(manifest["backend"]["name"], "api")
        self.assertTrue(manifest["guarantees"]["network_used"])
        stages = {stage["name"]: stage["status"] for stage in manifest["stages"]}
        self.assertEqual(stages["interpret"], "ok", manifest)
        self.assertEqual(stages["audit"], "ok", manifest)

        interpretation = json.loads(
            (folder / "bundle" / "llm" / "interpretation.json").read_text(encoding="utf-8"))
        self.assertEqual(interpretation["adapter"], "solar_csv")
        self.assertEqual(interpretation["tables"]["publications"][0]["publication_id"], "article")
        self.assertEqual(interpretation["counts"]["records"], 1)

        for path in folder.rglob("*"):
            if path.is_file() and path.suffix in {".json", ".log", ".txt"}:
                self.assertNotIn("sk-local-secret", path.read_text(encoding="utf-8", errors="replace"), path)


if __name__ == "__main__":
    unittest.main()
