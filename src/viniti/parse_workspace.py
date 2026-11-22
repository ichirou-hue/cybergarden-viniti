"""Read-only workspace view of a completed generic profile parse.

Sources retain their actual page and parser identity. Offline candidates are
not promoted to scientific results. LLM artifacts remain separate files.
"""

import json


def load_envelope(folder, job):
    """Caller must first verify the job owner, id and completed status."""
    if job.get("status") != "ok":
        raise ValueError("Результат разбора ещё не готов.")
    result = json.loads((folder / "bundle" / "result.json").read_text(encoding="utf-8"))
    if not isinstance(result, dict) or not isinstance(result.get("document"), dict):
        raise ValueError("Некорректный результат разбора.")
    document = result["document"]
    pages = document.get("pages")
    if type(pages) is not int or pages < 1:
        raise ValueError("В результате разбора отсутствуют страницы.")
    sources = []
    for source in result.get("sources") or []:
        if not isinstance(source, dict):
            continue
        page, quote = source.get("page"), source.get("quote")
        if type(page) is not int or not 1 <= page <= pages or not isinstance(quote, str) or not quote.strip():
            continue
        item = dict(source)
        item.update(source_id=source.get("id"), fragment=quote)
        sources.append(item)
    parser = (result.get("run") or {}).get("parser") or {}
    warnings = list(result.get("warnings") or [])
    warnings.append("Профильный разбор: источники и кандидаты не означают подтверждённые научные результаты.")
    if parser.get("name") == "docling":
        warnings.append("Источники распознаны Docling; режим OCR не подтверждает фактическое применение OCR на каждой странице.")
    return {
        "document_id": "parse:" + job["id"],
        "track": "universal", "status": "ok", "pages": pages,
        "title": job.get("filename") or document.get("publication_id") or job["id"],
        "publication_id": document.get("publication_id") or job["id"],
        "publication": {"publication_id": document.get("publication_id") or job["id"],
                        "title": job.get("filename") or "", "authors": []},
        "profile": result.get("profile") or {},
        "text_sources": sources if parser.get("name") != "docling" else [],
        "recognized_sources": sources if parser.get("name") == "docling" else [],
        "entities": {}, "results": [], "issues": [],
        "counts": {"results": 0, "issues": 0}, "warnings": warnings,
        "run": result.get("run") or {},
        # OCR mode is a request, not evidence that OCR actually ran.
        "ocr_used": False if parser.get("name") == "pdfplumber" else None,
        "scanned": None,
        "coverage": result.get("coverage") or {},
    }
