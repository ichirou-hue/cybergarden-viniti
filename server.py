"""HTTP-сервер CyberGarden / ВИНИТИ.

Два поколения API живут рядом:

* ``/api/papers…`` — старый экран одной статьи (``library.py`` + ``rag_facts``);
* ``/api/documents…`` — контракт каталога и карточки (ADR-0008), который
  наполняет RAG-модуль ``rag`` через ``rag_api.py``.

Фронтенд в ``static/`` обслуживается с этого же адреса, поэтому CORS не нужен.
"""

import argparse
import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
from urllib.parse import parse_qs, unquote, urlsplit

from pdfminer.pdfdocument import PDFPasswordIncorrect
from pdfminer.pdfparser import PDFSyntaxError

from demo import demo_pdf as legacy_demo_pdf  # noqa: F401  (совместимость старых импортов)
from library import Library, MAX_BYTES
from pdf_source import render
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


ROOT = Path(__file__).resolve().parent
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
}
PAPER = re.compile(r"/api/papers/([0-9a-f]{64})(?:/(pdf|sources|facts|headers)(?:/([1-9][0-9]{0,8})\.png)?)?")
HEAVY_FIELDS = {"entities", "results", "issues", "anchors", "text_sources"}
log = logging.getLogger(__name__)


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

    # --- хранилища ------------------------------------------------------
    def rag_store(self) -> RagStore:
        store = getattr(self.server, "rag_store", None)
        if store is None:
            # В тестах Handler создают без rag_store: кладём базу рядом
            # с библиотекой, чтобы не писать в рабочий каталог репозитория.
            base = getattr(self.server, "library", None)
            path = Path(base.path).parent / "rag.sqlite3" if base else ROOT / "data" / "rag.sqlite3"
            store = RagStore(path)
            self.server.rag_store = store
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

        if path in ASSETS:
            file, media_type = ASSETS[path]
            self.send_data((ROOT / "static" / file).read_bytes(), media_type)
            return

        if path == "/api/documents" or path.startswith("/api/documents/"):
            self.documents_get(path, query, single)
            return

        if path == "/api/jobs":
            self.jobs(single)
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
            q = (single.get("q") or "").strip().lower()
            if q:
                items = [
                    doc for doc in items
                    if q in " ".join([
                        str(doc.get("publication_id", "")), str(doc.get("title", "")),
                        str(doc.get("year", "")), " ".join(doc.get("authors") or []),
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
            self.send_data(
                render_anchor_png(pdf, anchor.get("page", 1), anchor.get("bbox")),
                "image/png",
            )
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

    def jobs(self, single):
        docs = self.rag_store().list(single.get("track", "rag"))
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
        try:
            paper_id, resource, key = PAPER.fullmatch(path).groups()
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
        if path in {"/api/documents", "/api/demo"}:
            self.documents_post(path)
            return
        if path == "/api/papers":
            self.legacy_upload()
            return
        self.send_json({"error": "Маршрут не найден."}, 404)

    def documents_post(self, path):
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            length = 0
        if path == "/api/documents" and length == 0:
            self.error("Нужен PDF размером до 20 МиБ.", 413)
            return
        if length > RAG_MAX_BYTES:
            self.error("Файл больше 20 МиБ.", 413)
            return
        if path == "/api/documents":
            if self.headers.get_content_type() != "application/pdf":
                self.error("Ожидается application/pdf.", 415)
                return
            data = self.rfile.read(length)
            track = self.headers.get("X-Track", "rag")
            if track != "rag":
                self.error("Эта область ещё не подключена.", 400)
                return
            name = unquote(self.headers.get("X-Filename", "article.pdf"))
            try:
                summary = self.rag_store().ingest(name, data, track=track)
            except ValueError as error:
                self.error(str(error), 422)
                return
            except Exception:
                log.exception("RAG ingest failed")
                self.error("Ошибка обработки PDF. Подробности в консоли сервера.", 500)
                return
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
        self.send_json(result, 201)


def main():
    parser = argparse.ArgumentParser(description="VINITI PDF evidence workspace")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "papers.sqlite3")
    args = parser.parse_args()
    with ThreadingHTTPServer(("127.0.0.1", args.port), Handler) as server:
        server.library = Library(args.db)
        server.rag_store = RagStore(args.db)
        print(f"http://127.0.0.1:{server.server_port}", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
