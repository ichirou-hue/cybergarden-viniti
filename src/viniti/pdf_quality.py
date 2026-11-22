"""Explainable technical-readiness heuristic, never a probability.

OCR-D and OmniDocBench motivate reporting text, structure and provenance
separately. They do not supply or validate the weights and thresholds below.
Without ground truth this score cannot measure CER/WER, structural accuracy,
scientific truth, or completeness of extraction.
"""

from .verify import compact

WEIGHTS = {
    "text_layer": 30,
    "text_density": 15,
    "ocr_dependency": 15,
    "tables": 15,
    "evidence": 15,
    "cleanliness": 10,
}
LABELS = {
    "text_layer": "Покрытие извлечённым текстом",
    "text_density": "Объём извлечённого текста",
    "ocr_dependency": "Зависимость от OCR",
    "tables": "Наличие табличных фрагментов",
    "evidence": "Количество источников",
    "cleanliness": "Предупреждения разбора",
}
DENSITY_TARGET = 1500
SOURCES_PER_PAGE = 6
WARNING_PENALTY = 0.15
RECOGNITION_WARNING_PENALTY = 0.10
SUCCESS_STATUSES = {"ok", "done"}


def _source_text(source):
    for name in ("fragment", "quote", "display_text", "text"):
        text = source.get(name)
        if isinstance(text, str) and compact(text):
            return text.strip()
    return ""


def _component(key, value, detail):
    weight = WEIGHTS[key]
    value = max(0.0, min(1.0, value)) if value is not None else None
    return {"key": key, "label": LABELS[key], "weight": weight,
            "value": round(value, 3) if value is not None else None,
            "points": round(value * weight, 1) if value is not None else None,
            "available": value is not None, "detail": detail}


def _sources(envelope, pages):
    """Combine native, result-anchor and recognition sources without duplicates.

    Solar text_sources deliberately excludes tables; the same sources exist in
    anchors and results[].source. Recognition sources also occur in anchors.
    Empty/invalid-page records are not evidence that a page contains text.
    """
    raw = []
    for name in ("text_sources", "recognized_sources"):
        value = envelope.get(name)
        if isinstance(value, list):
            raw.extend(value)
    anchors = envelope.get("anchors")
    if isinstance(anchors, dict):
        raw.extend(anchors.values())
    results = envelope.get("results")
    if isinstance(results, list):
        raw.extend(row.get("source") for row in results if isinstance(row, dict))
    seen = set()
    valid = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        page, text = item.get("page"), _source_text(item)
        if type(page) is not int or not 1 <= page <= pages or not text:
            continue
        marker = (page, compact(text))
        if marker in seen:
            continue
        seen.add(marker)
        valid.append({**item, "_text": text})
    return valid


def assess(envelope):
    """Return status/score/components/inputs/limits with unknown signals explicit.

    A score requires a successful parse, page count, source inventory, warning
    inventory, explicit OCR usage and at least one usable textual source.
    Missing metadata, partial/active/failed parsing and empty extraction yield
    status=insufficient and score=None. Low observed coverage remains measurable.
    """
    if not isinstance(envelope, dict):
        raise ValueError("Нужен конверт документа.")
    pages = envelope.get("pages")
    valid_pages = type(pages) is int and pages > 0
    status = envelope.get("status")
    ocr_used = envelope.get("ocr_used")
    ocr_known = type(ocr_used) is bool
    warnings = envelope.get("warnings")
    warning_known = isinstance(warnings, list)
    recognition_warnings = envelope.get("recognition_warnings", [])
    recognition_warning_known = isinstance(recognition_warnings, list)
    inventory_known = (isinstance(envelope.get("text_sources"), list)
                       or isinstance(envelope.get("anchors"), dict)
                       or isinstance(envelope.get("recognized_sources"), list))
    sources = _sources(envelope, pages) if valid_pages else []
    covered = {source["page"] for source in sources}
    total_chars = sum(len(source["_text"]) for source in sources)
    table_sources = sum(source.get("kind") == "table" for source in sources)
    recognized = envelope.get("recognized_sources")
    recognized_count = len(recognized) if isinstance(recognized, list) else None
    inputs = {
        "pages": pages if valid_pages else None,
        "pages_with_text": len(covered),
        "empty_pages": pages - len(covered) if valid_pages and inventory_known else None,
        "text_sources": len(envelope["text_sources"]) if isinstance(envelope.get("text_sources"), list) else None,
        "recognized_sources": recognized_count,
        "usable_sources": len(sources), "table_sources": table_sources,
        "results": len(envelope["results"]) if isinstance(envelope.get("results"), list) else None,
        "issues": len(envelope["issues"]) if isinstance(envelope.get("issues"), list) else None,
        "warnings": len(warnings) if warning_known else None,
        "recognition_warnings": len(recognition_warnings) if recognition_warning_known else None,
        "ocr_used": ocr_used if ocr_known else None,
        "scanned": envelope.get("scanned") if type(envelope.get("scanned")) is bool else None,
        "parser_status": status if isinstance(status, str) else None,
    }
    limits = [
        "Оценка технической пригодности разбора, а не научной достоверности статьи.",
        "Веса и пороги - собственная эвристика проекта; это не опубликованная метрика и не вероятность правильного извлечения.",
        "CER/WER и структурная точность не измеряются без эталона. Полнота таблиц, рисунков и формул не гарантируется.",
        "Большое число фрагментов и символов не доказывает правильность распознавания. Перекрывающиеся фрагменты могут завышать объём текста.",
        "Страница без найденного текста может содержать иллюстрацию или быть пустой; причина требует проверки.",
        "Отсутствие таблиц не означает дефект документа: компонент фиксирует лишь наблюдаемые табличные фрагменты.",
    ]
    if not valid_pages:
        return {"status": "insufficient", "score": None, "components": [],
                "inputs": inputs, "limits": limits + ["Неизвестно число страниц."]}
    measurable = inventory_known and bool(sources)
    coverage = len(covered) / pages if measurable else None
    density = min(1.0, total_chars / len(covered) / DENSITY_TARGET) if measurable else None
    ocr_value = (0.45 if ocr_used else 1.0) if ocr_known else None
    penalty = (WARNING_PENALTY * len(warnings)
               + RECOGNITION_WARNING_PENALTY * len(recognition_warnings)) \
        if warning_known and recognition_warning_known else None
    components = [
        _component("text_layer", coverage, f"Страниц с непустыми фрагментами: {len(covered)} из {pages}."),
        _component("text_density", density, f"Символов в уникальных фрагментах: {total_chars}; порог насыщения {DENSITY_TARGET} на покрытую страницу."),
        _component("ocr_dependency", ocr_value,
                   "OCR использован; точность распознавания не измерена." if ocr_used is True else
                   "Парсер явно сообщил, что OCR не использован." if ocr_used is False else
                   "Нет сведений об использовании OCR. Сам Docling не означает OCR."),
        _component("tables", (1.0 if table_sources else 0.5) if measurable else None,
                   f"Непустых табличных фрагментов: {table_sources}. При отсутствии используется нейтральное значение 0.5; полнота неизвестна."),
        _component("evidence", min(1.0, len(sources) / (pages * SOURCES_PER_PAGE)) if measurable else None,
                   f"Уникальных источников с текстом и страницей: {len(sources)}; порог насыщения {SOURCES_PER_PAGE} на страницу. Буквальность не оценивается."),
        _component("cleanliness", max(0.0, 1.0 - penalty) if penalty is not None else None,
                   f"Предупреждений: {inputs['warnings']}; распознавания: {inputs['recognition_warnings']}. Замечания о научных результатах также могут входить в warnings."),
    ]
    sufficient = isinstance(status, str) and status in SUCCESS_STATUSES and all(item["available"] for item in components)
    if not sufficient:
        limits.append("Недостаточно данных: нужен завершённый разбор, непустые источники и явные сведения об OCR и предупреждениях.")
    return {"status": "ok" if sufficient else "insufficient",
            "score": round(sum(item["points"] for item in components)) if sufficient else None,
            "components": components, "inputs": inputs, "limits": limits}
