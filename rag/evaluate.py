"""Измерение качества извлечения относительно ``dev_gold``.

Модуль отвечает на вопрос «стало ли лучше», которого не закрывает
официальный чекер: тот проверяет формат и цитаты, но не полноту и не
точность значений.

Метрики считаются по каждой таблице и по каждой публикации отдельно:
76 % эталона RAG приходится на одну статью, поэтому среднее по датасету
скрывает плохое качество на остальных.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence

from .kit import TABLE_ORDER
from .text import canonical_metric, fold, is_blank, values_equal

#: Поля, которые сравниваются как числа.
NUMERIC_FIELDS: dict[str, set[str]] = {
    "publications": {"year"},
    "configurations": {"chunk_size", "chunk_overlap", "top_k"},
    "experiments": {"latency_ms", "tokens_per_query", "sample_size"},
    "results": {
        "metric_k",
        "metric_value",
        "baseline_value",
        "sample_size",
        "normalized_value",
        "source_page",
    },
    "issues": {"source_page"},
}

#: Поля, которые имеет смысл сверять по значению.
CHECKED_FIELDS: dict[str, tuple[str, ...]] = {
    "publications": ("title", "year", "DOI", "authors", "in_scope"),
    "configurations": (
        "rag_variant",
        "retrieval_method",
        "embedding_model",
        "chunking_method",
        "chunk_size",
        "top_k",
        "generator_model",
        "prompt_strategy",
        "context_strategy",
    ),
    "datasets": ("dataset_name", "language", "data_split", "scientific_domain"),
    "tasks": ("task_type",),
    "experiments": (
        "evaluation_protocol",
        "baseline_name",
        "hardware",
        "sample_size",
    ),
    "results": (
        "metric_value",
        "metric_unit",
        "metric_direction",
        "metric_k",
        "source_page",
        "source_table",
        "source_figure",
        "extraction_status",
    ),
    "issues": ("check_name", "description", "source_page"),
}


@dataclass
class FieldScore:
    field: str
    compared: int = 0
    exact: int = 0
    false_fill: int = 0  # в эталоне пусто, у нас заполнено

    @property
    def accuracy(self) -> float:
        return self.exact / self.compared if self.compared else 0.0

    @property
    def false_fill_rate(self) -> float:
        return self.false_fill / self.compared if self.compared else 0.0


@dataclass
class TableScore:
    table: str
    gold: int = 0
    pred: int = 0
    matched: int = 0
    fields: list[FieldScore] = field(default_factory=list)
    missing: int = 0  # в эталоне есть, у нас нет
    extra: int = 0  # у нас есть, в эталоне нет
    missing_examples: list[str] = field(default_factory=list)
    extra_examples: list[str] = field(default_factory=list)

    @property
    def precision(self) -> float:
        return self.matched / self.pred if self.pred else 0.0

    @property
    def recall(self) -> float:
        return self.matched / self.gold if self.gold else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if p + r else 0.0


@dataclass
class Report:
    gold_dir: Path
    pred_dir: Path
    tolerance: float
    mode: str
    by_table: dict[str, TableScore]
    by_publication: dict[str, dict[str, TableScore]]
    by_table_value_mode: dict[str, TableScore] = field(default_factory=dict)
    by_table_cell_mode: dict[str, TableScore] = field(default_factory=dict)
    gold_total: int = 0
    pred_total: int = 0


# --- сопоставление ---------------------------------------------------------


def _sig_publications(row: Mapping[str, str]) -> tuple[str, ...]:
    return (row.get("publication_id", ""),)


def _sig_configurations(row: Mapping[str, str]) -> tuple[str, ...]:
    return (
        row.get("publication_id", ""),
        fold(row.get("rag_variant", "")),
        fold(row.get("generator_model", "")),
    )


def _sig_datasets(row: Mapping[str, str]) -> tuple[str, ...]:
    return (row.get("publication_id", ""), fold(row.get("dataset_name", "")))


def _sig_tasks(row: Mapping[str, str]) -> tuple[str, ...]:
    return (row.get("publication_id", ""), fold(row.get("task_type", "")))


def _sig_experiments(row: Mapping[str, str]) -> tuple[str, ...]:
    return (
        row.get("publication_id", ""),
        fold(row.get("evaluation_protocol", ""))[:120],
    )


def _sig_results_id(row: Mapping[str, str]) -> tuple[str, ...]:
    return (
        row.get("publication_id", ""),
        fold(row.get("config_id", "")),
        fold(row.get("dataset_id", "")),
        fold(row.get("task_id", "")),
        canonical_metric(row.get("metric_name", "")),
        fold(row.get("metric_k", "")),
        fold(row.get("metric_variant", "")),
    )


def _compact(value: str) -> str:
    return fold(value).replace(" ", "").replace("-", "")


def _resolve_results(
    rows: Sequence[Mapping[str, str]],
    submission: Mapping[str, Sequence[Mapping[str, str]]],
) -> list[dict[str, str]]:
    """Подставить в results семантические имена вместо внутренних id.

    Идентификаторы у эталона и предсказания всегда разные, поэтому сверять
    результаты по ``config_id``/``dataset_id`` бессмысленно. Сверяем по тому,
    что эти id обозначают: вариант, генератор, набор, метрика, k.
    """
    configs = {
        (row.get("publication_id", ""), row.get("config_id", "")): row
        for row in submission.get("configurations", [])
    }
    datasets = {
        (row.get("publication_id", ""), row.get("dataset_id", "")): row
        for row in submission.get("datasets", [])
    }
    tasks = {
        (row.get("publication_id", ""), row.get("task_id", "")): row
        for row in submission.get("tasks", [])
    }

    resolved: list[dict[str, str]] = []
    for row in rows:
        publication_id = row.get("publication_id", "")
        config = configs.get((publication_id, row.get("config_id", "")), {})
        dataset = datasets.get((publication_id, row.get("dataset_id", "")), {})
        task = tasks.get((publication_id, row.get("task_id", "")), {})
        payload = dict(row)
        payload["_variant"] = config.get("rag_variant", "")
        payload["_generator"] = config.get("generator_model", "")
        payload["_dataset"] = dataset.get("dataset_name", "")
        payload["_task"] = task.get("task_type", "")
        resolved.append(payload)
    return resolved


def _sig_results_semantic(row: Mapping[str, str]) -> tuple[str, ...]:
    # Набор данных — отдельное измерение, а не часть идентичности замера:
    # имена наборов в эталоне человеческие и часто составные
    # («Combined six QA benchmarks»). Сверяем конфигурацию + метрику + k.
    return (
        row.get("publication_id", ""),
        fold(row.get("_variant", "")),
        fold(row.get("_generator", "")),
        canonical_metric(row.get("metric_name", "")),
        fold(row.get("metric_k", "")),
    )


def _sig_results_value(row: Mapping[str, str]) -> tuple[str, ...]:
    return (
        row.get("publication_id", ""),
        canonical_metric(row.get("metric_name", "")),
        fold(row.get("metric_value", "")),
        fold(row.get("metric_k", "")),
    )


def _sig_results_cell(row: Mapping[str, str]) -> tuple[str, ...]:
    """Диагностика качества оцифровки: та же страница, метрика и значение."""
    return (
        row.get("publication_id", ""),
        fold(row.get("source_page", "")),
        canonical_metric(row.get("metric_name", "")),
        fold(row.get("metric_value", "")),
        fold(row.get("metric_k", "")),
    )


def _sig_issues(row: Mapping[str, str]) -> tuple[str, ...]:
    return (
        row.get("publication_id", ""),
        fold(row.get("check_name", "")),
        fold(row.get("source_fragment", ""))[:60],
    )


SIGNATURES: dict[str, Callable[[Mapping[str, str]], tuple[str, ...]]] = {
    "publications": _sig_publications,
    "configurations": _sig_configurations,
    "datasets": _sig_datasets,
    "tasks": _sig_tasks,
    "experiments": _sig_experiments,
    "results": _sig_results_id,
    "issues": _sig_issues,
}


def match(
    gold: Sequence[Mapping[str, str]],
    pred: Sequence[Mapping[str, str]],
    key: Callable[[Mapping[str, str]], tuple[str, ...]],
) -> tuple[list[tuple[int, int]], list[int], list[int]]:
    """Жадно сопоставить строки 1:1 по сигнатуре."""
    buckets: dict[tuple[str, ...], list[int]] = defaultdict(list)
    for index, row in enumerate(gold):
        buckets[key(row)].append(index)

    pairs: list[tuple[int, int]] = []
    used_gold: set[int] = set()
    unmatched_pred: list[int] = []
    for pred_index, row in enumerate(pred):
        bucket = buckets.get(key(row))
        if bucket:
            gold_index = bucket.pop(0)
            used_gold.add(gold_index)
            pairs.append((gold_index, pred_index))
        else:
            unmatched_pred.append(pred_index)

    unmatched_gold = [i for i in range(len(gold)) if i not in used_gold]
    return pairs, unmatched_gold, unmatched_pred


def _field_ok(table: str, name: str, gold_value: str, pred_value: str, tolerance: float) -> bool:
    if is_blank(gold_value) and is_blank(pred_value):
        return True
    if name in NUMERIC_FIELDS.get(table, set()):
        return values_equal(gold_value, pred_value, tolerance)
    return fold(gold_value) == fold(pred_value)


def score_table(
    table: str,
    gold: Sequence[Mapping[str, str]],
    pred: Sequence[Mapping[str, str]],
    *,
    tolerance: float = 0.0,
    key: Callable[[Mapping[str, str]], tuple[str, ...]] | None = None,
    examples: int = 5,
) -> TableScore:
    key = key or SIGNATURES[table]
    pairs, unmatched_gold, unmatched_pred = match(gold, pred, key)

    fields = {name: FieldScore(field=name) for name in CHECKED_FIELDS.get(table, ())}
    for gold_index, pred_index in pairs:
        gold_row, pred_row = gold[gold_index], pred[pred_index]
        for name, score in fields.items():
            gold_value = gold_row.get(name, "")
            pred_value = pred_row.get(name, "")
            score.compared += 1
            if _field_ok(table, name, gold_value, pred_value, tolerance):
                score.exact += 1
            elif is_blank(gold_value) and not is_blank(pred_value):
                score.false_fill += 1

    def describe(row: Mapping[str, str]) -> str:
        return " | ".join(
            f"{name}={row.get(name, '')}"
            for name in ("publication_id", "config_id", "dataset_id", "metric_name", "metric_value", "result_id")
            if row.get(name)
        )

    return TableScore(
        table=table,
        gold=len(gold),
        pred=len(pred),
        matched=len(pairs),
        fields=list(fields.values()),
        missing=len(unmatched_gold),
        extra=len(unmatched_pred),
        missing_examples=[describe(gold[i]) for i in unmatched_gold[:examples]],
        extra_examples=[describe(pred[i]) for i in unmatched_pred[:examples]],
    )


def evaluate(
    gold: Mapping[str, Sequence[Mapping[str, str]]],
    pred: Mapping[str, Sequence[Mapping[str, str]]],
    *,
    gold_dir: str | Path = "",
    pred_dir: str | Path = "",
    tolerance: float = 0.0,
) -> Report:
    """Сравнить предсказание с эталоном по всем семи таблицам."""
    gold_results = _resolve_results(gold.get("results", []), gold)
    pred_results = _resolve_results(pred.get("results", []), pred)
    resolved = {"gold": gold_results, "pred": pred_results}

    by_table: dict[str, TableScore] = {}
    for table in TABLE_ORDER:
        if table == "results":
            by_table[table] = score_table(
                table,
                gold_results,
                pred_results,
                tolerance=tolerance,
                key=_sig_results_semantic,
            )
            continue
        by_table[table] = score_table(
            table,
            gold.get(table, []),
            pred.get(table, []),
            tolerance=tolerance,
        )

    by_table_value: dict[str, TableScore] = {}
    by_table_value["results"] = score_table(
        "results",
        gold.get("results", []),
        pred.get("results", []),
        tolerance=tolerance,
        key=_sig_results_value,
    )

    by_table_cell: dict[str, TableScore] = {}
    by_table_cell["results"] = score_table(
        "results",
        gold.get("results", []),
        pred.get("results", []),
        tolerance=tolerance,
        key=_sig_results_cell,
    )

    by_publication: dict[str, dict[str, TableScore]] = {}
    publication_ids = sorted(
        {
            row.get("publication_id", "")
            for table in TABLE_ORDER
            for row in gold.get(table, [])
            if row.get("publication_id")
        }
    )
    for publication_id in publication_ids:
        scores: dict[str, TableScore] = {}
        for table in TABLE_ORDER:
            if table == "results":
                gold_rows = [r for r in gold_results if r.get("publication_id") == publication_id]
                pred_rows = [r for r in pred_results if r.get("publication_id") == publication_id]
                key = _sig_results_semantic
            else:
                gold_rows = [r for r in gold.get(table, []) if r.get("publication_id") == publication_id]
                pred_rows = [r for r in pred.get(table, []) if r.get("publication_id") == publication_id]
                key = None
            if not gold_rows and not pred_rows:
                continue
            scores[table] = score_table(
                table, gold_rows, pred_rows, tolerance=tolerance, key=key
            )
        by_publication[publication_id] = scores

    return Report(
        gold_dir=Path(gold_dir),
        pred_dir=Path(pred_dir),
        tolerance=tolerance,
        mode="id",
        by_table=by_table,
        by_publication=by_publication,
        by_table_value_mode=by_table_value,
        by_table_cell_mode=by_table_cell,
        gold_total=sum(len(gold.get(t, [])) for t in TABLE_ORDER),
        pred_total=sum(len(pred.get(t, [])) for t in TABLE_ORDER),
    )


# --- вывод -----------------------------------------------------------------


def _pct(value: float) -> str:
    return f"{100 * value:5.1f}%"


def render_text(report: Report, *, per_publication: bool = False) -> str:
    lines: list[str] = []
    lines.append(f"gold: {report.gold_dir}")
    lines.append(f"pred: {report.pred_dir}")
    lines.append(f"строк: эталон {report.gold_total}, предсказание {report.pred_total}")
    lines.append("")
    header = f"{'таблица':15s} {'gold':>6s} {'pred':>6s} {'match':>6s} {'P':>7s} {'R':>7s} {'F1':>7s}"
    lines.append(header)
    lines.append("-" * len(header))
    for table, score in report.by_table.items():
        lines.append(
            f"{table:15s} {score.gold:6d} {score.pred:6d} {score.matched:6d} "
            f"{_pct(score.precision):>7s} {_pct(score.recall):>7s} {_pct(score.f1):>7s}"
        )

    value_score = report.by_table_value_mode.get("results")
    if value_score is not None:
        lines.append("")
        lines.append(
            "results (сопоставление по значению, без внутренних id): "
            f"P {_pct(value_score.precision).strip()}, "
            f"R {_pct(value_score.recall).strip()}, "
            f"F1 {_pct(value_score.f1).strip()}"
        )

    cell_score = report.by_table_cell_mode.get("results")
    if cell_score is not None:
        lines.append(
            "results (оцифровка ячеек: страница + метрика + значение): "
            f"P {_pct(cell_score.precision).strip()}, "
            f"R {_pct(cell_score.recall).strip()}, "
            f"F1 {_pct(cell_score.f1).strip()}"
        )

    for table, score in report.by_table.items():
        if not score.fields:
            continue
        lines.append("")
        lines.append(f"[{table}] точность полей на сопоставленных строках")
        for item in score.fields:
            lines.append(
                f"  {item.field:22s} {_pct(item.accuracy):>7s}"
                f"   ложных заполнений: {item.false_fill}"
            )
        if score.missing_examples:
            lines.append("  не найдено (примеры):")
            for example in score.missing_examples:
                lines.append(f"    - {example}")
        if score.extra_examples:
            lines.append("  лишнее (примеры):")
            for example in score.extra_examples:
                lines.append(f"    - {example}")

    if per_publication:
        lines.append("")
        lines.append("по публикациям (results: P / R / F1)")
        for publication_id, tables in report.by_publication.items():
            score = tables.get("results")
            if score is None:
                continue
            lines.append(
                f"  {publication_id:8s} gold {score.gold:6d} pred {score.pred:6d} "
                f"{_pct(score.precision):>7s} {_pct(score.recall):>7s} {_pct(score.f1):>7s}"
            )

    return "\n".join(lines)


def report_to_dict(report: Report) -> dict:
    def table_dict(score: TableScore) -> dict:
        return {
            "gold": score.gold,
            "pred": score.pred,
            "matched": score.matched,
            "precision": round(score.precision, 4),
            "recall": round(score.recall, 4),
            "f1": round(score.f1, 4),
            "missing": score.missing,
            "extra": score.extra,
            "fields": [
                {
                    "field": item.field,
                    "compared": item.compared,
                    "exact": item.exact,
                    "accuracy": round(item.accuracy, 4),
                    "false_fill": item.false_fill,
                }
                for item in score.fields
            ],
        }

    return {
        "gold_dir": str(report.gold_dir),
        "pred_dir": str(report.pred_dir),
        "tolerance": report.tolerance,
        "mode": report.mode,
        "gold_total": report.gold_total,
        "pred_total": report.pred_total,
        "tables": {name: table_dict(score) for name, score in report.by_table.items()},
        "results_value_mode": (
            table_dict(report.by_table_value_mode["results"])
            if "results" in report.by_table_value_mode
            else None
        ),
        "publications": {
            publication_id: {name: table_dict(score) for name, score in tables.items()}
            for publication_id, tables in report.by_publication.items()
        },
    }
