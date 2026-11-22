"""Validate model candidates before exporting the solar template.

Literal source verification is not a scientific correctness guarantee.
This independent contract is provisional until agreed with the backend owner.
"""

from collections import defaultdict
from copy import deepcopy
import csv
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory
import unicodedata

from pdfplumber.utils.exceptions import PdfminerException

from .atomic_output import publish_directory
from .pdf_source import extract
from .solar_schema import KEYS, TABLES, UNITS, VOCAB


MAX_BYTES = 40 * 1024 * 1024
REVISION = "solar-2"
NUMBER = re.compile(r"[+-]?(?:\d+(?:[.,]\d+)?|[.,]\d+)(?:[eE][+-]?\d+)?")


def clean(text):
    return " ".join(unicodedata.normalize("NFKC", text).replace("−", "-").split())


def decimal(text):
    text = clean(text)
    if not NUMBER.fullmatch(text):
        raise ValueError(f"Ожидалось одно конечное число: {text[:60]!r}")
    try:
        value = Decimal(text.replace(",", "."))
        if not value.is_finite() or abs(value.adjusted()) > 100:
            raise ValueError("Число вне поддерживаемого диапазона.")
        return value
    except InvalidOperation as error:
        raise ValueError("Некорректное число.") from error


def number_text(value):
    return format(value.normalize(), "f")


def unit_name(unit):
    return clean(unit).replace("²", "2").replace("^2", "2").replace(" ", "").replace("Ω", "Ohm")


THOUSANDS = re.compile(r"(?<![\d.,])([1-9]\d{0,2}(?:,\d{3})+(?:\.\d+)?)(?![\d.,])")


def plain_number(text):
    """Drop a thousands separator only; a decimal comma is left untouched.

    ``1,197`` is one thousand one hundred ninety-seven and the page prints it
    that way, so a literal check has to read both spellings as the same token.
    ``0,71`` is a decimal comma and ``0,710`` may be one too, so a group that
    starts with a zero is never treated as thousands.
    """
    return THOUSANDS.sub(lambda match: match.group(1).replace(",", ""), clean(text))


DECIMAL_COMMA = re.compile(r"(?<=\d),(?=\d{1,2}(?!\d))")


def dotted(text):
    """A decimal comma is the same number as a decimal point."""
    return DECIMAL_COMMA.sub(".", text)


def has_value(raw, quote, *, spread=""):
    """Match a complete original token, including context outside a short quote."""
    raw, quote = dotted(plain_number(raw)), dotted(plain_number(quote))
    if not raw:
        return False
    scalar = bool(NUMBER.fullmatch(raw))
    tokens = {(match.start(), match.end()) for match in NUMBER.finditer(quote)} if scalar else set()
    for match in re.finditer(re.escape(raw), quote):
        before, after = quote[:match.start()], quote[match.end():]
        if before and (re.search(r"[\w.,]$", before) or re.search(r"[+\-±–—<>≤≥]\s*$", before)):
            continue
        if scalar and (match.start(), match.end()) not in tokens:
            continue
        if not scalar and after and re.match(r"^[\w.,]", after):
            continue
        if scalar:
            if re.match(r"^\s*(?:±|\+/-|[–—−-]|\bto\b|[<>≤≥])", after):
                if not spread or not re.match(r"^\s*(?:±|\+/-)\s*" + re.escape(clean(spread)) + r"(?![\w.,])", after):
                    continue
            if re.search(r"(?:±|\+/-|[–—−-]|\bto)\s*$", before):
                continue
        return True
    return False


def unit_pattern(unit):
    """Regex for a printed unit, tolerant to how papers typeset it.

    ``mA/cm2``, ``mA cm-2`` and ``mA cm−2`` are the same unit; a slash may be
    printed as a space, and an exponent may carry a minus sign. Spaces and the
    superscript marker are already gone after :func:`unit_name`.
    """
    normalized = unit_name(unit)
    parts = []
    for index, char in enumerate(normalized):
        if char == "/":
            parts.append(r"[/\s]*")
        elif char == "*":
            parts.append(r"[\*\u00b7\s]*")
        elif char == "2" and index and (normalized[index - 1].isalpha() or normalized[index - 1] == ")"):
            parts.append(r"\s*[-\u2212]?\s*2")
        else:
            parts.append(re.escape(char) + r"\s*")
    return r"(?<![A-Za-z\u00b5\u03bc])" + "".join(parts) + r"(?![A-Za-z0-9\u00b5\u03bc])"


def has_unit(unit, anchors):
    if not unit:
        return False
    pattern = unit_pattern(unit)
    # Keep the spaces: removing them would glue "%" to the next word and the
    # boundary check would then reject a perfectly valid quote.
    return any(re.search(pattern, clean(source["quote"]).replace("²", "2").replace("^2", "2").replace("Ω", "Ohm"))
               for source in anchors)


def verify_entities(tables, evidence):
    for table, field, unit_field in (
        ("materials", "bandgap_value", "bandgap_unit"),
        ("devices", "active_area", "area_unit"),
        ("layers", "layer_thickness", "thickness_unit"),
        ("measurements", "light_intensity", "intensity_unit"),
    ):
        for row in tables[table]:
            if not row[field]:
                continue
            anchors = evidence[f"{table}/{row[KEYS[table]]}"]
            if not any(has_value(row[field], source["quote"]) for source in anchors):
                raise ValueError(f"{table}.{field}: число не подтверждено источником.")
            if not has_unit(row[unit_field], anchors):
                raise ValueError(f"{table}.{field}: единица не подтверждена источником.")


# Raised text for a PDF without a native text layer. ``solar_api`` uses the
# constant, not a copy of the sentence, so bumping REVISION cannot silently
# break the catalogue fallback for image-only uploads.
NO_TEXT_LAYER = "Нет текстового слоя. OCR в solar-2 пока не реализован."


def prepare_pdf(name, data):
    if len(data) > MAX_BYTES:
        raise ValueError("Нужен PDF размером до 40 МиБ.")
    if not data.startswith(b"%PDF-"):
        raise ValueError("Файл не является PDF.")
    publication_id = Path(name.replace("\\", "/")).stem
    if not publication_id or len(publication_id) > 240:
        raise ValueError("Некорректное имя публикации.")
    try:
        document = extract(data)
    except PdfminerException:
        raise ValueError("PDF повреждён, зашифрован или не может быть разобран.") from None
    if not document["sources"]:
        raise ValueError(NO_TEXT_LAYER)
    return {
        **document, "publication_id": publication_id, "sha256": sha256(data).hexdigest(),
        "track": "solar", "schema_version": REVISION,
    }


def same_number(left, right):
    """Two printed spellings of one value: ``23`` and ``23.0`` are the same.

    A duplicate may be restated with different precision, so the comparison is
    numeric when both sides parse and literal otherwise.
    """
    try:
        return decimal(left) == decimal(right)
    except ValueError:
        return clean(left) == clean(right)


def validate_candidates(candidate, document):
    if not isinstance(candidate, dict) or set(candidate) != set(TABLES):
        raise ValueError("Ответ должен содержать ровно семь таблиц solar.")
    tables, evidence = {}, {}
    sources = {source["id"]: source for source in document["sources"]}
    pid = document["publication_id"]
    for table, columns in TABLES.items():
        rows = candidate[table]
        if not isinstance(rows, list) or len(rows) > 20000:
            raise ValueError(f"{table}: ожидался ограниченный список записей.")
        tables[table] = []
        seen = set()
        for original in rows:
            if not isinstance(original, dict) or set(original) - set(columns) - {"source_ids"}:
                raise ValueError(f"{table}: неизвестные поля.")
            if any(not isinstance(value, str) for key, value in original.items() if key != "source_ids"):
                raise ValueError(f"{table}: CSV-поля должны быть строками, пропуски — пустыми строками.")
            row = {column: original.get(column, "").strip() for column in columns}
            if row["publication_id"] != pid:
                raise ValueError("publication_id не совпадает с именем PDF.")
            key = row[KEYS[table]]
            if not key or key in seen:
                raise ValueError(f"{table}: пустой или повторяющийся идентификатор.")
            seen.add(key)
            for (vtable, field), allowed in VOCAB.items():
                if table == vtable and row[field] and row[field] not in allowed:
                    raise ValueError(f"{table}.{field}: значение вне словаря.")
            source_ids = original.get("source_ids", [])
            if not isinstance(source_ids, list) or any(type(i) is not int or i not in sources for i in source_ids):
                raise ValueError(f"{table}/{key}: неизвестный source_id.")
            if table not in {"publications", "issues"} and not source_ids:
                raise ValueError(f"{table}/{key}: нужны source_ids подтверждающих фрагментов.")
            anchors = [sources[i] for i in dict.fromkeys(source_ids)]
            if table in {"results", "issues"}:
                page, fragment = row["source_page"], clean(row["source_fragment"])
                if not page.isascii() or not page.isdigit() or not 1 <= int(page) <= document["pages"]:
                    raise ValueError(f"{table}/{key}: неверная страница PDF.")
                matching = [
                    source for source in anchors
                    if source["page"] == int(page) and fragment in clean(source["quote"])
                ]
                if not fragment or not matching:
                    raise ValueError(f"{table}/{key}: цитата не найдена в указанных источниках на странице.")
                if table == "results":
                    if not row["extraction_status"] or not row["result_origin"] or not row["metric_name"]:
                        raise ValueError(f"results/{key}: нужны статус, происхождение и показатель.")
                    if row["extraction_status"] == "extracted":
                        raw = clean(row["raw_value"])
                        # Avoid accepting 12 just because the source contains 112.
                        if not has_value(raw, fragment, spread=row["spread_value"]) or not any(
                            has_value(raw, source["quote"], spread=row["spread_value"]) for source in matching
                        ):
                            raise ValueError(f"results/{key}: исходное значение отсутствует в цитате.")
                    if row["time_point"] and (
                        not any(has_value(row["time_point"], source["quote"]) for source in anchors)
                        or not has_unit(row["time_unit"], anchors)
                    ):
                        raise ValueError(f"results/{key}: time_point/единица не подтверждены источником.")
            evidence[f"{table}/{key}"] = anchors
            tables[table].append(row)
    if len(tables["publications"]) != 1:
        raise ValueError("На документ нужна ровно одна публикация.")
    publication = tables["publications"][0]
    if publication["in_scope"] not in {"true", "false"}:
        raise ValueError("Нужно явное решение in_scope.")
    if publication["in_scope"] == "false":
        if not publication["exclusion_reason"] or any(tables[t] for t in TABLES if t != "publications"):
            raise ValueError("Исключённая статья требует причину и пустые предметные таблицы.")
    elif publication["exclusion_reason"]:
        raise ValueError("У включённой статьи exclusion_reason должен быть пуст.")
    # Model-generated issues are not trusted; semantic checks run deterministically.
    if tables["issues"]:
        raise ValueError("Модель должна оставить issues пустой: проверки выполняются программно.")
    check_links(tables)
    verify_entities(tables, evidence)
    return tables, evidence


def check_links(tables):
    indexes = {table: {r[KEYS[table]]: r for r in rows} for table, rows in tables.items()}
    references = {
        "devices": {"absorber_material_id": "materials"},
        "layers": {"device_id": "devices"},
        "measurements": {"device_id": "devices"},
        "results": {"device_id": "devices", "measurement_id": "measurements",
                    "material_id": "materials", "duplicate_of": "results"},
    }
    for table, fields in references.items():
        for row in tables[table]:
            for field, target in fields.items():
                values = row[field].split(";") if table == "measurements" else [row[field]]
                if any(value and value.strip() not in indexes[target] for value in values):
                    raise ValueError(f"{table}.{field}: ссылка на отсутствующую запись.")
    for row in tables["results"]:
        if row["result_origin"] == "cited":
            if row["device_id"] or row["measurement_id"] or not row["cited_publication_id"]:
                raise ValueError("Цитируемый результат требует ссылку на работу, без собственного прибора/измерения.")
        if row["measurement_id"] and row["device_id"]:
            measurement = indexes["measurements"][row["measurement_id"]]
            if measurement["device_id"] and row["device_id"] not in {v.strip() for v in measurement["device_id"].split(";")}:
                raise ValueError("Прибор результата не совпадает с прибором измерения.")
        # A named result material must be the absorber of the referenced device.
        # An empty side is unknown, not a licence to fill it from the other one;
        # the reference existence check above already handled a missing device.
        if row["device_id"] and row["material_id"]:
            absorber = indexes["devices"][row["device_id"]]["absorber_material_id"]
            if absorber and absorber != row["material_id"]:
                raise ValueError("results.material_id: материал результата не совпадает с поглотителем прибора.")
        visited, current = set(), row
        while current["duplicate_of"]:
            if current["result_id"] in visited:
                raise ValueError("Цикл duplicate_of.")
            visited.add(current["result_id"])
            current = indexes["results"][current["duplicate_of"]]


def issue(tables, records, rule, description):
    first = records[0]
    tables["issues"].append({
        "publication_id": first["publication_id"], "issue_id": f"I{len(tables['issues']) + 1}",
        "record_ids": ";".join(row["result_id"] for row in records), "check_name": rule,
        "description": description, "source_page": first["source_page"],
        "source_fragment": first["source_fragment"],
    })


def normalize(tables, evidence=None):
    for row in tables["results"]:
        # Never accept model-computed normalized values without reproducing them.
        for field in ("metric_value", "metric_unit", "normalized_value", "normalized_unit"):
            row[field] = ""
        original_spread = row["spread_value"]
        row["spread_value"] = ""
        if original_spread:
            row["note"] = (row["note"] + f" | Original spread: {original_spread} {row['raw_unit']}").strip(" |")
        if row["extraction_status"] != "extracted":
            continue
        target, factors = UNITS.get(row["metric_name"], ("", {}))
        raw_unit = unit_name(row["raw_unit"])
        # A fraction is a scale, not a printed unit: pages write "0.71" and never
        # the word "fraction". A dimensionless metric likewise has no unit to
        # find. Only the conversion table may accept these.
        scale = raw_unit == "fraction" or (raw_unit == "" and "" in factors)
        if evidence is not None and not scale and not has_unit(row["raw_unit"], evidence[f"results/{row['result_id']}"]):
            issue(tables, [row], "normalization", "Единица/шкала не подтверждена источниками: пересчёт не выполнялся.")
            continue
        try:
            value = decimal(row["raw_value"])
        except ValueError:
            issue(tables, [row], "normalization", "Не одно конечное число: сохранена исходная запись.")
            continue
        factor = factors.get(raw_unit)
        if factor is None:
            issue(tables, [row], "normalization", "Неизвестная единица/шкала: пересчёт не выполнялся.")
            continue
        normalized = number_text(value * Decimal(factor))
        row.update(metric_value=normalized, metric_unit=target,
                   normalized_value=normalized, normalized_unit=target)
        if original_spread:
            anchors = evidence[f"results/{row['result_id']}"] if evidence else []
            ending = r"(?![\w.,])" if unit_name(row["raw_unit"]) == "%" else r"(?![\w.,]|\s*%)"
            confirmed = any(
                re.search(r"(?:±|\+/-)\s*" + re.escape(clean(original_spread)) + ending, clean(a["quote"]))
                and not re.search(r"\brelative\b|относительн", clean(a["quote"]), re.I)
                and (unit_name(row["raw_unit"]) != "%" or re.search(
                    re.escape(clean(row["raw_value"])) + r"\s*(?:±|\+/-)\s*"
                    + re.escape(clean(original_spread)) + r"\s*%", clean(a["quote"]),
                ))
                for a in anchors
            )
            if evidence is not None and not confirmed:
                row["spread_value"] = ""
                issue(tables, [row], "normalization", "Абсолютный разброс не подтверждён рядом с ±; оставлен в note.")
            else:
                try:
                    row["spread_value"] = number_text(decimal(original_spread) * Decimal(factor))
                except ValueError:
                    issue(tables, [row], "normalization", "Разброс не является одним конечным числом; оставлен в note.")
    # Entity fields have only one value/unit pair in the template.
    conversions = (
        ("layers", "layer_thickness", "thickness_unit", "nm", {"nm": "1", "μm": "1000", "µm": "1000", "um": "1000"}),
        ("devices", "active_area", "area_unit", "cm2", {"cm2": "1", "mm2": ".01", "m2": "10000"}),
        ("materials", "bandgap_value", "bandgap_unit", "eV", {"eV": "1", "meV": ".001"}),
    )
    for table, field, unit_field, target, factors in conversions:
        for row in tables[table]:
            if not row[field]:
                continue
            value = decimal(row[field])
            factor = factors.get(unit_name(row[unit_field]))
            if factor is None:
                raise ValueError(f"{table}.{field}: неподдерживаемая единица; нельзя выгрузить как {target}.")
            raw = f"{row[field]} {row[unit_field]}"
            row[field], row[unit_field] = number_text(value * Decimal(factor)), target
            row["note"] = (row["note"] + f" | Original {field}: {raw}").strip(" |")
    for row in tables["results"]:
        if row["time_point"]:
            value = decimal(row["time_point"])
            factors = UNITS["T80"][1]
            factor = factors.get(unit_name(row["time_unit"]))
            if factor is None:
                raise ValueError("Неизвестная единица time_point.")
            row["note"] = (row["note"] + f" | Original time: {row['time_point']} {row['time_unit']}").strip(" |")
            row["time_point"], row["time_unit"] = number_text(value * Decimal(factor)), "h"


def check_consistency(tables):
    groups = defaultdict(list)
    materials = {row["material_id"]: row for row in tables["materials"]}
    measurements = {row["measurement_id"]: row for row in tables["measurements"]}
    for row in tables["results"]:
        if not row["normalized_value"]:
            continue
        value = decimal(row["normalized_value"])
        metric = row["metric_name"]
        if value < 0 or (row["normalized_unit"] == "%" and value > 100):
            issue(tables, [row], "range", f"{metric} = {value} {row['normalized_unit']}: вне допустимого диапазона.")
        if row["result_origin"] == "primary" and row["device_id"] and row["measurement_id"] and not row["duplicate_of"]:
            key = tuple(row[field] for field in (
                "device_id", "measurement_id", "aggregation", "time_point", "time_unit",
            ))
            groups[key].append(row)
        material = materials.get(row["material_id"])
        if metric == "Voc" and material and material["bandgap_value"] and material["bandgap_unit"] == "eV":
            bandgap = decimal(material["bandgap_value"])
            if value >= bandgap:
                issue(tables, [row], "voc_limit", f"Voc = {value} V >= Eg/q = {bandgap} V.")
    for records in groups.values():
        metrics = defaultdict(list)
        for row in records:
            metrics[row["metric_name"]].append(row)
        # Ambiguous repeated measurements must not form arbitrary physical tuples.
        def unique(name):
            rows = metrics[name]
            return rows[0] if len(rows) == 1 else None
        pce, voc, jsc, ff = [unique(name) for name in ("PCE", "Voc", "Jsc", "FF")]
        if all((pce, voc, jsc, ff)):
            measurement = measurements[pce["measurement_id"]]
            if measurement["light_intensity"] and unit_name(measurement["intensity_unit"]) == "mW/cm2":
                pin = decimal(measurement["light_intensity"])
                if pin > 0:
                    expected = decimal(voc["normalized_value"]) * decimal(jsc["normalized_value"]) * decimal(ff["normalized_value"]) / pin
                    actual = decimal(pce["normalized_value"])
                    if expected > 0 and abs(actual - expected) / expected > Decimal(".03"):
                        issue(tables, [pce, voc, jsc, ff], "jv_consistency",
                              f"PCE = {actual} %, расчёт = {number_text(expected)} %; расхождение > 3% от расчёта.")
        for metric, rows in metrics.items():
            if len({r["normalized_value"] for r in rows}) > 1:
                issue(tables, rows, "contradiction",
                      f"{metric}: разные значения при одинаковых явных идентификаторах и условиях: "
                      + "; ".join(r["normalized_value"] for r in rows))
        eqe = unique("Jsc_EQE")
        jv = unique("Jsc")
        if eqe and jv and decimal(jv["normalized_value"]) > 0:
            first, second = decimal(jv["normalized_value"]), decimal(eqe["normalized_value"])
            if abs(first - second) / first > Decimal(".05"):
                issue(tables, [jv, eqe], "eqe_consistency",
                      f"Jsc = {first}, Jsc_EQE = {second} mA/cm2: расхождение > 5% от Jsc.")
    results = {row["result_id"]: row for row in tables["results"]}
    for row in tables["results"]:
        if row["duplicate_of"]:
            original = results[row["duplicate_of"]]
            fields = ("device_id", "measurement_id", "material_id", "metric_name", "aggregation",
                      "time_point", "time_unit", "result_origin", "cited_publication_id", "extraction_status")
            if any(row[field] != original[field] for field in fields):
                raise ValueError("duplicate_of объединяет разные результаты/условия.")
            if row["normalized_value"] and original["normalized_value"]:
                equal = same_number(row["normalized_value"], original["normalized_value"]) \
                    and row["normalized_unit"] == original["normalized_unit"]
            else:
                equal = same_number(row["raw_value"], original["raw_value"]) \
                    and clean(row["raw_unit"]) == clean(original["raw_unit"])
            if not equal:
                raise ValueError("duplicate_of объединяет разные значения.")


# Fields that bind a row to process conditions or to the size of the sample.
# The inventory below only reports that such claims exist; it never decides
# whether they are true and never suggests demoting, blanking or rewriting a
# value. A row is a reference (table, key, field names), never a cell value.
ATTRIBUTION_FIELDS = {
    "layers": ("annealing_temperature", "annealing_time", "atmosphere", "deposition_method"),
    "results": ("n_devices", "spread_value", "aggregation"),
}
ATTRIBUTION_ROW_LIMIT = 500
ATTRIBUTION_NOTE = (
    "Инвентарь полей, привязывающих строку к условиям процесса или размеру выборки. "
    "Он не проверяет научную принадлежность и не доказывает корректность строк: "
    "state=none значит только отсутствие таких полей, а не проверенную науку. "
    "Все строки модели остаются неподтверждёнными."
)


def attribution_review(tables):
    """A bounded inventory of process and sample-size claims, with no verdict.

    This is metadata, not an oracle: it never demotes, blanks or rewrites a
    value and never asserts that a row is scientifically attributed. Every model
    row stays unverified, so ``requires_review`` is always true. ``state`` is
    ``unverified`` only when at least one sensitive field is nonempty, and
    ``none`` describes this inventory alone. Row references carry the table, the
    row key and the nonempty field names, never a cell value or document text.
    ``counts`` covers the whole input even when ``rows`` is capped, and
    ``truncated`` says whether that cap was hit.

    ``finalize`` calls this right after ``validate_candidates`` and before
    ``normalize``, because normalization moves ``spread_value`` into ``note``
    and would otherwise erase a claim the model actually made.
    """
    counts = {
        "rows": 0, "fields": 0,
        "by_table": {table: {"rows": 0, "fields": 0} for table in ATTRIBUTION_FIELDS},
        "by_field": {
            f"{table}.{field}": 0
            for table, fields in ATTRIBUTION_FIELDS.items() for field in fields
        },
    }
    rows = []
    for table, fields in ATTRIBUTION_FIELDS.items():
        for row in tables.get(table, []):
            if not isinstance(row, dict):
                continue
            present = [
                field for field in fields
                if isinstance(row.get(field), str) and row[field].strip()
            ]
            if not present:
                continue
            counts["rows"] += 1
            counts["fields"] += len(present)
            counts["by_table"][table]["rows"] += 1
            counts["by_table"][table]["fields"] += len(present)
            for field in present:
                counts["by_field"][f"{table}.{field}"] += 1
            if len(rows) < ATTRIBUTION_ROW_LIMIT:
                key = row.get(KEYS[table], "")
                rows.append({
                    "table": table, "key": key if isinstance(key, str) else "",
                    "fields": present,
                })
    return {
        "state": "unverified" if counts["fields"] else "none",
        "requires_review": True,
        "counts": counts,
        "rows": rows,
        "truncated": counts["rows"] > len(rows),
        "note": ATTRIBUTION_NOTE,
    }


def finalize(candidate, document, *, model=""):
    tables, evidence = validate_candidates(deepcopy(candidate), document)
    # Inventory the model claims before ``normalize`` moves a spread into the
    # note, so a claim the model actually made is not lost from the inventory.
    # This only reads: tables, normalization, issues and values are untouched.
    attribution = attribution_review(tables)
    normalize(tables, evidence)
    check_consistency(tables)
    warnings = [*document["warnings"],
                "Проверено буквальное наличие источников, но не научная корректность связей и полнота.",
                "OCR и оцифровка графиков не реализованы; единицы raw_unit нормализуются по словарю шаблона, остальные требуют ручной проверки."]
    return {
        **document, "tables": tables, "evidence": evidence, "warnings": warnings,
        "extraction": {
            "revision": REVISION, "model": model, "source_verification": "literal",
            "attribution": attribution,
        },
    }


def export_bundle(result, output):
    """Write a fresh directory only after every file has been serialized."""
    output = Path(output)
    if output.exists():
        raise ValueError("Каталог результата уже существует; выберите новый путь.")
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".solar-", dir=output.parent) as temporary:
        folder = Path(temporary) / "bundle"
        folder.mkdir()
        (folder / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2), encoding="utf-8",
        )
        for table, columns in TABLES.items():
            with (folder / f"{table}.csv").open("w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=columns)
                writer.writeheader()
                writer.writerows(result["tables"][table])
        publish_directory(folder, output)
