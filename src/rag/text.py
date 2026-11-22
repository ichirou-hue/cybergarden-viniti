"""Нормализация текста и сравнение чисел.

Правила намеренно повторяют ``tools/check_submission.py`` организаторов:
типографика схлопывается, переносы слов склеиваются, а число считается
совпавшим, если оно равно либо является более грубым округлением другого.
"""

from __future__ import annotations

import re
import unicodedata
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

_WS = re.compile(r"\s+")
_HYPHEN_BREAK = re.compile(r"(?<=\w)-\s*\r?\n\s*(?=\w)")
_NUMBER = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")


def norm(text: str) -> str:
    """Нормализовать текст так же, как официальный чекер."""
    text = unicodedata.normalize("NFKC", text or "").replace("\u00ad", "")
    text = _HYPHEN_BREAK.sub("", text)
    return _WS.sub(" ", text).strip()


def fold(text: str) -> str:
    """Ключ для нестрогого сравнения строк."""
    return norm(text).casefold()


def is_blank(value: object) -> bool:
    return value is None or str(value).strip() == ""


def to_decimal(value: object) -> Decimal | None:
    """Разобрать число. Пустая строка и мусор дают ``None``."""
    if is_blank(value):
        return None
    raw = str(value).strip().replace(",", ".")
    if not _NUMBER.match(raw):
        return None
    try:
        number = Decimal(raw)
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def same_at_reported_precision(left: object, right: object) -> bool:
    """Совпадают ли числа с учётом явно заявленной точности.

    ``0.2`` и ``0.20`` совпадают; ``0.2`` и ``0.21`` — нет.
    Ни одно из значений не переписывается.
    """
    x, y = to_decimal(left), to_decimal(right)
    if x is None or y is None:
        return False
    if x == y:
        return True
    ex = x.as_tuple().exponent
    ey = y.as_tuple().exponent
    if ex == ey:
        return False
    coarse, fine = (x, y) if ex > ey else (y, x)
    quant = Decimal(1).scaleb(coarse.as_tuple().exponent)
    try:
        return fine.quantize(quant, rounding=ROUND_HALF_UP) == coarse
    except InvalidOperation:
        return False


def numbers_equal(left: object, right: object, tolerance: float = 0.0) -> bool:
    """Сравнить числа: точное совпадение, округление или допуск."""
    if same_at_reported_precision(left, right):
        return True
    if tolerance <= 0:
        return False
    x, y = to_decimal(left), to_decimal(right)
    if x is None or y is None:
        return False
    return abs(x - y) <= Decimal(str(tolerance))


def values_equal(left: object, right: object, tolerance: float = 0.0) -> bool:
    """Сравнить значения: числа — численно, остальное — без регистра."""
    lx, ly = to_decimal(left), to_decimal(right)
    if lx is not None and ly is not None:
        return numbers_equal(left, right, tolerance)
    return fold(str(left or "")) == fold(str(right or ""))


#: Синонимы, которые встречаются в статьях про RAG.
_METRIC_ALIASES: dict[str, str] = {
    "em": "em",
    "exact match": "em",
    "exact-match": "em",
    "f1": "f1",
    "f1 score": "f1",
    "f1-score": "f1",
    "accuracy": "accuracy",
    "acc": "accuracy",
    "precision": "precision",
    "recall": "recall",
    "mrr": "mrr",
    "ndcg": "ndcg",
    "hit rate": "hit_rate",
    "hit@k": "hit_rate",
    "bleu": "bleu",
    "rouge": "rouge",
    "rouge-l": "rouge_l",
    "faithfulness": "faithfulness",
    "answer correctness": "answer_correctness",
    "context precision": "context_precision",
    "context recall": "context_recall",
}


def canonical_metric(name: str) -> str:
    """Каноническое имя метрики для сопоставления, без потери исходного."""
    key = fold(name).replace("_", " ").strip()
    if key in _METRIC_ALIASES:
        return _METRIC_ALIASES[key]
    return key.replace(" ", "_")
