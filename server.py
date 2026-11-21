import argparse
import csv
import io
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import logging
from pathlib import Path
import re
import sqlite3
from urllib.parse import parse_qs, unquote, urlsplit

from pdfminer.pdfdocument import PDFPasswordIncorrect
from pdfminer.pdfparser import PDFSyntaxError

import db as storage
import export_csv
from demo import demo_pdf
from library import Library, MAX_BYTES
from pdf_source import render


ROOT = Path(__file__).resolve().parent
STATIC_DIR = (ROOT / "static").resolve()
STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}
PAPER = re.compile(r"/api/papers/([0-9a-f]{64})(?:/(pdf|sources|facts|headers)(?:/([1-9][0-9]{0,8})\.png)?)?")
AREAS = {"solar", "rag"}
JOB_STATUSES = ("ok", "error", "skipped", "running", "queued")
log = logging.getLogger(__name__)


def static_file(path):
    """Resolve a URL path to a servable file inside static/, or None.

    Only the root of static/ is served, and only the extensions in STATIC_TYPES.
    The resolved path must stay inside static/, so '..' and nested paths fall
    through to the normal 404 instead of leaking a file from the repository.
    """
    name = "index.html" if path in ("", "/") else unquote(path).lstrip("/")
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        return None
    if Path(name).suffix.lower() not in STATIC_TYPES:
        return None
    candidate = (STATIC_DIR / name).resolve()
    try:
        candidate.relative_to(STATIC_DIR)
    except ValueError:
        return None
    if not candidate.is_file():
        return None
    return candidate


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

    def csv_slice(self, area, spec, columns, rows):
        """Render a user slice with the area's own byte conventions (BOM and CRLF)."""
        buffer = io.StringIO(newline="")
        writer = csv.writer(buffer, lineterminator=spec["newline"])
        writer.writerow(columns)
        for row in rows:
            writer.writerow(["" if row[name] is None else str(row[name]) for name in columns])
        text = buffer.getvalue()
        if spec["bom"]:
            return b"\xef\xbb\xbf" + text.encode("utf-8")
        return text.encode("utf-8")

    def pick_area(self, raw):
        area = (raw or "rag").strip() or "rag"
        if area not in AREAS:
            self.send_json({"error": f"Недопустимая область: {area}. Допустимы: solar, rag."}, 400)
            return None
        return area

    def data_connection(self):
        """Open the results database for this request, or answer 503 and return None."""
        path = getattr(self.server, "data", None)
        if path is None or not Path(path).exists():
            self.send_json({"error": "База результатов не подключена. Запустите python db.py --init "
                                      "и python import_gold.py, затем перезапустите сервер."}, 503)
            return None
        return storage.connect(path)

    def slice_arguments(self, query, area):
        """Read the constructor query into arguments for export_csv.select_rows."""
        table = (query.get("table") or ["results"])[0]
        spec = storage.load_passport()["areas"][area]
        meta = spec["tables"].get(table)
        if meta is None:
            self.send_json({"error": f"Нет таблицы {table} в области {area}. "
                                      f"Доступны: {', '.join(spec['tables'])}"}, 400)
            return None
        columns = ",".join(query.get("col") or [])
        if not columns:
            columns = ",".join(meta["columns"])
        chosen = [name.strip() for name in columns.split(",") if name.strip()]
        unknown = [name for name in chosen if name not in meta["columns"]]
        if unknown:
            self.send_json({"error": f"Нет таких полей: {', '.join(unknown)}"}, 400)
            return None
        for required in export_csv.ALWAYS_KEPT:
            if required in meta["columns"] and required not in chosen:
                chosen.append(required)
        try:
            limit = int((query.get("limit") or ["0"])[0])
        except ValueError:
            self.send_json({"error": "limit должен быть числом."}, 400)
            return None
        return {
            "table": table, "spec": spec, "meta": meta, "columns": chosen,
            "wheres": query.get("where") or [],
            "order": (query.get("order") or [""])[0],
            "limit": max(0, limit),
        }

    def issue_arguments(self, query, area):
        """Read the issues query into exact-value filters plus a clamped limit."""
        columns = storage.load_passport()["areas"][area]["tables"]["issues"]["columns"]
        filters = {}
        for name, values in query.items():
            if name in ("area", "limit"):
                continue
            if name not in columns:
                self.send_json({"error": f"Нет такого фильтра {name}. Допустимы: "
                                          f"{', '.join(columns)}, limit."}, 400)
                return None
            filters[name] = values[0]
        try:
            limit = int((query.get("limit") or ["100"])[0])
        except ValueError:
            self.send_json({"error": "limit должен быть числом."}, 400)
            return None
        return {"filters": filters, "limit": max(0, min(limit, 1000))}

    def table_count(self, conn, area, table):
        return conn.execute(
            f"SELECT COUNT(*) FROM {storage.quote(storage.table_name(area, table))} "
            f"WHERE area = ?", (area,)).fetchone()[0]

    def stats_document(self, conn):
        """Per-area row counts, job statuses and document totals, straight from the schema."""
        passport = storage.load_passport()
        areas = {}
        for area, spec in passport["areas"].items():
            tables = {table: self.table_count(conn, area, table) for table in spec["tables"]}
            jobs = {status: 0 for status in JOB_STATUSES}
            for status, number in conn.execute(
                    "SELECT status, COUNT(*) FROM jobs WHERE area = ? GROUP BY status", (area,)):
                jobs[status] = number
            total, pages, chars = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(page_count), 0), COALESCE(SUM(text_chars), 0) "
                "FROM documents WHERE area = ?", (area,)).fetchone()
            by_status = {status: number for status, number in conn.execute(
                "SELECT parser_status, COUNT(*) FROM documents WHERE area = ? "
                "GROUP BY parser_status", (area,))}
            # "Разобрано" - всё, что перестало быть pending и не упало с ошибкой.
            parsed = sum(number for status, number in by_status.items()
                         if status not in ("", "pending", "error"))
            areas[area] = {
                "publications": tables.get("publications", 0),
                "tables": tables,
                "jobs": jobs,
                "documents": {"total": total, "parsed": parsed, "pages": pages,
                              "chars": chars, "parser_status": by_status},
            }
        return {"areas": areas}

    def do_GET(self):
        split = urlsplit(self.path)
        path = split.path
        query = parse_qs(split.query)
        area_raw = (query.get("area") or [None])[0]
        target = static_file(path)
        if target is not None:
            self.send_data(target.read_bytes(), STATIC_TYPES[target.suffix.lower()])
            return
        if path == "/api/passport":
            passport = storage.load_passport()
            if area_raw is None:
                self.send_json(passport)
                return
            area = self.pick_area(area_raw)
            if area is None:
                return
            self.send_json({"version": passport["version"], "area": area,
                            **passport["areas"][area]})
            return
        if path in ("/api/results", "/api/results.csv"):
            area = self.pick_area(area_raw)
            if area is None:
                return
            arguments = self.slice_arguments(query, area)
            if arguments is None:
                return
            conn = self.data_connection()
            if conn is None:
                return
            try:
                rows = export_csv.select_rows(
                    conn, area, arguments["table"], arguments["meta"],
                    arguments["columns"], arguments["wheres"],
                    arguments["order"], arguments["limit"])
            except SystemExit as error:
                self.send_json({"error": str(error)}, 400)
                return
            finally:
                conn.close()
            if path == "/api/results.csv":
                self.send_data(self.csv_slice(area, arguments["spec"],
                                              arguments["columns"], rows),
                               "text/csv; charset=utf-8",
                               Content_Disposition="attachment")
                return
            self.send_json({
                "area": area, "table": arguments["table"],
                "columns": arguments["columns"], "count": len(rows),
                "rows": [{name: row[name] for name in arguments["columns"]} for row in rows],
            })
            return
        if path == "/api/issues":
            area = self.pick_area(area_raw)
            if area is None:
                return
            arguments = self.issue_arguments(query, area)
            if arguments is None:
                return
            conn = self.data_connection()
            if conn is None:
                return
            sql = (f"SELECT * FROM {storage.quote(storage.table_name(area, 'issues'))} "
                   f"WHERE area = ?")
            params = [area]
            for column, value in arguments["filters"].items():
                sql += f" AND {storage.quote(column)} = ?"
                params.append(value)
            sql += " ORDER BY _line LIMIT ?"
            params.append(arguments["limit"])
            try:
                rows = conn.execute(sql, params).fetchall()
            except sqlite3.OperationalError as error:
                self.send_json({"error": f"Таблица issues недоступна: {error}"}, 500)
                return
            finally:
                conn.close()
            self.send_json({"area": area, "count": len(rows),
                            "issues": [dict(row) for row in rows]})
            return
        if path == "/api/stats":
            conn = self.data_connection()
            if conn is None:
                return
            try:
                document = self.stats_document(conn)
            except sqlite3.OperationalError as error:
                self.send_json({"error": f"Схема базы неполна: {error}"}, 500)
                return
            finally:
                conn.close()
            self.send_json(document)
            return
        if path == "/api/papers":
            area = self.pick_area(area_raw)
            if area is None:
                return
            self.send_json(self.server.library.list(area))
            return
        try:
            paper_id, resource, key = PAPER.fullmatch(path).groups()
            area = self.pick_area(area_raw)
            if area is None:
                return
            if not resource:
                self.send_json(self.server.library.get(paper_id, area=area),
                               Content_Disposition="attachment" if "download" in query else "inline")
                return
            document, data = self.server.library.get(paper_id, with_pdf=True, area=area)
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
        except (KeyError, IndexError, AttributeError, ValueError):
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
        area = self.pick_area(self.headers.get("X-Area"))
        if area is None:
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            size = 0
        if not 0 < size <= MAX_BYTES:
            self.send_json({"error": "Нужен PDF размером до 32 МиБ."}, 413)
            return
        data = self.rfile.read(size)
        try:
            result = self.server.library.ingest(
                unquote(self.headers.get("X-Filename", "article.pdf")), data, area=area)
        except (PDFSyntaxError, PDFPasswordIncorrect, ValueError):
            self.send_json({"error": "Не удалось прочитать PDF. Проверьте формат, пароль и лимит 250 страниц."}, 422)
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
    parser.add_argument("--data", type=Path, default=storage.DEFAULT_DB,
                        help="база результатов для /api/results (db.py --init)")
    args = parser.parse_args()
    with ThreadingHTTPServer(("127.0.0.1", args.port), Handler) as server:
        server.library = Library(args.db)
        server.data = args.data
        print(f"http://127.0.0.1:{server.server_port}", flush=True)
        if not args.data.exists():
            print(f"нет базы результатов {args.data}: /api/results вернёт 503, "
                  f"запустите python db.py --init и python import_gold.py", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
