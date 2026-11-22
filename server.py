"""HTTP-сервер CyberGarden / ВИНИТИ.

Два поколения API живут рядом:

* ``/api/papers…`` — старый экран одной статьи (``library.py`` + ``rag_facts``);
* ``/api/documents…`` — контракт каталога и карточки (ADR-0008), который
  наполняет RAG-модуль ``rag`` через ``rag_api.py``.

Фронтенд в ``static/`` обслуживается с этого же адреса, поэтому CORS не нужен.
"""

import argparse
import csv
from hashlib import sha256
import io
import hmac
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
import re
import sqlite3
import subprocess
import sys
import threading
from time import time
import uuid
from urllib.parse import parse_qs, unquote, urlsplit

import db as storage
import auth
import export_csv as result_export
import llm_provider
import llm_shell
import profiles_store
import profile_assistant

from pdfminer.pdfdocument import PDFPasswordIncorrect
from pdfminer.pdfparser import PDFSyntaxError

from demo import demo_pdf as legacy_demo_pdf  # noqa: F401  (совместимость старых импортов)
from library import Library, MAX_BYTES
from pdf_source import extract, render
from rag_api import (
    MAX_BYTES as RAG_MAX_BYTES,
    RagStore,
    demo_pdf as rag_demo_pdf,
    export_csv,
    facets,
    filter_results,
    render_anchor_png,
    sources as list_sources,
)
from solar_api import SolarStore, solar_sources, RECOGNITION_REVISION
from solar_api import MAX_BYTES as SOLAR_MAX_BYTES
from viniti import diagnostics, paths


ROOT = paths.REPO_ROOT
STATIC_DIR = paths.STATIC_DIR.resolve()
STATIC_TYPES = {
    ".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8", ".mjs": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8", ".svg": "image/svg+xml",
    ".png": "image/png", ".ico": "image/x-icon",
}
AREAS = {"solar", "rag"}
JOB_STATUSES = ("ok", "error", "skipped", "running", "queued")
PARSE_MAX_BYTES = 40 * 1024 * 1024
PARSE_ID = re.compile(r"[0-9a-f]{16}")
try:
    PARSE_TIMEOUT = max(1, int(os.environ.get("VINITI_PARSE_TIMEOUT", "1800")))
except ValueError:
    PARSE_TIMEOUT = 1800
PARSE_FILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,120}")
PARSE_KINDS = {"tables", "figures", "charts"}
PARSE_LLM_MODES = {"off", "mock", "api"}
# Network LLM mode is a deployment decision, not a per-request one: the
# operator must set this flag explicitly, and the key stays in the environment.
PARSE_LLM_NETWORK = os.environ.get("VINITI_PARSE_LLM", "").strip().lower() in {
    "api", "on", "1", "true", "yes"}


def llm_api_status(environ=None):
    """Whether the api mode is both enabled and configured; never exposes the key."""
    status = llm_provider.describe(environ)
    status["enabled"] = bool(PARSE_LLM_NETWORK)
    status["available"] = bool(status["configured"] and PARSE_LLM_NETWORK)
    return status


def static_file(path):
    if path in ("/manual-profile", "/solar", "/solar.html"):
        path = "/manual-profile.html"
    name = "index.html" if path in ("", "/") else unquote(path).lstrip("/")
    parts = Path(name).parts
    if not name or name.startswith("/") or "\\" in name or any(part in ("", ".", "..") for part in parts):
        return None
    if Path(name).suffix.lower() not in STATIC_TYPES:
        return None
    candidate = (STATIC_DIR / name).resolve()
    try:
        candidate.relative_to(STATIC_DIR)
    except ValueError:
        return None
    return candidate if candidate.is_file() else None
ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/api.js": ("api.js", "text/javascript; charset=utf-8"),
    "/catalog.js": ("catalog.js", "text/javascript; charset=utf-8"),
    "/document.js": ("document.js", "text/javascript; charset=utf-8"),
    "/dom.js": ("dom.js", "text/javascript; charset=utf-8"),
    "/evidence.js": ("evidence.js", "text/javascript; charset=utf-8"),
    "/passport.js": ("passport.js", "text/javascript; charset=utf-8"),
    "/solar.mjs": ("solar.mjs", "text/javascript; charset=utf-8"),
    "/ui.js": ("ui.js", "text/javascript; charset=utf-8"),
    "/tracks/index.js": ("tracks/index.js", "text/javascript; charset=utf-8"),
    "/tracks/rag.js": ("tracks/rag.js", "text/javascript; charset=utf-8"),
    "/tracks/solar.js": ("tracks/solar.js", "text/javascript; charset=utf-8"),
}
PAPER = re.compile(r"/api/papers/([0-9a-f]{64})(?:/(pdf|sources|facts|headers)(?:/([1-9][0-9]{0,8})\.png)?)?")
HEAVY_FIELDS = {"entities", "results", "issues", "anchors", "text_sources", "recognized_sources", "recognition_warnings"}
# Configured by diagnostics.configure() in main(); importing the module does
# not touch process-wide logging.
log = diagnostics.get_logger("server")


class Handler(BaseHTTPRequestHandler):
    # --- ответы ---------------------------------------------------------
    def send_data(self, data, media_type, status=200, **headers):
        self.send_response(status)
        self.send_header("Content-Type", media_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        for key, value in headers.items():
            self.send_header(key.replace("_", "-"), value)
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, document, status=200, **headers):
        self.send_data(json.dumps(document, ensure_ascii=False).encode(),
                       "application/json; charset=utf-8", status, **headers)

    def error(self, message, status=400):
        self.send_json({"error": message}, status)

    def pick_area(self, raw):
        area = (raw or "rag").strip() or "rag"
        if area not in AREAS:
            self.error(f"Недопустимая область: {area}. Допустимы: solar, rag.", 400)
            return None
        return area

    def data_connection(self):
        path = getattr(self.server, "data", None)
        message = ("База результатов не подключена. Запустите python db.py --init и "
                   "python import_gold.py, затем перезапустите сервер.")
        if path is None or not Path(path).exists():
            self.error(message, 503)
            return None
        try:
            conn = storage.connect(path)
        except (SystemExit, sqlite3.Error):
            self.error(message, 503)
            return None
        # The same file now holds the auth tables, so an auth-only database
        # (created by the first registration) must not be mistaken for an
        # initialized results database.
        try:
            has_documents = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='documents'"
            ).fetchone()
        except sqlite3.Error:
            has_documents = None
        if has_documents is None:
            conn.close()
            self.error(message, 503)
            return None
        return conn

    # --- cookie authentication -------------------------------------------
    def auth_store(self):
        store = getattr(self.server, "auth", None)
        if store is not None:
            return store
        path = getattr(self.server, "data", None)
        if path is None or not Path(path).exists():
            return None
        store = auth.Auth(path)
        self.server.auth = store
        return store

    def session_token(self):
        for item in (self.headers.get("Cookie") or "").split(";"):
            name, separator, value = item.strip().partition("=")
            if separator and name == auth.COOKIE_NAME and value:
                return value
        return None

    def current_session(self):
        store = self.auth_store()
        token = self.session_token()
        if store is not None:
            return store.authenticate(token) if token else None
        return None

    # The built-in demo card is common to every signed-in account. Owned
    # uploads stay private, but the demo is a fixed card that two accounts can
    # open without the content-addressed ingest answering 409 to the second one.
    SHARED_DOCUMENTS = frozenset({"rag:demo"})

    @classmethod
    def shared_document(cls, document_id):
        return document_id in cls.SHARED_DOCUMENTS

    def owns_document(self, area, document_id):
        session = self.current_session()
        store = self.auth_store()
        if session is None or store is None:
            return False
        if self.shared_document(document_id):
            return True
        return store.owns_document(area, document_id, session["user"]["id"])

    def visible_documents(self, area, items):
        """Filter a catalog slice without one SQLite round-trip per document."""
        store = self.auth_store()
        if store is None:
            return items
        session = self.current_session()
        allowed = store.owned_documents(area, session["user"]["id"]) if session else set()
        return [item for item in items
                if self.shared_document(item.get("document_id"))
                or item.get("document_id") in allowed]

    def claim_uploaded_document(self, area, document_id, session):
        """True when ``session`` may receive the ingested document.

        A content-addressed ingest returns the document already stored for a
        repeated upload.  When another user owns it, answering 201 would hand
        back a card that ``owns_document`` then hides, so the caller must
        refuse instead.  Without an auth store the server is anonymous and
        ownership is not enforced.
        """
        store = self.auth_store()
        if store is None or session is None or not document_id:
            return True
        if self.shared_document(document_id):
            return True
        return store.claim_document(area, document_id, session["user"]["id"]) != "conflict"

    def same_origin(self):
        host = (self.headers.get("Host") or "").lower()
        for header in ("Origin", "Referer"):
            value = self.headers.get(header)
            if not value:
                continue
            parsed = urlsplit(value)
            return parsed.netloc.lower() == host and parsed.scheme in {"http", "https"}
        return True

    def require_user(self, csrf=True):
        if self.auth_store() is None:
            self.error("Требуется вход.", 401)
            return None
        session = self.current_session()
        if session is None:
            self.error("Требуется вход.", 401)
            return None
        if not self.same_origin():
            self.error("Запрос с другого источника отклонён.", 403)
            return None
        if csrf and not hmac.compare_digest(session["csrf_token"], self.headers.get("X-CSRF-Token", "")):
            self.error("Неверный CSRF-токен.", 403)
            return None
        return session

    def auth_payload(self):
        data = self.payload(64 * 1024, "application/json")
        if data is None:
            return None
        try:
            value = json.loads(data)
        except (UnicodeError, json.JSONDecodeError):
            self.error("Тело должно быть корректным JSON в UTF-8.", 400)
            return None
        if not isinstance(value, dict):
            self.error("Ожидается JSON-объект.", 400)
            return None
        return value

    def auth_register(self):
        if not self.same_origin():
            self.error("Запрос с другого источника отклонён.", 403); return
        store = self.auth_store()
        if store is None:
            self.error("Аутентификация не настроена.", 503); return
        value = self.auth_payload()
        if value is None: return
        try:
            user = store.register(value.get("username"), value.get("password"))
            token, user, csrf_token, expires_at = store.login(value.get("username"), value.get("password"), self.client_address[0], self.headers.get("User-Agent", ""), limit=False)
        except auth.AuthError as error:
            self.error(error.message, error.status); return
        self.send_json({"user": user, "csrf_token": csrf_token, "expires_at": expires_at}, 201,
                       Set_Cookie=auth.cookie_header(token, store.cookie_secure, store.session_ttl))

    def auth_login(self):
        if not self.same_origin():
            self.error("Запрос с другого источника отклонён.", 403); return
        store = self.auth_store()
        if store is None:
            self.error("Аутентификация не настроена.", 503); return
        value = self.auth_payload()
        if value is None: return
        try:
            token, user, csrf_token, expires_at = store.login(value.get("username"), value.get("password"), self.client_address[0], self.headers.get("User-Agent", ""), self.session_token())
        except auth.AuthError as error:
            self.error(error.message, error.status); return
        self.send_json({"user": user, "csrf_token": csrf_token, "expires_at": expires_at},
                       Set_Cookie=auth.cookie_header(token, store.cookie_secure, store.session_ttl))

    def auth_logout(self):
        session = self.current_session()
        if session is not None:
            if not self.same_origin() or not hmac.compare_digest(session["csrf_token"], self.headers.get("X-CSRF-Token", "")):
                self.error("Неверный CSRF-токен.", 403); return
            store = self.auth_store(); store.logout(session["token"])
        self.send_json({"ok": True}, Set_Cookie=auth.clear_cookie())

    # --- профили ручного этапа ------------------------------------------
    def profiles_root(self):
        configured = getattr(self.server, "profiles_dir", None)
        return Path(configured) if configured is not None else paths.DATA_DIR / "profiles"

    def profile_root_for(self, session):
        return self.profiles_root() / session["user"]["id"]

    def profile_scope(self, name, *, require_owner=True):
        if name in profiles_store.BUILTINS:
            return self.profiles_root()
        session = self.current_session()
        if session is None:
            if require_owner:
                self.error("Профиль не найден.", 404)
            return None
        return self.profile_root_for(session)

    def profile_roots(self, name):
        """Read roots in order: the user's folder, then the pre-auth flat one."""
        root = self.profile_scope(name)
        if root is None:
            return []
        roots = [Path(root)]
        legacy = self.profiles_root()
        if legacy is not None and Path(legacy) not in roots:
            roots.append(Path(legacy))
        return roots

    def load_profile(self, name):
        last = None
        for root in self.profile_roots(name):
            try:
                return profiles_store.load(name, root=root)
            except KeyError as error:
                last = error
        raise last or KeyError(name)

    def parses_root(self):
        configured = getattr(self.server, "parse_root", None)
        return Path(configured) if configured is not None else paths.DATA_DIR / "parses"

    def payload(self, limit, media=None):
        if media and self.headers.get_content_type() != media:
            self.error(f"Ожидается {media}.", 415)
            return None
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            self.error("Неверная длина тела запроса.", 400)
            return None
        if length <= 0:
            self.error("Пустое тело запроса.", 413)
            return None
        if length > limit:
            self.error(f"Тело больше {limit // (1024 * 1024)} МиБ.", 413)
            return None
        return self.rfile.read(length)

    def profiles_get(self, path):
        segments = [unquote(part) for part in path.split("/") if part]
        if segments == ["api", "profiles"]:
            session = self.current_session()
            if not session:
                self.send_json({"profiles": profiles_store.list_profiles(
                    root=self.profiles_root(), include_users=False)})
                return
            items = profiles_store.list_profiles(root=self.profile_root_for(session))
            known = {item["id"] for item in items}
            # Profiles saved before per-user folders stay readable and listed.
            for item in profiles_store.list_profiles(root=self.profiles_root()):
                if item.get("source") != "builtin" and item["id"] not in known:
                    items.append(item)
            self.send_json({"profiles": items})
            return
        if len(segments) != 3:
            self.error("Маршрут не найден.", 404)
            return
        name = segments[2]
        if not self.profile_roots(name):
            return
        try:
            profile, source, _ = self.load_profile(name)
        except KeyError:
            self.error("Профиль не найден.", 404)
            return
        except ValueError as error:
            self.error(str(error), 422)
            return
        self.send_json({
            "profile": profile,
            "source": source,
            "summary": profiles_store.summary(profile, source=source, name=name),
        })

    def profiles_put(self, path):
        segments = [unquote(part) for part in path.split("/") if part]
        if len(segments) != 3:
            self.error("Маршрут не найден.", 404)
            return
        name = segments[2]
        if name in profiles_store.BUILTINS:
            self.error("Встроенный профиль нельзя перезаписать; сохраните копию под другим id.", 409)
            return
        session = self.current_session()
        if session is None:
            self.error("Профиль не найден.", 404)
            return
        data = self.payload(profiles_store.MAX_BYTES, "application/json")
        if data is None:
            return
        try:
            document = json.loads(data)
        except (UnicodeError, json.JSONDecodeError):
            self.error("Тело должно быть корректным JSON в UTF-8.", 400)
            return
        try:
            saved = profiles_store.save(name, document, root=self.profile_root_for(session))
        except ValueError as error:
            self.error(str(error), 422)
            return
        self.send_json(saved)

    def profiles_delete(self, path):
        segments = [unquote(part) for part in path.split("/") if part]
        if len(segments) != 3:
            self.error("Маршрут не найден.", 404)
            return
        name = segments[2]
        session = self.current_session()
        if session is None:
            self.error("Профиль не найден.", 404)
            return
        try:
            profiles_store.delete(name, root=self.profile_root_for(session))
        except KeyError:
            self.error("Профиль не найден.", 404)
            return
        except ValueError as error:
            self.error(str(error), 409)
            return
        self.send_json({"deleted": name})

    # --- разбор PDF без LLM ---------------------------------------------
    def parse_post(self, session=None):
        if session is None:
            session = self.current_session()
            if session is None:
                self.error("Требуется вход.", 401)
                return
        data = self.payload(PARSE_MAX_BYTES, "application/pdf")
        if data is None:
            return
        name = (self.headers.get("X-Profile") or "solar").strip()
        parser = (self.headers.get("X-Parser") or "auto").strip().lower()
        if parser == "auto":
            parser = "docling"
        ocr = (self.headers.get("X-OCR") or "auto").strip().lower()
        raw_charts = (self.headers.get("X-Charts") or "").strip().lower()
        llm = (self.headers.get("X-LLM") or "off").strip().lower() or "off"
        if parser not in {"auto", "vision", "pdfplumber", "docling"}:
            self.error("Парсер должен быть auto, vision, pdfplumber или docling.", 400)
            return
        if parser == "vision" and (not os.environ.get("OPENROUTER_API_KEY") or not os.environ.get("OPENROUTER_VISION_MODEL")):
            self.error("Для явного vision-разбора задайте OPENROUTER_API_KEY и OPENROUTER_VISION_MODEL на сервере.", 503)
            return
        if ocr not in {"auto", "full", "off"}:
            self.error("OCR должен быть auto, full или off.", 400)
            return
        if parser == "pdfplumber" and ocr != "auto":
            self.error("OCR full/off требует парсер docling.", 400)
            return
        if raw_charts not in {"", "0", "1", "false", "true", "no", "yes", "off", "on"}:
            self.error("X-Charts должен быть 1 или 0.", 400)
            return
        charts = raw_charts in {"1", "true", "yes", "on"}
        if charts and parser != "docling":
            self.error("Извлечение данных графиков требует парсер docling.", 400)
            return
        if llm not in PARSE_LLM_MODES:
            self.error("X-LLM должен быть off, mock или api.", 400)
            return
        if llm == "api":
            status = llm_api_status()
            if not status["enabled"]:
                self.error(
                    "LLM api отключён на сервере: задайте VINITI_PARSE_LLM=api.", 403)
                return
            if not status["configured"]:
                self.error(
                    "LLM api включён, но не настроен: нужны OPENAI_API_KEY и OPENAI_MODEL.", 503)
                return
        try:
            if not self.profile_roots(name):
                return
            _, source, profile_path = self.load_profile(name)
        except KeyError:
            self.error("Профиль не найден.", 404)
            return
        except ValueError as error:
            self.error(str(error), 422)
            return
        filename = Path(unquote(self.headers.get("X-Filename", "article.pdf")).replace("\\", "/")).name
        if not filename or len(filename) > 240:
            self.error("Некорректное имя файла.", 400)
            return
        run_id = uuid.uuid4().hex[:16]
        folder = self.parses_root() / run_id
        try:
            folder.mkdir(parents=True, exist_ok=False)
            (folder / "input.pdf").write_bytes(data)
        except OSError:
            log.exception("Не удалось создать каталог разбора")
            self.error("Не удалось сохранить PDF для разбора.", 500)
            return
        session = self.current_session()
        job = {
            "id": run_id, "status": "queued", "profile": name, "profile_source": source,
            "parser": parser, "ocr": ocr if parser == "docling" else "off", "charts": charts,
            "llm": llm,
            "filename": filename, "sha256": sha256(data).hexdigest(), "bytes": len(data),
            "created_at": round(time(), 3), "updated_at": round(time(), 3),
            "owner_user_id": session["user"]["id"],
        }
        self.write_job(folder, job)
        diagnostics.event(log, "parse.queued", job_id=run_id,
                          parser=parser, ocr=job["ocr"], charts=charts,
                          profile=name, bytes=len(data))
        threading.Thread(
            target=self.run_parse,
            args=(folder, profile_path, parser, ocr, charts, llm),
            daemon=True,
        ).start()
        self.send_json(job, 202)

    def read_job(self, folder):
        try:
            return json.loads((folder / "job.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def write_job(self, folder, job):
        temporary = folder / f".job-{uuid.uuid4().hex}.json"
        temporary.write_text(json.dumps(job, ensure_ascii=False, allow_nan=False), encoding="utf-8")
        os.replace(temporary, folder / "job.json")

    def run_parse(self, folder, profile_path, parser, ocr, charts, llm="off"):
        slot = getattr(self.server, "docling_slot", None) if parser == "docling" else None
        slots = getattr(self.server, "parse_slots", None)
        with diagnostics.context(job_id=folder.name, parser=parser):
            try:
                if slots is not None:
                    slots.acquire()
                if slot is not None:
                    slot.acquire()
                started = round(time(), 3)
                job = self.read_job(folder)
                job.update(status="running", started_at=started, updated_at=started)
                self.write_job(folder, job)
                diagnostics.event(log, "parse.running",
                                  ocr=job.get("ocr"), charts=job.get("charts"),
                                  profile=job.get("profile"))
                interpreter = sys.executable
                if parser == "docling":
                    interpreter = os.environ.get("VINITI_DOCLING_PYTHON") or interpreter
                publication_id = Path(job.get("filename", "article.pdf")).stem or "article"
                command = [
                    interpreter, "-X", "utf8", str(paths.PARSE_SCRIPT), str(folder / "input.pdf"),
                    "--profile", str(profile_path), "--publication-id", publication_id,
                    "-o", str(folder / "bundle"), "--parser", parser,
                ]
                if parser == "docling":
                    command += ["--ocr", ocr]
                    if charts:
                        command.append("--charts")
                if llm == "mock":
                    command += ["--llm-shell", "mock"]
                elif llm == "api":
                    # The key and model travel through the inherited environment,
                    # never through argv, job.json or parse.log.
                    command += ["--llm-shell", "api"]
                child_env = {
                    **os.environ,
                    "HF_HOME": os.environ.get("HF_HOME") or str(paths.DATA_DIR / "solar" / "docling-models"),
                }
                if llm == "api" and PARSE_LLM_NETWORK:
                    # Keep the worker's explicit network opt-in aligned with the
                    # admission check, including embedded/test HTTP servers.
                    child_env["VINITI_PARSE_LLM"] = "api"
                completed = subprocess.run(
                    command, cwd=ROOT, capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=PARSE_TIMEOUT, check=False,
                    env=child_env,
                )
                self.write_log(folder, completed)
                job = self.read_job(folder)
                if completed.returncode:
                    lines = (completed.stderr or completed.stdout or "").strip().splitlines()
                    message = lines[-1] if lines else "Разбор завершился с ошибкой."
                    job.update(status="error", error=message[:500], returncode=completed.returncode)
                    # No message text here: the subprocess stderr may quote the
                    # document, and the same string is already in job.json.
                    diagnostics.error(log, "parse.error",
                                      returncode=completed.returncode)
                else:
                    lines = (completed.stdout or "").strip().splitlines()
                    try:
                        summary = json.loads(lines[-1]) if lines else {}
                    except json.JSONDecodeError:
                        summary = {}
                    if isinstance(summary, dict):
                        summary.pop("output", None)
                    if job.get("document_id", "").startswith("solar:"):
                        recognized = json.loads((folder / "bundle" / "result.json").read_text(encoding="utf-8"))
                        self.solar_store().attach_recognition(job["document_id"], recognized)
                    job.update(status="ok", summary=summary)
                job.update(updated_at=round(time(), 3), elapsed_seconds=round(time() - started, 3))
                self.write_job(folder, job)
                if job.get("status") == "ok":
                    diagnostics.event(log, "parse.ok",
                                      duration_ms=int(job["elapsed_seconds"] * 1000),
                                      pages=job.get("summary", {}).get("pages", 0),
                                      sources=job.get("summary", {}).get("sources", 0),
                                      candidates=job.get("summary", {}).get("candidates", 0),
                                      warnings=job.get("summary", {}).get("warnings", 0))
            except subprocess.TimeoutExpired:
                job = self.read_job(folder)
                job.update(status="error", error=f"Разбор превысил лимит {PARSE_TIMEOUT} с.",
                           updated_at=round(time(), 3))
                self.write_job(folder, job)
                diagnostics.error(log, "parse.timeout",
                                  timeout_s=PARSE_TIMEOUT)
            except OSError as error:
                job = self.read_job(folder)
                job.update(status="error", error=f"Не удалось запустить разбор: {error}"[:500],
                           updated_at=round(time(), 3))
                self.write_job(folder, job)
                diagnostics.exception(log, "parse.spawn_failed",
                                      kind=type(error).__name__)
            except Exception:
                # One record, not two: the traceback is already in the event.
                diagnostics.exception(log, "parse.failed")
                job = self.read_job(folder)
                job.update(status="error", error="Внутренняя ошибка разбора; подробности в логе сервера.",
                           updated_at=round(time(), 3))
                self.write_job(folder, job)
            finally:
                if slot is not None:
                    slot.release()
                if slots is not None:
                    slots.release()

    def write_log(self, folder, completed):
        text = (completed.stdout or "") + ("\n" + completed.stderr if completed.stderr else "")
        try:
            (folder / "parse.log").write_text(text[-64000:], encoding="utf-8")
        except OSError:
            log.warning("Не удалось записать parse.log")

    def start_catalog_parse(self, document_id, filename, data):
        """Run recognition independently of scientific result extraction."""
        run_id = uuid.uuid4().hex[:16]
        folder = self.parses_root() / run_id
        folder.mkdir(parents=True, exist_ok=False)
        (folder / "input.pdf").write_bytes(data)
        job = {"id": run_id, "status": "queued", "profile": "solar", "profile_source": "builtin",
               "document_id": document_id, "parser": "docling", "ocr": "auto", "charts": False,
               "filename": Path(filename.replace("\\", "/")).name,
               "sha256": sha256(data).hexdigest(), "bytes": len(data),
               "created_at": round(time(), 3), "updated_at": round(time(), 3)}
        self.write_job(folder, job)
        threading.Thread(target=self.run_parse,
                         args=(folder, ROOT / "profiles" / "solar.json", "docling", "auto", False),
                         daemon=True).start()
        return run_id

    def parse_get(self, path, session=None):
        if session is None:
            session = self.current_session()
        if session is None:
            self.error("Разбор не найден.", 404)
            return
        segments = [unquote(part) for part in path.split("/") if part]
        if len(segments) < 3 or not PARSE_ID.fullmatch(segments[2]):
            self.error("Разбор не найден.", 404)
            return
        folder = self.parses_root() / segments[2]
        job = self.read_job(folder)
        if not job:
            self.error("Разбор не найден.", 404)
            return
        if job.get("owner_user_id") != session["user"]["id"]:
            self.error("Разбор не найден.", 404)
            return
        started = job.get("started_at", job.get("created_at", 0))
        if job.get("status") == "running" and time() - started > PARSE_TIMEOUT + 60:
            # Не перезаписываем job на диске: рабочий процесс мог ещё завершиться.
            job.update(status="error", error="Разбор прерван: процесс не завершился за отведённое время.")
        if len(segments) == 3:
            self.send_json(job)
            return
        if segments[3] == "result":
            if job.get("status") != "ok":
                self.error("Результат ещё не готов.", 409)
                return
            try:
                data = (folder / "bundle" / "result.json").read_bytes()
            except OSError:
                self.error("Файл результата не найден.", 404)
                return
            self.send_data(data, "application/json; charset=utf-8")
            return
        if segments[3] == "llm":
            try:
                data = (folder / "bundle" / "llm" / "manifest.json").read_bytes()
            except OSError:
                self.error("Артефакты LLM-оболочки не найдены.", 404)
                return
            self.send_data(data, "application/json; charset=utf-8")
            return
        if segments[3] == "file" and len(segments) > 4:
            self.parse_file(folder / "bundle", segments[4:])
            return
        self.error("Маршрут не найден.", 404)

    def parse_file(self, base, parts):
        relative = PurePosixPath(*parts)
        allowed = relative.parts in (
            {("result.json",), ("parser.json",), ("candidates.csv",)}
            | {("llm", name) for name in llm_shell.ARTIFACT_NAMES}
        )
        if not allowed and (
            len(relative.parts) != 3 or relative.parts[0] != "assets"
            or relative.parts[1] not in PARSE_KINDS
            or not PARSE_FILE.fullmatch(relative.parts[2])
            or relative.suffix not in {".png", ".csv"}
        ):
            self.error("Файл не найден.", 404)
            return
        base = base.resolve()
        try:
            path = (base / relative).resolve()
            path.relative_to(base)
        except ValueError:
            self.error("Файл не найден.", 404)
            return
        if not path.is_file():
            self.error("Файл не найден.", 404)
            return
        media = {".json": "application/json; charset=utf-8", ".csv": "text/csv; charset=utf-8",
                 ".png": "image/png"}.get(path.suffix, "application/octet-stream")
        self.send_data(path.read_bytes(), media)

    def csv_slice(self, area, spec, columns, rows):
        buffer = io.StringIO(newline="")
        writer = csv.writer(buffer, lineterminator=spec["newline"])
        writer.writerow(columns)
        for row in rows:
            writer.writerow(["" if row[name] is None else str(row[name]) for name in columns])
        data = buffer.getvalue().encode("utf-8")
        return b"\xef\xbb\xbf" + data if spec["bom"] else data

    def slice_arguments(self, query, area):
        table = (query.get("table") or ["results"])[0]
        spec = storage.load_passport()["areas"][area]
        meta = spec["tables"].get(table)
        if meta is None:
            self.error(f"Нет таблицы {table} в области {area}. Доступны: {', '.join(spec['tables'])}", 400)
            return None
        chosen = [x.strip() for x in ",".join(query.get("col") or []).split(",") if x.strip()]
        chosen = chosen or list(meta["columns"])
        unknown = [x for x in chosen if x not in meta["columns"]]
        if unknown:
            self.error(f"Нет таких полей: {', '.join(unknown)}", 400)
            return None
        for required in result_export.ALWAYS_KEPT:
            if required in meta["columns"] and required not in chosen:
                chosen.append(required)
        try:
            limit = max(0, int((query.get("limit") or ["0"])[0]))
        except ValueError:
            self.error("limit должен быть числом.", 400)
            return None
        return {"table": table, "spec": spec, "meta": meta, "columns": chosen,
                "wheres": query.get("where") or [],
                "order": (query.get("order") or [""])[0], "limit": limit}

    def issue_arguments(self, query, area):
        columns = storage.load_passport()["areas"][area]["tables"]["issues"]["columns"]
        filters = {}
        for name, values in query.items():
            if name in ("area", "limit"):
                continue
            if name not in columns:
                self.error(f"Нет такого фильтра {name}. Допустимы: {', '.join(columns)}, limit.", 400)
                return None
            filters[name] = values[0]
        try:
            limit = max(0, min(int((query.get("limit") or ["100"])[0]), 1000))
        except ValueError:
            self.error("limit должен быть числом.", 400)
            return None
        return filters, limit

    def stats_document(self, conn):
        passport = storage.load_passport()
        result = {}
        for area, spec in passport["areas"].items():
            tables = {name: conn.execute(
                f"SELECT COUNT(*) FROM {storage.quote(storage.table_name(area, name))} WHERE area = ?", (area,)
            ).fetchone()[0] for name in spec["tables"]}
            jobs = {status: 0 for status in JOB_STATUSES}
            for status, count in conn.execute("SELECT status, COUNT(*) FROM jobs WHERE area=? GROUP BY status", (area,)):
                jobs[status] = count
            total, pages, chars = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(page_count),0), COALESCE(SUM(text_chars),0) FROM documents WHERE area=?", (area,)
            ).fetchone()
            statuses = {status: count for status, count in conn.execute(
                "SELECT parser_status, COUNT(*) FROM documents WHERE area=? GROUP BY parser_status", (area,))}
            result[area] = {"publications": tables.get("publications", 0), "tables": tables,
                            "jobs": jobs, "documents": {"total": total, "parsed": sum(v for k, v in statuses.items() if k not in ("", "pending", "error")), "pages": pages, "chars": chars, "parser_status": statuses}}
        return {"areas": result}

    # --- хранилища ------------------------------------------------------
    def rag_store(self) -> RagStore:
        store = getattr(self.server, "rag_store", None)
        if store is None:
            # В тестах Handler создают без rag_store: кладём базу рядом
            # с библиотекой, чтобы не писать в рабочий каталог репозитория.
            base = getattr(self.server, "library", None)
            path = Path(base.path).parent / "rag.sqlite3" if base else paths.DATA_DIR / "rag.sqlite3"
            store = RagStore(path)
            self.server.rag_store = store
        return store

    def solar_store(self) -> SolarStore:
        store = getattr(self.server, "solar_store", None)
        if store is None:
            base = getattr(self.server, "library", None)
            path = Path(base.path).parent / "solar.sqlite3" if base else paths.DATA_DIR / "solar.sqlite3"
            store = SolarStore(path)
            self.server.solar_store = store
        return store

    # --- общие помощники ------------------------------------------------
    @staticmethod
    def _int(query, name, default, minimum=0, maximum=None):
        raw = (query.get(name) or [""])[0]
        if raw == "":
            return default
        try:
            value = int(raw)
        except (TypeError, ValueError) as error:
            raise ValueError(f"Параметр {name} должен быть целым числом.") from error
        if value < minimum:
            raise ValueError(f"Параметр {name} не может быть меньше {minimum}.")
        return min(value, maximum) if maximum is not None else value

    @staticmethod
    def _page(query, default=50, maximum=200):
        return Handler._int(query, "limit", default, 1, maximum), Handler._int(query, "offset", 0, 0)

    # --- GET ------------------------------------------------------------
    def do_GET(self):
        parts = urlsplit(self.path)
        path = parts.path
        query = parse_qs(parts.query)
        single = {key: values[0] for key, values in query.items()}

        if path == "/api/auth/me":
            session = self.current_session()
            if session is None:
                self.send_json({"user": None})
            else:
                self.send_json({"user": session["user"], "csrf_token": session["csrf_token"], "expires_at": session["expires_at"]})
            return
        if path == "/api/auth/csrf":
            session = self.current_session()
            if session is None:
                self.error("Требуется вход.", 401)
            else:
                self.send_json({"csrf_token": session["csrf_token"]})
            return

        target = static_file(path)
        if target is not None:
            self.send_data(target.read_bytes(), STATIC_TYPES[target.suffix.lower()])
            return

        area_raw = (query.get("area") or [None])[0]
        if path == "/api/passport":
            passport = storage.load_passport()
            if area_raw is None:
                self.send_json(passport)
            else:
                area = self.pick_area(area_raw)
                if area is not None:
                    self.send_json({"version": passport["version"], "area": area, **passport["areas"][area]})
            return

        if path in ("/api/results", "/api/results.csv"):
            area = self.pick_area(area_raw)
            if area is None:
                return
            args = self.slice_arguments(query, area)
            if args is None:
                return
            conn = self.data_connection()
            if conn is None:
                return
            try:
                rows = result_export.select_rows(conn, area, args["table"], args["meta"], args["columns"], args["wheres"], args["order"], args["limit"])
            except SystemExit as error:
                self.error(str(error), 400)
                return
            finally:
                conn.close()
            if path.endswith(".csv"):
                self.send_data(self.csv_slice(area, args["spec"], args["columns"], rows), "text/csv; charset=utf-8", Content_Disposition="attachment")
            else:
                self.send_json({"area": area, "table": args["table"], "columns": args["columns"], "count": len(rows), "rows": [{name: row[name] for name in args["columns"]} for row in rows]})
            return

        if path == "/api/issues":
            area = self.pick_area(area_raw)
            if area is None:
                return
            args = self.issue_arguments(query, area)
            if args is None:
                return
            filters, limit = args
            conn = self.data_connection()
            if conn is None:
                return
            sql = f"SELECT * FROM {storage.quote(storage.table_name(area, 'issues'))} WHERE area=?"
            params = [area]
            for column, value in filters.items():
                sql += f" AND {storage.quote(column)}=?"
                params.append(value)
            sql += " ORDER BY _line LIMIT ?"
            params.append(limit)
            try:
                rows = conn.execute(sql, params).fetchall()
            finally:
                conn.close()
            self.send_json({"area": area, "count": len(rows), "issues": [dict(row) for row in rows]})
            return

        if path == "/api/stats":
            conn = self.data_connection()
            if conn is None:
                return
            try:
                self.send_json(self.stats_document(conn))
            finally:
                conn.close()
            return

        if path == "/api/profiles" or path.startswith("/api/profiles/"):
            self.profiles_get(path)
            return

        if path == "/api/llm":
            self.send_json(llm_api_status())
            return

        if path.startswith("/api/parse/"):
            self.parse_get(path, self.current_session())
            return

        if path == "/api/documents" or path.startswith("/api/documents/"):
            self.documents_get(path, query, single)
            return

        if path == "/api/jobs":
            self.jobs(single)
            return

        if path == "/api/papers":
            session = self.require_user(csrf=False)
            if session is None:
                return
            store = self.auth_store()
            allowed = store.owned_documents("rag", session["user"]["id"])
            items = [item for item in self.server.library.list()
                     if item.get("id") in allowed or self.shared_document(item.get("id"))]
            self.send_json(items)
            return

        if path.startswith("/api/papers/"):
            self.legacy_paper(path, parts.query)
            return

        self.error("Маршрут не найден.", 404)

    def documents_get(self, path, query, single):
        segments = [unquote(part) for part in path.split("/") if part]
        if segments == ["api", "documents"]:
            try:
                limit, offset = self._page(query)
            except ValueError as error:
                self.error(str(error), 400)
                return
            items = self.rag_store().list(single.get("track", "rag"))
            if single.get("track", "rag") == "solar":
                items = self.solar_store().list()
            items = self.visible_documents(single.get("track", "rag"), items)
            q = (single.get("q") or "").strip().lower()
            if q:
                items = [
                    doc for doc in items
                    if q in " ".join([
                        str(doc.get("publication_id", "")), str(doc.get("title", "")),
                        str(doc.get("year", "")),
                        # solar stores authors as one string, RAG as a list
                        doc.get("authors") if isinstance(doc.get("authors"), str)
                        else " ".join(doc.get("authors") or []),
                    ]).lower()
                ]
            status = single.get("status")
            if status:
                items = [doc for doc in items if doc.get("status") == status]
            sort = single.get("sort", "")
            key = {
                "status": lambda doc: doc.get("status", ""),
                "issues": lambda doc: -int(doc.get("issues_count", 0)),
                "results": lambda doc: -int(doc.get("results_count", 0)),
            }.get(sort, lambda doc: doc.get("publication_id", ""))
            items.sort(key=key)
            self.send_json({"total": len(items), "items": items[offset: offset + limit]})
            return

        if len(segments) < 3 or segments[:2] != ["api", "documents"]:
            self.error("Маршрут не найден.", 404)
            return

        document_id = segments[2]
        store = self.auth_store()
        if store is not None:
            track = "solar" if document_id.startswith("solar:") else "rag"
            if not self.owns_document(track, document_id):
                self.error("Документ не найден.", 404)
                return
        if document_id.startswith("solar:"):
            self.solar_documents_get(document_id, segments[3:], query, single)
            return
        envelope = self.rag_store().get(document_id)
        if envelope is None:
            self.error("Документ не найден.", 404)
            return

        resource = segments[3] if len(segments) > 3 else ""
        key = segments[4] if len(segments) > 4 else ""

        if not resource:
            self.send_json({name: value for name, value in envelope.items() if name not in HEAVY_FIELDS})
            return

        if resource == "facets":
            self.send_json(facets(envelope))
            return

        if resource == "pdf":
            pdf = self.rag_store().pdf(document_id)
            if pdf is None:
                self.error("PDF не найден.", 404)
                return
            self.send_data(pdf, "application/pdf", Content_Disposition="inline")
            return

        if resource == "results":
            try:
                limit, offset = self._page(query)
            except ValueError as error:
                self.error(str(error), 400)
                return
            rows = filter_results(envelope, single)
            self.send_json({"total": len(rows), "items": rows[offset: offset + limit]})
            return

        if resource == "issues":
            try:
                limit, offset = self._page(query, default=200, maximum=500)
            except ValueError as error:
                self.error(str(error), 400)
                return
            issues = envelope.get("issues", [])
            self.send_json({"total": len(issues), "items": issues[offset: offset + limit]})
            return

        if resource == "sources":
            try:
                limit = self._int(query, "limit", 200, 1, 500)
            except ValueError as error:
                self.error(str(error), 400)
                return
            items = list_sources(envelope, single)
            self.send_json({"total": len(items), "items": items[:limit]})
            return

        if resource == "entities":
            kind = single.get("type", "configurations")
            items = envelope.get("entities", {}).get(kind, [])
            self.send_json({"total": len(items), "items": items})
            return

        if resource == "anchors" and (key.endswith(".json") or key.endswith(".png")):
            try:
                source_id = int(key.rsplit(".", 1)[0])
            except ValueError:
                self.error("Идентификатор источника должен быть числом.", 400)
                return
            anchor = envelope.get("anchors", {}).get(str(source_id))
            if anchor is None:
                self.error("Источник не найден.", 404)
                return
            if key.endswith(".json"):
                self.send_json(anchor)
                return
            pdf = self.rag_store().pdf(document_id)
            if pdf is None:
                self.error("PDF не найден.", 404)
                return
            try:
                png = render_anchor_png(
                    pdf,
                    anchor.get("page", 1),
                    anchor.get("bbox"),
                    bboxes=anchor.get("bboxes"),
                )
            except (IndexError, TypeError, ValueError) as error:
                # A stale or malformed anchor is data, not a server error.
                # In particular, never let page=0/out-of-range reach the
                # request loop as an unhandled exception.
                self.error(str(error), 400)
                return
            except (ImportError, RuntimeError):
                log.exception("PNG preview rendering failed")
                self.error("Не удалось создать PNG-превью. Откройте PDF напрямую.", 503)
                return
            self.send_data(png, "image/png")
            return

        if resource == "export.json":
            self.send_data(
                json.dumps(envelope, ensure_ascii=False).encode(),
                "application/json; charset=utf-8",
                Content_Disposition=f"attachment; filename=\"{document_id.replace(':', '-')}.json\"",
            )
            return

        if resource == "export.csv":
            self.send_data(
                export_csv(envelope),
                "text/csv; charset=utf-8",
                Content_Disposition=f"attachment; filename=\"{document_id.replace(':', '-')}.csv\"",
            )
            return

        self.error("Маршрут не найден.", 404)

    def solar_documents_get(self, document_id, resources, query, single):
        envelope = self.solar_store().get(document_id)
        if envelope is None:
            self.error("Документ не найден.", 404)
            return
        resource = resources[0] if resources else ""
        key = resources[1] if len(resources) > 1 else ""
        if not resource:
            self.send_json({name: value for name, value in envelope.items() if name not in HEAVY_FIELDS})
        elif resource == "facets":
            self.send_json({"metrics": [], "ks": [], "datasets": [], "configs": [], "statuses": []})
        elif resource == "results":
            try:
                limit, offset = self._page(query)
            except ValueError as error:
                self.error(str(error), 400)
                return
            # The shared filter applies the search and the "issues only" switch;
            # without it the catalog UI showed every row for any query.
            items = filter_results(envelope, single)
            self.send_json({"total": len(items), "items": items[offset:offset + limit]})
        elif resource == "issues":
            try:
                limit, offset = self._page(query, default=200, maximum=500)
            except ValueError as error:
                self.error(str(error), 400)
                return
            items = envelope.get("issues", [])
            self.send_json({"total": len(items), "items": items[offset:offset + limit]})
        elif resource == "sources":
            try:
                limit = self._int(query, "limit", 200, 1, 500)
            except ValueError as error:
                self.error(str(error), 400)
                return
            items = solar_sources(envelope, single.get("kind", ""),
                                  (single.get("q") or "").strip().lower())
            self.send_json({"total": len(items), "items": items[:limit]})
        elif resource == "entities":
            self.send_json({"total": 0, "items": []})
        elif resource == "anchors" and (key.endswith(".json") or key.endswith(".png")):
            try:
                source_id = int(key.rsplit(".", 1)[0])
            except ValueError:
                self.error("Идентификатор источника должен быть числом.", 400)
                return
            anchor = envelope.get("anchors", {}).get(str(source_id))
            if anchor is None:
                self.error("Источник не найден.", 404)
                return
            if key.endswith(".json"):
                self.send_json(anchor)
                return
            data = self.solar_store().pdf(document_id)
            if data is None:
                self.error("PDF не найден.", 404)
                return
            try:
                png = render_anchor_png(
                    data,
                    anchor.get("page", 1),
                    anchor.get("bbox"),
                    bboxes=anchor.get("bboxes"),
                )
            except (IndexError, TypeError, ValueError) as error:
                # A stale or malformed anchor is data, not a server error.
                # page=0/None/out-of-range must answer 400, never crash the
                # request loop.
                self.error(str(error), 400)
                return
            except (ImportError, RuntimeError):
                log.exception("Solar PNG preview rendering failed")
                self.error("Не удалось создать PNG-превью. Откройте PDF напрямую.", 503)
                return
            self.send_data(png, "image/png")
        elif resource == "export.json":
            self.send_data(json.dumps(envelope, ensure_ascii=False).encode(), "application/json; charset=utf-8", Content_Disposition=f"attachment; filename=\"{document_id.replace(':', '-')}.json\"")
        elif resource == "export.csv":
            data = self.solar_store().export_csv(document_id)
            if data is None:
                self.error("Документ не найден.", 404)
            else:
                self.send_data(data, "text/csv; charset=utf-8", Content_Disposition=f"attachment; filename=\"{document_id.replace(':', '-')}.csv\"")
        elif resource == "pdf":
            data = self.solar_store().pdf(document_id)
            if data is None:
                self.error("PDF не найден.", 404)
            else:
                self.send_data(data, "application/pdf", Content_Disposition="inline")
        else:
            self.error("Маршрут не найден.", 404)

    def jobs(self, single):
        track = single.get("track", "rag")
        docs = self.solar_store().list() if track == "solar" else self.rag_store().list(track)
        docs = self.visible_documents(track, docs)
        done = sum(doc.get("status") == "done" for doc in docs)
        partial = sum(doc.get("status") == "partial" for doc in docs)
        failed = sum(doc.get("status") == "failed" for doc in docs)
        total = len(docs)
        self.send_json({
            "state": "done" if total and done + partial + failed >= total else "running",
            "total": total,
            "done": done,
            "partial": partial,
            "failed": failed,
            "items": [
                {
                    "document_id": doc.get("document_id"),
                    "status": doc.get("status"),
                    "error": doc.get("error", ""),
                }
                for doc in docs
            ],
        })

    def legacy_paper(self, path, raw_query):
        match = PAPER.fullmatch(path)
        if match is None:
            self.error("Документ не найден.", 404)
            return
        paper_id = match.group(1)
        # Without an auth store the server runs anonymously (tests, old demo),
        # and ownership is not enforced anywhere else; keep that mode readable.
        if self.auth_store() is not None and not self.owns_document("rag", paper_id):
            self.error("Документ не найден.", 404)
            return
        try:
            paper_id, resource, key = match.groups()
            if not resource:
                self.send_json(
                    self.server.library.get(paper_id),
                    Content_Disposition="attachment" if raw_query == "download" else "inline",
                )
                return
            document, data = self.server.library.get(paper_id, with_pdf=True)
            if resource == "pdf" and key is None:
                self.send_data(data, "application/pdf", Content_Disposition="attachment")
                return
            if resource == "pdf" or key is None:
                raise KeyError(path)
            index = int(key) - 1
            if resource == "sources":
                anchor = document["sources"][index]
            else:
                fact = document["facts"][index]
                anchor = fact["header" if resource == "headers" else "evidence"]
            if anchor is None:
                raise KeyError(path)
        except (KeyError, IndexError, AttributeError):
            self.send_json({"error": "Источник не найден."}, 404)
            return
        self.send_data(render(data, anchor["page"], anchor["bbox"]), "image/png")

    # --- POST -----------------------------------------------------------
    def do_POST(self):
        path = urlsplit(self.path).path
        if path == "/api/auth/register":
            self.auth_register(); return
        if path == "/api/auth/login":
            self.auth_login(); return
        if path == "/api/auth/logout":
            self.auth_logout(); return
        if path == "/api/profile/assist":
            if self.require_user() is None:
                return
            raw = self.payload(64 * 1024, "application/json")
            if raw is None:
                return
            try:
                result = profile_assistant.suggest(json.loads(raw))
            except (ValueError, UnicodeError, json.JSONDecodeError) as error:
                self.error(str(error), 400)
            except RuntimeError as error:
                self.error(str(error), 503)
            else:
                self.send_json(result)
            return
        if path == "/api/profile/source":
            if self.require_user() is None: return
            data = self.payload(MAX_BYTES, "application/pdf")
            if data is not None:
                try:
                    if not data.startswith(b"%PDF-"):
                        raise ValueError("Invalid PDF")
                    parser = (self.headers.get("X-Parser") or "pdfplumber").strip().lower()
                    if parser in {"auto", "vision"}:
                        raise ValueError("Автоматический vision-разбор доступен через фоновый POST /api/parse.")
                    elif parser == "pdfplumber":
                        result = extract(data)
                    else:
                        raise ValueError("Неизвестный парсер.")
                except ValueError as error:
                    self.error(str(error), 422)
                except (PDFSyntaxError, PDFPasswordIncorrect):
                    self.error("Не удалось прочитать PDF. Проверьте формат, пароль и число страниц.", 422)
                except Exception:
                    log.exception("Manual profile PDF extraction failed")
                    self.error("Ошибка обработки PDF.", 500)
                else:
                    self.send_json(result)
            return
        if path in {"/api/documents", "/api/demo"}:
            session = self.require_user()
            if session is not None:
                self.documents_post(path, session)
            return
        if path == "/api/documents/stream":
            session = self.require_user()
            if session is not None:
                self.documents_stream(session)
            return
        if path.startswith("/api/profiles/"):
            if self.require_user() is not None:
                self.profiles_put(path)
            return
        if path == "/api/parse":
            session = self.require_user()
            if session is not None:
                self.parse_post(session)
            return
        if path == "/api/papers":
            if self.require_user() is not None:
                self.legacy_upload()
            return
        self.send_json({"error": "Маршрут не найден."}, 404)

    def do_PUT(self):
        path = urlsplit(self.path).path
        if path.startswith("/api/profiles/"):
            if self.require_user() is not None:
                self.profiles_put(path)
            return
        self.send_json({"error": "Маршрут не найден."}, 404)

    def do_DELETE(self):
        path = urlsplit(self.path).path
        if path.startswith("/api/profiles/"):
            if self.require_user() is not None:
                self.profiles_delete(path)
            return
        self.send_json({"error": "Маршрут не найден."}, 404)

    def documents_stream(self, session):
        track = self.headers.get("X-Track", "rag").strip().lower()
        if track not in AREAS:
            self.error("Недопустимая область. Допустимы: rag, solar.", 400)
            return
        limit = SOLAR_MAX_BYTES if track == "solar" else RAG_MAX_BYTES
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if not 0 < length <= limit or self.headers.get_content_type() != "application/pdf":
            self.error(f"Нужен PDF размером до {limit // (1024 * 1024)} МиБ.", 400)
            return
        data = self.rfile.read(length)
        if not data.startswith(b"%PDF-"):
            self.error("Файл не является PDF.", 422)
            return
        name = unquote(self.headers.get("X-Filename", "article.pdf"))
        started = time()
        diagnostics.event(log, "document.ingest.start", area=track,
                          bytes=len(data), stream=True)
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.close_connection = True

        def emit(completed, label, result=None, error=None):
            message = {"completed": completed, "total": 4, "label": label}
            if result is not None: message["result"] = result
            if error is not None: message["error"] = error
            self.wfile.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
            self.wfile.flush()

        try:
            emit(0, "PDF загружен")
            if track == "solar":
                result = self.solar_store().ingest(name, data, progress=emit)
            else:
                result = self.rag_store().ingest(name, data, track=track, progress=emit)
            document_id = result.get("document_id") if isinstance(result, dict) else None
            if not self.claim_uploaded_document(track, document_id, session):
                diagnostics.warning(log, "document.ingest.conflict",
                                    area=track, document_id=document_id or "")
                emit(0, "Документ уже загружен другим пользователем.",
                     error="Документ с таким содержимым уже загружен другим пользователем.")
            else:
                counts = (result or {}).get("counts") or {}
                diagnostics.event(log, "document.ingest.done", area=track,
                                  duration_ms=int((time() - started) * 1000),
                                  document_id=document_id or "", stream=True,
                                  results=counts.get("results", 0),
                                  issues=counts.get("issues", 0))
                emit(4, "Обработка завершена", result=result)
        except ValueError as error:
            diagnostics.error(log, "document.ingest.rejected",
                              area=track, kind="ValueError")
            emit(0, "Ошибка обработки", error=str(error))
        except Exception:
            # One record, not two: the traceback is already in the event.
            diagnostics.exception(log, "document.ingest.failed",
                                  area=track, stream=True)
            emit(0, "Ошибка обработки", error="Ошибка обработки PDF. Подробности в консоли сервера.")

    def documents_post(self, path, session):
        started = time()
        track = self.headers.get("X-Track", "rag").strip().lower()
        max_bytes = SOLAR_MAX_BYTES if track == "solar" else RAG_MAX_BYTES
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            length = 0
        if path == "/api/documents" and length == 0:
            self.error(f"Нужен PDF размером до {max_bytes // (1024 * 1024)} МиБ.", 413)
            return
        if length > max_bytes:
            self.error(f"Файл больше {max_bytes // (1024 * 1024)} МиБ.", 413)
            return
        if path == "/api/documents":
            if self.headers.get_content_type() != "application/pdf":
                self.error("Ожидается application/pdf.", 415)
                return
            data = self.rfile.read(length)
            name = unquote(self.headers.get("X-Filename", "article.pdf"))
            diagnostics.event(log, "document.ingest.start", area=track,
                              bytes=len(data), stream=False)
            try:
                if track == "solar":
                    summary = self.solar_store().ingest(name, data)
                    recognition = summary.get("recognition", {})
                    if recognition.get("status") != "done" or recognition.get("revision") != RECOGNITION_REVISION:
                        summary["recognition_job_id"] = self.start_catalog_parse(summary["document_id"], name, data)
                elif track == "rag":
                    summary = self.rag_store().ingest(name, data, track=track)
                else:
                    self.error("Недопустимая область. Допустимы: rag, solar.", 400)
                    return
            except ValueError as error:
                diagnostics.error(log, "document.ingest.rejected",
                                  area=track, kind="ValueError")
                self.error(str(error), 422)
                return
            except Exception:
                diagnostics.exception(log, "document.ingest.failed",
                                      area=track)
                self.error("Ошибка обработки PDF. Подробности в консоли сервера.", 500)
                return
            if not self.claim_uploaded_document(track, summary.get("document_id"), session):
                diagnostics.warning(log, "document.ingest.conflict",
                                    area=track,
                                    document_id=summary.get("document_id", ""))
                self.error("Документ с таким содержимым уже загружен другим пользователем.", 409)
                return
            counts = summary.get("counts") or {}
            diagnostics.event(log, "document.ingest.done", area=track,
                              duration_ms=int((time() - started) * 1000),
                              document_id=summary.get("document_id", ""),
                              status=summary.get("status", ""),
                              pages=summary.get("pages", 0),
                              results=counts.get("results", 0),
                              issues=counts.get("issues", 0))
            self.send_json(summary, 201)
            return

        # Демо-статья: генерируем PDF с настоящей таблицей и прогоняем экстрактор.
        track = self.headers.get("X-Track", "rag")
        if track != "rag":
            self.error("Демо для этой области ещё не подключено.", 404)
            return
        summary = self.rag_store().ingest(
            "retrieval-demo.pdf",
            rag_demo_pdf(),
            track="rag",
            publication_id="DEMO",
            forced_id="rag:demo",
        )
        if not self.claim_uploaded_document("rag", summary.get("document_id"), session):
            self.error("Документ с таким содержимым уже загружен другим пользователем.", 409)
            return
        self.send_json(summary, 201)

    def legacy_upload(self):
        if self.headers.get_content_type() != "application/pdf":
            self.send_json({"error": "Ожидается application/pdf."}, 415)
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            size = 0
        if not 0 < size <= MAX_BYTES:
            self.send_json({"error": "Нужен PDF размером до 20 МиБ."}, 413)
            return
        data = self.rfile.read(size)
        try:
            result = self.server.library.ingest(
                unquote(self.headers.get("X-Filename", "article.pdf")), data)
        except (PDFSyntaxError, PDFPasswordIncorrect, ValueError):
            self.send_json({"error": "Не удалось прочитать PDF. Проверьте формат, пароль и лимит 150 страниц."}, 422)
            return
        except Exception:
            log.exception("PDF processing failed")
            self.send_json({"error": "Ошибка обработки PDF. Подробности в консоли сервера."}, 500)
            return
        session = self.current_session()
        document_id = sha256(data).hexdigest()
        if not self.claim_uploaded_document("rag", document_id, session):
            self.send_json({"error": "Документ с таким содержимым уже загружен другим пользователем."}, 409)
            return
        self.send_json(result, 201)



def serve_until_interrupt(server):
    """Крутит serve_forever, а Ctrl+C превращает в короткое сообщение без traceback."""
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Сервер остановлен.", flush=True)


def recover_parse_jobs(root):
    """Mark jobs owned by a previous process as interrupted, never replaying API calls."""
    root = Path(root)
    if not root.exists():
        return
    for folder in root.iterdir():
        if not folder.is_dir() or not PARSE_ID.fullmatch(folder.name):
            continue
        job_path = folder / "job.json"
        try:
            job = json.loads(job_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if job.get("status") not in {"queued", "running"}:
            continue
        job.update(status="error", error="Разбор прерван перезапуском сервера; запустите его снова.",
                   updated_at=round(time(), 3))
        temporary = folder / f".job-recover-{uuid.uuid4().hex}.json"
        try:
            temporary.write_text(json.dumps(job, ensure_ascii=False, allow_nan=False), encoding="utf-8")
            os.replace(temporary, job_path)
        except OSError:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def main():
    diagnostics.configure()
    parser = argparse.ArgumentParser(description="VINITI PDF evidence workspace")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--db", type=Path, default=paths.DATA_DIR / "papers.sqlite3")
    parser.add_argument("--data", type=Path, default=paths.DATA_DIR / "viniti.sqlite3")
    args = parser.parse_args()
    with ThreadingHTTPServer(("127.0.0.1", args.port), Handler) as server:
        server.library = Library(args.db)
        server.rag_store = RagStore(args.db)
        server.data = args.data
        server.auth = auth.Auth(args.data)
        server.profiles_dir = paths.DATA_DIR / "profiles"
        server.parse_root = paths.DATA_DIR / "parses"
        recover_parse_jobs(server.parse_root)
        server.parse_slots = threading.BoundedSemaphore(2)
        server.docling_slot = threading.Semaphore(1)
        diagnostics.event(log, "server.start", port=server.server_port,
                          db=str(args.db), data=str(args.data),
                          parse_root=str(server.parse_root))
        print(f"http://127.0.0.1:{server.server_port}", flush=True)
        serve_until_interrupt(server)


if __name__ == "__main__":
    main()
