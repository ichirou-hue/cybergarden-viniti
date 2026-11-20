"""Извлечение RAG-сущностей из публикации.

Версия 0.1 покрывает табличные результаты (Table 1 в D001): строки вида
``Метод .204 .330 ...`` разбираются детерминированно, без LLM.
Всё, что не подтверждено текстом страницы, остаётся пустым.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from .kit import TABLE_ORDER
from .pdf import Document
from .text import norm

#: Строка, состоящая из одного числа: ``.204``, ``0.33``, ``-1.2``.
_NUMBER_LINE = re.compile(r"^[+\-]?(?:\d+(?:\.\d*)?|\.\d+)$")
#: Строка таблицы: имя метода и ровно N чисел.
_ROW = re.compile(
    r"^(?P<name>[A-Za-z][A-Za-z0-9\-\.\+_/ ]*?)\s+(?P<nums>(?:[+\-]?(?:\d+(?:\.\d*)?|\.\d+)\s*)+)$"
)

#: Как в статье сокращают названия наборов данных и что писать в CSV.
DATASET_ALIASES = {
    "2wiki": "2WikiMultihopQA",
    "2wikimultihopqa": "2WikiMultihopQA",
    "hotpotqa": "HotpotQA",
    "musique": "MuSiQue",
}

#: Определения метрик из раздела 4.1 (проверяются глазами один раз).
METRIC_DEFINITIONS = {
    "EM": ("answer quality", "exact match after standard text normalization", "higher_is_better"),
    "F1": ("answer quality", "token-level overlap after standard text normalization", "higher_is_better"),
}

_ID_PREFIX = {
    "configurations": "C",
    "datasets": "DS",
    "tasks": "T",
    "experiments": "E",
    "results": "R",
    "issues": "I",
}
_ID_WIDTH = {"configurations": 2, "datasets": 2, "tasks": 2, "experiments": 2, "results": 3, "issues": 2}


@dataclass
class Extraction:
    """Строки семи таблиц для одной публикации."""

    publication_id: str
    tables: dict[str, list[dict[str, str]]] = field(
        default_factory=lambda: {name: [] for name in TABLE_ORDER}
    )

    def add(self, table: str, row: dict[str, str]) -> None:
        self.tables[table].append(row)

    def __len__(self) -> int:
        return sum(len(rows) for rows in self.tables.values())


class Builder:
    """Выдаёт устойчивые идентификаторы и не дублирует сущности."""

    def __init__(self, publication_id: str) -> None:
        self.publication_id = publication_id
        self.extraction = Extraction(publication_id)
        self._counters = {table: 0 for table in _ID_PREFIX}
        self._seen: dict[tuple[str, tuple[str, ...]], str] = {}

    def _new_id(self, table: str) -> str:
        self._counters[table] += 1
        width = _ID_WIDTH[table]
        return f"{_ID_PREFIX[table]}{self._counters[table]:0{width}d}"

    def _entity(
        self,
        table: str,
        signature: Sequence[str],
        row: dict[str, str],
    ) -> str:
        key = (table, tuple(fold_part(part) for part in signature))
        if key in self._seen:
            return self._seen[key]
        identifier = self._new_id(table)
        self._seen[key] = identifier
        payload = {"publication_id": self.publication_id}
        payload.update(row)
        payload[entity_key(table)] = identifier
        self.extraction.add(table, payload)
        return identifier

    # --- сущности -------------------------------------------------------
    def publication(self, **fields: str) -> None:
        row = {"publication_id": self.publication_id}
        row.update({k: v for k, v in fields.items() if v is not None})
        self.extraction.add("publications", row)

    def config(self, **fields: str) -> str:
        signature = [
            fields.get("rag_variant", ""),
            fields.get("generator_model", ""),
            fields.get("retrieval_method", ""),
            fields.get("embedding_model", ""),
        ]
        return self._entity("configurations", signature, fields)

    def dataset(self, **fields: str) -> str:
        return self._entity("datasets", [fields.get("dataset_name", "")], fields)

    def task(self, **fields: str) -> str:
        return self._entity("tasks", [fields.get("task_type", "")], fields)

    def experiment(self, **fields: str) -> str:
        return self._entity(
            "experiments",
            [fields.get("evaluation_protocol", "")[:120], fields.get("hardware", "")],
            fields,
        )

    def result(self, **fields: str) -> str:
        signature = [
            fields.get("config_id", ""),
            fields.get("dataset_id", ""),
            fields.get("task_id", ""),
            fields.get("metric_name", ""),
            fields.get("metric_k", ""),
            fields.get("metric_variant", ""),
        ]
        key = ("results", tuple(fold_part(p) for p in signature))
        if key in self._seen:
            return self._seen[key]
        identifier = self._new_id("results")
        self._seen[key] = identifier
        payload = {"publication_id": self.publication_id, "result_id": identifier}
        payload.update(fields)
        self.extraction.add("results", payload)
        return identifier

    def issue(self, **fields: str) -> str:
        identifier = self._new_id("issues")
        payload = {"publication_id": self.publication_id, "issue_id": identifier}
        payload.update(fields)
        self.extraction.add("issues", payload)
        return identifier


def entity_key(table: str) -> str:
    return {
        "configurations": "config_id",
        "datasets": "dataset_id",
        "tasks": "task_id",
        "experiments": "experiment_id",
        "results": "result_id",
        "issues": "issue_id",
        "publications": "publication_id",
    }[table]


def fold_part(value: str) -> str:
    return norm(value).casefold().replace(" ", "")


# --- разбор Table 1 --------------------------------------------------------


@dataclass
class GridTable:
    """Числовая таблица: строки-методы и координатная сетка условий."""

    page: int
    caption: str
    methods: list[tuple[str, list[float]]]
    row_fragments: dict[str, str]
    generators: list[str]
    datasets: list[str]
    metrics: list[str]

    @property
    def shape(self) -> tuple[int, int]:
        return len(self.methods), len(self.generators) * len(self.datasets) * len(self.metrics)


def _is_number_line(line: str) -> bool:
    return bool(_NUMBER_LINE.match(line.strip()))


def parse_numeric_grid(page_text: str, page_number: int) -> GridTable | None:
    """Разобрать таблицу, где каждая ячейка вынесена на отдельную строку.

    Так PyMuPDF отдаёт ячейки PDF-таблиц: заголовок, затем имя метода и
    подряд все числа строки. Строки-разделители (``GraphRAG``) отбрасываются.
    """
    lines = [line.strip() for line in page_text.splitlines() if line.strip()]
    try:
        start = lines.index("Methods")
    except ValueError:
        return None

    generators = lines[start + 1 : start + 4]
    dataset_tokens = lines[start + 4 : start + 13]
    metric_tokens = lines[start + 13 : start + 31]
    if len(generators) != 3 or len(dataset_tokens) != 9 or len(metric_tokens) != 18:
        return None
    if not all(token and not token[0].isdigit() for token in metric_tokens):
        return None

    datasets = _unique(dataset_tokens[:3])
    metrics = _unique(metric_tokens[:2])

    methods: list[tuple[str, list[float]]] = []
    fragments: dict[str, str] = {}
    name: str | None = None
    numbers: list[float] = []

    def flush() -> None:
        nonlocal name, numbers
        if name and len(numbers) == len(generators) * len(datasets) * len(metrics):
            methods.append((name, numbers))
            fragments[name] = "\n".join([name] + [format_number(v) for v in numbers])
        name, numbers = None, []

    for line in lines[start + 31 :]:
        if _is_number_line(line):
            if name is None:
                break
            numbers.append(float(line))
            continue
        flush()
        if line.lower().startswith("table"):
            break
        name = line
    flush()

    if not methods:
        return None
    return GridTable(
        page=page_number,
        caption="Table 1",
        methods=methods,
        row_fragments=fragments,
        generators=generators,
        datasets=datasets,
        metrics=metrics,
    )


def format_number(value: float) -> str:
    """Вернуть число так, как оно напечатано: без лишних нулей."""
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return text if text else "0"


def _unique(items: Iterable[str]) -> list[str]:
    seen: list[str] = []
    for item in items:
        if item not in seen:
            seen.append(item)
    return seen


# --- сборка строк ----------------------------------------------------------


def extract_table_one(
    builder: Builder,
    table: GridTable,
    section: str = "4.1; 4.2",
    hardware: str = "4 NVIDIA A6000 GPUs",
) -> int:
    """Разложить Table 1 в configurations / datasets / tasks / experiments / results."""
    n_datasets = len(table.datasets)
    n_metrics = len(table.metrics)

    dataset_ids = [
        builder.dataset(
            dataset_name=DATASET_ALIASES.get(name.casefold(), name),
            note=f"PDF p. {table.page}, section {section.split(';')[0].strip()}.",
        )
        for name in table.datasets
    ]
    task_id = builder.task(
        task_type="open-domain multi-hop question answering",
        note=f"PDF p. {table.page}, section {section.split(';')[0].strip()}.",
    )
    experiment_id = builder.experiment(
        evaluation_protocol=(
            "Table 1 main evaluation. EM and F1 after standard text normalization; "
            "same data splits as prior work. Table 1 reports the authors' experiments "
            "and rerun baselines."
        ),
        hardware=hardware,
        note=f"PDF p. {table.page}, sections {section}.",
    )

    produced = 0
    for method, numbers in table.methods:
        for g_index, generator in enumerate(table.generators):
            config_id = builder.config(
                rag_variant=method,
                generator_model=generator,
                note=(
                    f"PDF p. {table.page}, Table 1 row {method}, "
                    f"generator {generator}."
                ),
            )
            for d_index, dataset in enumerate(table.datasets):
                for m_index, metric in enumerate(table.metrics):
                    offset = (g_index * n_datasets + d_index) * n_metrics + m_index
                    raw = format_number(numbers[offset])
                    group, variant, direction = METRIC_DEFINITIONS.get(
                        metric, ("", "", "")
                    )
                    builder.result(
                        experiment_id=experiment_id,
                        config_id=config_id,
                        dataset_id=dataset_ids[d_index],
                        task_id=task_id,
                        metric_group=group,
                        metric_name=metric,
                        metric_variant=variant,
                        metric_direction=direction,
                        metric_value=_with_leading_zero(raw),
                        metric_unit="fraction",
                        raw_value=raw,
                        raw_unit="",
                        normalized_value=_with_leading_zero(raw),
                        normalized_unit="fraction",
                        result_origin="primary",
                        source_page=str(table.page),
                        source_section=section,
                        source_table=table.caption,
                        source_fragment=table.row_fragments[method],
                        extraction_status="extracted",
                        note=(
                            f"Table 1: row {method}; generator {generator}; "
                            f"dataset {DATASET_ALIASES.get(dataset.casefold(), dataset)}; "
                            f"column {metric}. Leading zero added without scale conversion; "
                            f"no unit symbol printed in the table."
                        ),
                    )
                    produced += 1
    return produced


def _with_leading_zero(value: str) -> str:
    if value.startswith("."):
        return "0" + value
    if value.startswith("-."):
        return "-0" + value[1:]
    return value


# --- точка входа -----------------------------------------------------------


def extract_document(publication_id: str, document: Document) -> Extraction:
    """Извлечь всё, что поддерживается текущей версией экстрактора."""
    builder = Builder(publication_id)
    builder.publication(**extract_metadata(document))

    for page in document.pages:
        table = parse_numeric_grid(page.text, page.number)
        if table is not None:
            extract_table_one(builder, table)

    if not builder.extraction.tables["results"]:
        builder.issue(
            check_name="no_structured_results_extracted",
            description=(
                "Детерминированный экстрактор не нашёл числовой таблицы результатов. "
                "Публикация требует отдельного разбора; пустые значения не заполнены."
            ),
            source_page="",
            source_fragment="",
            record_ids="",
        )
    return builder.extraction


_TITLE_HINT = re.compile(r"^[A-Z][^.]{20,200}$")


def extract_metadata(document: Document) -> dict[str, str]:
    """Грубые метаданные с первой страницы: заголовок, год, DOI, авторы."""
    if not document.pages:
        return {}
    lines = [norm(line) for line in document.pages[0].text.splitlines() if norm(line)]
    meta: dict[str, str] = {}

    for line in lines[:12]:
        if _TITLE_HINT.match(line) and "title" not in meta:
            meta["title"] = line
            break

    joined = "\n".join(lines)
    doi = re.search(r"\b(10\.\d{4,9}/[^\s,;]+)", joined)
    if doi:
        meta["DOI"] = doi.group(1).rstrip(".")
    year = re.search(r"\b(19|20)\d{2}\b", joined)
    if year:
        meta["year"] = year.group(0)
    return meta
