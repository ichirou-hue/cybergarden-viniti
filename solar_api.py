"""Small HTTP adapter for one-document Solar extraction.

The web catalogue stores Solar envelopes separately from the RAG store, but uses
the same document JSON contract. Extraction remains the existing verified
``extract.run`` + ``TableFiller`` pipeline.
"""
from __future__ import annotations

import hashlib
import csv
import io
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from datetime import UTC, datetime

import db as storage
from csv_safe import safe_csv_value
import extract
import pdf_source

MAX_BYTES = 32 * 1024 * 1024
TRACK = "solar"


class SolarStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("CREATE TABLE IF NOT EXISTS solar_documents (document_id TEXT PRIMARY KEY, content_hash TEXT NOT NULL, summary TEXT NOT NULL, envelope TEXT NOT NULL, pdf BLOB NOT NULL, updated_at TEXT NOT NULL)")
            conn.execute("CREATE INDEX IF NOT EXISTS solar_documents_hash ON solar_documents(content_hash)")

    def _connect(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def list(self, track=TRACK):
        if track != TRACK:
            return []
        with closing(self._connect()) as conn:
            return [json.loads(row["summary"]) for row in conn.execute("SELECT summary FROM solar_documents ORDER BY updated_at DESC")]

    def get(self, document_id):
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT envelope FROM solar_documents WHERE document_id=?", (document_id,)).fetchone()
        return json.loads(row["envelope"]) if row else None

    def pdf(self, document_id):
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT pdf FROM solar_documents WHERE document_id=?", (document_id,)).fetchone()
        return row["pdf"] if row else None

    def export_csv(self, document_id):
        envelope = self.get(document_id)
        if envelope is None:
            return None
        columns = ["result_id", "metric_name", "metric_value", "metric_unit",
                   "device_id", "measurement_id", "source_page", "source_fragment"]
        stream = io.StringIO(newline="")
        writer = csv.writer(stream, lineterminator="\r\n")
        writer.writerow(columns)
        for row in envelope.get("results", []):
            source = row.get("source") or {}
            writer.writerow([safe_csv_value(value) for value in
                             ([row.get(column, "") for column in columns[:-2]]
                              + [source.get("page", ""), source.get("fragment", "")])])
        return b"\xef\xbb\xbf" + stream.getvalue().encode("utf-8")

    def ingest(self, name, data):
        if len(data) > MAX_BYTES or not data.startswith(b"%PDF-"):
            raise ValueError("Нужен PDF размером до 32 МиБ.")
        content_hash = hashlib.sha256(data).hexdigest()
        with closing(self._connect()) as conn:
            old = conn.execute("SELECT summary FROM solar_documents WHERE content_hash=?", (content_hash,)).fetchone()
        if old:
            return json.loads(old["summary"])
        started = time.monotonic()
        document_id = f"solar:{Path(name).stem[:32] or 'article'}-{content_hash[:8]}".lower()
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "article.pdf"
            path.write_bytes(data)
            temp_db = Path(tmp) / "results.sqlite3"
            storage.init(temp_db)
            publication_id = f"S{content_hash[:12].upper()}"
            summary = extract.run("solar", publication_id, data, extract.TableFiller(), db_path=temp_db, source_path=name)
            parsed = pdf_source.extract(data)
            conn = storage.connect(temp_db)
            try:
                rows = [dict(row) for row in conn.execute('SELECT * FROM "solar_results" WHERE area=? ORDER BY _line', ("solar",))]
                publication = conn.execute('SELECT * FROM "solar_publications" WHERE area=? LIMIT 1', ("solar",)).fetchone()
            finally:
                conn.close()
        publication = dict(publication) if publication else {}
        results = []
        anchors = {}
        for index, row in enumerate(rows, 1):
            try:
                page = int(row.get("source_page") or 1)
            except (TypeError, ValueError):
                page = 1
            source = {"source_id": 1000 + index, "page": page, "kind": "table", "table": row.get("source_table", ""), "fragment": row.get("source_fragment", ""), "quote": row.get("source_fragment", ""), "bbox": None}
            anchors[str(source["source_id"])] = source
            results.append({"result_id": row.get("result_id", f"X{index:03d}"), "metric_name": row.get("metric_name", ""), "metric_value": row.get("metric_value", ""), "metric_unit": row.get("metric_unit", ""), "raw_value": row.get("metric_value", ""), "extraction_status": row.get("extraction_status", "extracted"), "source": source, "issues": [], "device_id": row.get("device_id", ""), "measurement_id": row.get("measurement_id", "")})
        duration = int((time.monotonic() - started) * 1000)
        title = publication.get("title") or Path(name).stem
        envelope = {"document_id": document_id, "track": TRACK, "publication_id": publication.get("publication_id") or publication_id, "title": title, "year": publication.get("year", ""), "authors": publication.get("authors", ""), "pages": parsed.get("pages", 0), "status": "done" if results else "partial", "error": "", "results_count": len(results), "issues_count": 0, "scanned": not bool(parsed.get("sources")), "ocr_used": False, "handler_revision": 1, "duration_ms": duration, "updated_at": datetime.now(UTC).isoformat(), "publication": publication, "warnings": (["Результаты не найдены: baseline Solar-экстрактор не извлёк подтверждённых строк."] if not results else []), "counts": {"results": len(results), "issues": 0}, "run": {"handler": "solar", "source_revision": 1, "model": "deterministic-solar-table-extractor", "duration_ms": duration}, "export": {"json": f"/api/documents/{document_id}/export.json", "csv": f"/api/documents/{document_id}/export.csv"}, "entities": {}, "results": results, "issues": [], "anchors": anchors, "text_sources": []}
        summary_keys = {k: v for k, v in envelope.items() if k not in {"entities", "results", "issues", "anchors", "text_sources", "publication", "counts", "run", "export", "warnings"}}
        with closing(self._connect()) as conn, conn:
            conn.execute("INSERT OR REPLACE INTO solar_documents VALUES (?, ?, ?, ?, ?, ?)", (document_id, content_hash, json.dumps(summary_keys, ensure_ascii=False), json.dumps(envelope, ensure_ascii=False), data, envelope["updated_at"]))
        return summary_keys
