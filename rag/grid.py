"""Восстановление числовых таблиц по координатам слов.

PyMuPDF отдаёт слова с bbox. Таблица узнаётся по вертикальному отрезку
строк, где много чисел; колонки — по кластерам x-центров чисел; шапка —
по строкам над первой числовой строкой. Поддерживаются:

* многоуровневая шапка (генератор × набор × метрика), как в D001;
* плоская шапка (колонка = метрика), как в D010;
* транспонированные таблицы (строка = метрика);
* подпись ``Table N`` над или под таблицей.

Модуль намеренно ничего не «достраивает»: нет числа в ячейке — нет строки
результата.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from statistics import median

from .pdf import Line, Page, Word
from .vocab import (
    Metric,
    canonical_dataset,
    is_number,
    looks_like_generator,
    normalize_generator,
    number_value,
    parse_metric,
)

CAPTION = re.compile(r"^\s*(?:table|таблица)\s+([0-9]+|[IVXLC]+)\s*[.:]?", re.I)
_CAPTION_ANY = re.compile(r"^\s*(?:table|таблица)\s+([0-9]+|[IVXLC]+)\b", re.I)
_GROUP_WORDS = {"methods", "method", "model", "models", "system", "systems", "approach", "approaches"}


@dataclass
class Column:
    center: float
    levels: list[str] = field(default_factory=list)
    metric: Metric | None = None
    dataset: str = ""
    generator: str = ""
    text: str = ""


@dataclass
class Row:
    label: str
    fragment: str
    values: dict[int, tuple[str, float]] = field(default_factory=dict)
    y: float = 0.0
    generator: str = ""
    dataset: str = ""


@dataclass
class Table:
    page: int
    caption: str
    caption_text: str
    columns: list[Column]
    rows: list[Row]
    transposed: bool = False
    page_text: str = ""

    @property
    def n_columns(self) -> int:
        return len(self.columns)


# --- вспомогательные -------------------------------------------------------


def _clean_token(text: str) -> str:
    token = text.strip()
    while token and token[0] in "([{":
        token = token[1:]
    while token and token[-1] in ")]},;:":
        token = token[:-1]
    return token


def _numeric_words(line: Line) -> list[Word]:
    found: list[Word] = []
    for word in line.words:
        token = _clean_token(word.text)
        if token and is_number(token):
            found.append(Word(token, word.x0, word.y0, word.x1, word.y1))
    return found


def _looks_prose(line: Line) -> bool:
    """Строка похожа на абзац, а не на шапку таблицы."""
    text = line.text.strip()
    if len(text) > 120:
        return True
    words = text.split()
    return len(words) > 12 and text.endswith((".", ";", "?", "!"))


def _is_data_line(line: Line) -> bool:
    words = [w for w in line.words if w.text.strip()]
    if len(words) < 2:
        return False
    numbers = _numeric_words(line)
    if len(numbers) < 2:
        return False
    return len(numbers) / len(words) >= 0.3


def _cluster(values: list[float], tolerance: float) -> list[float]:
    values = sorted(values)
    if not values:
        return []
    groups: list[list[float]] = [[values[0]]]
    for value in values[1:]:
        if value - groups[-1][-1] <= tolerance:
            groups[-1].append(value)
        else:
            groups.append([value])
    return [sum(group) / len(group) for group in groups]


def _best_clusters(values: list[float], target: int) -> list[float]:
    best: list[float] = []
    best_score = None
    for tolerance in (4, 6, 8, 10, 12, 14, 16, 20, 24, 30):
        clusters = _cluster(values, tolerance)
        score = abs(len(clusters) - target)
        if best_score is None or score < best_score:
            best, best_score = clusters, score
        if score == 0:
            break
    return best


_LABEL_HEADS = {
    "method",
    "methods",
    "model",
    "models",
    "approach",
    "approaches",
    "system",
    "systems",
    "variant",
    "variants",
    "name",
    "method name",
    "technique",
}


def _label_column_start(
    header_lines: list[Line],
    first_number_x: float,
) -> float | None:
    """x-начало колонки с названием метода, если слева есть ещё колонки."""
    start: float | None = None
    for line in header_lines:
        for word in line.words:
            if (
                word.text.strip().casefold() in _LABEL_HEADS
                and word.x0 < first_number_x - 4
                and (start is None or word.x0 > start)
            ):
                start = word.x0
    return start


def _row_label(line: Line, numbers: list[Word], label_start: float | None = None) -> str:
    if not numbers:
        return ""
    first = numbers[0].x0
    left = label_start - 1.0 if label_start is not None else None
    label = " ".join(
        w.text
        for w in line.words
        if w.x1 <= first + 0.5 and (left is None or w.x0 >= left)
    )
    for mark in "✓✗✔✘×✕":
        label = label.replace(mark, " ")
    return re.sub(r"\s+", " ", label).strip(" .:|-")


def _cells(line: Line, gap: float = 6.0) -> list[tuple[str, float, float]]:
    """Разбить строку шапки на ячейки по горизонтальным разрывам."""
    words = sorted(line.words, key=lambda w: w.x0)
    if not words:
        return []
    cells: list[list[Word]] = [[words[0]]]
    for word in words[1:]:
        if word.x0 - cells[-1][-1].x1 <= gap:
            cells[-1].append(word)
        else:
            cells.append([word])
    return [
        (" ".join(w.text for w in cell).strip(), min(w.x0 for w in cell), max(w.x1 for w in cell))
        for cell in cells
        if " ".join(w.text for w in cell).strip()
    ]


def _column_for(x: float, centers: list[float]) -> int | None:
    if not centers:
        return None
    best = min(range(len(centers)), key=lambda i: abs(centers[i] - x))
    return best


def _territories(
    cells: list[tuple[str, float, float]], left: float, right: float
) -> list[tuple[float, float]]:
    """Границы «владений» ячеек шапки: середина между соседними центрами."""
    centers = [(x0 + x1) / 2 for _, x0, x1 in cells]
    bounds: list[tuple[float, float]] = []
    for index in range(len(cells)):
        low = left if index == 0 else (centers[index - 1] + centers[index]) / 2
        high = right if index == len(cells) - 1 else (centers[index] + centers[index + 1]) / 2
        bounds.append((low, high))
    return bounds


def _cell_for(center: float, cells: list[tuple[str, float, float]], bounds: list[tuple[float, float]]) -> str:
    for index, (low, high) in enumerate(bounds):
        if low <= center < high:
            return cells[index][0]
    return ""


def _caption(page: Page, band_top: float, band_bottom: float) -> tuple[str, str, float]:
    """Найти подпись таблицы рядом с полосой. Возвращает (номер, текст, y)."""
    candidates = [line for line in page.lines if _CAPTION_ANY.match(line.text)]
    best: tuple[str, str, float] | None = None
    for line in candidates:
        distance = min(abs(line.y0 - band_top), abs(line.y0 - band_bottom))
        if distance > 160:
            continue
        if best is None or distance < abs(best[2] - band_top):
            match = _CAPTION_ANY.match(line.text)
            numeral = match.group(1) if match else ""
            label = f"Table {numeral}" if numeral else line.text.strip()
            best = (label, line.text.strip(), line.y0)
    return best or ("", "", 0.0)


# --- основной разбор -------------------------------------------------------


def find_tables(page: Page) -> list[Table]:
    """Найти и разобрать все числовые таблицы на странице."""
    lines = list(page.lines)
    if not lines:
        return []
    heights = [line.y1 - line.y0 for line in lines if line.y1 > line.y0]
    if not heights:
        return []
    height = median(heights) or 10.0

    data = [line for line in lines if _is_data_line(line)]
    if not data:
        return []

    caption_ys = [line.y0 for line in lines if _CAPTION_ANY.match(line.text)]
    bands: list[list[Line]] = []
    for line in data:
        same_band = (
            bands
            and line.y0 - bands[-1][-1].y1 <= max(2.2 * height, 16.0)
            and not any(bands[-1][-1].y1 < y < line.y0 for y in caption_ys)
        )
        if same_band:
            bands[-1].append(line)
        else:
            bands.append([line])

    tables: list[Table] = []
    for band in bands:
        if len(band) < 2:
            continue
        table = _parse_band(page, band, height)
        if table is not None and table.rows:
            tables.append(table)
    return tables


def _parse_band(page: Page, band: list[Line], height: float) -> Table | None:
    top = band[0].y0
    bottom = band[-1].y1

    header_lines: list[Line] = []
    cursor = top
    for line in reversed([l for l in page.lines if l.y1 <= top + 1]):
        if cursor - line.y1 > max(1.7 * height, 12.0):
            break
        if _CAPTION_ANY.match(line.text):
            break  # подпись — граница таблицы, в шапку не входит
        if _looks_prose(line):
            break
        header_lines.append(line)
        cursor = line.y0
        if len(header_lines) >= 5:
            break
    header_lines.reverse()

    # Числа всех строк полосы -> колонки.
    centers: list[float] = []
    counts: list[int] = []
    row_numbers: dict[int, list[Word]] = {}
    for index, line in enumerate(band):
        numbers = _numeric_words(line)
        row_numbers[index] = numbers
        counts.append(len(numbers))
        centers.extend((word.x0 + word.x1) / 2 for word in numbers)
    target = int(median(counts)) if counts else 0
    if target < 2:
        return None
    column_centers = _best_clusters(centers, target)
    if len(column_centers) < 2:
        return None

    # Шапка: ячейки по уровням, только уровни, накрывающие таблицу.
    x0 = min(center for center in column_centers)
    x1 = max(center for center in column_centers)
    width = max(x1 - x0, 1.0)
    levels: list[list[tuple[str, float, float]]] = []
    for line in header_lines:
        cells = _cells(line)
        if not cells:
            continue
        span = max(c[2] for c in cells) - min(c[1] for c in cells)
        coverage = min(1.0, span / (width + 1.0))
        if len(cells) == 1 and coverage < 0.85:
            continue  # метка строки-разделителя или угол шапки
        if coverage < 0.45:
            continue
        levels.append(cells)

    columns: list[Column] = []
    left, right = x0 - 12, x1 + 12
    level_bounds = [_territories(cells, left, right) for cells in levels]
    for center in column_centers:
        column = Column(center=center)
        for cells, bounds in zip(levels, level_bounds):
            text = _cell_for(center, cells, bounds)
            if text:
                column.levels.append(text)
        column.text = " | ".join(column.levels)
        for text in reversed(column.levels):
            metric = parse_metric(text)
            if metric is not None:
                column.metric = metric
                break
        for text in column.levels:
            if looks_like_generator(text):
                column.generator = normalize_generator(text)
                break
        for text in reversed(column.levels):
            name = canonical_dataset(text)
            if name:
                column.dataset = name
                break
        columns.append(column)

    # Строки.
    left_edges = [min(word.x0 for word in words) for words in row_numbers.values() if words]
    first_number_x = median(left_edges) if left_edges else 0.0
    label_start = _label_column_start(header_lines, first_number_x)
    rows: list[Row] = []
    for index, line in enumerate(band):
        numbers = row_numbers[index]
        if len(numbers) < 2:
            continue
        label = _row_label(line, numbers, label_start)
        if not label:
            continue
        row = Row(label=label, fragment=line.text, y=line.y0)
        for word in numbers:
            center = (word.x0 + word.x1) / 2
            column_index = _column_for(center, column_centers)
            if column_index is None or column_index in row.values:
                continue
            value = number_value(word.text)
            if value is None:
                continue
            row.values[column_index] = (word.text, value)
        if row.values:
            rows.append(row)

    if not rows:
        return None

    # Групповые подписи внутри таблицы: «Llama3.3-70B», «Natural Questions».
    band_ids = {id(line) for line in band}
    groups: list[tuple[float, str, str]] = []
    for line in page.lines:
        if id(line) in band_ids or not (top - 1.0 <= line.y0 <= bottom + 1.0):
            continue
        if _numeric_words(line):
            continue
        text = line.text.strip()
        if not text or len(text.split()) > 8:
            continue
        generator = normalize_generator(text) if looks_like_generator(text) else ""
        dataset = canonical_dataset(text) or ""
        if generator or dataset:
            groups.append((line.y0, generator, dataset))
    for row in rows:
        candidates = [group for group in groups if group[0] <= row.y]
        if candidates:
            _, generator, dataset = max(candidates, key=lambda item: item[0])
            row.generator = generator
            row.dataset = dataset

    caption, caption_text, _ = _caption(page, top, bottom)
    if caption_text.lower().startswith("figure"):
        return None
    table = Table(
        page=page.number,
        caption=caption,
        caption_text=caption_text,
        columns=columns,
        rows=rows,
        page_text=page.text,
    )
    table.transposed = _looks_transposed(rows, columns)
    if table.transposed:
        _transpose(table)
    elif all(column.metric is None for column in table.columns):
        inferred = _metric_from_caption(caption_text)
        if inferred is not None:
            for column in table.columns:
                column.metric = inferred
    return table


def _metric_from_caption(caption: str) -> Metric | None:
    """Если шапка без метрик, попробовать метрику из подписи (ровно одну)."""
    if not caption:
        return None
    chunks = re.split(r"[,;:]", caption)
    found: dict[str, Metric] = {}
    for chunk in chunks:
        metric = parse_metric(chunk.strip(" ."))
        if metric is not None:
            found.setdefault(metric.name, metric)
    return next(iter(found.values())) if len(found) == 1 else None


def _looks_transposed(rows: list[Row], columns: list[Column]) -> bool:
    metric_rows = sum(1 for row in rows if parse_metric(row.label) is not None)
    metric_columns = sum(1 for column in columns if column.metric is not None)
    return metric_rows >= max(2, len(rows) // 2) and metric_columns < len(columns) // 2


def _transpose(table: Table) -> None:
    """Строка = метрика, колонка = конфигурация → обычный вид."""
    new_columns: list[Column] = []
    for index, row in enumerate(table.rows):
        metric = parse_metric(row.label)
        column = Column(center=float(index), metric=metric, text=row.label)
        if metric is not None:
            column.text = metric.name + (f"@{metric.k}" if metric.k else "")
        new_columns.append(column)

    new_rows: list[Row] = []
    for column_index, column in enumerate(table.columns):
        label = column.generator or ""
        if not label:
            for text in reversed(column.levels):
                if not parse_metric(text) and canonical_dataset(text) is None:
                    label = text
                    break
        label = label or column.text or f"column {column_index + 1}"
        row = Row(label=label, fragment=" ".join(
            f"{old_row.label} {old_row.values[column_index][0]}"
            for old_row in table.rows
            if column_index in old_row.values
        ))
        for row_index, old_row in enumerate(table.rows):
            if column_index in old_row.values:
                row.values[row_index] = old_row.values[column_index]
        if row.values:
            new_rows.append(row)

    table.columns = new_columns
    table.rows = new_rows
