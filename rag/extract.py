"""Извлечение RAG-сущностей из публикации.

Конвейер детерминированный:

1. метаданные первой страницы;
2. проза: задача, наборы, конфигурация по умолчанию;
3. таблицы: ``grid.find_tables`` восстанавливает сетку по координатам слов,
   ``vocab`` узнаёт метрики и наборы;
4. прозаические результаты (осторожно, только при явном числе и наборе);
5. проверки согласованности (``issues``).

Всё, что не подтверждено текстом страницы, остаётся пустым. Графики не
оцифровываются: это отдельная CV-задача, а не текстовое извлечение.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from .grid import Table, find_tables
from .kit import TABLE_ORDER
from .metadata import extract_metadata
from .pdf import Document, Page
from .text import norm
from .vocab import (
    Metric,
    canonical_dataset,
    detect_tasks,
    normalize_generator,
    number_value,
    parse_metric,
)

_ID_PREFIX = {
    "configurations": "C",
    "datasets": "DS",
    "tasks": "T",
    "experiments": "E",
    "results": "R",
    "issues": "I",
}
_ID_WIDTH = {"configurations": 2, "datasets": 2, "tasks": 2, "experiments": 2, "results": 3, "issues": 2}

_UNSPECIFIED_DATASET = "Unspecified"
_UNSPECIFIED_TASK = "unspecified"

#: Колонки разницы: значение в них — дельта, а не метрика.
_DELTA_COLUMN = re.compile(
    r"(^|[\s|(])(?:Δ|∆|delta|diff|difference|improvement|gain)(?=$|[\s|)])",
    re.I,
)

_PROPOSED = re.compile(
    r"(?:we\s+(?:propose|present|introduce|develop)|our\s+(?:approach|method|system))\s+"
    r"([A-Z][A-Za-z0-9\-]{2,30})"
)
_GENERATOR_IN_TEXT = re.compile(
    r"\b(GPT-4o(?:\s+mini)?|GPT-4|GPT-3\.5|Llama[\s\-]?\d[\w.\-]*|LLaMA[\s\-]?\d[\w.\-]*|"
    r"Qwen[\w.\-]*|Mistral[\w.\-]*|Mixtral[\w.\-]*|Claude[\w.\-]*|Gemini[\w.\-]*|"
    r"DeepSeek[\w.\-]*|Vicuna[\w.\-]*|Flan-T5[\w.\-]*|Gemma[\w.\-]*|Phi-\d[\w.\-]*)"
)
_MODEL_ID = re.compile(
    r"\b((?:gpt|llama|qwen|mistral|mixtral|claude|gemini|deepseek)[a-z0-9.\-]*(?:-[a-z0-9.\-]+)+)\b",
    re.I,
)


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
            fields.get("experiment_id", ""),
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


# --- значения --------------------------------------------------------------


def metric_value_text(raw: str) -> str:
    """Число в том же масштабе, но без единиц и с ведущим нулём."""
    token = raw.strip().replace("\u2212", "-").replace("\u00a0", "").replace(" ", "")
    if token.endswith("%"):
        token = token[:-1]
    elif token.endswith("ms"):
        token = token[:-2]
    elif token.endswith("s") and not token.endswith("ss"):
        token = token[:-1]
    if token.startswith("."):
        token = "0" + token
    elif token.startswith("-."):
        token = "-0" + token[1:]
    if re.fullmatch(r"[+\-]?0,\d+", token):
        token = token.replace(",", ".")
    elif re.fullmatch(r"[+\-]?\d{1,3}(?:,\d{3})+", token):
        token = token.replace(",", "")
    elif "," in token and "." not in token:
        token = token.replace(",", ".")
    return token


def raw_unit_text(raw: str) -> str:
    """Единица, напечатанная прямо в ячейке (если есть)."""
    token = raw.strip()
    if token.endswith("%"):
        return "%"
    if token.endswith("ms"):
        return "ms"
    if token.endswith("s") and not token.endswith("ss"):
        return "s"
    return ""


def quote_from_page(page_text: str, label: str, raw: str, fallback: str) -> str:
    """Непрерывный кусок текста страницы: подпись строки и значение рядом."""
    if not page_text:
        return fallback
    text = re.sub(r"(?<=\w)-\s*\r?\n\s*(?=\w)", "", page_text)
    flat = re.sub(r"\s+", " ", text).strip()
    label_flat = re.sub(r"(?<=\w)-\s+", "", re.sub(r"\s+", " ", label).strip())
    if not label_flat:
        return fallback
    position = -1
    for candidate in (
        label_flat,
        label_flat.rstrip(" .:|-"),
        label_flat[:12].strip(),
        label_flat[:6].strip(),
    ):
        if candidate:
            position = flat.find(candidate)
            if position >= 0:
                break
    if position < 0:
        value_position = flat.find(raw)
        if value_position < 0:
            return fallback
        return flat[max(0, value_position - 60) : value_position + len(raw)].strip()
    end = flat.find(raw, position)
    if 0 <= end - position <= 220:
        return flat[position : end + len(raw)]
    return flat[position : position + 120].strip()


_BOUNDED_METRICS = {
    "EM",
    "F1",
    "Accuracy",
    "Precision",
    "Recall",
    "NDCG",
    "MRR",
    "MAP",
    "Hit Rate",
    "AUC",
    "ROUGE-L",
    "ROUGE-1",
    "ROUGE-2",
    "BLEU",
    "BERTScore",
    "Faithfulness",
    "Answer relevance",
    "Context precision",
    "Context recall",
    "Answer correctness",
    "Answer semantic similarity",
    "Context relevance",
    "Noise robustness",
    "Completeness",
    "Correctness",
    "Fluency",
    "Coherence",
    "Relevance",
    "Groundedness",
    "Win rate",
}


def metric_unit(metric: Metric, raw: str, value: float, header: str = "") -> str:
    if "%" in raw or metric.unit_hint == "%" or "%" in header:
        return "%"
    if metric.name == "Latency":
        return "ms" if raw.rstrip().endswith("ms") else "s"
    if 0.0 <= value <= 1.0:
        return "fraction"
    if metric.name in _BOUNDED_METRICS and value <= 100.0:
        return "%"
    return "score"


def clean_variant(label: str) -> str:
    """Убрать групповые приставки и пометки авторства из метки метода."""
    text = re.sub(r"\s*[∗*†‡]+$", "", (label or "").strip())
    for prefix in ("Simple ", "Complex ", "Oracle "):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    text = re.sub(r"\s*\((?:Ours|ours|Our method)\)\s*$", "", text)
    text = text.replace("Adaptive Self-RAG", "Self-RAG")
    return re.sub(r"\s+", " ", text).strip()


def metric_display(metric: Metric) -> str:
    if metric.k and not metric.name.endswith("@" + metric.k):
        return f"{metric.name}@{metric.k}"
    return metric.name


# --- проза -----------------------------------------------------------------


def choose_task(prose: str) -> str:
    tasks = detect_tasks(prose)
    if "multi-hop question answering" in tasks and re.search(r"open[\s\-]?domain", prose, re.I):
        return "open-domain multi-hop question answering"
    return tasks[0] if tasks else ""


def proposed_variant(prose: str) -> str:
    match = _PROPOSED.search(prose)
    return match.group(1) if match else ""


def resolve_generator(name: str, fallback: str) -> str:
    """Дополнить короткое имя модели точным идентификатором из текста."""
    if not name:
        return fallback
    if not fallback:
        return name
    short = name.casefold().replace(" ", "")
    long = fallback.casefold().replace(" ", "")
    if long.startswith(short) and len(long) > len(short):
        return fallback
    return name


def generator_from_text(prose: str) -> str:
    # Точный API-идентификатор в прозе информативнее заголовка таблицы.
    identifiers = [m.group(1) for m in _MODEL_ID.finditer(prose)]
    if identifiers:
        best = max(identifiers, key=len)
        if len(best) >= 8:
            return best
    match = _GENERATOR_IN_TEXT.search(prose)
    if not match:
        return ""
    name = match.group(1)
    tail = prose[match.end(): match.end() + 12]
    size = re.match(r"\s*\((\d+(?:\.\d+)?\s*[BMK])\)", tail)
    if size:
        name = f"{name} {size.group(1)}"
    return normalize_generator(name)


_GPU_MODEL = re.compile(
    r"\b(A100|A800|A6000|H100|H800|H20|V100|L40S|L4|RTX\s*\d{4}|TPU\s*v\d[\w.]*)\b",
    re.I,
)
_GPU_LEAD = re.compile(r"((?:\d{1,2}\s*(?:[x×]\s*)?)?(?:(?:NVIDIA|AMD|Intel)\s+)?)$", re.I)
_GPU_TAIL = re.compile(r"(?:\s*\d{2}\s*GB)?(?:\s*(?:GPUs?|TPUs?))?", re.I)


def hardware_from_text(prose: str) -> str:
    """Железо из прозы: «4 NVIDIA A6000 GPUs» и подобные упоминания.

    Берётся только явно названная модель ускорителя, чтобы не выдумывать
    конфигурацию. Строка приводится к виду «<кол-во> <вендор> <модель> <юнит>».
    """
    flat = re.sub(r"\s+", " ", prose)
    for match in _GPU_MODEL.finditer(flat):
        head = _GPU_LEAD.search(flat[: match.start()])
        tail = _GPU_TAIL.match(flat, match.end())
        span = flat[(head.start(1) if head else match.start()): (tail.end() if tail else match.end())]
        span = span.strip(" ,.;:")
        lowered = span.casefold()
        if not re.search(r"nvidia|amd|intel|gpu|tpu", lowered):
            continue
        return span
    return ""


def sample_size_from_text(prose: str) -> str:
    """Единственное явное число сэмплов в прозе; иначе пусто."""
    counts = {
        m.group(1)
        for m in re.finditer(
            r"(\d{2,6})\s+(?:randomly\s+sampled|sampled|held-?out|test|evaluation)\s+"
            r"(?:queries|questions|samples|examples|instances|prompts)\b",
            prose,
            re.I,
        )
    }
    return counts.pop() if len(counts) == 1 else ""


def dataset_in_text(text: str) -> str:
    best = ""
    position = len(text) + 1
    for name in _DATASET_NAMES:
        index = text.casefold().find(name.casefold())
        if index >= 0 and index < position:
            best, position = name, index
    return best


_DATASET_NAMES: tuple[str, ...] = (
    "HotpotQA",
    "2WikiMultihopQA",
    "2Wiki",
    "MuSiQue",
    "Natural Questions",
    "TriviaQA",
    "WebQuestions",
    "SQuAD",
    "MS MARCO",
    "BEIR",
    "FEVER",
    "SciFact",
    "NFCorpus",
    "SciDocs",
    "TREC-COVID",
    "DBpedia-Entity",
    "PubMedQA",
    "BioASQ",
    "MedQA",
    "MedMCQA",
    "MMLU",
    "MIRAGE",
    "FeB4RAG",
    "CRAG",
    "CovidQA",
    "DelucionQA",
    "Emanual",
    "ExpertQA",
    "FinQA",
    "HAGRID",
    "SimpleQA",
    "TAT-QA",
    "TechQA",
    "FinanceBench",
    "LongHealth",
    "QuaLITY",
    "PopQA",
    "Bamboogle",
    "DROP",
    "WoW",
    "RAGTruth",
    "NewsQA",
    "SearchQA",
    "Dolly",
    "ASQA",
    "ELI5",
    "QAMPARI",
    "DocBench",
    "MMLongBench-Doc",
)


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]


_PROSE_RESULT = re.compile(
    r"\b(EM|exact\s+match|F1|accuracy|precision|recall|nDCG@?\d*|MRR@?\d*|"
    r"ROUGE-L|BLEU|hit\s+rate)\b[^.;%]{0,40}?"
    r"([+\-]?(?:\d{1,3}(?:\.\d+)?|\.\d+)\s*%?)",
    re.I,
)


def _prose_results(
    builder: Builder,
    document: Document,
    experiment_id: str,
    task_id: str,
    config_id: str,
) -> int:
    produced = 0
    for page in document.pages:
        if page.source == "empty":
            continue
        for sentence in _sentences(page.text):
            dataset = dataset_in_text(sentence)
            if not dataset:
                continue
            for match in _PROSE_RESULT.finditer(sentence):
                raw = match.group(2).strip()
                if "%" not in raw and not raw.startswith("."):
                    continue  # без знака процента число в прозе слишком двусмысленно
                metric = parse_metric(match.group(1))
                if metric is None:
                    continue
                value = number_value(raw)
                if value is None:
                    continue
                dataset_id = builder.dataset(
                    dataset_name=dataset,
                    note=f"PDF p. {page.number}, mentioned in prose.",
                )
                builder.result(
                    experiment_id=experiment_id,
                    config_id=config_id,
                    dataset_id=dataset_id,
                    task_id=task_id,
                    metric_group=metric.group,
                    metric_name=metric_display(metric),
                    metric_variant=metric.variant,
                    metric_direction=metric.direction,
                    metric_k=metric.k,
                    metric_value=metric_value_text(raw),
                    metric_unit=metric_unit(metric, raw, value),
                    raw_value=raw,
                    raw_unit=raw_unit_text(raw),
                    normalized_value=metric_value_text(raw),
                    normalized_unit=metric_unit(metric, raw, value),
                    result_origin="primary",
                    source_page=str(page.number),
                    source_table="",
                    source_figure="",
                    source_fragment=quote_from_page(page.text, sentence[:60], raw, sentence),
                    extraction_status="extracted",
                    note="Value stated in prose; sentence quoted verbatim.",
                )
                produced += 1
    return produced


# --- таблицы ---------------------------------------------------------------


def _extract_table(
    builder: Builder,
    table: Table,
    *,
    task_id: str,
    default_config: dict[str, str],
    default_dataset: str,
    hardware: str = "",
    sample_size: str = "",
) -> int:
    if not table.columns:
        return 0
    caption = table.caption or "unnumbered table"
    metrics = sorted({metric_display(c.metric) for c in table.columns if c.metric})
    datasets = sorted({c.dataset for c in table.columns if c.dataset})
    details = ""
    if datasets:
        details += f" Datasets in columns: {', '.join(datasets[:8])}."
    if metrics:
        details += f" Metrics: {', '.join(metrics[:8])}."
    protocol = (
        f"{caption}: one row per configuration, one column per dataset/metric;"
        f" values read from the table as printed.{details}"
    )
    if table.caption_text:
        protocol = f"{table.caption_text} {protocol}"
    # Число сэмплов ищем рядом с таблицей, а не по всей статье: иначе один
    # и тот же счётчик приписывается всем экспериментам подряд.
    local_sample = sample_size_from_text(f"{table.caption_text} {table.page_text}")
    experiment_id = builder.experiment(
        evaluation_protocol=protocol,
        hardware=hardware,
        sample_size=local_sample,
        note=f"PDF p. {table.page}, {caption}.",
    )

    row_datasets = {row.label: canonical_dataset(row.label) for row in table.rows}
    known = sum(1 for value in row_datasets.values() if value)
    rows_are_datasets = known >= max(1, len(table.rows) // 2)
    caption_generator = generator_from_text(table.caption_text)

    produced = 0
    for row in table.rows:
        variant = clean_variant(row.label)
        if not rows_are_datasets and not re.search(r"[A-Za-z]", variant):
            continue
        for column_index, (raw, value) in row.values.items():
            if column_index >= len(table.columns):
                continue
            column = table.columns[column_index]
            metric = column.metric
            if metric is None:
                continue
            if _DELTA_COLUMN.search(column.text):
                # Колонка разницы (Δ) — это не значение метрики.
                continue
            if value < 0:
                # В таблицах отрицательные числа — это дельты (Δ, «improvement»),
                # а не значения метрики. Как результат их не выдаём.
                continue

            if rows_are_datasets:
                dataset_name = row_datasets.get(row.label) or default_dataset
                config_fields = dict(default_config)
            else:
                dataset_name = column.dataset or row.dataset or default_dataset
                config_fields = {"rag_variant": variant}
                fallback = caption_generator or default_config.get("generator_model", "")
                generator = resolve_generator(row.generator or column.generator, fallback)
                if generator:
                    config_fields["generator_model"] = generator

            dataset_name = dataset_name or _UNSPECIFIED_DATASET
            dataset_id = builder.dataset(
                dataset_name=dataset_name,
                note=f"PDF p. {table.page}, {caption}.",
            )
            config_id = builder.config(
                **{
                    **config_fields,
                    "note": f"PDF p. {table.page}, {caption}, row {row.label}.",
                }
            )

            builder.result(
                experiment_id=experiment_id,
                config_id=config_id,
                dataset_id=dataset_id,
                task_id=task_id,
                metric_group=metric.group,
                metric_name=metric_display(metric),
                metric_variant=metric.variant,
                metric_direction=metric.direction,
                metric_k=metric.k,
                metric_value=metric_value_text(raw),
                metric_unit=metric_unit(metric, raw, value, column.text + " " + table.caption_text),
                raw_value=raw,
                raw_unit=raw_unit_text(raw),
                normalized_value=metric_value_text(raw),
                normalized_unit=metric_unit(metric, raw, value, column.text + " " + table.caption_text),
                result_origin="primary",
                source_page=str(table.page),
                source_table=table.caption,
                source_figure="",
                source_fragment=quote_from_page(
                    table.page_text, row.label, raw, row.fragment
                ),
                extraction_status="extracted",
                note=(
                    f"{caption}: row {row.label}; column "
                    f"{column.text or metric_display(metric)}. "
                    f"Value copied as printed; unit inferred from the header/value scale."
                ),
            )
            produced += 1
    return produced


# --- точка входа -----------------------------------------------------------


def extract_document(publication_id: str, document: Document) -> Extraction:
    """Извлечь всё, что поддерживается текущей версией экстрактора."""
    from .issues import add_issues

    builder = Builder(publication_id)
    builder.publication(**extract_metadata(document))

    if document.is_scan:
        builder.issue(
            check_name="no_text_layer",
            description=(
                "В PDF нет текстового слоя (скан). Без OCR извлечение невозможно; "
                "пустые значения не заполнены догадками."
            ),
            source_page="1",
            source_fragment="",
            record_ids="",
        )
        return builder.extraction

    prose = document.text()
    task_type = choose_task(prose) or _UNSPECIFIED_TASK
    task_id = builder.task(
        task_type=task_type,
        note="Task inferred from the publication text." if task_type != _UNSPECIFIED_TASK else "",
    )

    variant = proposed_variant(prose)
    generator = generator_from_text(prose)
    default_config: dict[str, str] = {}
    if variant:
        default_config["rag_variant"] = variant
    if generator:
        default_config["generator_model"] = generator

    default_dataset = dataset_in_text(prose)
    hardware = hardware_from_text(prose)
    sample_size = sample_size_from_text(prose)

    tables: list[Table] = []
    for page in document.pages:
        tables.extend(find_tables(page))

    for table in tables:
        _extract_table(
            builder,
            table,
            task_id=task_id,
            default_config=default_config,
            default_dataset=default_dataset,
            hardware=hardware,
            sample_size=sample_size,
        )

    # Прозаические результаты — только если таблицы не дали ни одной строки.
    if not builder.extraction.tables["results"] and default_config:
        config_id = builder.config(**default_config)
        experiment_id = builder.experiment(
            evaluation_protocol="Values stated in prose; no numeric table extracted.",
            hardware=hardware,
            sample_size=sample_size,
            note="Prose-only extraction.",
        )
        _prose_results(builder, document, experiment_id, task_id, config_id)

    add_issues(builder)
    return builder.extraction
