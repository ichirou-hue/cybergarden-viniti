from collections import defaultdict
from io import BytesIO
import re

import pdfplumber


MAX_PAGES = 250
X_TOLERANCE = 1.5
CAPTION = re.compile(r"^(?:fig(?:ure)?\.?|table|рис(?:унок)?\.?|таблица)\s+\d+\s*[.:–—]", re.I)
EQUATION = re.compile(r"^\w+(?:\([^)]{1,30}\))?\s*=\s*(?![+-]?\d+\s*%?\s*$)\S")


def bounds(words):
    return [
        min(word["x0"] for word in words), min(word["top"] for word in words),
        max(word["x1"] for word in words), max(word["bottom"] for word in words),
    ]


def text_anchor(words, page, kind, **details):
    text = " ".join(word["text"] for word in words)
    return {
        "page": page.page_number, "bbox": bounds(words), "quote": text,
        "kind": kind, **details,
    }


def is_inside(word, box):
    x = (word["x0"] + word["x1"]) / 2
    y = (word["top"] + word["bottom"]) / 2
    return box[0] <= x <= box[2] and box[1] <= y <= box[3]


def _pymupdf_page_words(data, page_number):
    """Fallback word extraction for pages pdfplumber cannot parse into words.

    Returns word dicts compatible with the source shape used by this module:
    x0/top/x1/bottom/text. Coordinates use the same top-origin page space as
    pdfplumber. Any import/runtime failure returns an empty list so callers can
    keep the existing empty-text warning behavior.
    """
    try:
        import fitz  # PyMuPDF
    except Exception:
        return []
    try:
        with fitz.open(stream=data, filetype="pdf") as doc:
            if not 1 <= page_number <= len(doc):
                return []
            page = doc[page_number - 1]
            raw = page.get_text("words") or []
    except Exception:
        return []
    words = []
    for item in raw:
        if len(item) < 5:
            continue
        x0, y0, x1, y1, text = item[:5]
        text = str(text).strip()
        if not text:
            continue
        words.append({
            "x0": float(x0), "top": float(y0), "x1": float(x1), "bottom": float(y1),
            "text": text,
            "_block": int(item[5]) if len(item) > 5 else 0,
            "_line": int(item[6]) if len(item) > 6 else 0,
        })
    words.sort(key=lambda word: (word["_block"], word["_line"], word["top"], word["x0"]))
    return words


def _pymupdf_line_sources(words, page_number):
    """Build line-level text sources from PyMuPDF words.

    Grouping uses PyMuPDF block/line ids when present, then falls back to top
    proximity. This is only used when pdfplumber word extraction is empty; table
    detection stays with pdfplumber and OCR is still absent.
    """
    if not words:
        return []
    groups = []
    current = [words[0]]
    current_key = (words[0].get("_block"), words[0].get("_line"))
    current_top = words[0]["top"]
    for word in words[1:]:
        key = (word.get("_block"), word.get("_line"))
        same_line = key == current_key
        if key == (0, 0) and current_key == (0, 0):
            same_line = abs(word["top"] - current_top) <= 2.5
        if same_line:
            current.append(word)
        else:
            groups.append(current)
            current = [word]
            current_key = key
            current_top = word["top"]
    groups.append(current)

    sources = []
    for group in groups:
        group = sorted(group, key=lambda word: word["x0"])
        quote = " ".join(word["text"] for word in group).strip()
        if not quote:
            continue
        kind = "caption" if CAPTION.match(quote) else "equation" if EQUATION.match(quote) else "text"
        clean = [{"x0": w["x0"], "top": w["top"], "x1": w["x1"], "bottom": w["bottom"], "text": w["text"]}
                 for w in group]
        sources.append({
            "page": page_number, "bbox": bounds(clean), "quote": quote, "kind": kind,
        })
    return sources


def table_rows(tables, words, page):
    for table_index, table in enumerate(tables, 1):
        for row_index, row in enumerate(table.rows, 1):
            cells = []
            for box in row.cells:
                inside = [word for word in words if box and is_inside(word, box)]
                cells.append(text_anchor(inside, page, "cell") if inside else None)
            if any(cells):
                yield {
                    "page": page.page_number, "bbox": list(row.bbox),
                    "quote": " | ".join(cell["quote"] if cell else "" for cell in cells),
                    "kind": "table", "table": table_index, "row": row_index, "cells": cells,
                }


def find_tables(page):
    tables = list(page.find_tables())
    rules = defaultdict(list)
    for edge in page.edges:
        if edge["orientation"] == "h" and edge["width"] >= 90:
            rules[round(edge["x0"]), round(edge["x1"])].append(edge["top"])
    for (left, right), positions in rules.items():
        stops = sorted(set(round(position, 1) for position in positions))
        if len(stops) < 3 or not 20 <= stops[-1] - stops[0] <= 360:
            continue
        # pdfplumber keeps objects that sit outside the MediaBox, so a stray rule
        # above or below the page yields a negative top. crop() refuses such a box
        # with ValueError, and that used to abort the whole document.
        box = (max(page.bbox[0], left - 2), max(page.bbox[1], stops[0]),
               min(page.bbox[2], right + 2),
               min(page.bbox[3], stops[-1] + 1))
        if box[0] >= box[2] or box[1] >= box[3]:
            continue
        page_area = max(0, page.bbox[2] - page.bbox[0]) * max(0, page.bbox[3] - page.bbox[1])
        box_area = (box[2] - box[0]) * (box[3] - box[1])
        if page_area and box_area > page_area / 2:
            continue
        if any(box[0] < t.bbox[2] and box[2] > t.bbox[0]
               and box[1] < t.bbox[3] and box[3] > t.bbox[1] for t in tables):
            continue
        candidates = page.crop(box).find_tables({
            "vertical_strategy": "text", "horizontal_strategy": "text",
            "min_words_vertical": 2, "text_x_tolerance": X_TOLERANCE,
        })
        for candidate in candidates:
            rows = candidate.extract(x_tolerance=X_TOLERANCE)
            numeric_rows = sum(
                sum(bool(re.search(r"\d[.,]\d|^\d+%?$", cell.strip()))
                    for cell in row if cell) >= 2 for row in rows
            )
            if numeric_rows >= 2:
                tables.append(candidate)
    return tables


def page_texts(data):
    """Whole-page text in reading order, one string per page number.

    Uses pypdf on purpose. pdfplumber keeps the geometry we need, but its text
    splits a sentence across subscripts (``J`` / ``SC`` on separate lines) and
    interleaves the two columns of a paper, so a sentence that crosses a column
    break stops being contiguous. pypdf keeps it contiguous, and it is what the
    organisers' validator compares quotes against. Falls back to pdfplumber when
    pypdf is unavailable.
    """
    try:
        from pypdf import PdfReader
    except ImportError:
        with pdfplumber.open(BytesIO(data)) as pdf:
            return {page.page_number: page.extract_text() or "" for page in pdf.pages}
    try:
        reader = PdfReader(BytesIO(data))
        return {number: page.extract_text() or ""
                for number, page in enumerate(reader.pages, 1)}
    except Exception:
        with pdfplumber.open(BytesIO(data)) as pdf:
            return {page.page_number: page.extract_text() or "" for page in pdf.pages}


def extract(data):
    sources, warnings, fallback_pages = [], [], []
    with pdfplumber.open(BytesIO(data)) as pdf:
        if not 1 <= len(pdf.pages) <= MAX_PAGES:
            raise ValueError(f"Поддерживаются PDF от 1 до {MAX_PAGES} страниц.")
        # After the page-count check, so an oversized PDF is refused before pypdf
        # spends minutes extracting text nobody will read.
        texts = page_texts(data)
        metadata = {key.lower(): str(value) for key, value in pdf.metadata.items() if value}
        for page in pdf.pages:
            deduped = page.dedupe_chars()
            words = deduped.extract_words(x_tolerance=X_TOLERANCE)
            if not words:
                fallback_words = _pymupdf_page_words(data, page.page_number)
                fallback_sources = _pymupdf_line_sources(fallback_words, page.page_number)
                if fallback_sources:
                    fallback_pages.append(page.page_number)
                    sources.extend(fallback_sources)
                else:
                    warnings.append(f"Страница {page.page_number}: текстовый слой пуст или отсутствует.")
                page.close()
                continue
            tables = find_tables(deduped)
            sources.extend(table_rows(tables, words, page))
            # Extract lines after excluding words belonging to detected tables.
            text = deduped.filter(
                lambda obj: obj.get("object_type") != "char"
                or not any(is_inside(obj, table.bbox) for table in tables)
            )
            for line in text.extract_text_lines(return_chars=True, x_tolerance=X_TOLERANCE):
                chars = [char for char in line["chars"] if char["text"].strip()]
                if not chars:
                    continue
                quote = line["text"]
                kind = "caption" if CAPTION.match(quote) else "equation" if EQUATION.match(quote) else "text"
                sources.append({
                    "page": page.page_number, "bbox": bounds(chars),
                    "quote": quote, "kind": kind,
                })
            page.close()
        sources.sort(key=lambda source: (source["page"], source["bbox"][1], source["bbox"][0]))
        for index, source in enumerate(sources, 1):
            source["id"] = index
        return {
            "title": metadata.get("title", ""), "author": metadata.get("author", ""),
            "pages": len(pdf.pages), "sources": sources, "warnings": warnings,
            "page_texts": texts, "fallback_pages": fallback_pages,
        }


def render(data, page_number, box):
    with pdfplumber.open(BytesIO(data)) as pdf:
        # pages[-1] on 0 would silently highlight the last page instead of failing.
        if not 1 <= page_number <= len(pdf.pages):
            raise ValueError(f"нет страницы {page_number}: в PDF {len(pdf.pages)} страниц")
        page = pdf.pages[page_number - 1]
        # PageImage accepts the same top-origin coordinates as the extractor.
        scale = min(100, 140_000 / max(page.width, page.height))
        image = page.to_image(resolution=scale, antialias=True)
        image.draw_rect(tuple(box), fill=(255, 191, 40, 45), stroke="#c66516", stroke_width=2)
        output = BytesIO()
        image.save(output, format="PNG", quantize=False)
        return output.getvalue()
