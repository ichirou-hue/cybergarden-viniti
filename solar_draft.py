"""Advisory-only row quarantine for an untrusted solar model draft.

``validate_draft`` is a diagnostic view of a model answer, not an exporter. It
never writes CSV, never calls ``solar_facts.finalize``, never merges accepted
rows into a database or a bundle, and never mutates the candidate. Every input
row is copied into an envelope with an explicit status and reason; accepted
rows keep the exact model values for human review, including values that a
later normalizer would rewrite.

The checks mirror ``solar_facts.validate_candidates``, ``verify_entities`` and
``check_links`` as a row-level gate: a bad row, and the rows that depend on it,
are quarantined instead of aborting the whole answer. A duplicate identifier
quarantines every copy, never an arbitrary first one. A literal quote only
proves that the printed text exists; it does not prove that a value is
scientifically correct or that a link is meaningful.

The output is deliberately bounded: a global shape failure, a publication gate
failure, an oversized candidate or an oversized draft is reported in
``blocked_by`` instead of producing an unbounded artifact.
"""

from collections import defaultdict
from copy import deepcopy
import json

from solar_facts import clean, has_unit, has_value, same_number
from solar_schema import KEYS, TABLES, VOCAB


SCHEMA_VERSION = "solar-draft-1"
MAX_ROWS_PER_TABLE = 20000
MAX_ARTIFACT_BYTES = 32 * 1024 * 1024
MAX_REASON = 500

SCIENTIFIC_TABLES = ("materials", "devices", "layers", "measurements", "results")
ENTITY_FIELDS = (
    ("materials", "bandgap_value", "bandgap_unit"),
    ("devices", "active_area", "area_unit"),
    ("layers", "layer_thickness", "thickness_unit"),
    ("measurements", "light_intensity", "intensity_unit"),
)
# ``(field, target table, value is a semicolon-separated list)``.
REFERENCES = {
    "devices": (("absorber_material_id", "materials", False),),
    "layers": (("device_id", "devices", False),),
    "measurements": (("device_id", "devices", True),),
    "results": (
        ("device_id", "devices", False),
        ("measurement_id", "measurements", False),
        ("material_id", "materials", False),
        ("duplicate_of", "results", False),
    ),
}
# Fields that ``check_consistency`` compares for ``duplicate_of``.
DUPLICATE_FIELDS = (
    "device_id", "measurement_id", "material_id", "metric_name", "aggregation",
    "time_point", "time_unit", "result_origin", "cited_publication_id",
    "extraction_status",
)


def _short(value, limit=80):
    text = clean(str(value))
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def _reason(text):
    text = clean(str(text))
    if len(text) > MAX_REASON:
        text = text[: MAX_REASON - 3].rstrip() + "..."
    return text


def _raw(row, column):
    """The input string, or ``None`` when the field is not a string."""
    value = row.get(column, "")
    return value if isinstance(value, str) else None


def _value(row, column):
    """The stripped value used by validation; the row itself is untouched."""
    value = _raw(row, column)
    return value.strip() if value is not None else None


def _key(row, table):
    if not isinstance(row, dict):
        return None
    key = _value(row, KEYS[table])
    return key or None


def _envelope(table, index, row, status="accepted", reason=""):
    return {
        "index": index,
        "key": _key(row, table),
        "status": status,
        "reason": _reason(reason) if reason else "",
        "row": deepcopy(row),
    }


def _quarantine(envelope, reason):
    envelope["status"] = "quarantined"
    text = reason if not envelope["reason"] else f"{envelope['reason']}; {reason}"
    envelope["reason"] = _reason(text)


def _counts(tables):
    per_table = {}
    for table in TABLES:
        rows = tables.get(table, [])
        accepted = sum(1 for envelope in rows if envelope["status"] == "accepted")
        per_table[table] = {
            "rows": len(rows),
            "accepted": accepted,
            "quarantined": len(rows) - accepted,
        }
    total = sum(item["rows"] for item in per_table.values())
    accepted = sum(item["accepted"] for item in per_table.values())
    return {
        "rows": total,
        "accepted": accepted,
        "quarantined": total - accepted,
        "tables": per_table,
    }


def _document_pid(document):
    """The publication id for an artifact, or an empty string if unusable.

    The document is untrusted input here, so this helper never raises and never
    echoes a document body. It is used for blocked artifacts too, where the
    rest of the envelope may already be known to be invalid.
    """
    if isinstance(document, dict):
        pid = document.get("publication_id")
        if isinstance(pid, str):
            return pid
    return ""


def _document_problem(document):
    """Return ``(code, reason)`` for an unusable document, or ``None``.

    ``solar_facts`` reads ``publication_id``, ``pages`` and the source ``id``,
    ``page`` and ``quote`` fields directly. A malformed envelope would crash
    the row checks, so it is rejected up front as ``document_invalid``. Reasons
    name the broken shape only and never include document text.
    """
    if not isinstance(document, dict):
        return "document_invalid", "Документ должен быть объектом."
    pid = document.get("publication_id")
    if not isinstance(pid, str) or not pid.strip():
        return "document_invalid", "У документа нет непустого publication_id."
    pages = document.get("pages")
    if type(pages) is not int or pages < 1:
        return "document_invalid", "У документа некорректное число страниц."
    sources = document.get("sources")
    if not isinstance(sources, list):
        return "document_invalid", "У документа нет списка источников."
    seen = set()
    for source in sources:
        if not isinstance(source, dict):
            return "document_invalid", "Источник должен быть объектом."
        source_id = source.get("id")
        if type(source_id) is not int or source_id in seen:
            return ("document_invalid",
                    "Идентификаторы источников должны быть уникальными целыми.")
        seen.add(source_id)
        page = source.get("page")
        if type(page) is not int or not 1 <= page <= pages:
            return "document_invalid", "Источник указывает на недопустимую страницу."
        if not isinstance(source.get("quote"), str):
            return "document_invalid", "Цитата источника должна быть строкой."
    return None


def empty_draft(document, status="missing", code="candidate_absent"):
    """A fixed-shape draft for an absent or unusable model answer.

    ``document`` is only read for ``publication_id``. The function never writes
    anything and never invents rows.
    """
    status = status if isinstance(status, str) and status.strip() else "missing"
    code = code if isinstance(code, str) and code.strip() else "candidate_absent"
    message = "Кандидат отсутствует." if code == "candidate_absent" else code
    tables = {table: [] for table in TABLES}
    return {
        "schema_version": SCHEMA_VERSION,
        "publication_id": _document_pid(document),
        "status": _short(status, 40),
        "publication_state": _short(status, 40),
        "blocked_by": [{"code": _short(code, 80), "reason": _reason(message)}],
        "tables": tables,
        "counts": _counts(tables),
    }


def _candidate_counts(candidate):
    """Counts for a blocked answer without copying or inspecting any row.

    Only list lengths are read, so a cyclic or non-JSON row cannot break the
    counts and the blocked artifact stays bounded.
    """
    per_table = {}
    for table in TABLES:
        values = candidate.get(table, []) if isinstance(candidate, dict) else []
        count = len(values) if isinstance(values, list) else 0
        per_table[table] = {"rows": count, "accepted": 0, "quarantined": count}
    total = sum(item["rows"] for item in per_table.values())
    return {
        "rows": total,
        "accepted": 0,
        "quarantined": total,
        "tables": per_table,
    }


def _blocked(document, reasons, rows=None, counts=None):
    """A bounded ``blocked`` artifact; rows are dropped if they do not fit."""
    tables = {table: [] for table in TABLES}
    if rows:
        for table, envelopes in rows.items():
            tables[table] = envelopes
    draft = {
        "schema_version": SCHEMA_VERSION,
        "publication_id": _document_pid(document),
        "status": "blocked",
        "publication_state": "blocked",
        "blocked_by": [
            {"code": _short(code, 80), "reason": _reason(reason)}
            for code, reason in reasons
        ],
        "tables": tables,
        "counts": counts if counts is not None else _counts(tables),
    }
    if not any(tables.values()):
        return draft
    try:
        size = len(json.dumps(
            draft, ensure_ascii=False, allow_nan=False, separators=(",", ":"),
        ).encode("utf-8"))
    except (TypeError, ValueError, UnicodeError):
        size = None
    if size is not None and size <= MAX_ARTIFACT_BYTES:
        return draft
    # Counts stay, rows are omitted: a blocked artifact must remain serializable
    # and bounded even when the untrusted input is not.
    return {
        **draft,
        "tables": {table: [] for table in TABLES},
        "blocked_by": draft["blocked_by"] + [{
            "code": "rows_omitted",
            "reason": _reason("Строки не включены: заблокированный черновик превышает "
                              "допустимый размер."),
        }],
    }


def _all_rows(candidate, reason):
    """Every row of a blocked answer, copied and quarantined for diagnosis."""
    rows = {}
    for table in TABLES:
        values = candidate.get(table, []) if isinstance(candidate, dict) else []
        if not isinstance(values, list):
            values = []
        rows[table] = [
            _envelope(table, index, row, "quarantined", reason)
            for index, row in enumerate(values)
        ]
    return rows


def _shape_reasons(candidate):
    if not isinstance(candidate, dict):
        return [("candidate_not_object",
                 "Кандидат должен быть объектом с семью таблицами solar.")]
    reasons = []
    missing = sorted(set(TABLES) - set(candidate))
    extra = sorted(set(candidate) - set(TABLES), key=str)
    if missing or extra:
        parts = []
        if missing:
            parts.append("нет таблиц: " + ", ".join(missing))
        if extra:
            parts.append("лишние ключи: " + ", ".join(_short(key, 40) for key in extra))
        reasons.append(("table_set_mismatch",
                        "Набор таблиц не совпадает с шаблоном solar: " + "; ".join(parts)))
    for table in TABLES:
        if table in candidate and not isinstance(candidate[table], list):
            reasons.append(("table_not_list",
                            f"Таблица {table} должна быть списком записей."))
    return reasons


def _publication_gate(candidate, document, sources):
    """Validate the single publication row before any other row is considered."""
    rows = candidate.get("publications")
    if not isinstance(rows, list) or len(rows) != 1:
        found = len(rows) if isinstance(rows, list) else "не список"
        return [("publication_count",
                 f"Нужна ровно одна публикация, получено {found}.")]
    row = rows[0]
    if not isinstance(row, dict):
        return [("publication_shape", "Публикация должна быть объектом.")]
    unknown = set(row) - set(TABLES["publications"]) - {"source_ids"}
    if unknown:
        return [("publication_shape",
                 f"Публикация: неизвестные поля {sorted(map(str, unknown))}.")]
    for column in TABLES["publications"]:
        if _raw(row, column) is None:
            return [("publication_shape",
                     f"Публикация: поле {column} должно быть строкой.")]
    if _value(row, "publication_id") != document["publication_id"]:
        return [("publication_id",
                 "publication_id публикации не совпадает с документом.")]
    scope = _value(row, "in_scope")
    if scope not in {"true", "false"}:
        return [("publication_scope", "Нужно явное решение in_scope true/false.")]
    reason = _value(row, "exclusion_reason")
    if scope == "false":
        if not reason:
            return [("exclusion_reason",
                     "Исключённая публикация требует непустой exclusion_reason.")]
        if any(candidate.get(table) for table in SCIENTIFIC_TABLES):
            return [("excluded_with_records",
                     "Исключённая публикация требует пустых научных таблиц.")]
    elif reason:
        return [("exclusion_reason",
                 "У включённой публикации exclusion_reason должен быть пуст.")]
    source_ids = row.get("source_ids", [])
    if not isinstance(source_ids, list) or any(
        type(source_id) is not int or source_id not in sources for source_id in source_ids
    ):
        return [("source_ids", "Публикация: неизвестный source_id.")]
    return []


def _row_reason(table, row, document, sources):
    """First structural or literal reason to quarantine one row, or ``None``."""
    if not isinstance(row, dict):
        return "запись должна быть объектом."
    if table == "issues":
        # Model issues are not trusted; deterministic checks own findings.
        return "issues создаются только программно; запись модели отклонена."
    columns = TABLES[table]
    unknown = set(row) - set(columns) - {"source_ids"}
    if unknown:
        return f"неизвестные поля: {sorted(map(str, unknown))}."
    for column in columns:
        if _raw(row, column) is None:
            return f"поле {column} должно быть строкой."
    values = {column: _value(row, column) for column in columns}
    if values["publication_id"] != document["publication_id"]:
        return "publication_id не совпадает с документом."
    key = values[KEYS[table]]
    if not key:
        return f"пустой идентификатор {KEYS[table]}."
    for (vtable, field), allowed in VOCAB.items():
        if table == vtable and values[field] and values[field] not in allowed:
            return f"{table}.{field}: значение вне словаря."
    source_ids = row.get("source_ids", [])
    if not isinstance(source_ids, list) or any(
        type(source_id) is not int or source_id not in sources for source_id in source_ids
    ):
        return f"{table}/{_short(key, 60)}: неизвестный source_id."
    if table not in {"publications", "issues"} and not source_ids:
        return f"{table}/{_short(key, 60)}: нужны source_ids подтверждающих фрагментов."
    anchors = [sources[source_id] for source_id in dict.fromkeys(source_ids)]
    if table == "results":
        page = values["source_page"]
        fragment = clean(values["source_fragment"])
        if not page.isascii() or not page.isdigit() or not 1 <= int(page) <= document["pages"]:
            return f"{table}/{_short(key, 60)}: неверная страница PDF."
        matching = [
            source for source in anchors
            if source["page"] == int(page) and fragment in clean(source["quote"])
        ]
        if not fragment or not matching:
            return f"{table}/{_short(key, 60)}: цитата не найдена в указанных источниках на странице."
        if not values["extraction_status"] or not values["result_origin"] or not values["metric_name"]:
            return f"results/{_short(key, 60)}: нужны статус, происхождение и показатель."
        if values["extraction_status"] == "extracted":
            raw = clean(values["raw_value"])
            if not has_value(raw, fragment, spread=values["spread_value"]) or not any(
                has_value(raw, source["quote"], spread=values["spread_value"])
                for source in matching
            ):
                return f"results/{_short(key, 60)}: исходное значение отсутствует в цитате."
        if values["time_point"] and (
            not any(has_value(values["time_point"], source["quote"]) for source in anchors)
            or not has_unit(values["time_unit"], anchors)
        ):
            return f"results/{_short(key, 60)}: time_point/единица не подтверждены источником."
        if values["result_origin"] == "cited" and (
            values["device_id"] or values["measurement_id"] or not values["cited_publication_id"]
        ):
            return (f"results/{_short(key, 60)}: cited результат требует ссылку на работу "
                    "без собственного прибора/измерения.")
    for entity_table, field, unit_field in ENTITY_FIELDS:
        if table != entity_table or not values[field]:
            continue
        if not any(has_value(values[field], source["quote"]) for source in anchors):
            return f"{table}.{field}: число не подтверждено источником."
        if not has_unit(values[unit_field], anchors):
            return f"{table}.{field}: единица не подтверждена источником."
    return None


def _mark_duplicates(table, envelopes):
    """Quarantine every copy of every repeated identifier."""
    groups = defaultdict(list)
    for envelope in envelopes:
        if envelope["key"]:
            groups[envelope["key"]].append(envelope)
    duplicates = {key for key, items in groups.items() if len(items) > 1}
    for key in duplicates:
        reason = f"повторяющийся идентификатор {_short(key, 60)}: все копии в карантине."
        for envelope in groups[key]:
            _quarantine(envelope, reason)
    return duplicates


def _duplicate_cycles(candidate):
    """Keys of result rows that participate in a ``duplicate_of`` cycle.

    Repeated identifiers are skipped here: every copy is already quarantined,
    and a graph over an ambiguous key would choose an arbitrary edge.
    """
    rows = candidate.get("results", [])
    counts = defaultdict(int)
    for row in rows:
        if isinstance(row, dict):
            key = _value(row, "result_id")
            if key:
                counts[key] += 1
    graph = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        key, target = _value(row, "result_id"), _value(row, "duplicate_of")
        if key and counts[key] == 1 and target:
            graph[key] = target
    state, cycle = {}, set()
    for start in graph:
        if state.get(start, 0):
            continue
        path, node = [], start
        while node is not None and state.get(node, 0) == 0:
            state[node] = 1
            path.append(node)
            node = graph.get(node)
        if node is not None and state.get(node) == 1:
            cycle.update(path[path.index(node):])
        for item in path:
            state[item] = 2
    return cycle


def _duplicate_matches(row, original):
    """``duplicate_of`` may only join the same conditions and value."""
    if any(_value(row, field) != _value(original, field) for field in DUPLICATE_FIELDS):
        return False
    row_normalized = _value(row, "normalized_value")
    original_normalized = _value(original, "normalized_value")
    if row_normalized and original_normalized:
        return (
            same_number(row_normalized, original_normalized)
            and _value(row, "normalized_unit") == _value(original, "normalized_unit")
        )
    return (
        same_number(_value(row, "raw_value"), _value(original, "raw_value"))
        and clean(_value(row, "raw_unit")) == clean(_value(original, "raw_unit"))
    )


def _raw_keys(tables):
    return {
        table: {envelope["key"] for envelope in envelopes if envelope["key"]}
        for table, envelopes in tables.items()
    }


def _accepted(tables):
    return {
        table: {
            envelope["key"]: envelope
            for envelope in envelopes
            if envelope["status"] == "accepted" and envelope["key"]
        }
        for table, envelopes in tables.items()
    }


def _link_reason(table, row, raw_keys, duplicate_keys, accepted):
    """First broken reference or duplicate condition, or ``None``."""
    for field, target, multiple in REFERENCES[table]:
        value = _value(row, field)
        if not value:
            continue
        items = [part.strip() for part in value.split(";")] if multiple else [value]
        for item in items:
            if not item:
                continue
            if item in duplicate_keys[target]:
                return (f"{table}.{field}: ссылка на неоднозначную запись "
                        f"{_short(item, 60)} (повторяющийся id).")
            if item not in raw_keys[target]:
                return (f"{table}.{field}: ссылка на отсутствующую запись "
                        f"{_short(item, 60)}.")
            if item not in accepted[target]:
                return (f"{table}.{field}: ссылка на запись {_short(item, 60)} "
                        "в карантине.")
        if table == "results" and field == "duplicate_of":
            original = accepted["results"].get(value)
            if original is not None and not _duplicate_matches(row, original["row"]):
                return ("results.duplicate_of: объединяет разные условия или значения.")
    if table == "results":
        device_id = _value(row, "device_id")
        measurement_id = _value(row, "measurement_id")
        if device_id and measurement_id:
            measurement = accepted["measurements"].get(measurement_id)
            if measurement is not None:
                measurement_devices = {
                    part.strip()
                    for part in _value(measurement["row"], "device_id").split(";")
                }
                if _value(measurement["row"], "device_id") and device_id not in measurement_devices:
                    return ("results.device_id: прибор результата не совпадает "
                            "с прибором измерения.")
        # A named result material must be the absorber of the referenced device.
        # An empty side is unknown and is never filled from the other one; a
        # missing or quarantined device was already reported by the loop above.
        material_id = _value(row, "material_id")
        device = accepted["devices"].get(device_id) if device_id else None
        if device is not None and material_id:
            absorber = _value(device["row"], "absorber_material_id")
            if absorber and absorber != material_id:
                return ("results.material_id: материал результата не совпадает "
                        "с поглотителем прибора.")
    return None


def _propagate_links(tables, duplicate_keys):
    """Quarantine rows whose references are missing, ambiguous or quarantined."""
    raw_keys = _raw_keys(tables)
    limit = sum(len(envelopes) for envelopes in tables.values()) + 1
    for _ in range(limit):
        accepted = _accepted(tables)
        changed = False
        for table in ("devices", "layers", "measurements", "results"):
            for envelope in tables[table]:
                if envelope["status"] != "accepted":
                    continue
                reason = _link_reason(
                    table, envelope["row"], raw_keys, duplicate_keys, accepted,
                )
                if reason:
                    _quarantine(envelope, reason)
                    changed = True
        if not changed:
            return
    for table in ("devices", "layers", "measurements", "results"):
        for envelope in tables[table]:
            if envelope["status"] == "accepted":
                _quarantine(envelope, "связи не сошлись за отведённое число проходов.")


def _draft_status(tables):
    counts = _counts(tables)
    if counts["quarantined"]:
        return "partial", counts
    return "ok", counts


def _publication_state(tables):
    publication = tables["publications"][0] if tables["publications"] else None
    if publication is None or publication["status"] != "accepted":
        return "quarantined"
    return "excluded" if _value(publication["row"], "in_scope") == "false" else "included"


def validate_draft(candidate, document):
    """Return a bounded, advisory ``solar-draft-1`` view of ``candidate``.

    The candidate is never mutated and never exported. An unusable document
    envelope is reported as ``document_invalid``. A global shape failure, an
    oversized candidate or a publication-gate failure reports ``blocked`` with
    bounded counts; a publication-gate block keeps the quarantined rows only
    when the whole artifact still fits the size limit. Otherwise each row is
    accepted or quarantined with its own reason, and the rows that depend on a
    quarantined row follow it. The accepted rows keep the original model
    values: this function does not normalize, blank or merge them.
    """
    problem = _document_problem(document)
    if problem is not None:
        return _blocked(document, [problem])

    sources = {source["id"]: source for source in document["sources"]}

    shape = _shape_reasons(candidate)
    if shape:
        # Never copy rows here: the shape may be broken precisely because a
        # value is huge or cyclic, and a blocked artifact must stay bounded.
        return _blocked(document, shape, counts=_candidate_counts(candidate))

    for table in TABLES:
        if len(candidate[table]) > MAX_ROWS_PER_TABLE:
            return _blocked(document, [(
                "row_limit",
                f"Таблица {table} превышает лимит {MAX_ROWS_PER_TABLE} записей.",
            )], counts=_candidate_counts(candidate))

    try:
        payload = json.dumps(
            candidate, ensure_ascii=False, allow_nan=False, separators=(",", ":"),
        )
    except (TypeError, ValueError, UnicodeError):
        return _blocked(document, [(
            "candidate_not_json", "Кандидат не сериализуется в JSON.",
        )], counts=_candidate_counts(candidate))
    if len(payload.encode("utf-8")) > MAX_ARTIFACT_BYTES:
        return _blocked(document, [(
            "artifact_too_large", "Кандидат превышает допустимый размер артефакта.",
        )], counts=_candidate_counts(candidate))

    gate = _publication_gate(candidate, document, sources)
    if gate:
        return _blocked(document, gate, rows=_all_rows(candidate, gate[0][1]))

    tables = {}
    for table in TABLES:
        envelopes = []
        for index, row in enumerate(candidate[table]):
            reason = _row_reason(table, row, document, sources)
            envelopes.append(_envelope(
                table, index, row,
                "quarantined" if reason else "accepted",
                reason or "",
            ))
        tables[table] = envelopes

    duplicate_keys = {
        table: _mark_duplicates(table, tables[table]) for table in TABLES
    }

    cycle = _duplicate_cycles(candidate)
    if cycle:
        reason = "цикл duplicate_of: " + ", ".join(
            _short(key, 40) for key in sorted(cycle)
        )
        for envelope in tables["results"]:
            if envelope["key"] in cycle:
                _quarantine(envelope, reason)

    _propagate_links(tables, duplicate_keys)

    status, counts = _draft_status(tables)
    draft = {
        "schema_version": SCHEMA_VERSION,
        "publication_id": _document_pid(document),
        "status": status,
        "publication_state": _publication_state(tables),
        "blocked_by": [],
        "tables": tables,
        "counts": counts,
    }
    try:
        size = len(json.dumps(
            draft, ensure_ascii=False, allow_nan=False, separators=(",", ":"),
        ).encode("utf-8"))
    except (TypeError, ValueError, UnicodeError):
        return _blocked(document, [(
            "artifact_not_json", "Черновик не сериализуется в JSON.",
        )], counts=counts)
    if size > MAX_ARTIFACT_BYTES:
        return _blocked(document, [(
            "artifact_too_large", "Черновик превышает допустимый размер артефакта.",
        )], counts=counts)
    return draft
