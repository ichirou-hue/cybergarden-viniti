"""Метаданные публикации с первой страницы.

Заголовок — самая крупная строка до аннотации; авторы — строки между
заголовком и аннотацией; год — из копирайта или шапки конференции; DOI —
регуляркой. Ничего не угадывается: нет признака — поле пустое.
"""

from __future__ import annotations

import re
from statistics import median

from .pdf import Document, Line

_ABSTRACT = re.compile(r"^\s*(abstract|аннотация)\b", re.I)
_EMAIL = re.compile(r"[\w.+-]+@[\w.-]+")
_DOI = re.compile(r"\b(10\.\d{4,9}/[^\s,;]+)")
_YEAR = re.compile(r"\b(19|20)\d{2}\b")
_AFFILIATION = re.compile(
    r"(universit|institut|department|laborator|school|college|inc\.|ltd|gmbh|"
    r"research|academy|centre|center|corporation|google|microsoft|amazon|meta\b)",
    re.I,
)


def _title_lines(lines: list[Line]) -> list[Line]:
    """Строки заголовка: самые крупные до аннотации."""
    if not lines:
        return []
    stop = len(lines)
    for index, line in enumerate(lines[:40]):
        if _ABSTRACT.match(line.text):
            stop = index
            break
    candidates = [
        line
        for line in lines[:stop]
        if 3 <= len(line.text.split()) <= 30
        and not _EMAIL.search(line.text)
        and not _AFFILIATION.search(line.text)
        and not line.text.strip().lower().startswith(("keywords", "doi", "http"))
    ]
    if not candidates:
        return []
    heights = [line.y1 - line.y0 for line in candidates]
    tallest = max(heights)
    if tallest <= 0:
        return []
    title = [line for line in candidates if line.y1 - line.y0 >= tallest * 0.92]
    title.sort(key=lambda line: line.y0)
    # Заголовок — подряд идущие строки.
    block: list[Line] = []
    for line in title:
        if not block or line.y0 - block[-1].y1 <= tallest * 0.8:
            block.append(line)
        else:
            break
    return block


def _authors(lines: list[Line], title: list[Line]) -> str:
    if not title:
        return ""
    bottom = max(line.y1 for line in title)
    names: list[str] = []
    for line in lines:
        if line.y0 < bottom:
            continue
        text = line.text.strip()
        if _ABSTRACT.match(text):
            break
        if not text or _EMAIL.search(text) or _AFFILIATION.search(text):
            continue
        if len(text) > 160:
            continue
        parts = re.split(r",|;|\band\b|&", text)
        for part in parts:
            part = part.strip(" .*†‡0123456789")
            if 1 <= len(part.split()) <= 5 and re.search(r"[A-Za-zА-Яа-я]", part):
                names.append(part)
        if names and line.y1 - bottom > 90:
            break
    return "; ".join(dict.fromkeys(names))


def extract_metadata(document: Document) -> dict[str, str]:
    """Собрать библиографические поля первой страницы."""
    if not document.pages:
        return {}
    page = document.pages[0]
    lines = list(page.lines)
    meta: dict[str, str] = {}

    title = _title_lines(lines)
    if title:
        meta["title"] = " ".join(line.text.strip() for line in title)

    authors = _authors(lines, title)
    if authors:
        meta["authors"] = authors

    text = page.text
    doi = _DOI.search(text)
    if doi:
        meta["DOI"] = doi.group(1).rstrip(".")

    years = _YEAR.findall(text)
    if years:
        # Год в копирайте/шапке конференции важнее года в ссылках.
        head = "\n".join(text.splitlines()[:12])
        head_years = _YEAR.findall(head)
        meta["year"] = head_years[0] if head_years else years[0]

    meta["in_scope"] = "true"
    return meta
