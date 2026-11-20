"""Проверки согласованности извлечённых результатов.

Каждая проверка — детерминированное правило над уже извлечёнными строками.
Замечание не «исправляет» данные: оно ссылается на конкретные ``result_id``
и оставляет решение человеку.
"""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from .extract import Builder

#: Метрики, которые по определению лежат в 0..1 (доля) или 0..100 (%).
_BOUNDED = {
    "em",
    "f1",
    "accuracy",
    "precision",
    "recall",
    "ndcg",
    "mrr",
    "hit rate",
    "auc",
    "rouge-l",
    "rouge-1",
    "rouge-2",
    "bleu",
    "faithfulness",
    "answer relevance",
    "context precision",
    "context recall",
}

#: Метрики, которые не должны убывать с ростом k.
_NON_DECREASING = {"recall", "hit rate", "ndcg"}


def _base_metric(name: str) -> str:
    return name.split("@", 1)[0].strip().casefold()


def add_issues(builder: "Builder") -> None:
    rows = builder.extraction.tables["results"]
    if not rows:
        builder.issue(
            check_name="no_structured_results_extracted",
            description=(
                "Экстрактор не нашёл числовых результатов. Публикация требует "
                "отдельного разбора; пустые значения не заполнены."
            ),
            source_page="",
            source_fragment="",
            record_ids="",
        )
        return

    # 1. Значение вне объявленной шкалы.
    out_of_scale: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        name = _base_metric(row.get("metric_name", ""))
        unit = row.get("metric_unit", "")
        if name not in _BOUNDED or unit not in {"fraction", "%"}:
            continue
        try:
            value = float(row.get("metric_value", ""))
        except ValueError:
            continue
        limit = 1.0 if unit == "fraction" else 100.0
        if value < 0 or value > limit:
            out_of_scale[(row.get("metric_name", ""), unit)].append(row)
    for (name, unit), group in out_of_scale.items():
        builder.issue(
            check_name="value_out_of_declared_scale",
            description=(
                f"{name} объявлена в шкале {unit}, но значение выходит за границы "
                f"0..{1 if unit == 'fraction' else 100}. Значение сохранено как напечатано; "
                "нужна ручная проверка единицы."
            ),
            source_page=group[0].get("source_page", ""),
            source_fragment=group[0].get("source_fragment", ""),
            record_ids=";".join(row["result_id"] for row in group[:20]),
        )

    # 2. Одна и та же метрика одной конфигурации имеет разные значения.
    by_key: dict[tuple[str, str, str, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        key = (
            row.get("config_id", ""),
            row.get("dataset_id", ""),
            row.get("metric_name", ""),
            row.get("metric_k", ""),
        )
        by_key[key].append(row)
    for group in by_key.values():
        values = {row.get("metric_value", "") for row in group}
        if len(values) > 1:
            builder.issue(
                check_name="conflicting_values_for_same_measurement",
                description=(
                    "Одна и та же метрика одной конфигурации и набора встречается "
                    "с разными значениями. Возможны разные эксперименты или разные "
                    "единицы; строки сохранены без усреднения."
                ),
                source_page=group[0].get("source_page", ""),
                source_fragment=group[0].get("source_fragment", ""),
                record_ids=";".join(row["result_id"] for row in group[:20]),
            )

    # 3. Recall@k / Hit@k / NDCG@k убывает с ростом k.
    curves: dict[tuple[str, str, str], list[tuple[int, float, dict[str, str]]]] = defaultdict(list)
    for row in rows:
        name = _base_metric(row.get("metric_name", ""))
        k = row.get("metric_k", "")
        if name not in _NON_DECREASING or not k.isdigit():
            continue
        try:
            value = float(row.get("metric_value", ""))
        except ValueError:
            continue
        curves[(row.get("config_id", ""), row.get("dataset_id", ""), name)].append(
            (int(k), value, row)
        )
    for (_, _, name), points in curves.items():
        points.sort(key=lambda item: item[0])
        for index in range(1, len(points)):
            k1, v1, row1 = points[index - 1]
            k2, v2, row2 = points[index]
            if k2 > k1 and v2 < v1:
                builder.issue(
                    check_name="metric_decreases_with_k",
                    description=(
                        f"{name} убывает при росте k ({k1}→{k2}: {v1}→{v2}). "
                        "Для этой метрики ожидается неубывание; проверьте колонку или единицу."
                    ),
                    source_page=row1.get("source_page", ""),
                    source_fragment=row1.get("source_fragment", ""),
                    record_ids=f"{row1['result_id']};{row2['result_id']}",
                )
                break

    # 4. Набор не идентифицирован.
    unknown = [
        row
        for row in rows
        if row.get("dataset_id")
        and _dataset_name(builder, row["dataset_id"]) in {"", "Unspecified"}
    ]
    if unknown:
        builder.issue(
            check_name="dataset_not_identified",
            description=(
                "В таблице не удалось надёжно определить набор данных. "
                "Строки сохранены с меткой Unspecified; требуется ручная проверка."
            ),
            source_page=unknown[0].get("source_page", ""),
            source_fragment=unknown[0].get("source_fragment", ""),
            record_ids=";".join(row["result_id"] for row in unknown[:20]),
        )


def _dataset_name(builder: "Builder", dataset_id: str) -> str:
    for row in builder.extraction.tables["datasets"]:
        if row.get("dataset_id") == dataset_id:
            return row.get("dataset_name", "")
    return ""
