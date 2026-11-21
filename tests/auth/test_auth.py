import json
from hashlib import sha256
from http.cookiejar import CookieJar
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import HTTPPasswordMgr, Request, build_opener, urlopen

import auth
from library import Library
from rag_api import RagStore, demo_pdf
from server import Handler, ThreadingHTTPServer


def tiny_pdf():
    return b"%PDF-1.4\n% minimal test body\n"


class AuthApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        root = Path(self.tmp.name)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.auth = auth.Auth(root / "auth.sqlite3", iterations=auth.MIN_ITERATIONS)
        self.server.library = Library(root / "papers.sqlite3")
        self.server.rag_store = RagStore(root / "papers.sqlite3")
        self.server.parse_root = root / "parses"
        self.thread = Thread(target=self.server.serve_forever)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.jar = CookieJar()
        self.client = build_opener(__import__("urllib.request").request.HTTPCookieProcessor(self.jar))

    def tearDown(self):
        self.server.shutdown(); self.thread.join(); self.server.server_close(); self.tmp.cleanup()

    def call(self, path, payload=None, headers=None, method=None, client=None):
        headers = dict(headers or {})
        data = None if payload is None else json.dumps(payload).encode()
        if data is not None:
            headers.setdefault("Content-Type", "application/json")
        request = Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with (client or self.client).open(request, timeout=20) as response:
                return response.status, response.headers, json.loads(response.read() or b"{}")
        except HTTPError as error:
            with error:
                raw = error.read()
                return error.code, error.headers, json.loads(raw or b"{}")

    def register(self, username):
        status, headers, body = self.call("/api/auth/register", {"username": username, "password": "correct horse"})
        self.assertEqual(status, 201, body)
        self.assertIn("HttpOnly", headers.get("Set-Cookie", ""))
        self.assertIn("SameSite=Lax", headers.get("Set-Cookie", ""))
        return body["csrf_token"]

    def register_client(self, client, username):
        request = Request(self.base + "/api/auth/register", data=json.dumps({
            "username": username, "password": "correct horse",
        }).encode(), headers={"Content-Type": "application/json"})
        with client.open(request, timeout=20) as response:
            return json.load(response)["csrf_token"]

    def test_username_can_be_a_work_email(self):
        csrf = self.register("person@viniti.ru")
        self.assertTrue(csrf)
        status, _, body = self.call("/api/auth/me")
        self.assertEqual(status, 200)
        self.assertEqual(body["user"]["username"], "person@viniti.ru")

    def test_demo_card_is_shared_between_users(self):
        csrf_a = self.register("demo_a")
        status, created = self.upload("/api/demo", demo_pdf(), csrf_a)
        self.assertEqual(status, 201, created)
        document_id = created["document_id"]
        self.assertEqual(document_id, "rag:demo")

        other = build_opener(__import__("urllib.request").request.HTTPCookieProcessor(CookieJar()))
        csrf_b = self.register_client(other, "demo_b")
        status, repeated = self.upload("/api/demo", demo_pdf(), csrf_b, client=other)
        self.assertEqual(status, 201, repeated)
        self.assertEqual(repeated["document_id"], document_id)

        status, card = self.get("/api/documents/" + quote(document_id, safe=""), client=other)
        self.assertEqual(status, 200, card)
        status, listing = self.get("/api/documents?track=rag", client=other)
        self.assertEqual(status, 200)
        self.assertIn(document_id, [item["document_id"] for item in listing["items"]])

    def test_user_profiles_are_isolated_between_users(self):
        csrf_a = self.register("profile_a")
        profile = {
            "id": "private_profile", "version": "1", "description": "A",
            "tables": {"equipment": {"key": "equipment_id", "fields": {
                "equipment_id": {"type": "id", "description": "id"},
            }}},
        }
        status, _, _ = self.call("/api/profiles/private_profile", profile,
                                 {"X-CSRF-Token": csrf_a}, method="PUT")
        self.assertEqual(status, 200)
        other = build_opener(__import__("urllib.request").request.HTTPCookieProcessor(CookieJar()))
        csrf_b = self.register_client(other, "profile_b")
        request = Request(self.base + "/api/profiles/private_profile")
        with self.assertRaises(HTTPError) as caught:
            other.open(request, timeout=20)
        self.assertEqual(caught.exception.code, 404)
        request = Request(self.base + "/api/profiles/private_profile", data=json.dumps(profile).encode(),
                          headers={"Content-Type": "application/json", "X-CSRF-Token": csrf_b}, method="PUT")
        with other.open(request, timeout=20) as response:
            self.assertEqual(response.status, 200)

    def test_register_login_logout_and_password_is_hashed(self):
        csrf = self.register("alice")
        status, _, body = self.call("/api/auth/login", {"username": "alice", "password": "wrong"})
        self.assertEqual(status, 401); self.assertEqual(body["error"], "Неверное имя пользователя или пароль.")
        old_cookie = next(iter(self.jar)).value
        status, _, body = self.call("/api/auth/login", {"username": "alice", "password": "correct horse"})
        self.assertEqual(status, 200); self.assertNotEqual(old_cookie, next(iter(self.jar)).value)
        csrf = body["csrf_token"]
        active_cookie = next(iter(self.jar)).value
        status, _, body = self.call("/api/auth/logout", headers={"X-CSRF-Token": csrf}, method="POST")
        self.assertEqual(status, 200); self.assertEqual(body, {"ok": True})
        status, _, body = self.call("/api/auth/me")
        self.assertEqual(status, 200); self.assertEqual(body, {"user": None})
        # The cookie captured before logout must be dead on the server too.
        request = Request(self.base + "/api/auth/me",
                          headers={"Cookie": f"{auth.COOKIE_NAME}={active_cookie}"})
        with build_opener().open(request, timeout=20) as response:
            self.assertEqual(json.load(response), {"user": None})
        conn = __import__("sqlite3").connect(self.server.auth.path)
        row = conn.execute("SELECT password_hash FROM users").fetchone()[0]
        conn.close()
        self.assertNotIn("correct horse", row); self.assertTrue(row.startswith("pbkdf2_sha256$"))

    def test_failed_logins_are_recorded_and_limited(self):
        store = auth.Auth(Path(self.tmp.name) / "limit.sqlite3", iterations=auth.MIN_ITERATIONS)
        store.register("limit_user", "correct horse")
        for _ in range(5):
            with self.assertRaises(auth.AuthError) as caught:
                store.login("limit_user", "wrong-password")
            self.assertEqual(caught.exception.status, 401)
        with self.assertRaises(auth.AuthError) as caught:
            store.login("limit_user", "correct horse")
        self.assertEqual(caught.exception.status, 429)
        with sqlite3.connect(store.path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM auth_failures").fetchone()[0], 5)

    def test_expired_session_is_rejected_and_deleted(self):
        now = [1000.0]
        store = auth.Auth(Path(self.tmp.name) / "ttl.sqlite3", iterations=auth.MIN_ITERATIONS,
                          session_ttl=10, clock=lambda: now[0])
        store.register("ttl_user", "correct horse")
        token, _, _, _ = store.login("ttl_user", "correct horse")
        self.assertIsNotNone(store.authenticate(token))
        now[0] += 11
        self.assertIsNone(store.authenticate(token))
        with sqlite3.connect(store.path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 0)

    def test_cross_origin_post_is_rejected(self):
        status, _, body = self.call("/api/auth/login",
                                    {"username": "nobody", "password": "correct horse"},
                                    {"Origin": "http://evil.example"})
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "Запрос с другого источника отклонён.")

    def test_auth_only_database_is_not_mistaken_for_results(self):
        self.register("stats_user")
        self.server.data = self.server.auth.path
        status, _, body = self.call("/api/stats")
        self.assertEqual(status, 503, body)
        self.assertIn("db.py --init", body["error"])

    def upload(self, path, data, csrf, client=None):
        request = Request(self.base + path, data=data, headers={
            "Content-Type": "application/pdf", "X-CSRF-Token": csrf,
            "X-Filename": "duplicate.pdf"})
        try:
            with (client or self.client).open(request, timeout=60) as response:
                return response.status, json.loads(response.read() or b"{}")
        except HTTPError as error:
            with error:
                return error.code, json.loads(error.read() or b"{}")

    def get(self, path, client=None):
        try:
            with (client or self.client).open(self.base + path, timeout=20) as response:
                return response.status, json.loads(response.read() or b"{}")
        except HTTPError as error:
            with error:
                return error.code, json.loads(error.read() or b"{}")

    def test_upload_requires_auth_and_csrf(self):
        request = Request(self.base + "/api/parse", data=tiny_pdf(), headers={"Content-Type": "application/pdf"})
        with self.assertRaises(HTTPError) as caught:
            with self.client.open(request, timeout=20):
                pass
        self.assertEqual(caught.exception.code, 401)
        csrf = self.register("bob")
        request = Request(self.base + "/api/parse", data=tiny_pdf(), headers={"Content-Type": "application/pdf"})
        with self.assertRaises(HTTPError) as caught:
            self.client.open(request, timeout=20)
        self.assertEqual(caught.exception.code, 403)
        request = Request(self.base + "/api/parse", data=tiny_pdf(), headers={
            "Content-Type": "application/pdf", "X-CSRF-Token": csrf,
            "X-Parser": "not-a-parser"})
        with self.assertRaises(HTTPError) as caught:
            self.client.open(request, timeout=20)
        self.assertEqual(caught.exception.code, 400)

    def test_duplicate_upload_belongs_to_first_uploader(self):
        pdf = demo_pdf()
        csrf_a = self.register("upload_a")
        status, created = self.upload("/api/documents", pdf, csrf_a)
        self.assertEqual(status, 201, created)
        document_id = created["document_id"]
        card = "/api/documents/" + quote(document_id, safe="")
        status, _ = self.get(card)
        self.assertEqual(status, 200)
        # The same user may repeat the upload: the stored document is theirs.
        status, _ = self.upload("/api/documents", pdf, csrf_a)
        self.assertEqual(status, 201)

        other = build_opener(__import__("urllib.request").request.HTTPCookieProcessor(CookieJar()))
        csrf_b = self.register_client(other, "upload_b")
        status, body = self.upload("/api/documents", pdf, csrf_b, client=other)
        self.assertEqual(status, 409, body)
        self.assertNotIn("document_id", body)
        status, _ = self.get(card, client=other)
        self.assertEqual(status, 404)
        status, _ = self.get(card)
        self.assertEqual(status, 200)

        conn = sqlite3.connect(self.server.auth.path)
        owner = conn.execute("SELECT user_id FROM document_owners WHERE area=? AND document_id=?", ("rag", document_id)).fetchone()[0]
        alice = conn.execute("SELECT id FROM users WHERE username='upload_a'").fetchone()[0]
        conn.close()
        self.assertEqual(owner, alice)

    def test_legacy_duplicate_upload_belongs_to_first_uploader(self):
        pdf = demo_pdf()
        csrf_a = self.register("legacy_a")
        status, created = self.upload("/api/papers", pdf, csrf_a)
        self.assertEqual(status, 201, created)
        paper_id = created["id"]
        self.assertEqual(paper_id, sha256(pdf).hexdigest())
        status, _ = self.get("/api/papers/" + paper_id)
        self.assertEqual(status, 200)

        other = build_opener(__import__("urllib.request").request.HTTPCookieProcessor(CookieJar()))
        csrf_b = self.register_client(other, "legacy_b")
        status, body = self.upload("/api/papers", pdf, csrf_b, client=other)
        self.assertEqual(status, 409, body)
        self.assertNotIn("id", body)
        status, _ = self.get("/api/papers/" + paper_id, client=other)
        self.assertEqual(status, 404)
        status, _ = self.get("/api/papers/" + paper_id)
        self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main()
