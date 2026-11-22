"""The assistant may propose fields, but only sourced values pass validation."""
import os
import json
from http.cookiejar import CookieJar
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from unittest import TestCase, mock
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener

from viniti import auth
from viniti.profile_assistant import suggest
from viniti.server import Handler, ThreadingHTTPServer


class ProfileAssistantTest(TestCase):
    def test_suggestions_keep_only_verifiable_values(self):
        payload = {"question": "Какие поля нужны?", "profile": {"types": []},
                   "sources": [{"page": 2, "quote": "PCE reached 21.5% in the device."}]}
        response = {"suggestions": [
            {"entity": "Устройство", "field": "КПД", "type": "number",
             "value": "21.5%", "page": 2, "quote": "PCE reached 21.5%"},
            {"entity": "Устройство", "field": "Материал", "type": "text",
             "value": "perovskite", "page": 2, "quote": "invented quote"},
        ]}
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "test", "OPENROUTER_MODEL": "test"}):
            result = suggest(payload, complete_fn=lambda *args, **kwargs: response)
        self.assertEqual(result["suggestions"][0]["value"], "21.5%")
        self.assertEqual(result["suggestions"][1]["value"], "")
        self.assertEqual(result["suggestions"][1]["quote"], "")

    def test_missing_configuration_is_explicit(self):
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "", "OPENROUTER_MODEL": ""}):
            with self.assertRaisesRegex(RuntimeError, "не настроен"):
                suggest({"question": "Нужны поля", "sources": [], "profile": {"types": []}})

    def start_server(self):
        """A real server with auth enabled, as production runs it."""
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.auth = auth.Auth(Path(directory.name) / "auth.sqlite3",
                                iterations=auth.MIN_ITERATIONS)
        thread = Thread(target=server.serve_forever)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        return server

    def register(self, server):
        """Register a user and return the opener carrying its session cookie."""
        client = build_opener(HTTPCookieProcessor(CookieJar()))
        request = Request(
            f"http://127.0.0.1:{server.server_port}/api/auth/register",
            data=json.dumps({"username": "profileuser", "password": "correct horse"}).encode(),
            headers={"Content-Type": "application/json"})
        with client.open(request, timeout=5) as response:
            return client, json.load(response)["csrf_token"]

    def assist(self, server, client, csrf=None):
        headers = {"Content-Type": "application/json"}
        if csrf is not None:
            headers["X-CSRF-Token"] = csrf
        request = Request(
            f"http://127.0.0.1:{server.server_port}/api/profile/assist",
            data=json.dumps({"question": "Какие поля?", "sources": [],
                             "profile": {"types": []}}).encode(),
            headers=headers, method="POST")
        try:
            with client.open(request, timeout=5) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            with error:
                return error.code, json.load(error)

    def test_http_requires_server_configuration(self):
        server = self.start_server()
        client, csrf = self.register(server)
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "", "OPENROUTER_MODEL": ""}):
            status, body = self.assist(server, client, csrf)
        # An authenticated request reaches the assistant, which reports the
        # missing OpenRouter configuration instead of failing on auth.
        self.assertEqual(status, 503)
        self.assertIn("не настроен", body["error"])

    def test_http_requires_session_cookie(self):
        server = self.start_server()
        status, body = self.assist(server, build_opener())
        self.assertEqual(status, 401)
        self.assertEqual(body["error"], "Требуется вход.")

    def test_http_requires_csrf_token(self):
        server = self.start_server()
        client, _ = self.register(server)
        status, body = self.assist(server, client)
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "Неверный CSRF-токен.")
