from collections import defaultdict
from io import BytesIO
import re

import pdfplumber


MAX_PAGES = 150
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
        box = (max(page.bbox[0], left - 2), stops[0],
               min(page.bbox[2], right + 2), min(page.bbox[3], stops[-1] + 1))
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


def extract(data):
    sources, warnings = [], []
    with pdfplumber.open(BytesIO(data)) as pdf:
        if not 1 <= len(pdf.pages) <= MAX_PAGES:
            raise ValueError(f"Поддерживаются PDF от 1 до {MAX_PAGES} страниц.")
        metadata = {key.lower(): str(value) for key, value in pdf.metadata.items() if value}
        for page in pdf.pages:
            deduped = page.dedupe_chars()
            words = deduped.extract_words(x_tolerance=X_TOLERANCE)
            if not words:
                warnings.append(f"Страница {page.page_number}: текстовый слой пуст или отсутствует.")
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
        }


def render(data, page_number, box):
    with pdfplumber.open(BytesIO(data)) as pdf:
        page = pdf.pages[page_number - 1]
        # PageImage accepts the same top-origin coordinates as the extractor.
        scale = min(100, 140_000 / max(page.width, page.height))
        image = page.to_image(resolution=scale, antialias=True)
        image.draw_rect(tuple(box), fill=(255, 191, 40, 45), stroke="#c66516", stroke_width=2)
        output = BytesIO()
        image.save(output, format="PNG", quantize=False)
        return output.getvalue()
