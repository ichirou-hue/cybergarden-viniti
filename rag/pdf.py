"""Работа с PDF: текстовый слой по страницам, рендер, OCR-фолбэк.

Текстовый слой читается через PyMuPDF, при его отсутствии — через
pdfplumber. Сканы без текстового слоя определяются явно: их нельзя
молча считать «пустой статьёй».
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass, replace
from pathlib import Path

from .text import norm

#: Порог, ниже которого страница считается не имеющей текстового слоя.
TEXT_LAYER_MIN_CHARS = 40


@dataclass(frozen=True)
class Word:
    """Слово текстового слоя с координатами."""

    text: str
    x0: float
    y0: float
    x1: float
    y1: float


@dataclass(frozen=True)
class Line:
    """Строка текстового слоя с координатами — основа разбора таблиц."""

    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    words: tuple[Word, ...] = ()
    column: int = 0  # 0: full-width/unknown; 1: left, 2: right


@dataclass(frozen=True)
class Page:
    number: int  # нумерация с единицы, как в CSV сдачи
    text: str
    source: str = "text"  # text | ocr | empty
    lines: tuple[Line, ...] = ()


@dataclass(frozen=True)
class Document:
    path: Path
    pages: tuple[Page, ...]
    parser: str = "pymupdf"

    def __len__(self) -> int:
        return len(self.pages)

    def page(self, number: int) -> Page:
        if not 1 <= number <= len(self.pages):
            raise IndexError(f"страница {number} вне диапазона 1..{len(self.pages)}")
        return self.pages[number - 1]

    @property
    def has_text_layer(self) -> bool:
        return any(len(norm(p.text)) >= TEXT_LAYER_MIN_CHARS for p in self.pages)

    @property
    def is_scan(self) -> bool:
        return not self.has_text_layer

    def text(self) -> str:
        return "\n\n".join(p.text for p in self.pages)

    def find(self, needle: str) -> list[int]:
        """Номера страниц, где нормализованный ``needle`` встречается дословно."""
        target = norm(needle)
        if not target:
            return []
        return [p.number for p in self.pages if target in norm(p.text)]


def pymupdf_module():
    """PyMuPDF под любым из двух имён пакета.

    Новые версии публикуют модуль как ``pymupdf``, старые как ``fitz``.
    Оба имени дают один и тот же API. Если нет ни одного, ``ImportError``
    пробрасывается наружу: вызывающий код решает, чем заменить PyMuPDF.
    """
    try:
        import pymupdf
    except ImportError:
        import fitz as pymupdf
    return pymupdf


def _lines_from_words(
    words: Iterable[tuple[float, float, float, float, str]],
) -> tuple[Line, ...]:
    """Строки с координатами: слова группируются по вертикали, затем по x.

    Координаты у обоих источников в одном пространстве: начало сверху,
    ``y`` растёт вниз. ``PyMuPDF`` отдаёт ``get_text("words")``, ``pdfplumber``
    ``extract_words()``; после приведения к кортежу разбор общий.
    """
    tokens = [
        (float(x0), float(y0), float(x1), float(y1), str(text))
        for x0, y0, x1, y1, text in words
        if str(text).strip()
    ]
    if not tokens:
        return ()
    tokens.sort(key=lambda item: ((item[1] + item[3]) / 2, item[0]))

    rows: list[dict] = []
    for x0, y0, x1, y1, text in tokens:
        center = (y0 + y1) / 2
        height = max(y1 - y0, 1.0)
        if rows and abs(center - rows[-1]["center"]) <= max(2.5, 0.45 * height):
            row = rows[-1]
            row["words"].append((x0, y0, x1, y1, text))
            row["center"] = (row["center"] * (len(row["words"]) - 1) + center) / len(row["words"])
        else:
            rows.append({"center": center, "words": [(x0, y0, x1, y1, text)]})

    # Table cells often have large gaps, too. Only repeated prose rows can
    # establish a column gutter; numeric table rows never vote for a split.
    def prose(items):
        return len(items) >= 6 and sum(any(c.isdigit() for c in item[4]) for item in items) < len(items) / 3

    gaps = []
    for row_index, row in enumerate(rows):
        items = sorted(row["words"], key=lambda item: item[0])
        if not prose(items):
            continue
        for left, right in zip(items, items[1:]):
            if right[0] - left[2] >= max(24, 2 * max(left[3] - left[1], right[3] - right[1])):
                gaps.append((left[2], right[0], row_index))
    cuts = []
    for left, right, _ in gaps:
        cut = (left + right) / 2
        support = {index for lo, hi, index in gaps if lo + 4 <= cut <= hi - 4}
        if len(support) >= 3:
            cuts.append((len(support), cut))
    gutter = max(cuts)[1] if cuts else None

    lines: list[Line] = []
    for row in rows:
        ordered = sorted(row["words"], key=lambda item: item[0])
        groups = [ordered]
        numeric = sum(any(c.isdigit() for c in item[4]) for item in ordered)
        table_row = numeric >= 2 and numeric >= len(ordered) / 3
        if gutter is not None and not table_row:
            left = [item for item in ordered if item[2] <= gutter]
            right = [item for item in ordered if item[0] >= gutter]
            if left and right and len(left) + len(right) == len(ordered):
                groups = [left, right]
        for group in groups:
            items = tuple(Word(text=text, x0=x0, y0=y0, x1=x1, y1=y1)
                          for x0, y0, x1, y1, text in group)
            x0, x1 = min(item.x0 for item in items), max(item.x1 for item in items)
            column = 0 if gutter is None or x0 < gutter < x1 else 1 if x1 <= gutter else 2
            lines.append(
                Line(
                    text=" ".join(item.text for item in items),
                    x0=x0,
                    y0=min(item.y0 for item in items),
                    x1=x1,
                    y1=max(item.y1 for item in items),
                    words=items,
                    column=column,
                )
            )
    lines.sort(key=lambda line: (round(line.y0, 1), line.x0))
    return tuple(lines)


def _page_lines(page) -> tuple[Line, ...]:
    """Строки страницы PyMuPDF с координатами."""
    return _lines_from_words(
        (x0, y0, x1, y1, text)
        for x0, y0, x1, y1, text, _block_no, _line_no, _word_no in page.get_text("words")
    )


def _pdfplumber_lines(page) -> tuple[Line, ...]:
    """Строки страницы pdfplumber с координатами.

    ``extract_words()`` отдаёт слова в том же пространстве координат, что и
    PyMuPDF (``top``/``bottom`` от верхнего края), поэтому дальше идёт общий
    разбор. Без этого таблицы pdfplumber не находились: у ``Page`` не было
    ``lines``, и координатное извлечение давало ноль результатов.
    """
    return _lines_from_words(
        (word["x0"], word["top"], word["x1"], word["bottom"], word["text"])
        for word in page.extract_words()
    )


def _load_pymupdf(path: Path) -> Document:
    pymupdf = pymupdf_module()

    with pymupdf.open(path) as document:
        pages = tuple(
            Page(
                number=i + 1,
                text=page.get_text("text") or "",
                lines=_page_lines(page),
            )
            for i, page in enumerate(document)
        )
    return Document(path=path, pages=pages, parser="pymupdf")


def _load_pdfplumber(path: Path) -> Document:
    import pdfplumber

    with pdfplumber.open(path) as pdf:
        pages = tuple(
            Page(
                number=i + 1,
                text=page.extract_text() or "",
                lines=_pdfplumber_lines(page),
            )
            for i, page in enumerate(pdf.pages)
        )
    return Document(path=path, pages=pages, parser="pdfplumber")


def load_document(path: str | Path, parser: str = "auto") -> Document:
    """Прочитать PDF постранично."""
    path = Path(path)
    if parser in ("auto", "pymupdf"):
        try:
            return _load_pymupdf(path)
        except ImportError:
            if parser == "pymupdf":
                raise
    return _load_pdfplumber(path)


def render_page_png(path: str | Path, page_number: int, dpi: int = 200) -> bytes:
    """Отрендерить страницу в PNG — для evidence и для OCR."""
    pymupdf = pymupdf_module()

    with pymupdf.open(path) as document:
        page = document[page_number - 1]
        pixmap = page.get_pixmap(dpi=dpi)
        return pixmap.tobytes("png")


# --- OCR -------------------------------------------------------------------


def tesseract_path() -> str | None:
    """Путь к tesseract, если он установлен."""
    return shutil.which("tesseract")


def ocr_available() -> bool:
    return tesseract_path() is not None


def ocr_page(path: str | Path, page_number: int, lang: str = "eng", dpi: int = 300) -> str:
    """Распознать одну страницу через tesseract."""
    executable = tesseract_path()
    if executable is None:
        raise RuntimeError(
            "tesseract не найден в PATH. Установите его "
            "(https://github.com/UB-Mannheim/tesseract/wiki) или работайте без OCR."
        )
    png = render_page_png(path, page_number, dpi=dpi)
    with tempfile.TemporaryDirectory() as tmp:
        image = Path(tmp) / "page.png"
        image.write_bytes(png)
        result = subprocess.run(
            [executable, str(image), "stdout", "-l", lang],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode != 0:
            raise RuntimeError(f"tesseract: {result.stderr.strip()}")
        return result.stdout


def with_ocr(document: Document, lang: str = "eng", dpi: int = 300) -> Document:
    """Дополнить документ OCR-текстом для страниц без текстового слоя."""
    pages: list[Page] = []
    for page in document.pages:
        if len(norm(page.text)) >= TEXT_LAYER_MIN_CHARS:
            pages.append(page)
            continue
        try:
            text = ocr_page(document.path, page.number, lang=lang, dpi=dpi)
        except RuntimeError:
            pages.append(page)
            continue
        pages.append(replace(page, text=text, source="ocr"))
    return replace(document, pages=tuple(pages))
