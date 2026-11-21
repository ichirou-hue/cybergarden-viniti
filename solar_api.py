"""Small HTTP adapter for one-document Solar extraction.

The web catalogue stores Solar envelopes separately from the RAG store, but uses
the same document JSON contract. Extraction is the deterministic
``solar_offline`` parser, the same one ``batch`` and the CSV export use, so the
site and the submission cannot disagree about a paper.
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

from csv_safe import safe_csv_value
import solar_offline
import solar_version

MAX_BYTES = 32 * 1024 * 1024
TRACK = "solar"


def _anchor(source):
    """One source in the shape the frontend evidence dialog expects.

    The drawable box comes from the verified ``pdf_source`` source itself, so
    nothing is guessed; ``bboxes`` keeps the RAG list shape for a multi-line
    fragment.
    """
    bbox = source.get("bbox")
    boxes = [list(map(float, bbox))] \
        if isinstance(bbox, (list, tuple)) and len(bbox) == 4 else []
    return {
        "source_id": source["id"],
        "page": source["page"],
        "section": source.get("section", ""),
        "figure": source.get("figure", ""),
        "kind": source.get("kind") or "text",
        "table": source.get("table", ""),
        "fragment": source.get("quote", ""),
        "quote": source.get("quote", ""),
        "bbox": boxes[0] if boxes else None,
        "bboxes": boxes,
    }


def _record_ids(value):
    """The template keeps record ids in one string (``G1;G2``), the UI wants a list."""
    return [item.strip() for item in str(value or "").replace(";", ",").split(",")
            if item.strip()]


def _issues(tables):
    return [{
        "issue_id": row.get("issue_id", ""),
        "check_name": row.get("check_name", ""),
        "description": row.get("description", ""),
        "record_ids": _record_ids(row.get("record_ids")),
        "source_page": row.get("source_page", ""),
        "source_fragment": row.get("source_fragment", ""),
    } for row in tables.get("issues", [])]


def _results(tables, evidence, issues):
    """Result rows plus the anchor and the checks that apply to each one.

    The finalized tables keep only template columns, so the anchor for a row
    comes from ``evidence``, which is exactly what ``solar_facts`` verified.
    ``config_id``/``dataset_id`` mirror the device and the measurement: the
    shared results table reads those two column keys, while the Solar template
    names the same links ``device_id``/``measurement_id``.
    """
    by_record = {}
    for issue in issues:
        for record in issue["record_ids"]:
            by_record.setdefault(record, []).append(issue)
    rows = []
    for row in tables.get("results", []):
        anchors = evidence.get(f"results/{row.get('result_id', '')}") or []
        rows.append({
            "result_id": row.get("result_id", ""),
            "metric_name": row.get("metric_name", ""),
            "metric_value": row.get("metric_value", ""),
            "metric_unit": row.get("metric_unit", ""),
            "raw_value": row.get("raw_value", ""),
            "raw_unit": row.get("raw_unit", ""),
            "normalized_value": row.get("normalized_value", ""),
            "normalized_unit": row.get("normalized_unit", ""),
            "metric_group": "",
            "aggregation": row.get("aggregation", ""),
            "time_point": row.get("time_point", ""),
            "time_unit": row.get("time_unit", ""),
            "result_origin": row.get("result_origin", ""),
            "cited_publication_id": row.get("cited_publication_id", ""),
            "extraction_status": row.get("extraction_status", ""),
            "duplicate_of": row.get("duplicate_of", ""),
            "note": row.get("note", ""),
            "material_id": row.get("material_id", ""),
            "device_id": row.get("device_id", ""),
            "measurement_id": row.get("measurement_id", ""),
            "config_id": row.get("device_id", ""),
            "dataset_id": row.get("measurement_id", ""),
            "source_page": row.get("source_page", ""),
            "source_section": row.get("source_section", ""),
            "source_table": row.get("source_table", ""),
            "source_figure": row.get("source_figure", ""),
            "source": _anchor(anchors[0]) if anchors else None,
            "issues": by_record.get(row.get("result_id", ""), []),
        })
    return rows


def _text_sources(sources):
    """Non-table sources for the fragments tab; result rows carry their own."""
    return [{
        "source_id": source["id"],
        "page": source["page"],
        "kind": source.get("kind") or "text",
        "fragment": source.get("quote", ""),
    } for source in sources if (source.get("kind") or "text") != "table"]


def solar_sources(envelope, kind="", needle=""):
    """Result anchors plus text sources, deduplicated, with real kinds kept.

    The shared RAG helper collapses kinds to table/text and does not dedupe
    across the two lists, so prose anchors repeat and captions disappear.
    """
    items, seen = [], set()
    for row in envelope.get("results", []):
        source = row.get("source")
        if source:
            items.append(source)
    items.extend(envelope.get("text_sources", []))
    unique = []
    for source in items:
        source_id = source.get("source_id")
        if source_id in seen:
            continue
        seen.add(source_id)
        unique.append(source)
    if kind:
        unique = [source for source in unique
                  if (source.get("kind") or "text") == kind]
    if needle:
        unique = [source for source in unique
                  if needle in str(source.get("fragment") or source.get("quote") or "").lower()]
    unique.sort(key=lambda source: (int(source.get("page") or 0), int(source.get("source_id") or 0)))
    return [{
        "source_id": source["source_id"],
        "page": source.get("page"),
        "kind": source.get("kind") or "text",
        "fragment": source.get("fragment") or source.get("quote") or "",
        "quote": source.get("fragment") or source.get("quote") or "",
    } for source in unique]


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

    @staticmethod
    def _envelope(document_id, name, result, started):
        """Frontend envelope built from one validated ``solar_offline`` result."""
        tables = result["tables"]
        publication = dict(tables["publications"][0]) if tables["publications"] else {}
        anchors = {str(source["id"]): _anchor(source) for source in result["sources"]}
        issues = _issues(tables)
        results = _results(tables, result.get("evidence") or {}, issues)
        duration = int((time.monotonic() - started) * 1000)
        warnings = list(result.get("warnings") or [])
        # An excluded paper is a finished answer, not a failed parse: the parser
        # deliberately left the subject tables empty and said why. "partial" is
        # reserved for pages the parser saw but could not read.
        excluded = publication.get("in_scope") == "false"
        unreadable = [warning for warning in warnings if "текстовый слой пуст" in warning]
        status = "partial" if unreadable else "done"
        if excluded:
            reason = publication.get("exclusion_reason") or "причина не указана"
            warnings.append(f"Статья вне области исследования: {reason}.")
        elif not results:
            warnings.append("Результаты не найдены: детерминированный экстрактор "
                            "не извлёк подтверждённых строк.")
        return {
            "document_id": document_id, "track": TRACK,
            "publication_id": publication.get("publication_id") or result.get("publication_id", ""),
            "title": publication.get("title") or Path(name).stem,
            "year": publication.get("year", ""),
            "authors": publication.get("authors", ""),
            "pages": result.get("pages", 0),
            "status": status,
            "error": "", "results_count": len(results), "issues_count": len(issues),
            "scanned": not bool(result.get("sources")), "ocr_used": False,
            "handler_revision": solar_version.PARSER_VERSION,
            "duration_ms": duration, "updated_at": datetime.now(UTC).isoformat(),
            "publication": publication, "warnings": warnings,
            "counts": {"results": len(results), "issues": len(issues)},
            "run": {"handler": "solar", "source_revision": solar_version.PARSER_VERSION,
                    "model": "solar-offline", "duration_ms": duration},
            "export": {"json": f"/api/documents/{document_id}/export.json",
                       "csv": f"/api/documents/{document_id}/export.csv"},
            "entities": {}, "results": results, "issues": issues, "anchors": anchors,
            "text_sources": _text_sources(result["sources"]),
        }

    def ingest(self, name, data):
        if len(data) > MAX_BYTES:
            raise ValueError("Нужен PDF размером до 32 МиБ.")
        if not data.startswith(b"%PDF-"):
            raise ValueError("Файл не является PDF.")
        content_hash = hashlib.sha256(data).hexdigest()
        cached_id = None
        with closing(self._connect()) as conn:
            old = conn.execute(
                "SELECT document_id, summary FROM solar_documents "
                "WHERE content_hash=? ORDER BY updated_at DESC LIMIT 1",
                (content_hash,)).fetchone()
        if old:
            summary = json.loads(old["summary"])
            # A cached envelope was produced by a specific parser revision: after
            # bumping PARSER_VERSION the site must reparse, not serve old rows.
            if summary.get("handler_revision") == solar_version.PARSER_VERSION:
                return summary
            # Reparse into the same card, whatever name the upload came with,
            # otherwise one PDF shows up twice in the catalogue.
            cached_id = old["document_id"]
        started = time.monotonic()
        document_id = cached_id or f"solar:{Path(name).stem[:32] or 'article'}-{content_hash[:8]}".lower()
        publication_id = f"S{content_hash[:12].upper()}"
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "article.pdf"
            path.write_bytes(data)
            # The checked pipeline: literal citations, normalized units and the
            # deterministic consistency issues. A ValueError means the paper did
            # not pass verification, not that the upload is malformed.
            result = solar_offline.extract_pdf(path, publication_id)
        envelope = self._envelope(document_id, name, result, started)
        summary_keys = {k: v for k, v in envelope.items() if k not in {"entities", "results", "issues", "anchors", "text_sources", "publication", "counts", "run", "export", "warnings"}}
        with closing(self._connect()) as conn, conn:
            conn.execute("INSERT OR REPLACE INTO solar_documents VALUES (?, ?, ?, ?, ?, ?)", (document_id, content_hash, json.dumps(summary_keys, ensure_ascii=False), json.dumps(envelope, ensure_ascii=False), data, envelope["updated_at"]))
        return summary_keys
