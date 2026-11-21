"""Работа с PDF: текстовый слой по страницам, рендер, OCR-фолбэк.

Текстовый слой читается через PyMuPDF, при его отсутствии — через
pdfplumber. Сканы без текстового слоя определяются явно: их нельзя
молча считать «пустой статьёй».
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
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


def _page_lines(page) -> tuple[Line, ...]:
    """Строки с координатами: слова группируются по вертикали, затем по x."""
    words = [
        (float(x0), float(y0), float(x1), float(y1), text)
        for x0, y0, x1, y1, text, _block_no, _line_no, _word_no in page.get_text("words")
        if text.strip()
    ]
    if not words:
        return ()
    words.sort(key=lambda item: ((item[1] + item[3]) / 2, item[0]))

    rows: list[dict] = []
    for x0, y0, x1, y1, text in words:
        center = (y0 + y1) / 2
        height = max(y1 - y0, 1.0)
        if rows and abs(center - rows[-1]["center"]) <= max(2.5, 0.45 * height):
            row = rows[-1]
            row["words"].append((x0, y0, x1, y1, text))
            row["center"] = (row["center"] * (len(row["words"]) - 1) + center) / len(row["words"])
        else:
            rows.append({"center": center, "words": [(x0, y0, x1, y1, text)]})

    lines: list[Line] = []
    for row in rows:
        items = tuple(
            Word(text=text, x0=x0, y0=y0, x1=x1, y1=y1)
            for x0, y0, x1, y1, text in sorted(row["words"], key=lambda item: item[0])
        )
        lines.append(
            Line(
                text=" ".join(item.text for item in items),
                x0=min(item.x0 for item in items),
                y0=min(item.y0 for item in items),
                x1=max(item.x1 for item in items),
                y1=max(item.y1 for item in items),
                words=items,
            )
        )
    lines.sort(key=lambda line: (round(line.y0, 1), line.x0))
    return tuple(lines)


def _load_pymupdf(path: Path) -> Document:
    import pymupdf  # PyMuPDF

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
            Page(number=i + 1, text=page.extract_text() or "")
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
    import pymupdf

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
