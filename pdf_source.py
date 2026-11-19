from io import BytesIO
import re

import pdfplumber


MAX_PAGES = 150
CAPTION = re.compile(r"^(?:fig(?:ure)?\.?|table|рис(?:унок)?\.?|таблица)\s+\d+", re.I)
EQUATION = re.compile(r"^(?=.{3,180}$)[\w(][\w\s@()./+\-–×∑]*\s*=\s*\S")


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


def extract(data):
    sources, warnings = [], []
    with pdfplumber.open(BytesIO(data)) as pdf:
        if not 1 <= len(pdf.pages) <= MAX_PAGES:
            raise ValueError(f"Поддерживаются PDF от 1 до {MAX_PAGES} страниц.")
        metadata = {key.lower(): str(value) for key, value in pdf.metadata.items() if value}
        for page in pdf.pages:
            words = page.dedupe_chars().extract_words()
            if not words:
                warnings.append(f"Страница {page.page_number}: текстовый слой пуст или отсутствует.")
                continue
            tables = page.find_tables()
            sources.extend(table_rows(tables, words, page))
            # Extract lines after excluding words belonging to detected tables.
            text = page.filter(
                lambda obj: obj.get("object_type") != "char"
                or not any(is_inside(obj, table.bbox) for table in tables)
            ).dedupe_chars()
            for line in text.extract_text_lines(return_chars=True):
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
