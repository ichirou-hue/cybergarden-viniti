from contextlib import closing
import json
from hashlib import sha256
from http.cookiejar import CookieJar
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from unittest import mock
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
        # The demo is readable by everyone, but it is explicitly not a run of
        # the account that happens to open it first.
        self.assertIs(created.get("shared"), True)
        self.assertIs(created.get("demo"), True)
        self.assertEqual(created.get("ownership"), "shared")

        other = build_opener(__import__("urllib.request").request.HTTPCookieProcessor(CookieJar()))
        csrf_b = self.register_client(other, "demo_b")
        status, repeated = self.upload("/api/demo", demo_pdf(), csrf_b, client=other)
        self.assertEqual(status, 201, repeated)
        self.assertEqual(repeated["document_id"], document_id)
        self.assertIs(repeated.get("shared"), True)
        self.assertEqual(repeated.get("ownership"), "shared")

        status, card = self.get("/api/documents/" + quote(document_id, safe=""), client=other)
        self.assertEqual(status, 200, card)
        self.assertIs(card.get("shared"), True)
        self.assertIs(card.get("demo"), True)
        self.assertEqual(card.get("ownership"), "shared")
        status, listing = self.get("/api/documents?track=rag", client=other)
        self.assertEqual(status, 200)
        items = [item for item in listing["items"] if item["document_id"] == document_id]
        self.assertEqual(len(items), 1, listing)
        self.assertIs(items[0].get("shared"), True)
        self.assertIs(items[0].get("demo"), True)
        self.assertEqual(items[0].get("ownership"), "shared")

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

    def test_legacy_flat_profiles_are_not_shared(self):
        # A profile saved before per-user folders has no owner. It must stay
        # invisible and unreadable for every authenticated user, while builtins
        # and user-specific profiles keep working.
        profiles = Path(self.tmp.name) / "profiles"
        profiles.mkdir()
        self.server.profiles_dir = profiles
        legacy = {
            "id": "legacy_shared", "version": "1", "description": "shared",
            "tables": {"equipment": {"key": "equipment_id", "fields": {
                "equipment_id": {"type": "id", "description": "id"},
            }}},
        }
        (profiles / "legacy_shared.json").write_text(json.dumps(legacy), encoding="utf-8")

        self.register("legacy_reader_a")
        other = build_opener(__import__("urllib.request").request.HTTPCookieProcessor(CookieJar()))
        self.register_client(other, "legacy_reader_b")
        for client in (self.client, other, build_opener()):
            status, body = self.get("/api/profiles", client=client)
            self.assertEqual(status, 200, body)
            ids = {item["id"] for item in body["profiles"]}
            self.assertIn("solar", ids)
            self.assertNotIn("legacy_shared", ids)
            status, body = self.get("/api/profiles/legacy_shared", client=client)
            self.assertEqual(status, 404, body)

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
        with closing(sqlite3.connect(store.path)) as conn:
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
        with closing(sqlite3.connect(store.path)) as conn:
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

    def upload(self, path, data, csrf, client=None, track=None):
        headers = {
            "Content-Type": "application/pdf", "X-CSRF-Token": csrf,
            "X-Filename": "duplicate.pdf"}
        if track:
            headers["X-Track"] = track
        request = Request(self.base + path, data=data, headers=headers)
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

    def test_parse_job_list_is_owner_scoped_and_safe(self):
        csrf = self.register("parse_list_owner")
        with mock.patch.object(Handler, "run_parse", return_value=None):
            status, created = self.upload("/api/parse", tiny_pdf(), csrf)
        self.assertEqual(status, 202, created)
        job_id = created["id"]

        status, listing = self.get("/api/parse?active=1")
        self.assertEqual(status, 200, listing)
        self.assertEqual(listing["total"], 1, listing)
        item = listing["items"][0]
        self.assertEqual(item["id"], job_id)
        self.assertEqual(item["status"], "queued")
        self.assertIs(item["active"], True)
        # The list is a display view: no stored PDF, hash or owner id.
        for forbidden in ("sha256", "owner_user_id", "bytes", "input.pdf"):
            self.assertNotIn(forbidden, item)
            self.assertNotIn(forbidden, json.dumps(item))

        other = build_opener(__import__("urllib.request").request.HTTPCookieProcessor(CookieJar()))
        self.register_client(other, "parse_list_other")
        status, listing = self.get("/api/parse", client=other)
        self.assertEqual(status, 200, listing)
        self.assertEqual(listing["total"], 0, listing)
        status, body = self.get("/api/parse/" + job_id, client=other)
        self.assertEqual(status, 404, body)

        status, body = self.get("/api/parse", client=build_opener())
        self.assertEqual(status, 401, body)

    def test_parse_job_survives_navigation_and_is_not_cancelled(self):
        csrf = self.register("parse_nav")
        with mock.patch.object(Handler, "run_parse", return_value=None):
            status, created = self.upload("/api/parse", tiny_pdf(), csrf)
        self.assertEqual(status, 202, created)
        job_id = created["id"]

        status, first = self.get("/api/parse/" + job_id)
        self.assertEqual(status, 200, first)
        self.assertEqual(first["status"], "queued")

        # Leaving the page is not a cancellation: unrelated navigation and a
        # new browser session still see the same job file.
        status, _ = self.get("/api/documents?track=rag")
        self.assertEqual(status, 200)
        fresh = build_opener(__import__("urllib.request").request.HTTPCookieProcessor(CookieJar()))
        status, _, login = self.call("/api/auth/login", {
            "username": "parse_nav", "password": "correct horse"}, client=fresh)
        self.assertEqual(status, 200, login)
        status, restored = self.get("/api/parse/" + job_id, client=fresh)
        self.assertEqual(status, 200, restored)
        self.assertEqual(restored["id"], job_id)
        self.assertEqual(restored["status"], "queued")
        status, listing = self.get("/api/parse?active=1", client=fresh)
        self.assertEqual(status, 200, listing)
        self.assertIn(job_id, [item["id"] for item in listing["items"]])

        # There is no cancellation endpoint: DELETE must not remove the job.
        status, _, _ = self.call("/api/parse/" + job_id, method="DELETE",
                                 headers={"X-CSRF-Token": csrf})
        self.assertEqual(status, 404)
        status, still = self.get("/api/parse/" + job_id)
        self.assertEqual(status, 200, still)
        self.assertEqual(still["status"], "queued")

    def test_parse_job_list_accepts_status_alias_and_rejects_bad_limit(self):
        csrf = self.register("parse_filters")
        with mock.patch.object(Handler, "run_parse", return_value=None):
            status, created = self.upload("/api/parse", tiny_pdf(), csrf)
        self.assertEqual(status, 202, created)
        job_id = created["id"]
        status, listing = self.get("/api/parse?status=active")
        self.assertEqual(status, 200, listing)
        self.assertIn(job_id, [item["id"] for item in listing["items"]])
        status, body = self.get("/api/parse?limit=0")
        self.assertEqual(status, 400, body)

    def test_parse_job_without_owner_is_not_listed(self):
        self.register("legacy_parse_owner")
        folder = self.server.parse_root / "0123456789abcdef"
        folder.mkdir(parents=True)
        (folder / "job.json").write_text(json.dumps({
            "id": "0123456789abcdef", "status": "queued", "filename": "legacy.pdf",
        }), encoding="utf-8")
        status, listing = self.get("/api/parse")
        self.assertEqual(status, 200, listing)
        self.assertEqual(listing["total"], 0, listing)
        status, body = self.get("/api/parse/0123456789abcdef")
        self.assertEqual(status, 404, body)

    def test_duplicate_upload_belongs_to_first_uploader(self):
        pdf = demo_pdf()
        csrf_a = self.register("upload_a")
        status, created = self.upload("/api/documents", pdf, csrf_a)
        self.assertEqual(status, 201, created)
        document_id = created["document_id"]
        self.assertIs(created.get("shared"), False)
        self.assertEqual(created.get("ownership"), "owner")
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

    def test_catalog_recognition_job_belongs_to_uploader(self):
        from tests.test_solar_offline import TABLE_PAGE, make_pdf
        import solar_api
        self.server.solar_store = solar_api.SolarStore(Path(self.tmp.name) / "solar.sqlite3")
        csrf = self.register("catalog_owner")
        # Recognition runs in a background thread; the subprocess is not part of
        # this test, only the ownership of the job it would fill.
        with mock.patch.object(Handler, "run_parse", return_value=None):
            status, created = self.upload(
                "/api/documents", make_pdf(TABLE_PAGE, table=True), csrf, track="solar")
        self.assertEqual(status, 201, created)
        job_id = created.get("recognition_job_id")
        self.assertTrue(job_id, created)

        status, _, me = self.call("/api/auth/me")
        self.assertEqual(status, 200)
        status, job = self.get("/api/parse/" + job_id)
        self.assertEqual(status, 200, job)
        self.assertEqual(job["owner_user_id"], me["user"]["id"])
        # The owner reaches the job before it finishes: 409, not 404.
        status, _ = self.get("/api/parse/" + job_id + "/result")
        self.assertEqual(status, 409)

        other = build_opener(__import__("urllib.request").request.HTTPCookieProcessor(CookieJar()))
        self.register_client(other, "catalog_other")
        for suffix in ("", "/result", "/llm", "/file/bundle/result.json"):
            status, body = self.get("/api/parse/" + job_id + suffix, client=other)
            self.assertEqual(status, 404, (suffix, body))

    def test_repeated_solar_upload_reuses_active_recognition_job(self):
        from tests.test_solar_offline import TABLE_PAGE, make_pdf
        import solar_api
        self.server.solar_store = solar_api.SolarStore(Path(self.tmp.name) / "solar.sqlite3")
        csrf = self.register("catalog_repeat_owner")
        pdf = make_pdf(TABLE_PAGE, table=True)
        with mock.patch.object(Handler, "run_parse", return_value=None):
            status, first = self.upload("/api/documents", pdf, csrf, track="solar")
            self.assertEqual(status, 201, first)
            # The same bytes are one card and one queued recognition: a second
            # job would duplicate work and hide the first id from the client.
            status, second = self.upload("/api/documents", pdf, csrf, track="solar")
        self.assertEqual(status, 201, second)
        self.assertEqual(second["document_id"], first["document_id"])
        self.assertTrue(first.get("recognition_job_id"), first)
        self.assertEqual(second.get("recognition_job_id"), first["recognition_job_id"])
        jobs = [folder for folder in self.server.parse_root.iterdir() if folder.is_dir()]
        self.assertEqual(len(jobs), 1, jobs)

    def test_register_rejects_password_over_256_characters(self):
        status, _, body = self.call("/api/auth/register",
                                    {"username": "long_password_user", "password": "x" * 257})
        self.assertEqual(status, 422, body)
        self.assertIn("256", body["error"])

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


class EmailRulesTest(unittest.TestCase):
    """auth.email_problem is the single authority for email-shaped logins."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.store = auth.Auth(Path(self.tmp.name) / "email.sqlite3",
                               iterations=auth.MIN_ITERATIONS)

    def tearDown(self):
        self.tmp.cleanup()

    def test_work_emails_pass(self):
        for value in ("person@viniti.ru", "first.last+tag@sub.example.org", "a@b.io"):
            self.assertIsNone(auth.email_problem(value), value)

    def test_numeric_only_tld_is_rejected(self):
        problem = auth.email_problem("tdhbtdnd@tdbbdb.45")
        self.assertIsNotNone(problem)
        self.assertIn("верхнего уровня", problem)

    def test_malformed_emails_are_rejected(self):
        for value in ("", "person", "person@", "@viniti.ru", "person@@viniti.ru",
                      "person@viniti", "person@-viniti.ru", "person@viniti..ru",
                      ".person@viniti.ru", "person@viniti.ru.", "person@viniti.r",
                      "person@viniti.45", "a" * 65 + "@viniti.ru", "person@" + "a" * 250 + ".ru"):
            self.assertIsNotNone(auth.email_problem(value), value)

    def test_register_applies_email_rules_and_keeps_plain_logins(self):
        with self.assertRaises(auth.AuthError) as caught:
            self.store.register("tdhbtdnd@tdbbdb.45", "correct horse")
        self.assertEqual(caught.exception.status, 422)
        # Plain logins stay accepted: the browser form registers an email, but
        # existing accounts and the tests use bare names.
        self.store.register("plain_login", "correct horse")
        self.store.register("person@viniti.ru", "correct horse")
        with self.assertRaises(auth.AuthError) as caught:
            self.store.register("person@viniti.ru", "correct horse")
        self.assertEqual(caught.exception.status, 409)

    def test_password_upper_bound(self):
        self.store.register("boundary_user", "x" * auth.MAX_PASSWORD_LENGTH)
        with self.assertRaises(auth.AuthError) as caught:
            self.store.register("too_long_user", "x" * (auth.MAX_PASSWORD_LENGTH + 1))
        self.assertEqual(caught.exception.status, 422)
        self.assertIn(str(auth.MAX_PASSWORD_LENGTH), caught.exception.message)
        with self.assertRaises(ValueError):
            auth.hash_password("x" * (auth.MAX_PASSWORD_LENGTH + 1), auth.MIN_ITERATIONS)


if __name__ == "__main__":
    unittest.main()
