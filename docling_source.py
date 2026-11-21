"""Optional local Docling adapter. No Docling imports on the default path.

Chart outputs are visual candidates, never literal-verified text facts.
"""

import csv
from hashlib import sha256
from importlib.metadata import PackageNotFoundError, version
from io import BytesIO, StringIO
import math
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
from time import monotonic

import pdfplumber
from pdfplumber.utils.exceptions import PdfminerException

from pdf_source import MAX_PAGES
from solar_facts import MAX_BYTES, NUMBER, clean


MAX_ASSET_BYTES = 32 * 1024 * 1024
MAX_TABLE_CELLS = 100000


def make_converter(*, ocr="auto", charts=False):
    if ocr not in {"auto", "full", "off"}:
        raise ValueError("OCR должен быть auto, full или off.")
    try:
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions, TesseractCliOcrOptions
        from docling.document_converter import DocumentConverter, PdfFormatOption
    except ImportError:
        raise ValueError("Docling недоступен. Установите requirements-docling.txt в отдельном окружении Python 3.11+.") from None
    options = PdfPipelineOptions()
    options.do_ocr = ocr != "off"
    options.generate_picture_images = True
    options.generate_page_images = False
    options.images_scale = 2.0
    options.enable_remote_services = False
    if hasattr(options, "do_chart_extraction"):
        options.do_chart_extraction = False
    if hasattr(options, "do_picture_description"):
        options.do_picture_description = False
    if options.do_ocr:
        if not shutil.which("tesseract"):
            raise ValueError("Для Docling OCR требуется установленный tesseract с языком eng.")
        # Prefer current API, retaining compatibility with the pinned release.
        try:
            from docling.datamodel.pipeline_options import OcrMode
        except ImportError:
            options.ocr_options = TesseractCliOcrOptions(lang=["eng"], force_full_page_ocr=ocr == "full")
        else:
            options.ocr_options = TesseractCliOcrOptions(
                lang=["eng"], mode=OcrMode.FULL_PAGE if ocr == "full" else OcrMode.DEFAULT,
            )
    if charts:
        if not hasattr(options, "do_chart_extraction") or not hasattr(options, "chart_extraction_options"):
            raise ValueError("Установленный Docling не поддерживает chart extraction.")
        options.do_picture_classification = True
        options.do_chart_extraction = True
        chart_options = options.chart_extraction_options
        chart_options.model_spec.trust_remote_code = False
        for name, value in (("chart2csv", True), ("chart2code", False), ("chart2summary", False)):
            if not hasattr(chart_options, name):
                raise ValueError("Несовместимый chart API Docling; обновите optional-окружение.")
            setattr(chart_options, name, value)
    return DocumentConverter(format_options={
        InputFormat.PDF: PdfFormatOption(pipeline_options=options),
    })


def page_anchor(item, pages):
    provenance = item.get("prov")
    if not isinstance(provenance, list) or len(provenance) != 1:
        raise ValueError("Нужен один проверяемый page/bbox; составной элемент не локализован.")
    prov = provenance[0]
    if not isinstance(prov, dict):
        raise ValueError("Некорректный provenance Docling.")
    page = prov.get("page_no")
    if type(page) is not int or str(page) not in pages:
        raise ValueError("Неизвестная страница Docling.")
    page_data = pages[str(page)]
    if not isinstance(page_data, dict):
        raise ValueError("Некорректная страница Docling.")
    size, box = page_data.get("size", {}), prov.get("bbox", {})
    if not isinstance(size, dict) or not isinstance(box, dict):
        raise ValueError("Некорректные bbox/size Docling.")
    values = [size.get("width"), size.get("height"), *[box.get(k) for k in ("l", "t", "r", "b")]]
    if any(type(value) not in {int, float} or not math.isfinite(value) for value in values):
        raise ValueError("Некорректные размеры/координаты Docling.")
    width, height, left, top, right, bottom = values
    if box.get("coord_origin") == "BOTTOMLEFT":
        top, bottom = height - top, height - bottom
    elif box.get("coord_origin") != "TOPLEFT":
        raise ValueError("Неизвестное начало координат Docling.")
    if not (0 <= left < right <= width and 0 <= top < bottom <= height):
        raise ValueError("BBox вне страницы Docling.")
    return {"page": page, "bbox": [left, top, right, bottom], "bbox_scope": "block",
            "extraction_method": "docling", "verification": "parser_text_only"}


def table_grid(data):
    if not isinstance(data, dict):
        raise ValueError("Неизвестный формат таблицы Docling.")
    rows, columns = data.get("num_rows"), data.get("num_cols")
    if any(type(n) is not int or not 0 <= n <= MAX_TABLE_CELLS for n in (rows, columns)) or rows * columns > MAX_TABLE_CELLS:
        raise ValueError("Неподдерживаемый размер таблицы Docling.")
    cells = data.get("table_cells")
    if not isinstance(cells, list) or len(cells) > MAX_TABLE_CELLS:
        raise ValueError("Неизвестные ячейки таблицы Docling.")
    grid = [[""] * columns for _ in range(rows)]
    occupied = set()
    for cell in cells:
        if not isinstance(cell, dict):
            raise ValueError("Неверная ячейка Docling.")
        r, c = cell.get("start_row_offset_idx"), cell.get("start_col_offset_idx")
        end_r, end_c = cell.get("end_row_offset_idx"), cell.get("end_col_offset_idx")
        if (any(type(n) is not int for n in (r, c, end_r, end_c))
                or not 0 <= r < end_r <= rows or not 0 <= c < end_c <= columns
                or (r, c) in occupied or not isinstance(cell.get("text"), str)):
            raise ValueError("Некорректные offsets/текст ячейки Docling.")
        positions = {(i, j) for i in range(r, end_r) for j in range(c, end_c)}
        if occupied & positions:
            raise ValueError("Перекрывающиеся spans таблицы Docling.")
        occupied.update(positions)
        # Keep spans in table metadata, do not duplicate values into covered cells.
        grid[r][c] = cell["text"]
    return grid


def csv_bytes(grid):
    stream = StringIO(newline="")
    csv.writer(stream).writerows(grid)
    return stream.getvalue().encode("utf-8")


def table_metadata(data):
    return {key: data[key] for key in ("num_rows", "num_cols", "table_cells", "orientation") if key in data}


def normalize_document(raw, *, charts=False):
    """Normalize the versioned DoclingDocument JSON contract, not inferred text."""
    if not isinstance(raw, dict) or not isinstance(raw.get("pages"), dict) or not raw["pages"]:
        raise ValueError("Docling не вернул страницы документа.")
    sources, tables, figures, visual_candidates, warnings, assets = [], [], [], [], [], {}
    pages = {str(key): value for key, value in raw["pages"].items()}
    for collection in ("texts", "tables", "pictures"):
        if not isinstance(raw.get(collection, []), list):
            raise ValueError("Неверная коллекция DoclingDocument.")
    for item in raw.get("texts", []):
        if not isinstance(item, dict):
            raise ValueError("Неверный текстовый элемент Docling.")
        text = item.get("text", "")
        if not isinstance(text, str):
            raise ValueError("Неверный текст Docling.")
        if not text.strip():
            continue
        try:
            anchor = page_anchor(item, pages)
        except ValueError as error:
            warnings.append(f"Текст пропущен: {error}")
            continue
        sources.append({**anchor, "quote": text, "kind": "caption" if item.get("label") == "caption" else "text"})
    for index, item in enumerate(raw.get("tables", []), 1):
        if not isinstance(item, dict):
            raise ValueError("Неверный табличный элемент Docling.")
        try:
            anchor = page_anchor(item, pages)
            grid = table_grid(item.get("data"))
        except ValueError as error:
            warnings.append(f"Таблица {index} пропущена: {error}")
            continue
        path = f"assets/tables/table-{index:04}.csv"
        assets[path] = csv_bytes(grid)
        tables.append({**anchor, "id": index, "csv": path, "data": table_metadata(item["data"]),
                       "status": "requires_review"})
        for row_index, row in enumerate(grid, 1):
            if any(row):
                cells = [{**anchor, "quote": text, "kind": "cell", "bbox_scope": "table"} if text else None for text in row]
                sources.append({**anchor, "bbox_scope": "table", "quote": " | ".join(row),
                                "kind": "table", "table": index, "row": row_index, "cells": cells})
    for index, item in enumerate(raw.get("pictures", []), 1):
        if not isinstance(item, dict):
            raise ValueError("Неверное изображение Docling.")
        try:
            anchor = page_anchor(item, pages)
        except ValueError as error:
            warnings.append(f"Изображение {index} пропущено: {error}")
            continue
        captions = []
        for reference in item.get("captions", []):
            if not isinstance(reference, dict):
                continue
            pointer = reference.get("$ref", reference.get("cref", ""))
            if not isinstance(pointer, str):
                continue
            parts = pointer.split("/")
            if len(parts) == 3 and parts[:2] == ["#", "texts"] and parts[2].isascii() and parts[2].isdigit():
                position = int(parts[2])
                if position < len(raw.get("texts", [])):
                    captions.append(raw["texts"][position].get("text", ""))
        figure = {**anchor, "id": index, "image": f"assets/figures/figure-{index:04}.png",
                  "caption": " ".join(captions), "status": "requires_review"}
        figures.append(figure)
        metadata = item.get("meta") or {}
        if not isinstance(metadata, dict):
            warnings.append(f"Изображение {index}: неподдерживаемый формат метаданных.")
            continue
        chart = metadata.get("tabular_chart")
        if charts and chart is not None:
            try:
                grid = table_grid(chart.get("chart_data"))
            except (ValueError, AttributeError) as error:
                warnings.append(f"График {index}: неподдерживаемый chart payload: {error}")
                continue
            if not grid or not any(
                NUMBER.fullmatch(clean(value).removesuffix("%").strip()) for row in grid for value in row
            ):
                warnings.append(f"График {index}: числовые ячейки не получены; crop сохранён для проверки.")
                continue
            path = f"assets/charts/chart-{index:04}.csv"
            assets[path] = csv_bytes(grid)
            visual_candidates.append({
                **anchor, "verification": "visual_unverified", "figure_id": index,
                "image": figure["image"], "caption": figure["caption"], "csv": path,
                "status": "requires_review", "method": "docling_chart2csv",
                "data": {key: chart[key] for key in ("title", "confidence", "created_by") if key in chart}
                        | {"chart_data": table_metadata(chart["chart_data"])},
            })
    if sum(len(content) for content in assets.values()) > MAX_ASSET_BYTES:
        raise ValueError("Таблицы/графики превышают лимит артефактов 32 МиБ.")
    if charts and not visual_candidates:
        warnings.append("Chart extraction запрошен, но числовые данные графиков не получены. Это не доказательство отсутствия графиков.")
    sources.sort(key=lambda source: (source["page"], source["bbox"][1], source["bbox"][0]))
    for index, source in enumerate(sources, 1):
        source["id"] = index
    return {
        "pages": len(pages), "sources": sources, "tables": tables, "figures": figures,
        "visual_candidates": visual_candidates, "warnings": warnings,
    }, assets


def prepare_pdf(name, data, *, ocr="auto", charts=False, converter=None):
    started = monotonic()
    if not data.startswith(b"%PDF-") or len(data) > MAX_BYTES:
        raise ValueError("Нужен PDF размером до 40 МиБ.")
    try:
        with pdfplumber.open(BytesIO(data)) as pdf:
            total_pages = len(pdf.pages)
    except PdfminerException:
        raise ValueError("PDF повреждён, зашифрован или не может быть разобран.") from None
    if not 1 <= total_pages <= MAX_PAGES:
        raise ValueError(f"Поддерживаются PDF от 1 до {MAX_PAGES} страниц.")
    publication_id = Path(name.replace("\\", "/")).stem
    if not publication_id or len(publication_id) > 240:
        raise ValueError("Некорректное имя публикации.")
    if converter is None:
        converter = make_converter(ocr=ocr, charts=charts)
    with TemporaryDirectory(prefix="docling-input-") as temporary:
        path = Path(temporary) / "input.pdf"
        path.write_bytes(data)
        try:
            conversion = converter.convert(path, max_num_pages=MAX_PAGES, max_file_size=MAX_BYTES)
        except Exception:
            # Third-party model/hub errors may contain URLs or credentials.
            raise ValueError("Docling conversion не завершён. Проверьте optional-зависимости, Tesseract, доступ к весам моделей и память.") from None
        status = getattr(conversion.status, "value", conversion.status)
        if status != "success":
            raise ValueError("Docling вернул неполную/неуспешную конверсию; результат не публикуется.")
        raw = conversion.document.export_to_dict()
        if not isinstance(raw, dict) or not isinstance(raw.get("pages"), dict) or {
            str(key) for key in raw["pages"]
        } != {str(page) for page in range(1, total_pages + 1)}:
            raise ValueError("Нумерация страниц Docling не соответствует исходному PDF.")
        parsed, assets = normalize_document(raw, charts=charts)
        if parsed["pages"] != total_pages:
            raise ValueError("Docling обработал не все страницы; молчаливый частичный результат запрещён.")
        picture_objects = conversion.document.pictures
        for figure in parsed["figures"]:
            picture = picture_objects[figure["id"] - 1]
            image = picture.get_image(conversion.document)
            if image is None:
                raise ValueError("Docling не сохранил crop изображения; нельзя опубликовать неподтверждённый указатель.")
            stream = BytesIO()
            image.save(stream, format="PNG")
            assets[figure["image"]] = stream.getvalue()
            if sum(len(content) for content in assets.values()) > MAX_ASSET_BYTES:
                raise ValueError("Изображения и таблицы превышают лимит артефактов 32 МиБ.")
    if not parsed["sources"] and not parsed["figures"]:
        raise ValueError("Docling не извлёк текст/таблицы/изображения; проверьте OCR.")
    try:
        installed = version("docling")
    except PackageNotFoundError:
        installed = "test-double"
    parser_output = {
        key: parsed[key] for key in ("tables", "figures", "visual_candidates")
    }
    parser_output["parser"] = {
        "name": "docling", "version": installed, "ocr": ocr, "charts_requested": charts,
        "conversion_status": status, "document_schema_version": raw.get("version", ""),
        "elapsed_seconds": round(monotonic() - started, 6),
    }
    return {
        "publication_id": publication_id, "sha256": sha256(data).hexdigest(), "title": "", "author": "",
        "pages": total_pages, "sources": parsed["sources"], "parser_output": parser_output,
        "warnings": [*parsed["warnings"],
                     "Docling text/OCR проверяется только по результату парсера: цифры необходимо сверять с изображением страницы.",
                     "Данные графиков — visual candidates requires_review; они не превращаются автоматически в подтверждённые метрики."],
    }, assets
