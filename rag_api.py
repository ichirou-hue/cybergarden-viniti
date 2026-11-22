"""HTTP-адаптер RAG-модуля для фронтенда каталога.

Модуль ``rag`` — это детерминированный экстрактор и CLI. Здесь он
подключается к веб-интерфейсу: PDF сохраняется в SQLite, извлекается в
семь таблиц, а результат раздаётся по контракту ADR-0008
(``/api/documents``, карточка, результаты, замечания, якоря, экспорт).

Никаких внешних API и LLM: только локальный PDF и ``rag.extract``.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import sqlite3
import time
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Iterable

from rag.extract import extract_document
from rag.pdf import Document, load_document, pymupdf_module
from rag.text import norm
from csv_safe import safe_csv_value

MAX_BYTES = 20 * 1024 * 1024
HANDLER_REVISION = 2
TRACK = "rag"

STATUS_LABELS = {
    "extracted": "извлечено",
    "present_not_extractable": "не извлекается",
    "absent": "нет в статье",
}

ENTITY_KEYS = ("configurations", "datasets", "tasks", "experiments")
HEAVY_FIELDS = {"entities", "results", "issues", "anchors", "text_sources"}


# --------------------------------------------------------------------------- #
# Утилиты
# --------------------------------------------------------------------------- #


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _num(value: Any) -> float | None:
    raw = _text(value).replace(",", ".").replace("\u2212", "-").replace("\u00a0", "")
    if not raw:
        return None
    match = re.fullmatch(r"[+\-]?\d+(?:\.\d+)?(?:[eE][+\-]?\d+)?", raw)
    if not match:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _int(value: Any) -> int | None:
    raw = _text(value)
    return int(raw) if raw.isdigit() else None


def _decimal_text(value: Any) -> str:
    return "" if value is None else str(value)


def _authors(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    raw = _text(value)
    if not raw:
        return []
    return [part.strip() for part in re.split(r";|\band\b|&", raw) if part.strip()]


def _slug(value: str, limit: int = 40) -> str:
    cleaned = "".join(ch for ch in value if ch.isalnum() or ch in "-_").strip("-_")
    return (cleaned or "article")[:limit].lower()


def _fold(value: str) -> str:
    return norm(value).casefold()


def _status_for(document: Document, results: int) -> str:
    if document.is_scan:
        return "partial"
    return "done" if results else "partial"


# --------------------------------------------------------------------------- #
# Якоря для просмотрщика PDF
# --------------------------------------------------------------------------- #


def _rects_for(document: Document, page_number: int, fragment: str, raw: str = "") -> list[list[float]]:
    """Найти прямоугольники фрагмента, включая переходы между строками."""
    try:
        page = document.page(page_number)
    except (IndexError, ValueError):
        return []

    raw_tokens = [token for token in re.split(r"[\s,;]+", raw.strip()) if token]
    for token in raw_tokens:
        cleaned = token.strip("()[]{}.,;:%")
        if not cleaned:
            continue
        for line in page.lines:
            for word in line.words:
                if word.text.strip("()[]{}.,;:%") == cleaned:
                    return [[word.x0, word.y0, word.x1, word.y1]]

    target = _fold(fragment)
    if target:
        tokens = target.split()
        # Search within a line, then continue only on the immediately
        # following visual line. Flattening all words would cross unrelated
        # columns and produce a misleading wide highlight.
        lines = list(page.lines)
        # A fragment may contain a physical line break. Match its token
        # stream against adjacent lines, never against the whole page.
        for line_index in range(len(lines) - 1):
            first, second = lines[line_index], lines[line_index + 1]
            first_words = [_fold(word.text) for word in first.words]
            second_words = [_fold(word.text) for word in second.words]
            for split in range(1, len(tokens)):
                if first_words[-split:] + second_words[:len(tokens) - split] == tokens:
                    groups = [first.words[-split:], second.words[:len(tokens) - split]]
                    return [[min(w.x0 for w in group), min(w.y0 for w in group),
                             max(w.x1 for w in group), max(w.y1 for w in group)]
                            for group in groups if group]
        for line_index, line in enumerate(lines):
            for start in range(len(line.words)):
                matched: list[Any] = []
                cursor = line_index
                word_index = start
                while len(matched) < len(tokens) and cursor < len(lines):
                    current = lines[cursor]
                    if cursor != line_index and (current.y0 <= lines[cursor - 1].y0
                                                  or current.x0 > line.x1):
                        break
                    while word_index < len(current.words) and len(matched) < len(tokens):
                        if _fold(current.words[word_index].text) != tokens[len(matched)]:
                            break
                        matched.append(current.words[word_index])
                        word_index += 1
                    if len(matched) < len(tokens):
                        if word_index != len(current.words):
                            break
                        cursor += 1
                        word_index = 0
                    else:
                        by_line: dict[float, list[Any]] = {}
                        for word in matched:
                            by_line.setdefault(word.y0, []).append(word)
                        return [[min(w.x0 for w in group), min(w.y0 for w in group),
                                 max(w.x1 for w in group), max(w.y1 for w in group)]
                                for group in by_line.values()]
        for line in page.lines:
            line_text = _fold(line.text)
            if target[:40] and (target[:40] in line_text or line_text[:40] in target):
                return [[line.x0, line.y0, line.x1, line.y1]]
            line_words = [_fold(word.text) for word in line.words]
            for start in range(max(1, len(line_words) - len(tokens) + 1)):
                if line_words[start : start + len(tokens)] == tokens:
                    matched = line.words[start : start + len(tokens)]
                    return [[min(word.x0 for word in matched), min(word.y0 for word in matched),
                             max(word.x1 for word in matched), max(word.y1 for word in matched)]]
    return []


def _rect_for(document: Document, page_number: int, fragment: str, raw: str = "") -> list[float] | None:
    """Backward-compatible union bbox for callers using the old API."""
    rects = _rects_for(document, page_number, fragment, raw)
    if not rects:
        return None
    return [min(rect[0] for rect in rects), min(rect[1] for rect in rects),
            max(rect[2] for rect in rects), max(rect[3] for rect in rects)]


def _anchor(
    document: Document,
    *,
    source_id: int,
    page: int | str,
    fragment: str,
    kind: str = "table",
    table: str = "",
    section: str = "",
) -> dict[str, Any]:
    # Do not use ``or 1`` here: page zero is an explicit invalid page and
    # must not silently point at the first page.  Keeping it in the envelope
    # makes the bad source visible, while _rects_for leaves it without a bbox
    # so it cannot accidentally be rendered as a valid highlight.
    page_number = _int(page)
    fragment = _text(fragment)
    bboxes = _rects_for(document, page_number, fragment) if page_number is not None else []
    bbox = _rect_for(document, page_number, fragment) if page_number is not None else None
    return {
        "source_id": source_id,
        "page": page_number,
        "section": section,
        "table": table,
        "figure": "",
        "fragment": fragment,
        "bbox": bbox,
        "bboxes": bboxes,
        "kind": kind,
    }


def render_anchor_png(pdf: bytes, page: int, bbox: list[float] | None = None,
                      dpi: int = 150, bboxes: list[list[float]] | None = None) -> bytes:
    """Отрендерить страницу и подсветить найденный прямоугольник."""
    pymupdf = pymupdf_module()

    with pymupdf.open(stream=pdf, filetype="pdf") as document:
        if not 1 <= page <= len(document):
            raise ValueError(f"страница {page} вне диапазона 1..{len(document)}")
        target = document[page - 1]
        rectangles = bboxes or ([bbox] if bbox else [])
        for box in rectangles:
            if box and len(box) == 4:
                rect = pymupdf.Rect(*box)
                # Толщина в пунктах PDF; при рендере масштабируется вместе со страницей.
                target.draw_rect(rect, color=(0.78, 0.29, 0.18), width=2.2)
        pixmap = target.get_pixmap(dpi=dpi)
        return pixmap.tobytes("png")


# --------------------------------------------------------------------------- #
# Преобразование Extraction -> конверт фронтенда
# --------------------------------------------------------------------------- #


def _entity_label(kind: str, row: dict[str, str]) -> str:
    if kind == "configurations":
        return (
            _text(row.get("rag_variant"))
            or _text(row.get("generator_model"))
            or _text(row.get("retrieval_method"))
            or _text(row.get("config_id"))
        )
    if kind == "datasets":
        return _text(row.get("dataset_name")) or _text(row.get("dataset_id"))
    if kind == "tasks":
        return _text(row.get("task_type")) or _text(row.get("task_id"))
    if kind == "experiments":
        protocol = _text(row.get("evaluation_protocol"))
        return (protocol[:70] + "…") if len(protocol) > 70 else (protocol or _text(row["experiment_id"]))
    return _text(row.get("label")) or _text(row.get("id"))


def _entity_detail(kind: str, row: dict[str, str]) -> str:
    ignored = {
        "publication_id",
        "config_id",
        "dataset_id",
        "task_id",
        "experiment_id",
        "rag_variant",
        "dataset_name",
        "task_type",
        "evaluation_protocol",
    }
    fields = [f"{key}={value}" for key, value in row.items() if key not in ignored and _text(value)]
    return " · ".join(fields)


def _entities(tables: dict[str, list[dict[str, str]]]) -> dict[str, list[dict[str, str]]]:
    result: dict[str, list[dict[str, str]]] = {}
    for kind in ENTITY_KEYS:
        items = []
        for row in tables.get(kind, []):
            identifier = row.get(
                {"configurations": "config_id", "datasets": "dataset_id",
                 "tasks": "task_id", "experiments": "experiment_id"}[kind],
                "",
            )
            items.append({
                "id": identifier,
                "label": _entity_label(kind, row),
                "detail": _entity_detail(kind, row),
                **row,
            })
        result[kind] = items
    return result


def _issue_rows(
    document: Document,
    tables: dict[str, list[dict[str, str]]],
) -> list[dict[str, Any]]:
    issues = []
    for index, row in enumerate(tables.get("issues", []), start=1):
        record_ids = [
            item.strip()
            for item in re.split(r"[;,]", _text(row.get("record_ids")))
            if item.strip()
        ]
        source_id = 5000 + index
        anchor = None
        if _int(row.get("source_page")):
            anchor = _anchor(
                document,
                source_id=source_id,
                page=row.get("source_page", ""),
                fragment=row.get("source_fragment", ""),
                kind="text",
            )
        issues.append({
            "issue_id": row.get("issue_id", f"I{index:02d}"),
            "check_name": row.get("check_name", ""),
            "description": row.get("description", ""),
            "record_ids": record_ids,
            "source": anchor,
        })
    return issues


def _result_rows(
    document: Document,
    tables: dict[str, list[dict[str, str]]],
    issues: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    issues_by_result: dict[str, list[dict[str, Any]]] = {}
    for issue in issues:
        for record_id in issue["record_ids"]:
            issues_by_result.setdefault(record_id, []).append(issue)

    # Подписи таблиц: одна строка-заголовок на таблицу, чтобы у результата
    # была вторая независимая ссылка «Заголовок».
    header_anchors: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(tables.get("results", []), start=1):
        table = _text(row.get("source_table"))
        if table and table not in header_anchors:
            page = row.get("source_page", "")
            header_anchors[table] = _anchor(
                document,
                source_id=2000 + len(header_anchors) + 1,
                page=page,
                fragment=table,
                kind="caption",
                table=table,
            )

    results = []
    anchors: dict[str, Any] = {}
    for index, row in enumerate(tables.get("results", []), start=1):
        result_id = row.get("result_id", f"R{index:03d}")
        source = _anchor(
            document,
            source_id=1000 + index,
            page=row.get("source_page", ""),
            fragment=row.get("source_fragment", ""),
            kind="table" if _text(row.get("source_table")) else "text",
            table=_text(row.get("source_table")),
        )
        anchors[str(source["source_id"])] = source
        header_source = header_anchors.get(_text(row.get("source_table")))
        if header_source:
            anchors[str(header_source["source_id"])] = header_source

        attached = issues_by_result.get(result_id, [])
        metric_name = _text(row.get("metric_name"))
        metric_k = _int(row.get("metric_k"))
        # rag хранит «Recall@5» в metric_name; фронтенд печатает k отдельно,
        # поэтому из имени убираем суффикс, чтобы не было «Recall@5 @5».
        if metric_k and metric_name.endswith(f"@{metric_k}"):
            metric_name = metric_name[: -(len(str(metric_k)) + 1)]
        results.append({
            "result_id": result_id,
            "metric_name": metric_name,
            "metric_k": metric_k,
            "metric_group": _text(row.get("metric_group")),
            "metric_value": _num(row.get("metric_value")),
            "metric_unit": _text(row.get("metric_unit")),
            "metric_variant": _text(row.get("metric_variant")),
            "metric_direction": _text(row.get("metric_direction")),
            "raw_value": _decimal_text(row.get("raw_value")),
            "raw_unit": _text(row.get("raw_unit")),
            "normalized_value": _num(row.get("normalized_value")),
            "normalized_unit": _text(row.get("normalized_unit")),
            "extraction_status": _text(row.get("extraction_status")) or "extracted",
            "result_origin": _text(row.get("result_origin")) or "primary",
            "cited_publication_id": _text(row.get("cited_publication_id")),
            "duplicate_of": _text(row.get("duplicate_of")),
            "aggregation": _text(row.get("aggregation")),
            "sample_size": _int(row.get("sample_size")) or _text(row.get("sample_size")),
            "note": _text(row.get("note")),
            "config_id": _text(row.get("config_id")),
            "dataset_id": _text(row.get("dataset_id")),
            "task_id": _text(row.get("task_id")),
            "experiment_id": _text(row.get("experiment_id")),
            "source": source,
            "header_source": header_source,
            "issues": [
                {
                    "issue_id": issue["issue_id"],
                    "check_name": issue["check_name"],
                    "description": issue["description"],
                }
                for issue in attached
            ],
        })
    return results, anchors


def _text_sources(document: Document) -> list[dict[str, Any]]:
    """Строки текста для вкладки «Фрагменты PDF» и глубоких ссылок."""
    items = []
    for page in document.pages:
        if page.source == "empty":
            continue
        for line in page.lines:
            text = line.text.strip()
            if len(text) < 25:
                continue
            source_id = 90000 + len(items)
            items.append({
                "source_id": source_id,
                "page": page.number,
                "kind": "text",
                "quote": text,
                "fragment": text,
                "bbox": [line.x0, line.y0, line.x1, line.y1],
                "column": line.column,
                "column_count": 2 if line.column else 1,
                "block_id": f"p{page.number}-b{source_id}",
            })
            if len(items) >= 500:
                return items
    return items


def build_envelope(
    *,
    document_id: str,
    name: str,
    pdf: bytes,
    publication_id: str,
    duration_ms: int,
    progress=None,
) -> dict[str, Any]:
    """Извлечь PDF и собрать конверт, который понимает фронтенд."""
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "article.pdf"
        path.write_bytes(pdf)
        document = load_document(path)
        if progress: progress(1, "Страницы PDF прочитаны")
        extraction = extract_document(publication_id, document)
        if progress: progress(2, "Результаты извлечены")

    tables = extraction.tables
    publication_row = dict((tables.get("publications") or [{}])[0])
    issues = _issue_rows(document, tables)
    results, anchors = _result_rows(document, tables, issues)
    for issue in issues:
        if issue.get("source"):
            anchors[str(issue["source"]["source_id"])] = issue["source"]
    text_sources = _text_sources(document)
    for source in text_sources:
        anchors[str(source["source_id"])] = {
            "source_id": source["source_id"],
            "page": source["page"],
            "section": "",
            "table": "",
            "figure": "",
            "fragment": source["fragment"],
            "bbox": source["bbox"],
            "kind": "text",
        }

    title = _text(publication_row.get("title")) or Path(name).stem
    year = _int(publication_row.get("year")) or 0
    authors = _authors(publication_row.get("authors"))
    scanned = document.is_scan
    status = _status_for(document, len(results))
    counts = {
        "results": len(results),
        "issues": len(issues),
        "by_status": {
            status_name: sum(1 for row in results if row["extraction_status"] == status_name)
            for status_name in STATUS_LABELS
        },
    }

    summary = {
        "document_id": document_id,
        "track": TRACK,
        "publication_id": publication_id,
        "title": title,
        "year": year,
        "authors": authors,
        "pages": len(document.pages),
        "status": status,
        "error": "",
        "results_count": len(results),
        "issues_count": len(issues),
        "scanned": scanned,
        "ocr_used": any(page.source == "ocr" for page in document.pages),
        "handler_revision": HANDLER_REVISION,
        "duration_ms": duration_ms,
        "updated_at": datetime.now(UTC).isoformat(),
    }

    return {
        **summary,
        "publication": {
            "publication_id": publication_id,
            "title": title,
            "year": year,
            "authors": authors,
            "doi": _text(publication_row.get("DOI")),
            "in_scope": _text(publication_row.get("in_scope")).lower() != "false",
            "exclusion_reason": "",
        },
        "warnings": (
            ["В PDF нет текстового слоя: скан без OCR."] if scanned else []
        ),
        "counts": counts,
        "run": {
            "handler": "rag",
            "source_revision": HANDLER_REVISION,
            "model": "deterministic-rag-extractor",
            "duration_ms": duration_ms,
        },
        "export": {
            "json": f"/api/documents/{document_id}/export.json",
            "csv": f"/api/documents/{document_id}/export.csv",
        },
        "entities": _entities(tables),
        "results": results,
        "issues": issues,
        "anchors": anchors,
        "text_sources": text_sources,
    }


def failed_envelope(
    *,
    document_id: str,
    name: str,
    publication_id: str,
    error: str,
    pages: int = 0,
    duration_ms: int = 0,
) -> dict[str, Any]:
    title = Path(name).stem or name
    summary = {
        "document_id": document_id,
        "track": TRACK,
        "publication_id": publication_id,
        "title": title,
        "year": 0,
        "authors": [],
        "pages": pages,
        "status": "failed",
        "error": error,
        "results_count": 0,
        "issues_count": 0,
        "scanned": False,
        "ocr_used": False,
        "handler_revision": 0,
        "duration_ms": duration_ms,
        "updated_at": datetime.now(UTC).isoformat(),
    }
    return {
        **summary,
        "publication": {
            "publication_id": publication_id,
            "title": title,
            "year": 0,
            "authors": [],
            "doi": "",
            "in_scope": True,
            "exclusion_reason": "",
        },
        "warnings": [error],
        "counts": {"results": 0, "issues": 0, "by_status": {}},
        "run": {"handler": "rag", "source_revision": 0, "model": "", "duration_ms": duration_ms},
        "export": {"json": "", "csv": ""},
        "entities": {kind: [] for kind in ENTITY_KEYS},
        "results": [],
        "issues": [],
        "anchors": {},
        "text_sources": [],
    }


# --------------------------------------------------------------------------- #
# Хранилище
# --------------------------------------------------------------------------- #


class RagStore:
    """SQLite-хранилище PDF и готовых конвертов."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db, db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS rag_documents (
                    document_id TEXT PRIMARY KEY,
                    track TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    publication_id TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    envelope TEXT NOT NULL,
                    pdf BLOB NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            db.execute("CREATE INDEX IF NOT EXISTS rag_documents_track ON rag_documents(track)")

    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        return db

    def list(self, track: str = TRACK) -> list[dict[str, Any]]:
        with closing(self.connect()) as db:
            rows = db.execute(
                "SELECT summary FROM rag_documents WHERE track = ? ORDER BY updated_at DESC",
                (track,),
            ).fetchall()
        return [json.loads(row["summary"]) for row in rows]

    def get(self, document_id: str) -> dict[str, Any] | None:
        with closing(self.connect()) as db:
            row = db.execute(
                "SELECT envelope FROM rag_documents WHERE document_id = ?",
                (document_id,),
            ).fetchone()
        return json.loads(row["envelope"]) if row else None

    def pdf(self, document_id: str) -> bytes | None:
        with closing(self.connect()) as db:
            row = db.execute(
                "SELECT pdf FROM rag_documents WHERE document_id = ?",
                (document_id,),
            ).fetchone()
        return row["pdf"] if row else None

    def find_by_hash(self, content_hash: str) -> dict[str, Any] | None:
        with closing(self.connect()) as db:
            row = db.execute(
                "SELECT summary FROM rag_documents WHERE content_hash = ?",
                (content_hash,),
            ).fetchone()
        return json.loads(row["summary"]) if row else None

    def save(self, envelope: dict[str, Any], pdf: bytes, content_hash: str) -> dict[str, Any]:
        summary = {
            key: value
            for key, value in envelope.items()
            if key not in HEAVY_FIELDS | {"publication", "counts", "run", "export", "warnings"}
        }
        with closing(self.connect()) as db, db:
            db.execute(
                "INSERT OR REPLACE INTO rag_documents "
                "(document_id, track, content_hash, publication_id, summary, envelope, pdf, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    envelope["document_id"],
                    envelope["track"],
                    content_hash,
                    envelope["publication_id"],
                    json.dumps(summary, ensure_ascii=False, allow_nan=False),
                    json.dumps(envelope, ensure_ascii=False, allow_nan=False),
                    pdf,
                    envelope["updated_at"],
                ),
            )
        return summary

    def ingest(
        self,
        name: str,
        data: bytes,
        *,
        track: str = TRACK,
        publication_id: str | None = None,
        forced_id: str | None = None,
        progress=None,
    ) -> dict[str, Any]:
        if len(data) > MAX_BYTES or not data.startswith(b"%PDF-"):
            raise ValueError("Нужен PDF размером до 20 МиБ.")
        content_hash = hashlib.sha256(data).hexdigest()
        existing = self.find_by_hash(content_hash)
        if existing and not forced_id and existing.get("handler_revision") == HANDLER_REVISION:
            return existing

        raw_name = Path(str(name).replace("\\", "/")).name[:240] or "article.pdf"
        pid = (publication_id or Path(raw_name).stem or "ARTICLE").strip()[:80]
        document_id = forced_id or f"{track}:{_slug(pid, 32)}-{content_hash[:8]}"
        started = time.monotonic()
        try:
            envelope = build_envelope(
                document_id=document_id,
                name=raw_name,
                pdf=data,
                publication_id=pid.upper(),
                duration_ms=0,
                progress=progress,
            )
        except Exception as error:  # noqa: BLE001 - статус ошибки нужен каталогу
            envelope = failed_envelope(
                document_id=document_id,
                name=raw_name,
                publication_id=pid.upper(),
                error=f"Не удалось разобрать PDF: {error}",
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        envelope["duration_ms"] = int((time.monotonic() - started) * 1000)
        envelope["run"]["duration_ms"] = envelope["duration_ms"]
        if progress: progress(3, "Карточка собрана")
        return self.save(envelope, data, content_hash)

    def meta(self, document_id: str) -> dict[str, Any] | None:
        with closing(self.connect()) as db:
            row = db.execute(
                "SELECT content_hash, publication_id FROM rag_documents WHERE document_id = ?",
                (document_id,),
            ).fetchone()
        return dict(row) if row else None


# --------------------------------------------------------------------------- #
# Выборки и фасеты
# --------------------------------------------------------------------------- #


def _config_label(envelope: dict[str, Any], config_id: str) -> str:
    for item in envelope.get("entities", {}).get("configurations", []):
        if item.get("config_id") == config_id:
            return item.get("label") or config_id
    return config_id


def _dataset_label(envelope: dict[str, Any], dataset_id: str) -> str:
    for item in envelope.get("entities", {}).get("datasets", []):
        if item.get("dataset_id") == dataset_id:
            return item.get("label") or dataset_id
    return dataset_id


def _task_label(envelope: dict[str, Any], task_id: str) -> str:
    for item in envelope.get("entities", {}).get("tasks", []):
        if item.get("task_id") == task_id:
            return item.get("label") or task_id
    return task_id


def filter_results(
    envelope: dict[str, Any],
    params: dict[str, str],
) -> list[dict[str, Any]]:
    rows = envelope.get("results", [])
    metric = _text(params.get("metric"))
    k = _text(params.get("k"))
    dataset = _text(params.get("dataset"))
    config = _text(params.get("config"))
    status = _text(params.get("status"))
    only_issues = _text(params.get("issues")) in {"1", "true", "on"}
    query = _fold(_text(params.get("q")))

    selected = []
    for row in rows:
        if metric and row.get("metric_name") != metric:
            continue
        if k and str(row.get("metric_k") or "") != k:
            continue
        if dataset and row.get("dataset_id") != dataset:
            continue
        if config and row.get("config_id") != config:
            continue
        if status and row.get("extraction_status") != status:
            continue
        if only_issues and not row.get("issues"):
            continue
        if query:
            source = row.get("source") or {}
            header = row.get("header_source") or {}
            metric_at_k = (
                f"{row.get('metric_name')}@{row.get('metric_k')}"
                if row.get("metric_k") else row.get("metric_name", "")
            )
            haystack = " ".join(str(value) for value in (
                row.get("result_id"), row.get("metric_name"), metric_at_k,
                row.get("metric_group"),
                row.get("metric_value"), row.get("raw_value"), row.get("raw_unit"),
                row.get("metric_unit"), row.get("normalized_value"), row.get("config_id"),
                row.get("dataset_id"), row.get("task_id"), row.get("experiment_id"),
                row.get("extraction_status"), row.get("note"),
                _config_label(envelope, row.get("config_id", "")),
                _dataset_label(envelope, row.get("dataset_id", "")),
                _task_label(envelope, row.get("task_id", "")),
                source.get("fragment", ""), header.get("fragment", ""),
                " ".join(issue.get("description", "") for issue in row.get("issues", [])),
            ))
            if query not in _fold(haystack):
                continue
        selected.append(row)
    return selected


def facets(envelope: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    metrics: dict[str, int] = {}
    ks: dict[str, int] = {}
    datasets: dict[str, int] = {}
    configs: dict[str, int] = {}
    statuses: dict[str, int] = {}
    for row in envelope.get("results", []):
        metrics[row.get("metric_name", "")] = metrics.get(row.get("metric_name", ""), 0) + 1
        if row.get("metric_k"):
            ks[str(row["metric_k"])] = ks.get(str(row["metric_k"]), 0) + 1
        datasets[row.get("dataset_id", "")] = datasets.get(row.get("dataset_id", ""), 0) + 1
        configs[row.get("config_id", "")] = configs.get(row.get("config_id", ""), 0) + 1
        statuses[row.get("extraction_status", "")] = statuses.get(row.get("extraction_status", ""), 0) + 1
    return {
        "metrics": [
            {"value": name, "label": name, "count": count}
            for name, count in sorted(metrics.items()) if name
        ],
        "ks": [
            {"value": value, "label": f"k={value}", "count": count}
            for value, count in sorted(ks.items(), key=lambda item: int(item[0]))
        ],
        "datasets": [
            {"value": name, "label": _dataset_label(envelope, name), "count": count}
            for name, count in sorted(datasets.items()) if name
        ],
        "configs": [
            {"value": name, "label": _config_label(envelope, name), "count": count}
            for name, count in sorted(configs.items()) if name
        ],
        "statuses": [
            {"value": name, "label": STATUS_LABELS.get(name, name), "count": count}
            for name, count in sorted(statuses.items()) if name
        ],
    }


def sources(envelope: dict[str, Any], params: dict[str, str]) -> list[dict[str, Any]]:
    kind = _text(params.get("kind"))
    query = _fold(_text(params.get("q")))
    items: list[dict[str, Any]] = []
    seen: set[int] = set()
    for row in envelope.get("results", []):
        source = row.get("source")
        if not source:
            continue
        if kind and kind != "table":
            continue
        if query and query not in _fold(source.get("fragment", "")):
            continue
        if source.get("source_id") in seen:
            continue
        seen.add(source.get("source_id"))
        items.append({
            "source_id": source["source_id"],
            "page": source["page"],
            "kind": "table" if source.get("table") else "text",
            "fragment": source.get("fragment", ""),
            "quote": source.get("fragment", ""),
        })
    if not kind or kind == "text":
        for source in envelope.get("text_sources", []):
            if query and query not in _fold(source.get("fragment", "")):
                continue
            items.append({
                "source_id": source["source_id"],
                "page": source["page"],
                "kind": "text",
                "fragment": source.get("fragment", ""),
                "quote": source.get("fragment", ""),
                **{key: source[key] for key in ("bbox", "column", "column_count", "block_id") if key in source},
            })
    items.sort(key=lambda item: (item["page"], item["source_id"]))
    return items


def export_csv(envelope: dict[str, Any]) -> bytes:
    output = io.StringIO()
    writer = csv.writer(output)
    columns = [
        "publication_id", "result_id", "experiment_id", "config_id", "dataset_id",
        "task_id", "metric_group", "metric_name", "metric_variant", "metric_direction",
        "metric_k", "metric_value", "metric_unit", "raw_value", "raw_unit",
        "normalized_value", "normalized_unit", "result_origin", "cited_publication_id",
        "source_page", "source_section", "source_table", "source_figure",
        "source_fragment", "extraction_status", "duplicate_of", "note",
    ]
    writer.writerow(columns)
    for row in envelope.get("results", []):
        source = row.get("source") or {}
        writer.writerow([safe_csv_value(value) for value in [
            envelope.get("publication_id", ""), row.get("result_id", ""),
            row.get("experiment_id", ""), row.get("config_id", ""),
            row.get("dataset_id", ""), row.get("task_id", ""),
            row.get("metric_group", ""), row.get("metric_name", ""),
            row.get("metric_variant", ""), row.get("metric_direction", ""),
            row.get("metric_k") or "", row.get("metric_value") if row.get("metric_value") is not None else "",
            row.get("metric_unit", ""), row.get("raw_value", ""), row.get("raw_unit", ""),
            row.get("normalized_value") if row.get("normalized_value") is not None else "",
            row.get("normalized_unit", ""), row.get("result_origin", ""),
            row.get("cited_publication_id", ""), source.get("page", ""),
            source.get("section", ""), source.get("table", ""),
            source.get("figure", ""), source.get("fragment", ""),
            row.get("extraction_status", ""), row.get("duplicate_of", ""),
            row.get("note", ""),
        ]])
    return output.getvalue().encode("utf-8-sig")


def demo_pdf() -> bytes:
    """Демо-статья с настоящей таблицей, которую понимает rag.grid."""
    from reportlab.pdfgen import canvas

    output = io.BytesIO()
    pdf = canvas.Canvas(output, pagesize=(595, 842), invariant=True)
    pdf.setFont("Helvetica-Bold", 16)
    pdf.drawString(56, 790, "Retrieval-Augmented Generation Benchmark Study")
    pdf.setFont("Helvetica", 10)
    pdf.drawString(56, 770, "Ivan Petrov; Anna Smirnova")
    pdf.drawString(56, 752, "Abstract")
    pdf.setFont("Helvetica", 9)
    pdf.drawString(56, 736, "We evaluate retrieval pipelines on HotpotQA and Natural Questions.")
    pdf.drawString(56, 722, "The RAG pipeline combines a retriever with a generator.")
    pdf.setFont("Helvetica-Bold", 10)
    pdf.drawString(56, 690, "Table 2. Retrieval quality on HotpotQA and Natural Questions.")
    pdf.setFont("Helvetica-Bold", 8)
    pdf.drawString(56, 672, "Configuration")
    pdf.drawString(190, 672, "HotpotQA Recall@5")
    pdf.drawString(320, 672, "HotpotQA Recall@10")
    pdf.drawString(450, 672, "Natural Questions Recall@5")
    pdf.setFont("Helvetica", 8)
    rows = [
        ("BM25", "0.81", "0.85", "0.70"),
        ("Dense E5", "0.78", "0.83", "0.69"),
        ("Hybrid", "0.84", "0.88", "0.73"),
    ]
    for index, (name, recall5, recall10, nq) in enumerate(rows):
        y = 652 - index * 18
        pdf.drawString(56, y, name)
        pdf.drawString(190, y, f"{recall5} +- 0.01")
        pdf.drawString(320, y, f"{recall10} +- 0.01")
        pdf.drawString(450, y, f"{nq} +- 0.02")
    pdf.showPage()
    pdf.save()
    return output.getvalue()
