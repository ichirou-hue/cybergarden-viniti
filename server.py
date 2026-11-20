import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import logging
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit

from pdfminer.pdfdocument import PDFPasswordIncorrect
from pdfminer.pdfparser import PDFSyntaxError

from demo import demo_pdf
from library import Library, MAX_BYTES
from pdf_source import render


ROOT = Path(__file__).resolve().parent
ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/solar": ("solar.html", "text/html; charset=utf-8"),
    "/solar.css": ("solar.css", "text/css; charset=utf-8"),
    "/solar.mjs": ("solar.mjs", "text/javascript; charset=utf-8"),
    "/solar-ui.mjs": ("solar-ui.mjs", "text/javascript; charset=utf-8"),
}
PAPER = re.compile(r"/api/papers/([0-9a-f]{64})(?:/(pdf|sources|facts|headers)(?:/([1-9][0-9]{0,8})\.png)?)?")
log = logging.getLogger(__name__)


class Handler(BaseHTTPRequestHandler):
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

    def do_GET(self):
        path = urlsplit(self.path).path
        if path in ASSETS:
            file, media_type = ASSETS[path]
            self.send_data((ROOT / "static" / file).read_bytes(), media_type)
            return
        if path == "/api/papers":
            self.send_json(self.server.library.list())
            return
        try:
            paper_id, resource, key = PAPER.fullmatch(path).groups()
            if not resource:
                self.send_json(self.server.library.get(paper_id),
                               Content_Disposition="attachment" if urlsplit(self.path).query == "download" else "inline")
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

    def do_POST(self):
        if self.path == "/api/demo":
            result = self.server.library.ingest("retrieval-demo.pdf", demo_pdf())
            self.send_json(result, 201)
            return
        if self.path != "/api/papers":
            self.send_json({"error": "Маршрут не найден."}, 404)
            return
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
        print(f"http://127.0.0.1:{server.server_port}", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
