"""Мок-API и статика для разработки каталога (этап F1, ADR-0008).

Инструмент разработки: не входит в продуктовый путь и не заменяет server.py.
Запуск:  .venv/bin/python tools/mock_api.py --port 8799
Контракт повторяет ADR-0008, чтобы frontend не ждал backend.
Состояние (загруженные документы) хранится в памяти и сбрасывается при перезапуске.
"""
import argparse
import csv
import hmac
import json
import mimetypes
import sys
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO, StringIO
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
MAX_BYTES = 20 * 1024 * 1024
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TRACKS = {"rag": "RAG", "solar": "Solar"}
STATUSES = ("queued", "running", "done", "partial", "failed")

# Мок-аутентификация повторяет контракт server.py: cookie-сессия, CSRF-токен
# и per-user профиль. Пароли и сессии живут в памяти процесса.
MOCK_CSRF = "mock-csrf-token"
MOCK_USERS = {}
MOCK_SESSION_COOKIE = "mock_session"
PARSE_JOBS = {}
MOCK_SOURCES = [
    {"id": 1, "page": 1, "bbox": [10, 10, 200, 20], "kind": "text",
     "quote": "The device absorber material is MAPbI3."},
    {"id": 2, "page": 1, "bbox": [10, 30, 220, 40], "kind": "text",
     "quote": "Device A achieved a power conversion efficiency of 21.4 %."},
    {"id": 3, "page": 2, "bbox": [10, 10, 200, 20], "kind": "text",
     "quote": "The open-circuit voltage was 1.10 V under AM1.5."},
    {"id": 4, "page": 2, "bbox": [10, 30, 240, 40], "kind": "text",
     "quote": "RAG retrieval accuracy was 0.82 on the benchmark dataset."},
]


def mock_user(username):
    return {"id": username, "username": username, "created_at": 1763769600}


DOCS = [
    {
        "document_id": "rag:d020", "track": "rag", "publication_id": "D020",
        "title": "Web Retrieval-Aware Chunking (W-RAC) for Efficient and Cost-Effective RAG",
        "year": 2026, "pages": 13, "status": "done", "error": "",
        "results_count": 408, "issues_count": 3, "scanned": False, "ocr_used": False,
        "handler_revision": 3, "duration_ms": 12400, "updated_at": "2026-11-20T18:02:00Z",
    },
    {
        "document_id": "rag:d001", "track": "rag", "publication_id": "D001",
        "title": "Retrieval-augmented answer quality: a systematic comparison",
        "year": 2025, "pages": 16, "status": "done", "error": "",
        "results_count": 416, "issues_count": 1, "scanned": False, "ocr_used": False,
        "handler_revision": 3, "duration_ms": 15100, "updated_at": "2026-11-20T18:02:10Z",
    },
    {
        "document_id": "rag:d014", "track": "rag", "publication_id": "D014",
        "title": "A large-scale benchmark of retrieval pipelines",
        "year": 2025, "pages": 18, "status": "done", "error": "",
        "results_count": 28649, "issues_count": 0, "scanned": False, "ocr_used": False,
        "handler_revision": 2, "duration_ms": 184000, "updated_at": "2026-11-20T18:20:00Z",
    },
    {
        "document_id": "rag:d009", "track": "rag", "publication_id": "D009",
        "title": "Hybrid retrieval for scientific question answering",
        "year": 2024, "pages": 22, "status": "done", "error": "",
        "results_count": 969, "issues_count": 2, "scanned": False, "ocr_used": False,
        "handler_revision": 3, "duration_ms": 21800, "updated_at": "2026-11-20T18:02:30Z",
    },
    {
        "document_id": "rag:d013", "track": "rag", "publication_id": "D013",
        "title": "Dense passage retrieval under domain shift (scan)",
        "year": 2024, "pages": 21, "status": "partial", "error": "",
        "results_count": 0, "issues_count": 0, "scanned": True, "ocr_used": False,
        "handler_revision": 3, "duration_ms": 3200, "updated_at": "2026-11-20T18:03:00Z",
    },
    {
        "document_id": "rag:d002", "track": "rag", "publication_id": "D002",
        "title": "Query rewriting for retrieval (scan)",
        "year": 2024, "pages": 17, "status": "partial", "error": "",
        "results_count": 0, "issues_count": 0, "scanned": True, "ocr_used": False,
        "handler_revision": 3, "duration_ms": 2900, "updated_at": "2026-11-20T18:03:10Z",
    },
    {
        "document_id": "rag:test-p058", "track": "rag", "publication_id": "P058",
        "title": "Retrieval benchmarks at scale",
        "year": 2023, "pages": 0, "status": "failed",
        "error": "превышен лимит размера PDF (28,2 МиБ при лимите 20 МиБ)",
        "results_count": 0, "issues_count": 0, "scanned": False, "ocr_used": False,
        "handler_revision": 0, "duration_ms": 0, "updated_at": "2026-11-20T18:04:00Z",
    },
    {
        "document_id": "rag:d005", "track": "rag", "publication_id": "D005",
        "title": "Evaluation protocols for open-domain QA",
        "year": 2024, "pages": 24, "status": "queued", "error": "",
        "results_count": 0, "issues_count": 0, "scanned": False, "ocr_used": False,
        "handler_revision": 0, "duration_ms": 0, "updated_at": "2026-11-20T18:05:00Z",
    },
]
BY_ID = {doc["document_id"]: doc for doc in DOCS}
for _doc in DOCS:
    _doc.setdefault("authors", ["A. Author", "B. Author"])

CONFIGS = [
    {"config_id": "C01", "label": "BM25", "detail": "retrieval_method=sparse"},
    {"config_id": "C02", "label": "Dense (E5)", "detail": "retrieval_method=dense"},
    {"config_id": "C03", "label": "W-RAC", "detail": "retrieval_method=hybrid, top_k=6"},
]
DATASETS = [
    {"dataset_id": "DS1", "label": "ExampleQA", "detail": "split=test"},
    {"dataset_id": "DS2", "label": "RAG-Multi-Corpus", "detail": "split=dev"},
]
TASKS = [{"task_id": "TD", "label": "Document QA", "detail": ""}]

STATUS_LABELS = {"extracted": "извлечено", "present_not_extractable": "не извлекается",
                 "absent": "нет в статье"}

# (metric, group, base value, unit, direction, k)
POOL = [
    ("Recall", "retrieval quality", 0.81, "fraction", "higher_is_better", 5),
    ("Recall", "retrieval quality", 0.70, "fraction", "higher_is_better", 10),
    ("Precision", "retrieval quality", 0.72, "fraction", "higher_is_better", 3),
    ("Precision", "retrieval quality", 0.66, "fraction", "higher_is_better", 6),
    ("nDCG", "retrieval quality", 0.56, "fraction", "higher_is_better", 10),
    ("MRR", "retrieval quality", 0.64, "fraction", "higher_is_better", None),
    ("Queries", "dataset statistics", 200.0, "queries", None, None),
    ("Documents", "dataset statistics", 51.0, "files", None, None),
    ("token cost", "ingestion cost", 12.4, "USD/1k queries", "lower_is_better", None),
]

ISSUES = {
    1: [{"issue_id": "I1", "check_name": "metric_scale_assumed",
         "description": "Шкала принята по заголовку таблицы: 81 → 0.81"}],
    2: [{"issue_id": "I2", "check_name": "recall_decreases",
         "description": "Recall@10 (0.70) ниже Recall@5 (0.81) для того же набора"}],
    7: [{"issue_id": "I3", "check_name": "metric_scale_assumed",
         "description": "Единица указана только в подписи рисунка"}],
}

# Число замечаний в каталоге должно совпадать с вкладкой «Замечания».
ISSUES_BY_DOC = {
    "rag:d020": ISSUES,
    "rag:d001": {3: [{"issue_id": "I1", "check_name": "metric_scale_assumed",
                      "description": "Шкала принята по заголовку таблицы: 72 → 0.72"}]},
    "rag:d009": {2: [{"issue_id": "I2", "check_name": "recall_decreases",
                      "description": "Recall@10 (0.70) ниже Recall@5 (0.81) для того же набора"}],
                 6: [{"issue_id": "I3", "check_name": "missing_unit",
                      "description": "У метрики MRR не указана единица измерения"}]},
}


def anchor_box(source_id):
    """Стабильно разные подсветки, чтобы превью менялось между источниками."""
    slot = int(source_id) % 4
    top = 133 + slot * 18
    return [56, top, 330, top + 16]


def anchor(source_id, page, quote, kind="cell", table=None):
    return {
        "page": page, "section": "Results", "table": table, "figure": "",
        "fragment": quote, "source_id": source_id, "bbox": anchor_box(source_id), "kind": kind,
    }


def make_row(doc, index):
    metric, group, base, unit, direction, k = POOL[(index - 1) % len(POOL)]
    config = CONFIGS[(index - 1) % len(CONFIGS)]
    value = base if index <= len(POOL) else round(base * (0.9 + (index % 9) / 100), 4)
    if metric in {"Recall", "Precision", "nDCG", "MRR"}:
        value = min(0.99, round(value, 4))
    if unit == "fraction":
        raw_value, raw_unit = f"{value * 100:.2f}".rstrip("0").rstrip("."), "%"
    else:
        raw_value, raw_unit = f"{value:g}", unit
    table = f"Table {2 + index % 3}"
    row = {
        "result_id": f"R{index:03d}",
        "metric_name": metric,
        "metric_k": k,
        "metric_group": group,
        "metric_value": value,
        "metric_unit": unit,
        "raw_value": raw_value,
        "raw_unit": raw_unit,
        "normalized_value": value,
        "normalized_unit": unit,
        "metric_direction": direction,
        "aggregation": "" if index % 4 else "mean",
        "sample_size": "" if index % 5 else 200,
        "result_origin": "cited" if index == 9 else "primary",
        "cited_publication_id": "Lewis et al., 2020" if index == 9 else "",
        "duplicate_of": f"R{index - 9:03d}" if index > 9 and index % 41 == 0 else "",
        "note": "",
        "config_id": config["config_id"],
        "dataset_id": DATASETS[index % len(DATASETS)]["dataset_id"],
        "task_id": "TD",
        "experiment_id": "DATA" if metric in {"Queries", "Documents"} else "EVAL",
        "extraction_status": "present_not_extractable" if index in {11, 12} else "extracted",
        "source": anchor(1000 + index, 4 + index % 5,
                         f"{config['label']} | {table} | {metric} | {value}",
                         table=table),
        "header_source": anchor(2000 + index, 4, f"{metric}@k ({unit})", table=table)
        if metric != "token cost" else None,
        "issues": ISSUES_BY_DOC.get(doc["document_id"], {}).get(index, []),
    }
    if row["extraction_status"] != "extracted":
        row["metric_value"] = None
        row["normalized_value"] = None
        row["note"] = "Значение указано в supplementary, недоступном в PDF"
    return row


def all_rows(doc):
    return (make_row(doc, i) for i in range(1, doc["results_count"] + 1))


def matches(row, filters):
    if filters.get("metric") and row["metric_name"] != filters["metric"]:
        return False
    if filters.get("k") and str(row["metric_k"] or "") != filters["k"]:
        return False
    if filters.get("dataset") and row["dataset_id"] != filters["dataset"]:
        return False
    if filters.get("config") and row["config_id"] != filters["config"]:
        return False
    if filters.get("status") and row["extraction_status"] != filters["status"]:
        return False
    if filters.get("issues") and not row["issues"]:
        return False
    q = (filters.get("q") or "").lower().strip()
    if q:
        metric_at_k = f"{row['metric_name']}@{row['metric_k']}" if row["metric_k"] else row["metric_name"]
        config_label = next((item["label"] for item in CONFIGS if item["config_id"] == row["config_id"]), "")
        dataset_label = next((item["label"] for item in DATASETS if item["dataset_id"] == row["dataset_id"]), "")
        task_label = next((item["label"] for item in TASKS if item["task_id"] == row["task_id"]), "")
        fragments = " ".join(filter(None, [
            row["source"]["fragment"] if row.get("source") else "",
            row["header_source"]["fragment"] if row.get("header_source") else "",
        ]))
        haystack = " ".join(str(value) for value in (
            row["result_id"], row["metric_name"], row["metric_group"], row["metric_value"],
            row["raw_value"], row["raw_unit"], row["metric_unit"], row["normalized_value"],
            row["config_id"], row["dataset_id"], row["task_id"], row["experiment_id"],
            row["extraction_status"], row["note"], row["cited_publication_id"],
            metric_at_k, config_label, dataset_label, task_label, fragments,
            "доля" if row["metric_unit"] == "fraction" else "",
        )) + " " + " ".join(issue["description"] for issue in row["issues"])
        if q not in haystack.lower():
            return False
    return True


FACETS = {}


def int_arg(single, name, default, minimum=0, maximum=None):
    raw = single.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"Параметр {name} должен быть целым числом.")
    if value < minimum:
        raise ValueError(f"Параметр {name} не может быть меньше {minimum}.")
    return min(value, maximum) if maximum is not None else value


def page_args(single, default=50, maximum=200):
    return int_arg(single, "limit", default, 1, maximum), int_arg(single, "offset", 0, 0)


def status_counts(doc):
    missing = sum(1 for index in (11, 12) if index <= doc["results_count"])
    return {
        "extracted": doc["results_count"] - missing,
        "present_not_extractable": missing,
        "absent": 0,
    }


def facets(doc):
    key = doc["document_id"]
    if key not in FACETS:
        metrics, ks, datasets, configs, statuses = {}, {}, {}, {}, {}
        for row in all_rows(doc):
            metrics[row["metric_name"]] = metrics.get(row["metric_name"], 0) + 1
            if row["metric_k"]:
                ks[str(row["metric_k"])] = ks.get(str(row["metric_k"]), 0) + 1
            datasets[row["dataset_id"]] = datasets.get(row["dataset_id"], 0) + 1
            configs[row["config_id"]] = configs.get(row["config_id"], 0) + 1
            statuses[row["extraction_status"]] = statuses.get(row["extraction_status"], 0) + 1
        FACETS[key] = {
            "metrics": [{"value": name, "label": name, "count": count}
                        for name, count in sorted(metrics.items())],
            "ks": [{"value": value, "label": f"k={value}", "count": count}
                   for value, count in sorted(ks.items(), key=lambda item: int(item[0]))],
            "datasets": [{"value": item["dataset_id"], "label": item["label"],
                          "count": datasets[item["dataset_id"]]}
                         for item in DATASETS if datasets.get(item["dataset_id"])],
            "configs": [{"value": item["config_id"], "label": item["label"],
                         "count": configs[item["config_id"]]}
                        for item in CONFIGS if configs.get(item["config_id"])],
            "statuses": [{"value": name, "label": STATUS_LABELS.get(name, name), "count": count}
                         for name, count in sorted(statuses.items())],
        }
    return FACETS[key]


def page_pdf(pages=10):
    """Синтетический PDF для мок-превью и скачивания."""
    from reportlab.pdfgen import canvas

    output = BytesIO()
    pdf = canvas.Canvas(output, pagesize=(595, 842), invariant=True)
    for page in range(1, pages + 1):
        pdf.setFont("Helvetica-Bold", 15)
        pdf.drawString(56, 780, "Retrieval evaluation")
        pdf.setFont("Helvetica", 9)
        pdf.drawString(500, 780, f"Page {page}")
        pdf.setFont("Helvetica", 10)
        for index, line in enumerate([
            "Table 2: Composition of the RAG-Multi-Corpus benchmark.",
            "Enterprise | Domain | Files | Queries",
            "Aventro Motors | Automotive | 51 | 200",
            "Cendara University | Academia | 41 | 186",
            "Velvera Technologies | Enterprise | 39 | 200",
        ]):
            pdf.drawString(56, 730 - index * 18, line)
        pdf.showPage()
    pdf.save()
    return output.getvalue()


def page_image(page, box):
    """Настоящий PNG: нужная страница и подсветка прямоугольником."""
    from viniti.pdf_source import render

    return render(page_pdf(), page, box)


ANCHOR_INDEX = {}


def anchor_index(doc):
    """Карта source_id → якорь: нужна для глубоких ссылок вида /source/<id>."""
    key = doc["document_id"]
    if key not in ANCHOR_INDEX:
        index = {}
        for row in all_rows(doc):
            index[row["source"]["source_id"]] = row["source"]
            if row["header_source"]:
                index[row["header_source"]["source_id"]] = row["header_source"]
        ANCHOR_INDEX[key] = index
    return ANCHOR_INDEX[key]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send_data(self, data, media_type, status=200):
        self.send_response(status)
        self.send_header("Content-Type", media_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, document, status=200, headers=None):
        data = json.dumps(document, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def error(self, message, status=404):
        self.send_json({"error": message}, status)

    def session_username(self):
        for item in (self.headers.get("Cookie") or "").split(";"):
            name, _, value = item.strip().partition("=")
            if name == MOCK_SESSION_COOKIE and value:
                return unquote(value)
        return ""

    def require_session(self):
        if self.session_username():
            return True
        self.error("Требуется вход.", 401)
        return False

    def require_csrf(self):
        if hmac.compare_digest(self.headers.get("X-CSRF-Token", ""), MOCK_CSRF):
            return True
        self.error("Неверный CSRF-токен.", 403)
        return False

    def static_file(self, path):
        relative = "index.html" if path == "/" else path.lstrip("/")
        if relative in ("manual-profile", "solar", "solar.html"):
            relative = "manual-profile.html"
        if relative.startswith("static/"):
            relative = relative[len("static/"):]
        target = (STATIC / relative).resolve()
        if STATIC.resolve() not in target.parents or not target.is_file():
            return None
        return target

    def do_GET(self):
        parts = urlsplit(self.path)
        path, query = parts.path, parse_qs(parts.query)
        single = {key: values[0] for key, values in query.items()}

        if not path.startswith("/api/"):
            target = self.static_file(path)
            if target is None:
                self.error("Маршрут не найден.", 404)
                return
            media_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            self.send_data(target.read_bytes(), media_type)
            return

        if path == "/api/auth/me":
            username = self.session_username()
            if not username:
                self.send_json({"user": None})
            else:
                self.send_json({"user": mock_user(username), "csrf_token": MOCK_CSRF,
                                "expires_at": 4102444800})
            return

        if path == "/api/auth/csrf":
            username = self.session_username()
            if not username:
                self.error("Требуется вход.", 401)
            else:
                self.send_json({"csrf_token": MOCK_CSRF})
            return

        if path == "/api/documents":
            items = [doc for doc in DOCS if doc["track"] == single.get("track", "rag")]
            q = single.get("q", "").lower()
            if q:
                items = [doc for doc in items
                         if q in " ".join([
                             doc["title"], doc["publication_id"], str(doc["year"]),
                             " ".join(doc["authors"]),
                         ]).lower()]
            if single.get("status"):
                items = [doc for doc in items if doc["status"] == single["status"]]
            order = {
                "status": lambda doc: doc["status"],
                "issues": lambda doc: -doc["issues_count"],
                "results": lambda doc: -doc["results_count"],
            }.get(single.get("sort", ""), lambda doc: doc["publication_id"])
            items = sorted(items, key=order)
            limit, offset = page_args(single, default=50, maximum=200)
            self.send_json({"total": len(items), "items": items[offset:offset + limit]})
            return

        if path == "/api/jobs":
            docs = [doc for doc in DOCS if doc["track"] == single.get("track", "rag")]
            finished = sum(doc["status"] in {"done", "partial", "failed"} for doc in docs)
            self.send_json({
                "state": "done" if finished >= 90 else "running", "total": 90,
                "done": sum(doc["status"] == "done" for doc in docs),
                "partial": sum(doc["status"] == "partial" for doc in docs),
                "failed": sum(doc["status"] == "failed" for doc in docs),
                "items": [{"document_id": doc["document_id"], "status": doc["status"],
                           "error": doc["error"]} for doc in docs],
            })
            return

        if path.startswith("/api/parse/"):
            segments = [unquote(part) for part in path.split("/") if part]
            job = PARSE_JOBS.get(segments[2]) if len(segments) > 2 else None
            if job is None:
                self.error("Разбор не найден.", 404)
                return
            if len(segments) == 3:
                self.send_json(job)
                return
            if len(segments) == 4 and segments[3] == "result":
                self.send_json({
                    "document": {"pages": 2, "filename": job["filename"]},
                    "sources": MOCK_SOURCES,
                    "warnings": [],
                })
                return
            self.error("Маршрут не найден.", 404)
            return

        segments = [unquote(part) for part in path.split("/") if part]
        # /api/documents/<id>[/<resource>[/<key>]]
        if len(segments) < 3 or segments[:2] != ["api", "documents"]:
            self.error("Маршрут не найден.", 404)
            return
        document_id = segments[2]
        resource = segments[3] if len(segments) > 3 else ""
        key = segments[4] if len(segments) > 4 else ""
        doc = BY_ID.get(document_id)
        if doc is None:
            self.error("Документ не найден.", 404)
            return

        if not resource:
            self.send_json({
                **doc,
                "publication": {"publication_id": doc["publication_id"], "title": doc["title"],
                                "year": doc["year"], "authors": doc["authors"],
                                "doi": "", "in_scope": True, "exclusion_reason": ""},
                "warnings": (["Страница 3: текстовый слой пуст или отсутствует."] if doc["scanned"] else [])
                            + (["Мок: PDF принят без разбора."] if doc.get("mock_upload") else []),
                "counts": {"results": doc["results_count"], "issues": doc["issues_count"],
                           "by_status": status_counts(doc)},
                "run": {"handler": "rag", "source_revision": 2,
                        "model": "mock-structured-v1", "duration_ms": doc["duration_ms"]},
                "export": {"json": f"/api/documents/{document_id}/export.json",
                           "csv": f"/api/documents/{document_id}/export.csv"},
            })
            return

        if resource == "facets":
            self.send_json(facets(doc) if not doc["scanned"] else
                           {"metrics": [], "ks": [], "datasets": [], "configs": [], "statuses": []})
            return

        if resource == "pdf":
            self.send_data(page_pdf(), "application/pdf")
            return

        if resource == "results":
            try:
                limit, offset = page_args(single)
            except ValueError as error:
                self.error(str(error), 400)
                return
            rows = [row for row in all_rows(doc) if matches(row, single)]
            self.send_json({"total": len(rows), "items": rows[offset:offset + limit]})
            return

        if resource == "issues":
            try:
                limit, offset = page_args(single, default=200, maximum=500)
            except ValueError as error:
                self.error(str(error), 400)
                return
            items = []
            for row in all_rows(doc):
                for issue in row["issues"]:
                    items.append({
                        **issue,
                        "record_ids": issue.get("record_ids") or [row["result_id"]],
                        "source": row["source"],
                    })
            self.send_json({"total": len(items), "items": items[offset:offset + limit]})
            return

        if resource == "sources":
            if doc["scanned"] and not doc["ocr_used"]:
                self.send_json({"total": 0, "items": []})
                return
            try:
                limit = int_arg(single, "limit", 200, 1, 500)
            except ValueError as error:
                self.error(str(error), 400)
                return
            kind = single.get("kind", "")
            q = single.get("q", "").lower()
            items = []
            if not kind or kind == "table":
                for row in all_rows(doc):
                    anchor = row["source"]
                    if q and q not in anchor["fragment"].lower():
                        continue
                    items.append({"source_id": anchor["source_id"], "page": anchor["page"],
                                  "kind": "table", "quote": anchor["fragment"]})
            if not kind or kind == "text":
                for offset, quote in enumerate([
                    "Following prior work, we use the same evaluation protocol.",
                    "We report Recall@5 and Recall@10 on the development split.",
                    "All runs use a single NVIDIA A100 with 40 GB of memory.",
                ]):
                    if not q or q in quote.lower():
                        items.append({"source_id": 90000 + offset, "page": 2,
                                      "kind": "text", "quote": quote})
            items.sort(key=lambda item: (item["page"], item["source_id"]))
            self.send_json({"total": len(items), "items": items[:limit]})
            return

        if resource == "entities":
            kind = single.get("type", "configurations")
            table = {"configurations": CONFIGS, "datasets": DATASETS,
                     "tasks": TASKS, "experiments": [{"experiment_id": "EVAL", "label": "Evaluation"},
                                                     {"experiment_id": "DATA", "label": "Dataset statistics"}]}
            self.send_json({"total": len(table.get(kind, [])), "items": table.get(kind, [])})
            return

        if resource == "anchors" and key.endswith(".json"):
            try:
                sid = int_arg({"source_id": key[:-5]}, "source_id", 0, 0)
            except ValueError:
                self.error("Идентификатор источника должен быть числом.", 400)
                return
            anchor = anchor_index(doc).get(sid)
            if anchor is None:
                self.error("Источник не найден.", 404)
                return
            self.send_json({**anchor, "bbox": anchor_box(sid)})
            return

        if resource == "anchors" and key.endswith(".png"):
            try:
                sid = int_arg({"source_id": key[:-4]}, "source_id", 0, 0)
            except ValueError:
                self.error("Идентификатор источника должен быть числом.", 400)
                return
            anchor = anchor_index(doc).get(sid) or {}
            self.send_data(page_image(anchor.get("page", 1), anchor_box(sid)), "image/png")
            return

        if resource == "export.json":
            rows = list(all_rows(doc))
            self.send_json({**doc, "results": rows})
            return

        if resource == "export.csv":
            output = StringIO()
            writer = csv.writer(output)
            columns = [
                "publication_id", "result_id", "experiment_id", "config_id", "dataset_id",
                "task_id", "metric_group", "metric_name", "metric_variant", "metric_direction",
                "metric_k", "metric_value", "metric_unit", "baseline_value", "aggregation",
                "sample_size", "raw_value", "raw_unit", "normalized_value", "normalized_unit",
                "result_origin", "cited_publication_id", "source_page", "source_section",
                "source_table", "source_figure", "source_fragment", "extraction_status",
                "duplicate_of", "note",
            ]
            writer.writerow(columns)
            for row in all_rows(doc):
                source = row.get("source") or {}
                writer.writerow([
                    doc["publication_id"], row["result_id"], row["experiment_id"],
                    row["config_id"], row["dataset_id"], row["task_id"], row["metric_group"],
                    row["metric_name"], "", row["metric_direction"], row["metric_k"] or "",
                    row["metric_value"] if row["metric_value"] is not None else "",
                    row["metric_unit"], "", row["aggregation"],
                    row["sample_size"], row["raw_value"], row["raw_unit"],
                    row["normalized_value"] if row["normalized_value"] is not None else "",
                    row["normalized_unit"], row["result_origin"], row["cited_publication_id"],
                    source.get("page", ""), source.get("section", ""), source.get("table", ""),
                    source.get("figure", ""), source.get("fragment", ""),
                    row["extraction_status"], row["duplicate_of"], row["note"],
                ])
            self.send_data(output.getvalue().encode("utf-8-sig"), "text/csv; charset=utf-8")
            return

        self.error("Маршрут не найден.", 404)

    def do_POST(self):
        path = urlsplit(self.path).path
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            length = 0
        limit = 40 * 1024 * 1024 if path == "/api/profile/source" else MAX_BYTES
        if length > limit:
            self.error(f"Файл больше {limit // (1024 * 1024)} МиБ.", 413)
            return
        body = self.rfile.read(length) if length else b""
        if path in ("/api/auth/register", "/api/auth/login"):
            try:
                payload = json.loads(body or b"{}")
            except (UnicodeError, json.JSONDecodeError):
                self.error("Тело должно быть корректным JSON в UTF-8.", 400)
                return
            username = str(payload.get("username") or "").strip()
            password = str(payload.get("password") or "")
            if not username or len(password) < 8:
                self.error("Некорректное имя пользователя или пароль.", 422)
                return
            if path.endswith("register"):
                if username in MOCK_USERS:
                    self.error("Пользователь с таким логином или email уже существует.", 409)
                    return
                MOCK_USERS[username] = password
            elif MOCK_USERS.get(username) != password:
                self.error("Неверное имя пользователя или пароль.", 401)
                return
            cookie = f"{MOCK_SESSION_COOKIE}={quote(username)}; Path=/; HttpOnly; SameSite=Lax"
            self.send_json({"user": mock_user(username), "csrf_token": MOCK_CSRF,
                            "expires_at": 4102444800},
                           201 if path.endswith("register") else 200,
                           headers={"Set-Cookie": cookie})
            return
        if path == "/api/auth/logout":
            if self.session_username() and not self.require_csrf():
                return
            self.send_json({"ok": True}, headers={
                "Set-Cookie": f"{MOCK_SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"})
            return
        if path in ("/api/documents", "/api/demo", "/api/profile/source", "/api/parse"):
            if not self.require_session() or not self.require_csrf():
                return
        if path == "/api/parse":
            run_id = "mock-" + uuid.uuid4().hex[:10]
            filename = unquote(self.headers.get("X-Filename", "article.pdf"))
            PARSE_JOBS[run_id] = {
                "id": run_id, "status": "ok", "profile": self.headers.get("X-Profile", "solar"),
                "parser": "docling", "filename": filename, "warnings": [],
            }
            self.send_json(PARSE_JOBS[run_id], 202)
            return
        if path == "/api/documents" and self.headers.get_content_type() != "application/pdf":
            self.error("Ожидается application/pdf.", 415)
            return
        if path == "/api/documents" and length == 0:
            self.error("Нужен PDF размером до 20 МиБ.", 413)
            return
        if path == "/api/demo":
            track = self.headers.get("X-Track", "rag")
            if track == "solar":
                self.error("Демо для области Solar ещё не подключено.", 404)
                return
            self.send_json(BY_ID["rag:d020"], 201)
            return
        if path == "/api/profile/source":
            self.send_json({
                "title": "Synthetic profiling article", "author": "Mock",
                "pages": 2, "warnings": [], "page_texts": {"1": "mock"},
                "fallback_pages": [],
                "sources": MOCK_SOURCES,
            })
            return
        if path != "/api/documents":
            self.error("Маршрут не найден.", 404)
            return
        track = self.headers.get("X-Track", "rag")
        if track not in TRACKS:
            self.error("Неизвестная область.", 400)
            return
        name = unquote(self.headers.get("X-Filename", "article.pdf"))
        stem = Path(name).stem
        slug = "".join(ch for ch in stem if ch.isalnum() or ch in "-_")[:40] or "article"
        new_id = f"{track}:{slug.lower()}"
        if new_id not in BY_ID:
            template = dict(BY_ID["rag:d005"])
            template.update(document_id=new_id, track=track, publication_id=slug.upper(), title=name,
                            status="partial", results_count=0, issues_count=0, pages=1,
                            mock_upload=True, authors=["A. Author", "B. Author"])
            BY_ID[new_id] = template
            DOCS.append(template)
        self.send_json(BY_ID[new_id], 201)


def main():
    parser = argparse.ArgumentParser(description="Mock API for the catalog UI (dev only)")
    parser.add_argument("--port", type=int, default=8799)
    args = parser.parse_args()
    ThreadingHTTPServer.request_queue_size = 32
    with ThreadingHTTPServer(("127.0.0.1", args.port), Handler) as server:
        print(f"http://127.0.0.1:{server.server_port}", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()