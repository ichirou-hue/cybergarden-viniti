"""Чтение и запись CSV строго в порядке колонок шаблона.

Официальный чекер сравнивает заголовок с ``schema.json`` позиционно,
поэтому порядок колонок здесь не «косметика», а часть контракта.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Iterable, Mapping, Sequence

ENCODING = "utf-8"


class CsvFormatError(ValueError):
    """CSV не соответствует схеме."""


def read_table(path: str | Path, columns: Sequence[str] | None = None) -> list[dict[str, str]]:
    """Прочитать CSV в список словарей.

    Если ``columns`` задан, проверяется точное совпадение заголовка.
    """
    path = Path(path)
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        header = reader.fieldnames or []
        if columns is not None and list(header) != list(columns):
            raise CsvFormatError(
                f"{path.name}: заголовок не совпадает со схемой\n"
                f"  ожидалось: {list(columns)}\n"
                f"  получено:  {list(header)}"
            )
        return [{k: (v or "") for k, v in row.items() if k is not None} for row in reader]


def write_table(
    path: str | Path,
    columns: Sequence[str],
    rows: Iterable[Mapping[str, object]],
) -> int:
    """Записать CSV с заголовком ``columns``. Возвращает число строк."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = list(columns)
    allowed = set(columns)
    count = 0
    with path.open("w", encoding=ENCODING, newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=columns,
            extrasaction="raise",
            lineterminator="\n",
        )
        writer.writeheader()
        for row in rows:
            extra = set(row) - allowed
            if extra:
                raise CsvFormatError(
                    f"{path.name}: колонки вне схемы: {sorted(extra)}"
                )
            writer.writerow({name: _cell(row.get(name, "")) for name in columns})
            count += 1
    return count


def _cell(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def write_submission(
    out_dir: str | Path,
    schema,
    tables: Mapping[str, Iterable[Mapping[str, object]]],
) -> dict[str, int]:
    """Записать все семь таблиц сдачи. Возвращает ``{таблица: строк}``."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, int] = {}
    for table in schema.tables:
        rows = list(tables.get(table, []))
        written[table] = write_table(out_dir / f"{table}.csv", schema.columns(table), rows)
    return written


def read_submission(
    in_dir: str | Path,
    schema,
) -> dict[str, list[dict[str, str]]]:
    """Прочитать сдачу целиком, проверяя заголовки."""
    in_dir = Path(in_dir)
    return {
        table: read_table(in_dir / f"{table}.csv", schema.columns(table))
        for table in schema.tables
    }
