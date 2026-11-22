"""Profile-driven ML boundary, independent of the web server and storage.

Scripts emit source candidates, never fabricated devices or experiments.
Model output remains untrusted until local validation. No completeness claim.
"""

from copy import deepcopy
import csv
from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
import re
from tempfile import TemporaryDirectory
from time import monotonic

from atomic_output import publish_directory
from prompt_rules import DOCUMENT_RULES
from solar_facts import clean, decimal, finalize, has_unit, has_value, number_text, prepare_pdf, unit_name
from viniti import paths


REVISION = "profile-core-2"
NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")
TYPES = {"text", "number", "boolean", "id", "reference"}
# A boolean field is a claim about the document, so the quote has to print an
# explicit marker for the claimed value. Bare "on"/"off"/"not" are deliberately
# absent: they match ordinary prose and would accept an arbitrary sentence.
BOOLEAN_EVIDENCE = {
    "true": ("true", "yes", "enabled", "detected", "observed", "available"),
    "false": ("false", "no", "disabled", "absent", "unavailable", "not detected", "not observed"),
}
GENERIC_SYSTEM = """Extract data according to the supplied profile.
""" + DOCUMENT_RULES + """
Return ONLY a JSON object whose keys are profile table names, each a list of rows.
Rows contain the declared fields as strings (unknown = "") and field_sources.
Each field_sources entry for a nonempty substantive field is:
{"source_id": integer, "quote": "verbatim substring of that source",
 "raw_unit": "explicit original unit or empty",
 "unit_source_id": integer source for a separate unit header, if needed}.
IDs and reference values are local technical IDs, not quotations.
For reference fields provide context evidence in field_sources, but don't invent
links based only on proximity. Use an empty reference when uncertain.
Text and number fields must preserve the literal original value and signs.
Do not compute normalized values; the application does that.
Boolean fields are "true" or "false" only with explicit supporting context.
One row is one entity as described by the profile, not necessarily one page.
Do not merge different devices, conditions, splits or experiments.
Never manufacture missing values. Do not infer missing values from defaults.
No graph of citations between articles is requested.
"""


def compile_profile(profile):
    if not isinstance(profile, dict):
        raise ValueError("Профиль должен быть JSON-объектом.")
    profile = deepcopy(profile)
    allowed = {"id", "version", "description", "adapter", "tables", "metrics", "instructions"}
    if set(profile) - allowed or not isinstance(profile.get("id"), str) or not NAME.fullmatch(profile["id"]):
        raise ValueError("Неизвестные настройки или неверный id профиля.")
    if not isinstance(profile.get("version"), str) or not profile["version"]:
        raise ValueError("Нужна строковая версия профиля.")
    for field in ("description", "instructions"):
        if field in profile and not isinstance(profile[field], str):
            raise ValueError(f"{field}: ожидалась строка.")
    adapter = profile.setdefault("adapter", "fields")
    if not isinstance(adapter, str) or adapter not in {"fields", "solar_csv"}:
        raise ValueError("Неизвестный адаптер профиля.")
    metrics = profile.setdefault("metrics", {})
    if not isinstance(metrics, dict) or len(metrics) > 100:
        raise ValueError("metrics: ожидался ограниченный словарь.")
    for metric, aliases in metrics.items():
        if not isinstance(metric, str) or not NAME.fullmatch(metric) or not isinstance(aliases, list) or not aliases or any(
            not isinstance(alias, str) or not alias.strip() or len(alias) > 100 for alias in aliases
        ):
            raise ValueError("Некорректные aliases метрики.")
    if adapter == "solar_csv":
        if "tables" in profile:
            raise ValueError("Колонки solar задаются официальным адаптером, не переопределяются профилем.")
        return profile
    tables = profile.get("tables")
    if not isinstance(tables, dict) or not 1 <= len(tables) <= 20:
        raise ValueError("Профиль должен описывать от 1 до 20 таблиц.")
    for table, specification in tables.items():
        if not isinstance(table, str) or not NAME.fullmatch(table) or not isinstance(specification, dict) or set(specification) - {"key", "description", "fields"}:
            raise ValueError("Неверное описание таблицы.")
        fields = specification.get("fields")
        if not isinstance(fields, dict) or not 1 <= len(fields) <= 100:
            raise ValueError("Нужно от 1 до 100 полей таблицы.")
        key = specification.get("key")
        if not isinstance(key, str) or key not in fields or not isinstance(fields[key], dict) or fields[key].get("type") != "id":
            raise ValueError("Ключ таблицы должен быть полем типа id.")
        for field, spec in fields.items():
            if not isinstance(field, str) or field == "field_sources" or not NAME.fullmatch(field) or not isinstance(spec, dict) or set(spec) - {
                "type", "description", "required", "unit", "units", "minimum", "maximum", "aliases", "reference",
            }:
                raise ValueError("Неизвестные настройки поля.")
            if not isinstance(spec.get("type"), str) or spec["type"] not in TYPES or not isinstance(spec.get("description"), str) or not spec["description"]:
                raise ValueError("У каждого поля нужны поддерживаемый type и description.")
            if "required" in spec and type(spec["required"]) is not bool:
                raise ValueError("required должен быть boolean.")
            if spec["type"] == "reference" and (
                not isinstance(spec.get("reference"), str) or spec["reference"] not in tables
            ):
                raise ValueError("reference должен указывать таблицу профиля.")
            if spec["type"] == "number":
                if not isinstance(spec.get("unit", ""), str):
                    raise ValueError("unit должна быть строкой.")
                units = spec.setdefault("units", {spec["unit"]: "1"} if spec.get("unit") else {})
                if not isinstance(spec.get("unit", ""), str) or not isinstance(units, dict):
                    raise ValueError("Неверное описание единиц.")
                for unit, factor in units.items():
                    if not isinstance(unit, str) or not isinstance(factor, str) or decimal(factor) <= 0:
                        raise ValueError("Коэффициент единицы должен быть положительным числом-строкой.")
                if units and not spec.get("unit"):
                    raise ValueError("Для пересчёта нужна целевая unit.")
                for limit in ("minimum", "maximum"):
                    if limit in spec:
                        if not isinstance(spec[limit], str):
                            raise ValueError("Границы диапазона должны быть строками.")
                        decimal(spec[limit])
                if "minimum" in spec and "maximum" in spec and decimal(spec["minimum"]) > decimal(spec["maximum"]):
                    raise ValueError("minimum больше maximum.")
                aliases = spec.get("aliases", [])
                if not isinstance(aliases, list) or any(not isinstance(alias, str) or not alias.strip() for alias in aliases):
                    raise ValueError("aliases должны быть непустыми строками.")
    return profile


def response_schema(profile):
    """Compile a JSON response shape; local semantic validation is still required."""
    if profile["adapter"] == "solar_csv":
        from solar_schema import TABLES
        properties = {}
        for table, columns in TABLES.items():
            fields = {name: {"type": "string"} for name in columns}
            fields["source_ids"] = {"type": "array", "items": {"type": "integer"}}
            properties[table] = {"type": "array", "items": {
                "type": "object", "properties": fields, "additionalProperties": False,
            }}
    else:
        anchor = {
            "type": "object", "properties": {
                "source_id": {"type": "integer"}, "quote": {"type": "string"},
                "raw_unit": {"type": "string"}, "unit_source_id": {"type": "integer"},
            }, "required": ["source_id", "quote"], "additionalProperties": False,
        }
        properties = {}
        for table, spec in profile["tables"].items():
            fields = {name: {"type": "string"} for name in spec["fields"]}
            fields["field_sources"] = {
                "type": "object", "properties": {name: anchor for name in spec["fields"]},
                "additionalProperties": False,
            }
            properties[table] = {"type": "array", "items": {
                "type": "object", "properties": fields, "required": [spec["key"], "field_sources"],
                "additionalProperties": False,
            }}
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def load_profile(name):
    path = paths.PROFILES_DIR / f"{name}.json" if name in {"solar", "custom-example"} else Path(name)
    return compile_profile(json.loads(path.read_text(encoding="utf-8")))


def candidate_fields(profile):
    fields = dict(profile["metrics"])
    for table, specification in profile.get("tables", {}).items():
        for name, field in specification["fields"].items():
            if field["type"] == "number":
                fields[f"{table}.{name}"] = field.get("aliases") or [name]
    return fields


# A closed list of words that may stand between a metric name and its printed
# value. Prose such as "PCE reaches 18.4 %" is accepted; a number that merely
# sits near the name is not. The list stays explicit so recall grows without
# turning proximity into evidence.
CONNECTOR = re.compile(
    r"(?:=|:|≈|~|\b(?:is|was|are|were|of|at|reach|reaches|reached|achieve|achieves|"
    r"achieved|equal|equals|up\s+to|about|approximately)\b)", re.I,
)
NUMBER = re.compile(r"[+\-−]?(?:\d+(?:[.,]\d+)?|[.,]\d+)(?:[eE][+\-]?\d+)?")
# A cell holding a range, uncertainty or threshold is not a scalar value. The
# lookbehind keeps a plain negative number ("-20") out of this guard.
RANGE_GUARD = re.compile(r"(?:±|\+/-|[<>≤≥]|\bto\b|[–—]|(?<=\d)\s*-\s*(?=\d))", re.I)
# A clause that names a change prints a delta, not the metric itself:
# "improves Voc of 0.127 V" or "an increase in PCE of 2 %". The verb is only
# looked for before the value, so "PCE of 15.3 %, an increase over NiO" keeps
# its absolute reading. Comparative adjectives ("better", "higher") are not
# deltas: the gold set reads "better power conversion efficiency of 15.3 %"
# as the absolute value of that device.
DELTA = re.compile(
    r"(?:\b(?:improv(?:e|es|ed|ing|ement)|increas(?:e|es|ed|ing)|"
    r"enhanc(?:e|es|ed|ing|ement)|boost(?:s|ed|ing)?|gain(?:s|ed|ing)?|"
    r"reduc(?:e|es|ed|ing|tion)|decreas(?:e|es|ed|ing)|augment(?:s|ed|ing)?|"
    r"delta)\b|[Δ∆])", re.I,
)
# A threshold is a bound, not a measured value: "PCE above 20 %" says nothing
# about the value this device reached. Anchored at the end of the text before
# the number, so "above" in an earlier phrase cannot reject a later value.
THRESHOLD = re.compile(
    r"\b(?:exceed(?:s|ed|ing)?|over|above|more\s+than|greater\s+than|higher\s+than|"
    r"at\s+least|beyond|below|under|less\s+than|up\s+to)\s*$", re.I,
)
# The literal unit printed right after a value, if any. Only a closed list is
# accepted, so a following word is never mistaken for a unit.
UNIT_AFTER = re.compile(
    r"(?:mA\s*/\s*cm\s*(?:2|[-−]\s*2)|mA\s*cm\s*[-−]\s*2|"
    r"Ohm\s*\*?\s*cm\s*2|Ω\s*\*?\s*cm\s*2|mV|V|%|°C|h|min|s|fraction)"
    r"(?![A-Za-z0-9])"
)
# A header may print a subscript after a slash because the text layer lost the
# script position: "V / oc" and "J / sc" are the printed forms of Voc and Jsc.
SPLIT_SUBSCRIPT = re.compile(r"(?<=[A-Za-z])\s*/\s*(?=[a-z]{1,3}\b)")


def _joined_subscripts(text):
    """Rejoin a split subscript header without touching a real unit slash."""
    return SPLIT_SUBSCRIPT.sub("", text)


SENTENCE_END = re.compile(r"(?<!\d)[.!?;](?!\d)")


def _rejected_before(text, start):
    """True when the number at ``start`` is a delta or a threshold, not a value.

    Only the current sentence is inspected: a change verb in an earlier
    sentence does not describe this number. A decimal point is not a sentence
    end, so "improves Voc of 0.127 V, Jsc of 3.1" keeps its change verb.
    """
    before = text[max(0, start - 200):start]
    endings = list(SENTENCE_END.finditer(before))
    if endings:
        before = before[endings[-1].end():]
    return bool(DELTA.search(before) or THRESHOLD.search(before))


def _printed_unit(text, start):
    """The literal unit token right after ``start``, or an empty string."""
    tail = re.sub(r"^[\s/|,;:()\[\]]+", "", text[start:])
    match = UNIT_AFTER.match(tail)
    return match.group() if match else ""


def _alias_alternation(aliases):
    return "|".join(re.escape(alias) for alias in sorted(aliases, key=len, reverse=True))


def _expression(aliases):
    return re.compile(
        r"(?<!\w)(?:" + _alias_alternation(aliases) + r")(?!\w)\s*"
        r"(?:(?P<connector>" + CONNECTOR.pattern + r")\s*)?"
        r"(?P<value>" + NUMBER.pattern + r")(?![\w.,])", re.I,
    )


def _label_pattern(aliases):
    return re.compile(r"(?<!\w)(?:" + _alias_alternation(aliases) + r")(?!\w)", re.I)


def _cell_texts(source):
    """Cell texts of a table row source, or the printed row split on the delimiter."""
    cells = source.get("cells")
    if isinstance(cells, list) and cells:
        return [cell.get("quote", "") if isinstance(cell, dict) else "" for cell in cells]
    return source.get("quote", "").split(" | ")


def _scalar_in_cell(text):
    """The single clean numeric token in a cell, or None for ranges/ambiguity."""
    if RANGE_GUARD.search(text):
        return None
    matches = list(NUMBER.finditer(text))
    if len(matches) != 1 or _rejected_before(text, matches[0].start()):
        return None
    raw = matches[0].group()
    return raw if has_value(raw, text) else None


def _row_label(texts):
    """The first literal label of a table row; a numeric index is not a label."""
    for text in texts:
        if text and not NUMBER.fullmatch(text.strip()):
            return text
    return ""


def _best_label(text, patterns):
    """Pick the field whose alias is the longest match in a label cell.

    Comparing lengths keeps "stabilized PCE" from also being reported as "PCE".
    The returned match belongs to the joined text, never to the printed cell.
    """
    best = None
    for field, pattern in patterns.items():
        match = pattern.search(text)
        if match is not None and (best is None or match.end() - match.start() > best[1].end() - best[1].start()):
            best = (field, match)
    return best


def _candidate(field, raw, source, *, connector="", cell=None, header=None, note="",
               raw_unit="", row_label=""):
    context = {
        "kind": source.get("kind", "text"), "page": source.get("page"),
        "section": source.get("section") or "", "table": source.get("table"),
        "row": source.get("row"), "cell": cell,
    }
    if connector:
        context["connector"] = connector
    if header:
        context["header_source_id"], context["header_quote"] = header
    if row_label:
        context["row_label"] = row_label
    return {
        "field": field, "raw_value": raw, "raw_unit": raw_unit, "row_label": row_label,
        "status": "candidate", "context_status": "unresolved",
        "source_id": source["id"], "evidence": deepcopy(source), "context": context,
        "note": note or ("Keyword match only; entity and experimental context are not established."
                         if raw_unit else
                         "Keyword match only; unit, entity and experimental context are not established."),
    }


def script_candidates(document, profile):
    """Emit verifiable value candidates from the sources that already exist.

    Each candidate points at one real source id and repeats a literal numeric
    token printed in that source. Prose and captions use the closed connector
    list; a table row is read either from a label and value printed in the same
    row, or from a header cell of the same table that names the column. No
    source id, quote or value is generated, and a range, uncertainty or
    threshold never becomes a scalar candidate.
    """
    fields = candidate_fields(profile)
    expressions = {field: _expression(aliases) for field, aliases in fields.items()}
    patterns = {field: _label_pattern(aliases) for field, aliases in fields.items()}
    headers = {}
    for source in document["sources"]:
        if source.get("kind") == "table" and source.get("row") == 1:
            headers[(source.get("page"), source.get("table"))] = (_cell_texts(source), source["id"])
    # A sentence split over two text lines keeps its change verb: the lead of
    # "improves Voc of 0.127 V" is needed to reject "and PCE of 7.5 %".
    leads, buffers = {}, {}
    for source in document["sources"]:
        if source.get("kind", "text") != "text":
            continue
        page = source.get("page")
        buffer = buffers.get(page, "")
        leads[source["id"]] = buffer[-200:]
        buffers[page] = buffer + " " + source.get("quote", "")
    candidates = []
    for source in document["sources"]:
        kind = source.get("kind", "text")
        if kind == "cell":
            continue
        if kind != "table":
            lead = leads.get(source["id"], "")
            quote = source.get("quote", "")
            for field, expression in expressions.items():
                for match in expression.finditer(quote):
                    raw = match["value"]
                    if not has_value(raw, quote):
                        continue
                    if _rejected_before(lead + " " + quote, len(lead) + 1 + match.start("value")):
                        continue
                    candidates.append(_candidate(
                        field, raw, source, connector=match["connector"] or "",
                        raw_unit=_printed_unit(quote, match.end("value")),
                        note=("Caption keyword match; unit and experimental context are not established."
                              if kind == "caption" else
                              "Keyword match only; unit, entity and experimental context are not established."),
                    ))
            continue
        texts = _cell_texts(source)
        if not texts:
            continue
        label = _row_label(texts)
        # Same-row label and value: one numeric cell, no other numeric cells.
        for index, text in enumerate(texts):
            joined = _joined_subscripts(text)
            best = _best_label(joined, patterns)
            if best is None:
                continue
            numeric = [(other, value) for other, value in enumerate(texts)
                       if other != index and NUMBER.search(value)]
            if len(numeric) != 1:
                continue
            value_cell, value_text = numeric[0]
            raw = _scalar_in_cell(value_text)
            if raw is None or not has_value(raw, source.get("quote", "")):
                continue
            candidates.append(_candidate(
                best[0], raw, source, cell=value_cell,
                raw_unit=_printed_unit(joined, best[1].end()), row_label=label,
            ))
        # Header-named column: the header row and the data row are separate
        # sources, so the header id is recorded as context, never invented.
        header = headers.get((source.get("page"), source.get("table")))
        if source.get("row") == 1 or header is None:
            continue
        header_texts, header_id = header
        for index, text in enumerate(texts):
            if index >= len(header_texts):
                continue
            joined_header = _joined_subscripts(header_texts[index])
            best = _best_label(joined_header, patterns)
            if best is None:
                continue
            raw = _scalar_in_cell(text)
            if raw is None or not has_value(raw, source.get("quote", "")):
                continue
            candidates.append(_candidate(
                best[0], raw, source, cell=index, header=(header_id, header_texts[index]),
                raw_unit=_printed_unit(joined_header, best[1].end()), row_label=label,
            ))
    unique, seen = [], set()
    for candidate in candidates:
        key = (candidate["field"], candidate["raw_value"], candidate["source_id"])
        if key not in seen:
            seen.add(key)
            unique.append(candidate)
    return unique


def model_messages(document, profile, candidates, max_input_chars):
    if profile["adapter"] == "solar_csv":
        from solar_extract import messages
        prompt = messages(document, max_input_chars)
        extra = {
            "profile_id": profile["id"], "profile_version": profile["version"],
            "instructions": profile.get("instructions", ""),
            "script_candidates": [{key: row[key] for key in ("field", "raw_value", "source_id")} for row in candidates],
            "response_schema": response_schema(profile),
        }
        prompt[1]["content"] += "\n" + json.dumps(extra, ensure_ascii=False)
    else:
        prompt = [
            {"role": "system", "content": GENERIC_SYSTEM},
            {"role": "user", "content": json.dumps({
                "profile": profile, "publication_id": document["publication_id"],
                "response_schema": response_schema(profile),
                "sources": [{key: source[key] for key in ("id", "page", "quote", "kind") if key in source}
                            for source in document["sources"]],
                "script_candidates": [{key: row[key] for key in ("field", "raw_value", "source_id")} for row in candidates],
            }, ensure_ascii=False)},
        ]
    if sum(len(message["content"]) for message in prompt) > max_input_chars:
        raise ValueError("Превышен бюджет запроса; документ не обрезан. Chunking пока не реализован.")
    return prompt


def boolean_evidence(raw, quote):
    """Return the printed marker that supports ``raw`` in ``quote``, else None.

    The marker must appear as a whole word (or a printed phrase) of the same
    quote that carries the field. A nearby sentence, a default or a table
    neighbour is not evidence, so an unmarked boolean is never promoted to a
    table value.
    """
    text = clean(quote).lower()
    for marker in BOOLEAN_EVIDENCE[raw]:
        if re.search(rf"(?<![a-z]){re.escape(marker)}(?![a-z])", text):
            return marker
    return None


def validate_fields(candidate, document, profile):
    if not isinstance(candidate, dict) or set(candidate) != set(profile["tables"]):
        raise ValueError("Ответ не соответствует таблицам профиля.")
    records, tables, issues = [], {}, []
    sources = {source["id"]: source for source in document["sources"]}
    for table, specification in profile["tables"].items():
        rows = candidate[table]
        if not isinstance(rows, list) or len(rows) > 20000:
            raise ValueError("Ожидался ограниченный список строк.")
        tables[table], seen = [], set()
        for row in rows:
            fields = specification["fields"]
            if not isinstance(row, dict) or set(row) - set(fields) - {"field_sources"}:
                raise ValueError("Неизвестные поля записи.")
            if any(not isinstance(value, str) for key, value in row.items() if key != "field_sources"):
                raise ValueError("Значения полей должны быть строками.")
            key = row.get(specification["key"], "")
            if not key or key in seen:
                raise ValueError("Пустой или повторный ключ записи.")
            seen.add(key)
            provenance = row.get("field_sources", {})
            if not isinstance(provenance, dict) or set(provenance) - set(fields):
                raise ValueError("Неверные field_sources.")
            normalized, observations = {}, {}
            for name, field in fields.items():
                raw = row.get(name, "")
                observation = {"raw": raw or None, "value": raw or None, "unit": None,
                               "status": "unknown" if not raw else "extracted", "evidence_refs": []}
                if not raw and field.get("required"):
                    issues.append({"table": table, "record_id": key, "field": name, "rule": "required",
                                   "description": "Обязательное поле не извлечено; отсутствие в документе не установлено."})
                if raw and field["type"] != "id":
                    anchor = provenance.get(name)
                    if not isinstance(anchor, dict) or set(anchor) - {"source_id", "quote", "raw_unit", "unit_source_id"}:
                        raise ValueError(f"{table}.{name}: нужен источник поля.")
                    sid = anchor.get("source_id")
                    if type(sid) is not int or sid not in sources:
                        raise ValueError("Неизвестный source_id.")
                    source = sources[sid]
                    quote = anchor.get("quote")
                    if not isinstance(quote, str) or not clean(quote) or clean(quote) not in clean(source["quote"]):
                        raise ValueError("Цитата поля не найдена в PDF.")
                    observation["evidence_refs"] = [deepcopy(source)]
                    observation["source_fragment"] = quote
                    if field["type"] in {"text", "number"} and not (
                        has_value(raw, quote) and has_value(raw, source["quote"])
                    ):
                        raise ValueError(f"{table}.{name}: значение не подтверждено полным токеном источника.")
                    if field["type"] == "boolean" and raw not in {"true", "false"}:
                        raise ValueError("boolean: ожидались true/false.")
                    if field["type"] == "reference":
                        observation["status"] = "inferred_requires_review"
                    if field["type"] == "boolean":
                        if boolean_evidence(raw, quote) is None:
                            # Keep the printed literal as raw text only: without
                            # an explicit marker the claim stays unresolved.
                            observation["value"] = None
                            observation["status"] = "requires_review"
                            issues.append({"table": table, "record_id": key, "field": name,
                                           "rule": "boolean_evidence",
                                           "description": "Цитата не содержит явного признака "
                                                          "true/false; значение не подтверждено."})
                        else:
                            observation["status"] = "inferred_requires_review"
                    if field["type"] == "number":
                        observation["value"] = None
                        original_unit = anchor.get("raw_unit", "")
                        if not isinstance(original_unit, str):
                            raise ValueError("raw_unit должен быть строкой.")
                        observation["raw_unit"] = original_unit
                        anchors = [source]
                        if "unit_source_id" in anchor:
                            uid = anchor["unit_source_id"]
                            if type(uid) is not int or uid not in sources:
                                raise ValueError("Неизвестный unit_source_id.")
                            anchors.append(sources[uid])
                            observation["evidence_refs"].append(deepcopy(sources[uid]))
                        try:
                            value = decimal(raw)
                            if field.get("unit") or field.get("units"):
                                factors = {unit_name(unit): factor for unit, factor in field["units"].items()}
                                factor = factors.get(unit_name(original_unit))
                                if factor is None or not has_unit(original_unit, anchors):
                                    raise ValueError("Единица/шкала не подтверждена.")
                                value *= decimal(factor)
                                observation["unit"] = field["unit"]
                            elif original_unit:
                                raise ValueError("Единица не описана профилем, нельзя считать величину безразмерной.")
                            observation["value"] = number_text(value)
                            for limit, condition in (("minimum", lambda x, y: x < y), ("maximum", lambda x, y: x > y)):
                                if limit in field and condition(value, decimal(field[limit])):
                                    issues.append({"table": table, "record_id": key, "field": name, "rule": "range",
                                                   "description": f"{observation['value']} нарушает {limit}={field[limit]}."})
                        except ValueError as error:
                            observation["status"] = "requires_review"
                            issues.append({"table": table, "record_id": key, "field": name, "rule": "normalization",
                                           "description": str(error)})
                normalized[name] = observation["value"] or ""
                observations[name] = observation
            tables[table].append(normalized)
            records.append({"id": key, "entity": table, "fields": observations, "context_status": "requires_review"})
    for table, specification in profile["tables"].items():
        for row in tables[table]:
            for name, field in specification["fields"].items():
                if field["type"] == "reference" and row[name]:
                    target = field["reference"]
                    ids = {r[profile["tables"][target]["key"]] for r in tables[target]}
                    if row[name] not in ids:
                        raise ValueError(f"{table}.{name}: ссылка на отсутствующую сущность.")
    return records, tables, issues


def run(document, profile, *, mode="auto", model_client=None, model="", max_input_chars=200000):
    profile = compile_profile(profile)
    if mode not in {"scripts", "auto", "model"} or type(max_input_chars) is not int or max_input_chars <= 0:
        raise ValueError("Неверный режим или бюджет входа.")
    candidates = script_candidates(document, profile) if mode != "model" else []
    result = {
        "schema_version": REVISION, "profile_id": profile["id"], "profile_version": profile["version"],
        "document": {key: document[key] for key in ("publication_id", "sha256", "pages")},
        "sources": deepcopy(document["sources"]), "records": [], "candidates": candidates,
        "parser_output": deepcopy(document.get("parser_output", {})),
        "tables": {}, "issues": [], "warnings": list(document["warnings"]),
        "columns": {table: list(spec["fields"]) for table, spec in profile.get("tables", {}).items()},
        "coverage": {"status": "partial", "completeness": "not_measured",
                     "reason": "Только текстовый слой; OCR/графики не обработаны. Полнота не подтверждена."},
        "run": {"revision": REVISION, "mode": mode, "model": model if mode != "scripts" else "",
                "profile_sha256": sha256(json.dumps(profile, sort_keys=True, ensure_ascii=False).encode()).hexdigest()},
    }
    if result["parser_output"]:
        parser = result["parser_output"].get("parser", {})
        visual_count = len(result["parser_output"].get("visual_candidates", []))
        result["coverage"]["reason"] = (
            f"Парсер {parser.get('name', 'unknown')}; OCR запрошен в режиме {parser.get('ocr', 'unknown')}; "
            f"визуальных кандидатов {visual_count}. Полнота и научная точность не подтверждены."
        )
    if mode == "scripts":
        result["coverage"]["reason"] = "Только кандидаты: приборы, связи, единицы и полнота не установлены."
        result["warnings"].append("Отсутствие совпадений не означает отсутствие сведений в документе.")
        return result
    if model_client is None:
        raise ValueError("Для auto/model нужен явно подключённый model_client.")
    prompt = model_messages(document, profile, candidates, max_input_chars)
    started = monotonic()
    candidate = model_client(prompt)
    if profile["adapter"] == "solar_csv":
        solar = finalize(candidate, document, model=model)
        result.update(tables=solar["tables"], evidence=solar["evidence"], warnings=solar["warnings"])
        result["issues"] = deepcopy(solar["tables"]["issues"])
        result["adapter"] = "solar_csv"
        # Keep solar record-level evidence explicit, not invented field evidence.
        result["records"] = [
            {"entity": table, "fields": deepcopy(row), "evidence_scope": "record",
             "context_status": "requires_review"}
            for table, rows in solar["tables"].items() if table != "issues" for row in rows
        ]
    else:
        result["records"], result["tables"], result["issues"] = validate_fields(candidate, document, profile)
        result["adapter"] = "fields"
    result["run"]["input_chars"] = sum(len(message["content"]) for message in prompt)
    result["run"]["prompt_sha256"] = sha256(json.dumps(prompt, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    result["run"]["elapsed_seconds"] = round(monotonic() - started, 6)
    return result


def export_result(result, output, *, assets=None):
    output = Path(output)
    if output.exists():
        raise ValueError("Каталог результата уже существует.")
    parser_output = result.get("parser_output", {})
    references = set()
    for collection, fields in (
        ("tables", ("csv",)), ("figures", ("image",)), ("visual_candidates", ("csv", "image")),
    ):
        for item in parser_output.get(collection, []):
            for field in fields:
                if item.get(field):
                    references.add(item[field])
    if references - (assets or {}).keys():
        raise ValueError("Не переданы все файлы, на которые ссылается parser_output.")
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".extraction-", dir=output.parent) as temporary:
        folder = Path(temporary) / "result"
        folder.mkdir()
        (folder / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2), encoding="utf-8",
        )
        if assets:
            if sum(len(value) for value in assets.values() if isinstance(value, bytes)) > 32 * 1024 * 1024:
                raise ValueError("Артефакты превышают 32 МиБ.")
            for name, content in assets.items():
                if not isinstance(name, str) or "\\" in name or not isinstance(content, bytes):
                    raise ValueError("Неверный артефакт парсера.")
                path = PurePosixPath(name)
                if path.is_absolute() or ".." in path.parts or len(path.parts) != 3 or path.parts[:2] not in {
                    ("assets", "tables"), ("assets", "figures"), ("assets", "charts"),
                } or not NAME.fullmatch(path.stem.replace("-", "_")) or path.suffix not in {".csv", ".png"}:
                    raise ValueError("Небезопасный путь артефакта парсера.")
                destination = folder.joinpath(*path.parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(content)
        if parser_output:
            (folder / "parser.json").write_text(
                json.dumps(parser_output, ensure_ascii=False, allow_nan=False, indent=2), encoding="utf-8",
            )
        if result.get("adapter") == "solar_csv":
            from solar_schema import TABLES
            columns = TABLES
        else:
            columns = result.get("columns", {})
        if result["run"]["mode"] == "scripts":
            columns = {}
        for table, fields in columns.items():
            if not NAME.fullmatch(table):
                raise ValueError("Неверное имя таблицы.")
            if not fields:
                continue
            with (folder / f"{table}.csv").open("w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=fields)
                writer.writeheader()
                writer.writerows(result["tables"][table])
        if result["run"]["mode"] == "scripts":
            with (folder / "candidates.csv").open("w", encoding="utf-8", newline="") as stream:
                # Appended columns keep the existing order and add the evidence
                # that used to be dropped at export: row label, page, quote and
                # the structured context.
                fields = ["field", "raw_value", "raw_unit", "status", "context_status", "source_id",
                          "note", "row_label", "page", "quote", "context"]
                writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
                writer.writeheader()
                writer.writerows([
                    {**candidate,
                     "page": candidate["evidence"].get("page"),
                     "quote": candidate["evidence"].get("quote", ""),
                     "context": json.dumps(candidate.get("context", {}), ensure_ascii=False, sort_keys=True)}
                    for candidate in result["candidates"]
                ])
        publish_directory(folder, output)
