"""Offline orchestration shell for the LLM stages of the profile parse pipeline.

This module belongs to the profile-driven ``/api/parse`` path. It is not the
RAG filler contract in ``llm_contract.py``: here the input is a scripts-only
parse bundle written by ``extraction_core.export_result``, and the shell only
adds candidate and audit artifacts next to it.

The default mode is ``mock``: it never talks to a network and never needs an
API key. ``MockBackend`` derives deterministic proposals from the sources and
script candidates that the existing pipeline already produced.

The ``api`` mode is opt-in twice: the caller must pass ``allow_network=True``
and the environment must provide a key and a model (see ``llm_provider``).
``ApiBackend`` sends the same prompts that are stored in ``request.json`` to a
generic [OI]-compatible chat-completions endpoint. Every model-shaped response
is untrusted: it must pass the existing deterministic validators before it is
stored.

Guarantees of one run:

* ``result.json`` (the scripts-only passport) is read and hashed before and
  after; it is never rewritten;
* for the solar adapter the manifest carries ``solar``, a machine-readable
  scope and record state (``in_scope``, ``scope_reason``, record counts,
  unresolved candidate count, export status); it reports the interpretation,
  it never turns candidates into records;
* an optional model ``semantic`` block is validated separately into
  ``semantic.json``: every field needs its own literal anchor, records are
  tagged ``carrier="semantic_only"`` by code and never enter the scientific
  tables, the interpretation or the official CSV;
* the bounded parsed model candidate is kept in ``response.json`` before
  semantic extraction and finalize, so a rejected interpretation stays
  diagnosable; only the parsed object is stored, never keys or headers;
* an advisory per-row ``draft.json`` is written before the strict contract, so a
  rejected interpretation still shows which independent rows were acceptable;
  it is diagnostic only and never merges into the tables, passport or CSV;
* the bounded attribution inventory from finalize is copied into
  ``interpretation.attribution`` and summarized in the manifest solar block. It
  lists sensitive process and sample-size fields without a verdict, so it never
  blanks, demotes or rewrites a value, and ``state="none"`` is not a claim that
  the scientific relations were verified;
* writing an artifact is not part of the validation contract: an I/O or encoding
  failure surfaces as such instead of being recorded as a rejected response;
* fixed artifact names are cleared before every run, so a missing file means
  "not produced by this run". An interrupted rerun therefore leaves no fresh
  artifact rather than the previous run's value, and is read as invalid, not as
  recovered state;
* official CSV export is a code-only stage and is not invoked here;
* artifacts live only under ``<bundle>/llm`` and are written atomically;
* a malformed or untrusted response becomes a recorded ``rejected`` stage, not
  a silent pass and not a crash;
* input and output sizes are bounded, and the document is never truncated.
"""

from hashlib import sha256
from copy import deepcopy
import json
import os
from pathlib import Path
import re
from tempfile import NamedTemporaryFile

from extraction_core import compile_profile, model_messages, validate_fields
from prompt_rules import DOCUMENT_RULES
from solar_facts import clean, finalize, has_unit, has_value
from solar_schema import KEYS as SOLAR_KEYS
import llm_provider


REVISION = "llm-shell-1"
DEFAULT_INPUT_CHARS = 200_000
MAX_INPUT_CHARS = 2_000_000
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_ARTIFACT_BYTES = 32 * 1024 * 1024
MAX_BUNDLE_BYTES = 64 * 1024 * 1024
MAX_FINDINGS = 500
MAX_EVIDENCE = 8
MAX_TEXT = 2000
MAX_QUOTE = 1000
MODES = {"mock", "api"}
# Artifacts written for every profile; ``semantic.json`` and ``response.json``
# are solar-only and listed separately so the server whitelist and cleanup cover
# them without claiming that every run writes them.
BASE_ARTIFACT_NAMES = ("sources.json", "request.json", "interpretation.json",
                       "audit.json", "manifest.json")
SEMANTIC_ARTIFACT = "semantic.json"
# Bounded copy of the parsed model candidate, written before semantic extraction
# and finalize so a rejected interpretation stays diagnosable. It holds only the
# parsed response: never keys, headers or provider configuration.
RESPONSE_ARTIFACT = "response.json"
# Advisory draft view of the parsed candidate, written before finalize so a
# strict rejection still shows which rows were individually acceptable. It is
# never merged into the interpretation, the passport or the official CSV.
DRAFT_ARTIFACT = "draft.json"
# Mirrors solar_draft.SCHEMA_VERSION only for the fallback error shape used when
# that module cannot be imported; the validator's own value wins otherwise.
DRAFT_SCHEMA_VERSION = "solar-draft-1"
ARTIFACT_NAMES = (*BASE_ARTIFACT_NAMES, SEMANTIC_ARTIFACT, RESPONSE_ARTIFACT,
                  DRAFT_ARTIFACT)
SEMANTIC_SCHEMA_VERSION = "solar-semantic-1"
SEMANTIC_RECORD_KINDS = ("layers", "conditions", "metrics")
SEMANTIC_ENUMS = {
    "study_type": {"experimental", "simulation", "mixed", "review", "unknown"},
    "data_provenance": {"measured", "simulated", "literature", "unknown"},
    "thematic_relevance": {"relevant", "partially_relevant", "not_relevant", "unknown"},
}
# Bounded record shape; the model may not add free-form fields. ``page`` and
# ``carrier`` are reserved: code assigns them, a model value is rejected.
SEMANTIC_RECORD_FIELDS = {
    "layers": ("name", "role", "material", "thickness", "thickness_unit"),
    "conditions": ("name", "value", "unit"),
    "metrics": ("name", "value", "unit"),
}
# A record without these is not a record: an empty value is not an observation.
SEMANTIC_REQUIRED_FIELDS = {
    "layers": ("name", "material"),
    "conditions": ("name", "value"),
    "metrics": ("name", "value"),
}
# Top-level claims are separate from records: each one needs its own anchor in
# ``metadata_evidence``, otherwise it is not confirmed and becomes ``unknown``.
SEMANTIC_METADATA_FIELDS = (
    "study_type", "data_provenance", "thematic_relevance", "scope_reason",
)
SEMANTIC_METADATA_EVIDENCE = "metadata_evidence"
# Literal marker lexicons for claims that name a printed phenomenon. A marker
# only shows that the quote speaks about the phenomenon; it does not prove the
# scientific judgement, so this stays a literal gate, not a quality verdict.
SEMANTIC_CLAIM_MARKERS = {
    "study_type": {
        "experimental": ("measur", "experiment", "fabricat", "synthes"),
        "simulation": ("simulat", "comput", "calculat", "model"),
        "mixed": ("measur", "experiment", "simulat", "comput", "model"),
        "review": ("review", "overview", "survey"),
    },
    "data_provenance": {
        "measured": ("measur", "experiment"),
        "simulated": ("simulat", "comput", "calculat", "model"),
        "literature": ("literature", "reported", "taken from", "ref."),
    },
}
MAX_SEMANTIC_RECORDS = 200
MAX_SEMANTIC_ERRORS = 100
# Scientific tables of the solar template; ``publications`` is metadata and
# ``issues`` holds findings, so neither is counted as a scientific record.
SOLAR_RECORD_TABLES = ("materials", "devices", "layers", "measurements", "results")
RULE = re.compile(r"[a-z][a-z0-9_]{0,39}")
CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# Secret-looking forms are removed from advisory error text: a validator
# exception can echo a provider header, and the draft artifact is served to the
# document owner. The pattern is deliberately narrow (``name=value`` /
# ``Bearer value``), so ordinary words are not mangled.
SECRET_FORM = re.compile(
    r"(?i)\b(password|passwd|token|api[_-]?key|secret|authorization)\b\s*[=:]\s*\S+")
BEARER_FORM = re.compile(r"(?i)\bbearer\s+\S+")
AUDIT_SYSTEM = """Audit already extracted records for internal inconsistency.
""" + DOCUMENT_RULES + """
Return ONLY a JSON object {"findings": [...]}.
Each finding is {"table", "record_id", "field", "rule", "description", "evidence"}.
Use only table/record_id/field values present in the draft; evidence is a
nonempty list of {"source_id"} taken from the draft, optionally with "page".
Quote only verbatim substrings of that source. Do not invent records, fields or
sources. Report a finding only when the draft contradicts itself or a source;
return an empty list when nothing is found. Do not repeat findings that the
deterministic validation already lists as issues.

Distinguish two kinds of boolean, and do not treat them the same:
- Extracted experimental booleans (for example measurements.mask_used,
  measurements.certified, or any device/layer property) describe what the paper
  states. They are true or false only when a quote prints an explicit marker
  such as true/false, yes/no, masked/unmasked, certified/not certified; report a
  finding when such a value has no explicit marker in its own evidence. Keep
  reviewing these booleans as before.
- publications.in_scope is NOT an extracted experimental boolean and is NOT a
  measurement of the authors' setup. It is this profile's eligibility judgement
  for the official CSV, made by the extraction pipeline from the scope criteria
  (for example experimental single-junction thin-film device with a fabricated
  sample). Do NOT report a finding merely because the PDF does not print the
  literal word "false" or "no" next to in_scope. Judge in_scope against the
  profile criteria and the document evidence as a whole: report it only when the
  paper clearly meets or clearly contradicts those criteria, and describe the
  contradicting printed evidence. An exclusion_reason that paraphrases the
  criteria is not an inconsistency.
"""
SEMANTIC_INSTRUCTION = """

REQUIRED RESPONSE ENVELOPE (this supersedes any earlier instruction to return
only seven tables). Return ONE JSON object with EIGHT top-level keys: the seven
unchanged solar tables (publications, materials, devices, layers, measurements,
results, issues) plus "semantic". The "semantic" key is ALWAYS present, even when
nothing qualifies: then use unknown values and empty record lists. A response
without "semantic" is an incomplete answer.

The semantic block is stored separately: it never becomes an official CSV row or
a scientific table record.
{"publication_id": "<same publication_id>",
 "study_type": "experimental|simulation|mixed|review|unknown",
 "data_provenance": "measured|simulated|literature|unknown",
 "thematic_relevance": "relevant|partially_relevant|not_relevant|unknown",
 "scope_reason": "<short string or empty>",
 "metadata_evidence": {
   "study_type": {"source_id": 1, "page": 1, "quote": "<verbatim substring>"},
   "data_provenance": {...}, "thematic_relevance": {...}, "scope_reason": {...}},
 "records": {
   "layers": [{"name": "<printed label>", "role": "...", "material": "...",
               "thickness": "...", "thickness_unit": "...",
               "field_sources": {
                 "name": {"source_id": 1, "page": 1, "quote": "<verbatim substring>"},
                 "material": {...}, "thickness": {...}}}],
   "conditions": [{"name": "...", "value": "...", "unit": "...",
                   "field_sources": {"name": {...}, "value": {...}, "unit": {...}}}],
   "metrics": [{"name": "...", "value": "...", "unit": "...",
                "field_sources": {"name": {...}, "value": {...}, "unit": {...}}}]}}

Capture only values printed in the document, and only into layers, conditions
and metrics that the paper actually states. Never fabricate rows, never estimate
a value from a graph or a figure, and never fill a field to make the block look
complete: absent printed data means an empty list, not a guessed record.

Every quote MUST be a verbatim substring of the cited source on the cited page.
Give EVERY nonempty field its own anchor in "field_sources" (never one shared
quote for several fields) and every nonempty claim its own anchor in
"metadata_evidence". A field without an anchor is dropped, and a claim without
its anchor becomes "unknown". Every anchor must literally print its field: name,
role and material as printed text, thickness and value as a complete numeric
token (12 is not 112), unit as a printed unit. layers need name and material;
conditions and metrics need name and a nonempty value. Do NOT set carrier or
page: the application assigns them.

SOURCES ARE LINE FRAGMENTS, NOT SENTENCES. Each source id holds one stored line
or fragment exactly as the parser saved it, in the parser's order. Never join two
source ids into one quote, never reorder fragments to rebuild a sentence, and
never continue a quote across a line break or a hyphen: a fragment that ends in a
hyphen and the fragment after it are two different stored texts. Copy the stored
characters exactly, including OCR and font defects, missing spaces, split
ligatures and odd spellings. Do NOT repair the text: never restore a ligature,
never correct a spelling, never merge a broken word, and never insert a space or
a hyphen that the stored line does not contain.

Use the SHORTEST exact quote from ONE source id that still contains the value. If
the unit or the field name is printed in a different fragment, give that field
its own anchor to its own fragment; each anchor must literally contain what it
anchors, and no anchor may borrow text from another id. Inside this semantic
block, write name, role and material with the spelling printed in the source, not
with a normalized vocabulary term: normalization is applied by code, not by you.
This printed-spelling rule is specific to the semantic block; the seven official
tables below follow their own dictionary rule. For a mirrored,
garbled or unreadable axis label, copy the stored characters when they are
legible or leave the field unknown; never reconstruct a plausible label from the
number or from the surrounding figure.

Optional fields (role, thickness, unit and similar) may stay empty when the
source does not support them. A required field - layer material, condition name
and value, metric name and value - must never be invented: skip that record
instead of filling the gap.

SCIENTIFIC BINDING FOR THE SEVEN OFFICIAL TABLES. Their shape does not change,
but every value must be bound to the entity the paper actually attributes it to.

materials.csv holds absorber materials only: one row is the material that absorbs
light in the device. Transport, contact, electrode, buffer, passivation and
interlayer materials are not absorber rows. Record such a material in layers.csv
as the printed layer_material of its own layer row, and never create a materials
row just to give a transport layer a place.

Every results.material_id must reference the absorber of that result's device and
must equal devices.absorber_material_id for the same device_id. When the printed
evidence does not tie a value to an absorber material, leave material_id empty
instead of picking a material.

Processing conditions (annealing, temperature, time, atmosphere, solvent, spin
speed and similar) belong to the layer or device whose fabrication step the
source names. Never copy a condition from a neighbouring step, from a device
described next to it, or from one member of a control/target pair onto the other
row. A condition needs explicit attribution to that entity in the source;
without it the field stays empty. A shared process is shared only when the text
says it applies to those entities.

n_devices, spread_value and aggregation describe one specific result. Fill them
only when the source explicitly attributes that number or label to that value
under those conditions: "we fabricated 20 devices" is not n_devices for every
row, and a batch size, a group size or an average over an unrelated set is not a
property of an individual result. When the paper does not say that a value is a
champion, a mean or a median, leave aggregation empty.

raw_value, raw_unit and source_fragment are immutable printed text: copy them
exactly and never normalize, convert or round them. Normalization is applied by
code afterwards and must not be precomputed. A schema dictionary label - for
example a layer_function such as ETL, a material_class such as perovskite, a
measurement_type such as J-V - is a controlled vocabulary term, not a quote: it
does NOT have to appear literally in the source. Choose the nearest allowed
dictionary value and put the original printed wording into note of the same row,
while the evidence quote keeps the printed wording. Do not put a dictionary term
into source_fragment as if the paper had printed it, and do not drop a row only
because the paper words the concept differently.

A value read from a table must stay bound to its own column header and row label:
read the cell together with its header and never move a number to another column,
another row or another device because the layout is close. If the header or the
row label is unreadable, leave that value out instead of guessing where it
belongs.

Keep the existing distinction between own and borrowed data. A borrowed value has
result_origin "cited", empty device_id and measurement_id, and the paper's
reference marker in cited_publication_id. A repeated mention of the same value
under the same conditions is a duplicate with its own source and duplicate_of
pointing at the original result_id; a different value under the same conditions
is NOT a duplicate and must not be labelled as one. Leave issues empty: the
application runs the checks.

Scope rules for this block are independent of the official tables. If the
publication is out of scope for experimental solar CSV (for example a simulation
without a fabricated sample), the seven official tables still follow their own
scope rules, but the semantic block is STILL required and must describe the
study: set study_type and data_provenance from the text and capture the printed
simulated layers, conditions and metrics. Simulation or literature values are
semantic-only observations and MUST NOT be added to the seven official CSV
tables.
"""


def _json_bytes(value):
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def _bounded_text(value, limit=500):
    """Bounded, control-free text for artifact errors (never a raw response)."""
    return CONTROL.sub(" ", str(value))[:limit]


def _redacted_text(value, limit=300):
    """Bounded error text with secret-looking forms replaced."""
    text = BEARER_FORM.sub("Bearer <redacted>", str(value))
    text = SECRET_FORM.sub(r"\1=<redacted>", text)
    return _bounded_text(text, limit)


def _write_json(path, value):
    """Write one JSON file atomically inside its existing parent directory."""
    path = Path(path)
    data = (
        json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    if len(data) > MAX_ARTIFACT_BYTES:
        raise ValueError("Артефакт LLM-оболочки превышает допустимый размер.")
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("wb", dir=path.parent, prefix=f".{path.name}-", delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(data)
    try:
        os.replace(temporary, path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise
    return data


def _clear_artifacts(folder):
    """Remove only this module's fixed artifact names; never touch result.json."""
    for name in ARTIFACT_NAMES:
        (folder / name).unlink(missing_ok=True)


def _load_passport(bundle):
    path = bundle / "result.json"
    try:
        raw = path.read_bytes()
    except OSError:
        raise ValueError("В каталоге разбора нет result.json.") from None
    if len(raw) > MAX_BUNDLE_BYTES:
        raise ValueError("scripts-паспорт превышает допустимый размер.")
    try:
        result = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError):
        raise ValueError("scripts-паспорт не является корректным JSON.") from None
    if not isinstance(result, dict):
        raise ValueError("scripts-паспорт должен быть JSON-объектом.")
    run = result.get("run")
    if not isinstance(run, dict) or run.get("mode") != "scripts":
        raise ValueError("LLM-оболочка работает только с scripts-паспортом.")
    if not isinstance(result.get("sources"), list) or not isinstance(result.get("candidates"), list):
        raise ValueError("scripts-паспорт не содержит источников или кандидатов.")
    document = result.get("document")
    if not isinstance(document, dict):
        raise ValueError("scripts-паспорт не содержит описания документа.")
    if not isinstance(document.get("publication_id"), str) or not document["publication_id"]:
        raise ValueError("scripts-паспорт не содержит publication_id.")
    if type(document.get("pages")) is not int or document["pages"] < 1:
        raise ValueError("scripts-паспорт не содержит числа страниц.")
    return result, raw


def _profile_for(result, profile):
    candidate = profile if profile is not None else result.get("profile")
    if not isinstance(candidate, dict):
        raise ValueError("Профиль недоступен: передайте его явно или сохраните в scripts-паспорте.")
    profile = compile_profile(candidate)
    if profile["id"] != result.get("profile_id") or profile["version"] != result.get("profile_version"):
        raise ValueError("Профиль не совпадает с scripts-паспортом.")
    return profile


def _document_for(result):
    document = result["document"]
    return {
        "publication_id": document["publication_id"],
        "sha256": document.get("sha256", ""),
        "pages": document["pages"],
        "sources": result["sources"],
        "warnings": list(result.get("warnings") or []),
        "parser_output": result.get("parser_output") or {},
    }


def _source_index(document):
    sources = {}
    for source in document["sources"]:
        if not isinstance(source, dict):
            raise ValueError("Источник должен быть объектом.")
        sid = source.get("id")
        if type(sid) is not int or sid in sources:
            raise ValueError("Идентификаторы источников должны быть уникальными целыми.")
        if type(source.get("page")) is not int or not 1 <= source["page"] <= document["pages"]:
            raise ValueError("Источник указывает на недопустимую страницу.")
        if not isinstance(source.get("quote"), str):
            raise ValueError("Цитата источника должна быть строкой.")
        sources[sid] = source
    return sources


def _parse_response(response, max_response_bytes):
    """Accept only a bounded JSON object; a string is parsed as JSON."""
    if isinstance(response, str):
        if len(response.encode("utf-8")) > max_response_bytes:
            raise ValueError("Ответ модели превышает допустимый размер.")
        try:
            response = json.loads(response)
        except (UnicodeError, json.JSONDecodeError):
            raise ValueError("Ответ модели не является корректным JSON.") from None
    if not isinstance(response, dict):
        raise ValueError("Ответ модели должен быть JSON-объектом.")
    if len(_json_bytes(response)) > max_response_bytes:
        raise ValueError("Ответ модели превышает допустимый размер.")
    return response


def _check_backend(backend, allow_network=False):
    if not callable(getattr(backend, "complete", None)):
        raise ValueError("Бэкенд должен реализовывать complete(stage, payload).")
    if getattr(backend, "network", False) is not False and not allow_network:
        raise ValueError("Сетевой бэкенд требует явного разрешения сети (allow_network).")


def _check_budget(payload, max_input_chars):
    if len(json.dumps(payload, ensure_ascii=False, allow_nan=False)) > max_input_chars:
        raise ValueError("Превышен бюджет входа; данные не обрезаны.")


def _semantic_prompt(prompt, max_input_chars):
    """Append the optional semantic request to the solar system message."""
    prompt = [dict(message) for message in prompt]
    prompt[0]["content"] = prompt[0]["content"] + SEMANTIC_INSTRUCTION
    if sum(len(message["content"]) for message in prompt) > max_input_chars:
        raise ValueError("Превышен бюджет запроса вместе с semantic-блоком; документ не обрезан.")
    return prompt


def _semantic_error(kind, index, error):
    item = {"error": _bounded_text(error, 200)}
    if kind is not None:
        item["record"] = kind
    if index is not None:
        item["index"] = index
    return item


def _semantic_anchor(item, sources):
    """One validated ``{source_id, page, quote}`` anchor, or ``(None, reason)``.

    The anchor is only structurally and literally valid here: the quote must be
    a real substring of the cited source on the cited page. Whether the anchor
    actually supports a field value is checked separately, per field.
    """
    if not isinstance(item, dict) or set(item) != {"source_id", "page", "quote"}:
        return None, "ссылка должна содержать ровно source_id, page и quote."
    sid, page, quote = item.get("source_id"), item.get("page"), item.get("quote")
    if type(sid) is not int or sid not in sources:
        return None, "неизвестный source_id."
    source = sources[sid]
    if type(page) is not int or page != source["page"]:
        return None, "страница не совпадает со страницей источника."
    if not isinstance(quote, str) or not quote.strip() or len(quote) > MAX_QUOTE:
        return None, "нужна непустая цитата в пределах лимита."
    if clean(quote) not in clean(source["quote"]):
        return None, "цитата отсутствует в указанном источнике."
    return {"source_id": sid, "page": page, "quote": quote}, None


def _semantic_confirmed(field, value, anchor):
    """True when ``value`` is literally printed in this field's own anchor.

    Numbers and text both go through ``solar_facts.has_value``, which matches a
    complete token: ``12`` is not confirmed by ``112``, and ``SnO2`` does not
    match inside a longer word. Units go through ``has_unit``. A quote that is
    merely present in the source is not enough on its own.
    """
    if field in ("unit", "thickness_unit"):
        return has_unit(value, [{"quote": anchor["quote"]}])
    return has_value(value, anchor["quote"])


def _semantic_record(kind, row, sources):
    """One validated semantic record, or ``(None, reason)`` to quarantine it.

    Every nonempty field needs its own anchor in ``field_sources``, and that
    anchor must literally print the field value. A record without its required
    fields, with a field that has no anchor, or with an anchor that does not
    print the value is quarantined instead of being partly accepted.
    """
    if not isinstance(row, dict):
        return None, "запись должна быть объектом."
    allowed = set(SEMANTIC_RECORD_FIELDS[kind])
    unknown = set(row) - allowed - {"field_sources", "page", "carrier"}
    if unknown:
        return None, f"неизвестные поля {sorted(unknown)}."
    values = {}
    for field in SEMANTIC_RECORD_FIELDS[kind]:
        value = row.get(field, "")
        if not isinstance(value, str) or len(value) > MAX_TEXT:
            return None, f"{field} должен быть строкой."
        values[field] = value.strip()
    for field in SEMANTIC_REQUIRED_FIELDS[kind]:
        if not values[field]:
            return None, f"нужно непустое поле {field}."
    raw_sources = row.get("field_sources")
    if not isinstance(raw_sources, dict):
        return None, "нужен field_sources с отдельной ссылкой на каждое поле."
    unknown = set(raw_sources) - allowed
    if unknown:
        return None, f"field_sources: неизвестные поля {sorted(unknown)}."
    field_sources = {}
    for field, item in raw_sources.items():
        anchor, error = _semantic_anchor(item, sources)
        if anchor is None:
            return None, f"field_sources.{field}: {error}"
        field_sources[field] = anchor
    # Each accepted field and unit needs its own literal support.
    for field in SEMANTIC_RECORD_FIELDS[kind]:
        if not values[field]:
            continue
        anchor = field_sources.get(field)
        if anchor is None:
            return None, f"{field}: нет собственной ссылки в field_sources."
        if not _semantic_confirmed(field, values[field], anchor):
            return None, f"{field}: значение не напечатано в собственной цитате."
    # ``carrier`` and ``page`` are code-owned: a model value is ignored. The
    # record page comes from the first required field anchor, not from the model.
    first = SEMANTIC_REQUIRED_FIELDS[kind][0]
    return {
        "kind": kind, **values, "field_sources": field_sources,
        "carrier": "semantic_only", "page": field_sources[first]["page"],
    }, None


def _semantic_claim(field, value, anchor):
    """Confirm one top-level claim against its own metadata anchor.

    Returns ``(reported_value, error)``. ``unknown`` is an explicit non-claim
    and needs no evidence. Any other value needs its own anchor in
    ``metadata_evidence``; where a marker lexicon exists the anchor must also
    literally speak about the phenomenon. An unconfirmed or invalid claim is
    reported as ``"unknown"`` and paired with an error, so the artifact never
    presents a model claim as validated. This is a literal gate: a marker shows
    the quote is about the topic, it does not prove the scientific judgement.
    """
    if value == "unknown":
        return "unknown", None
    if anchor is None:
        return "unknown", f"{field}: нет собственной ссылки в metadata_evidence."
    markers = SEMANTIC_CLAIM_MARKERS.get(field, {}).get(value)
    if markers:
        text = clean(anchor["quote"]).lower()
        if not any(marker in text for marker in markers):
            return "unknown", f"{field}: собственная цитата не подтверждает значение."
    return value, None


def validate_solar_semantic(candidate, document):
    """Validate the optional model ``semantic`` block of one solar response.

    The result always has the fixed shape: ``schema_version``,
    ``publication_id``, ``status``, the three enum strings, ``scope_reason``,
    ``metadata_evidence``, ``records`` and ``errors``. It never raises for a
    malformed block and never rejects the whole interpretation: a bad record is
    quarantined into ``errors`` and omitted, while good records are kept.

    ``status`` is ``missing`` when the block is absent (backward compatible),
    ``rejected`` when the block is unusable as a whole (not an object, or a
    publication id that does not match the document), ``partial`` when some
    records or claims were dropped, and ``ok`` otherwise.

    This is a literal-evidence gate, not a scientific quality check. A record
    field is accepted only when its value and unit are printed in that field's
    own anchor; a top-level claim is accepted only with its own metadata anchor,
    and a claim without one becomes ``unknown``. Every output record carries
    ``carrier="semantic_only"`` and a ``page`` assigned by code; a model-supplied
    ``carrier`` or ``page`` is ignored.
    """
    sources = _source_index(document)
    pid = document["publication_id"]
    result = {
        "schema_version": SEMANTIC_SCHEMA_VERSION,
        "publication_id": pid,
        "status": "missing",
        "study_type": None,
        "data_provenance": None,
        "thematic_relevance": None,
        "scope_reason": None,
        "metadata_evidence": {},
        "records": {kind: [] for kind in SEMANTIC_RECORD_KINDS},
        "errors": [],
    }
    semantic = candidate.get("semantic") if isinstance(candidate, dict) else None
    if semantic is None:
        return result
    if not isinstance(semantic, dict):
        return {**result, "status": "rejected",
                "errors": [_semantic_error(None, None, "semantic должен быть объектом.")]}

    errors = []
    if semantic.get("publication_id") != pid:
        return {**result, "status": "rejected",
                "errors": [_semantic_error(
                    None, None, "publication_id semantic не совпадает с документом.")]}

    metadata = {}
    raw_metadata = semantic.get(SEMANTIC_METADATA_EVIDENCE, {})
    if not isinstance(raw_metadata, dict):
        errors.append(_semantic_error(None, None, "metadata_evidence должен быть объектом."))
    else:
        unknown = set(raw_metadata) - set(SEMANTIC_METADATA_FIELDS)
        if unknown:
            errors.append(_semantic_error(
                None, None, f"metadata_evidence: неизвестные поля {sorted(unknown)}."))
        for field, item in raw_metadata.items():
            if field not in SEMANTIC_METADATA_FIELDS:
                continue
            anchor, error = _semantic_anchor(item, sources)
            if anchor is None:
                errors.append(_semantic_error(
                    None, None, f"metadata_evidence.{field}: {error}"))
            else:
                metadata[field] = anchor

    for field, allowed in SEMANTIC_ENUMS.items():
        value = semantic.get(field)
        if not (isinstance(value, str) and value in allowed):
            # An invalid value is not a claim: report it as unknown with an issue.
            result[field] = "unknown"
            errors.append(_semantic_error(None, None, f"{field}: недопустимое значение."))
            continue
        confirmed, error = _semantic_claim(field, value, metadata.get(field))
        result[field] = confirmed
        if error is not None:
            errors.append(_semantic_error(None, None, error))
        elif field in metadata:
            result["metadata_evidence"][field] = metadata[field]

    reason = semantic.get("scope_reason")
    if reason is not None and not isinstance(reason, str):
        errors.append(_semantic_error(None, None, "scope_reason должен быть строкой."))
    elif isinstance(reason, str) and reason.strip():
        if "scope_reason" in metadata:
            result["scope_reason"] = reason.strip()
            result["metadata_evidence"]["scope_reason"] = metadata["scope_reason"]
        else:
            errors.append(_semantic_error(
                None, None, "scope_reason: нет собственной ссылки в metadata_evidence."))

    raw_records = semantic.get("records", {})
    if not isinstance(raw_records, dict):
        errors.append(_semantic_error(None, None, "records должен быть объектом."))
        raw_records = {}
    else:
        unknown = set(raw_records) - set(SEMANTIC_RECORD_KINDS)
        if unknown:
            errors.append(_semantic_error(
                None, None, f"records: неизвестные списки {sorted(unknown)}."))

    for kind in SEMANTIC_RECORD_KINDS:
        rows = raw_records.get(kind, [])
        if not isinstance(rows, list):
            errors.append(_semantic_error(kind, None, "ожидался список записей."))
            continue
        if len(rows) > MAX_SEMANTIC_RECORDS:
            errors.append(_semantic_error(
                kind, None, f"список обрезан до {MAX_SEMANTIC_RECORDS} записей."))
            rows = rows[:MAX_SEMANTIC_RECORDS]
        for index, row in enumerate(rows):
            record, error = _semantic_record(kind, row, sources)
            if record is None:
                errors.append(_semantic_error(kind, index, error))
            else:
                result["records"][kind].append(record)

    result["errors"] = errors[:MAX_SEMANTIC_ERRORS]
    result["status"] = "ok" if not errors else "partial"
    return result


def _draft_absent(document, status="missing", code="candidate_absent", error=None):
    """Fixed-shape draft for an absent, skipped or failed advisory validation.

    ``status`` and ``code`` follow ``solar_draft.empty_draft``; the optional
    ``error`` text is bounded and sanitized, so a validator exception never
    carries a raw document body or a provider secret into the artifact.
    """
    try:
        import solar_draft
        draft = solar_draft.empty_draft(document, status=status, code=code)
    except Exception:  # advisory module absent: keep the documented shape
        draft = {
            "schema_version": DRAFT_SCHEMA_VERSION,
            "publication_id": document["publication_id"],
            "status": status, "publication_state": status, "blocked_by": [],
            "tables": {}, "counts": {"rows": 0, "accepted": 0, "quarantined": 0,
                                     "tables": {}},
        }
    if error is not None:
        draft["error"] = _redacted_text(error)
    return draft


def validate_solar_draft(candidate, document):
    """Advisory ``solar-draft-1`` view of the parsed candidate; never raises.

    The strict ``solar_facts.finalize`` contract is all-or-nothing, so one bad
    row hides every acceptable row. This helper runs the separate draft
    validator to keep a bounded, per-row view of what the model actually
    returned. It is diagnostic only: nothing from it is merged into the
    interpretation, the passport or the official CSV.

    A ``RuntimeError`` (or any other validator failure) becomes a sanitized
    ``status="error"`` draft and the caller's finalize still runs. The artifact
    write happens in the caller, so an I/O error is not hidden here.
    """
    try:
        import solar_draft
    except Exception as error:
        return _draft_absent(document, "error", "validator_error",
                             f"Модуль черновика недоступен: {type(error).__name__}.")
    try:
        return solar_draft.validate_draft(candidate, document)
    except Exception as error:  # advisory only: never reject the interpretation
        return _draft_absent(document, "error", "validator_error",
                             f"{type(error).__name__}: {error}")


def _draft_state(draft):
    """Bounded status and counts for the manifest, from the draft artifact."""
    status = draft.get("status") if isinstance(draft.get("status"), str) else "error"
    counts = draft.get("counts") if isinstance(draft.get("counts"), dict) else {}
    summary = {}
    for name in ("rows", "accepted", "quarantined"):
        value = counts.get(name)
        summary[name] = value if type(value) is int and value >= 0 else 0
    return status, summary



def _audit_messages(payload, sources, max_input_chars):
    """Bounded audit prompt built from the read-only draft and its sources."""
    content = json.dumps({
        "draft": payload["draft"],
        "sources": [
            {key: source[key] for key in ("id", "page", "quote", "kind") if key in source}
            for source in sources
        ],
        "response_schema": {
            "type": "object",
            "properties": {"findings": {"type": "array"}},
            "required": ["findings"],
        },
    }, ensure_ascii=False, allow_nan=False)
    if len(content) > max_input_chars:
        raise ValueError("Превышен бюджет входа аудита; данные не обрезаны.")
    return [{"role": "system", "content": AUDIT_SYSTEM},
            {"role": "user", "content": content}]


def _solar_records(tables, evidence):
    """Record-level observations for solar tables; evidence stays record-scoped."""
    records = []
    for table, rows in tables.items():
        if table == "issues":
            continue
        key = SOLAR_KEYS[table]
        for row in rows:
            anchors = evidence.get(f"{table}/{row[key]}", [])
            refs = [{"id": source["id"], "page": source["page"]} for source in anchors]
            records.append({
                "entity": table, "id": row[key],
                "fields": {
                    name: {"raw": value or None, "evidence_refs": list(refs)}
                    for name, value in row.items()
                },
                "evidence_scope": "record",
            })
    return records


def _unresolved_candidate(candidate):
    """True unless the candidate carries an explicit non-candidate state.

    The rule follows the candidate contract of ``extraction_core``: a script
    candidate has ``status == "candidate"`` and ``context_status ==
    "unresolved"``. A missing status is treated as unresolved, because an
    unknown state is not evidence that the candidate became a record. The shell
    never upgrades a candidate into a record.
    """
    if not isinstance(candidate, dict):
        return True
    return (candidate.get("status") in (None, "candidate")
            or candidate.get("context_status") in (None, "unresolved"))


def _attribution_inventory(solar):
    """The bounded attribution inventory from ``finalize``, or ``None``.

    The inventory is produced by ``solar_facts.attribution_review``; this shell
    only forwards it. An older finalize without the key, or a rejected
    interpretation, has no inventory and must not look verified.
    """
    extraction = solar.get("extraction") if isinstance(solar, dict) else None
    inventory = extraction.get("attribution") if isinstance(extraction, dict) else None
    return inventory if isinstance(inventory, dict) else None


def _attribution_summary(inventory):
    """Bounded state and counts for the manifest solar block.

    ``state`` is forwarded only when it is one of the two documented values;
    anything else, including a missing legacy inventory, is ``unknown``. The
    counts are forwarded as a shallow copy so the manifest never aliases the
    interpretation object.

    ``requires_review`` is always true here, and it is not derived from
    ``state``: ``state="none"`` only means no sensitive inventory field is
    nonempty. The scientific binding and completeness of the model rows are
    never verified by this shell, so review stays required in every case.
    """
    if not isinstance(inventory, dict):
        return "unknown", {}, True
    state = inventory.get("state")
    if state not in ("unverified", "none"):
        state = "unknown"
    counts = inventory.get("counts")
    counts = deepcopy(counts) if isinstance(counts, dict) else {}
    return state, counts, True


def solar_state(interpretation, candidates, *, status=None, export_status="skipped"):
    """Machine-readable scope and record state of one solar interpretation.

    The block reports only what the interpretation already contains: it never
    promotes candidates to records and never guesses a scope decision. When no
    verified publication row exists (interpretation skipped or rejected),
    ``in_scope`` and ``scope_reason`` stay ``None`` and the counts are zero,
    while ``interpretation_status`` says why.

    ``scientific_record_counts`` counts the rows the interpretation itself
    produced in the scientific tables; ``unresolved_candidate_count`` counts
    script candidates that stayed candidates, so it is not a measure of
    extraction quality.
    """
    candidates = list(candidates) if isinstance(candidates, (list, tuple)) else []
    if status is None:
        status = interpretation.get("status", "not_run") if isinstance(
            interpretation, dict) else "not_run"
    tables = (interpretation.get("tables") or {}) if status == "ok" else {}
    publications = tables.get("publications") or []
    publication = publications[0] if publications and isinstance(publications[0], dict) else {}
    # Only a string carries the template value; a boolean or a number is not
    # the "true"/"false" marker and stays unknown instead of being coerced.
    scope = publication.get("in_scope")
    in_scope = (
        {"true": True, "false": False}.get(scope.strip().lower())
        if isinstance(scope, str) else None
    )
    scope_reason = None
    if in_scope is False:
        reason = publication.get("exclusion_reason")
        scope_reason = reason.strip() if isinstance(reason, str) and reason.strip() else None
    inventory = interpretation.get("attribution") if isinstance(interpretation, dict) else None
    attribution_state, attribution_counts, attribution_review = _attribution_summary(inventory)
    return {
        "interpretation_status": status,
        "in_scope": in_scope,
        "scope_reason": scope_reason,
        "scientific_record_counts": {
            table: len(tables.get(table) or []) for table in SOLAR_RECORD_TABLES
        },
        "candidate_count": len(candidates),
        "unresolved_candidate_count": sum(
            1 for candidate in candidates if _unresolved_candidate(candidate)
        ),
        # Process and sample-size claims the model filled in. This is an
        # inventory of sensitive fields, not a verdict: state "none" only means
        # no such field is nonempty, and requires_review stays true for every
        # model row. The two verification keys are invariants of this shell and
        # do not depend on the inventory.
        "attribution_state": attribution_state,
        "attribution_counts": attribution_counts,
        "requires_review": attribution_review,
        "scientific_binding_verification": "not_verified",
        "scientific_completeness_verification": "not_verified",
        "official_csv_written": False,
        "export_status": export_status,
    }


class MockBackend:
    """Deterministic stand-in for a model; no network, no key, no invention.

    Interpretation reuses only script candidates whose value was already
    verified against one source. Audit re-reports deterministic validation
    issues as suspected findings with source references.
    """

    name = "deterministic-mock"
    network = False

    def complete(self, stage, payload):
        if not isinstance(payload, dict):
            raise ValueError("Запрос к бэкенду должен быть объектом.")
        if stage == "interpret":
            return self.interpret(payload)
        if stage == "audit":
            return self.audit(payload)
        raise ValueError("Неизвестная стадия бэкенда.")

    def interpret(self, payload):
        profile = payload["profile"]
        sources = {source["id"]: source for source in payload["sources"]}
        rows = {table: {} for table in profile["tables"]}
        for candidate in payload["candidates"]:
            field = candidate.get("field") if isinstance(candidate, dict) else None
            if not isinstance(field, str) or "." not in field:
                continue
            table, name = field.split(".", 1)
            if table not in rows or name not in profile["tables"][table]["fields"]:
                continue
            sid = candidate.get("source_id")
            source = sources.get(sid)
            if source is None:
                continue
            row = rows[table].setdefault(sid, {"key": f"M{sid}", "values": {}, "fields": {}})
            row["values"].setdefault(name, str(candidate["raw_value"]))
            row["fields"].setdefault(name, {
                "source_id": sid, "quote": source["quote"], "raw_unit": "",
            })
        candidate = {}
        for table, table_rows in rows.items():
            key = profile["tables"][table]["key"]
            candidate[table] = [
                {key: table_rows[sid]["key"], **table_rows[sid]["values"],
                 "field_sources": table_rows[sid]["fields"]}
                for sid in sorted(table_rows)
            ]
        return candidate

    def audit(self, payload):
        draft = payload["draft"]
        sources = {source["id"]: source for source in payload["sources"]}
        records = {(record["entity"], record["id"]): record for record in draft["records"]}
        findings = []
        for issue in draft["issues"]:
            record = records.get((issue.get("table"), issue.get("record_id")))
            if record is None:
                continue
            field = issue.get("field", "")
            fields = record.get("fields") or {}
            observation = fields.get(field) if isinstance(fields, dict) else None
            refs = list((observation or {}).get("evidence_refs") or [])
            if not refs:
                for other in fields.values():
                    refs = list((other or {}).get("evidence_refs") or [])
                    if refs:
                        break
            evidence = [
                {"source_id": ref["id"], "page": sources[ref["id"]]["page"]}
                for ref in refs[:MAX_EVIDENCE] if ref.get("id") in sources
            ]
            if not evidence:
                continue
            findings.append({
                "table": issue["table"], "record_id": issue["record_id"], "field": field,
                "rule": issue.get("rule") or "llm_audit",
                "description": issue.get("description", ""), "evidence": evidence,
            })
            if len(findings) >= MAX_FINDINGS:
                break
        return {"findings": findings}


class ApiBackend:
    """Opt-in network backend over a generic [OI]-compatible endpoint.

    Configuration comes from ``llm_provider.config_from_env`` only; the key is
    never stored in artifacts, logs or the job. ``complete`` forwards the
    prompt that the shell already recorded in ``request.json``. Transport
    timeout and retries come from ``OPENAI_LLM_TIMEOUT``/``OPENAI_LLM_RETRIES``
    and can still be overridden by explicit keyword options.
    """

    name = "api"
    network = True

    def __init__(self, config=None, **options):
        self.config = config if config is not None else llm_provider.config_from_env()
        self.model = self.config["model"]
        self.options = {**llm_provider.transport_options(config=self.config), **options}

    def complete(self, stage, payload):
        messages = payload.get("messages")
        if not isinstance(messages, list) or not messages:
            raise ValueError("Для api-бэкенда нужны сообщения запроса.")
        return llm_provider.chat(messages, self.config, **self.options)


def _validate_findings(response, draft, sources):
    """All-or-nothing audit validation against the read-only draft and sources."""
    if set(response) != {"findings"}:
        raise ValueError("Аудит должен вернуть ровно объект findings.")
    rows = response["findings"]
    if not isinstance(rows, list) or len(rows) > MAX_FINDINGS:
        raise ValueError("Ожидался ограниченный список findings.")
    records = {(record["entity"], record["id"]): record for record in draft["records"]}
    findings = []
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise ValueError(f"finding {index}: ожидался объект.")
        unknown = set(row) - {"table", "record_id", "field", "rule", "description", "evidence"}
        if unknown:
            raise ValueError(f"finding {index}: неизвестные поля {sorted(unknown)}.")
        table, record_id, field = row.get("table"), row.get("record_id"), row.get("field")
        if not all(isinstance(value, str) and value for value in (table, record_id, field)):
            raise ValueError(f"finding {index}: нужны table, record_id и field.")
        record = records.get((table, record_id))
        if record is None:
            raise ValueError(f"finding {index}: запись отсутствует в черновике.")
        if field not in (record.get("fields") or {}):
            raise ValueError(f"finding {index}: поле отсутствует в записи.")
        description = row.get("description")
        if not isinstance(description, str) or not description.strip() or len(description) > MAX_TEXT:
            raise ValueError(f"finding {index}: нужно описание в пределах {MAX_TEXT} символов.")
        rule = row.get("rule", "llm_audit")
        if not isinstance(rule, str) or not RULE.fullmatch(rule):
            raise ValueError(f"finding {index}: некорректное имя правила.")
        evidence_rows = row.get("evidence")
        if not isinstance(evidence_rows, list) or not 1 <= len(evidence_rows) <= MAX_EVIDENCE:
            raise ValueError(f"finding {index}: нужно от 1 до {MAX_EVIDENCE} ссылок.")
        evidence = []
        for item in evidence_rows:
            if not isinstance(item, dict) or set(item) - {"source_id", "page", "quote"}:
                raise ValueError(f"finding {index}: некорректная ссылка на источник.")
            sid = item.get("source_id")
            if type(sid) is not int or sid not in sources:
                raise ValueError(f"finding {index}: неизвестный source_id.")
            source = sources[sid]
            page = item.get("page", source["page"])
            if type(page) is not int or page != source["page"]:
                raise ValueError(f"finding {index}: страница не совпадает с источником.")
            quote = item.get("quote")
            if quote is not None:
                if not isinstance(quote, str) or not quote.strip() or len(quote) > MAX_QUOTE:
                    raise ValueError(f"finding {index}: некорректная цитата.")
                if clean(quote) not in clean(source["quote"]):
                    raise ValueError(f"finding {index}: цитата отсутствует в источнике.")
            evidence.append({"source_id": sid, "page": page, **({"quote": quote} if quote else {})})
        findings.append({
            "table": table, "record_id": record_id, "field": field, "rule": rule,
            "description": description, "status": "suspected", "evidence": evidence,
        })
    return findings


def summarize(manifest):
    """Compact status for the parse job and the CLI summary.

    The whole solar block is preserved as-is, so additive keys such as the
    attribution state and the verification invariants reach the caller without
    a second whitelist that could silently drop them.
    """
    stages = {stage["name"]: stage["status"] for stage in manifest["stages"]}
    status = manifest.get("status") or (
        "rejected" if "rejected" in stages.values() else "ok")
    summary = {
        "status": status, "stages": stages, "counts": dict(manifest.get("counts", {})),
        "mode": manifest.get("mode", ""), "backend": dict(manifest.get("backend", {})),
    }
    if isinstance(manifest.get("solar"), dict):
        summary["solar"] = dict(manifest["solar"])
    return summary


def run_bundle(bundle, *, profile=None, backend=None, mode="mock", allow_network=False,
               max_input_chars=DEFAULT_INPUT_CHARS, max_response_bytes=MAX_RESPONSE_BYTES):
    """Run the LLM shell over one published scripts-only parse bundle.

    ``bundle`` is the directory that already contains ``result.json``. The
    function returns the manifest and writes artifacts under ``bundle/llm``.
    A rejected interpretation or audit is recorded in the manifest; it is not
    raised as an exception. Configuration and I/O errors still raise.

    ``mode="api"`` sends the recorded prompts to a generic [OI]-compatible
    endpoint and is refused unless ``allow_network`` is true and the
    environment provides a key and a model.
    """
    if mode not in MODES:
        raise ValueError("Поддерживаются режимы mock и api.")
    if type(max_input_chars) is not int or not 1 <= max_input_chars <= MAX_INPUT_CHARS:
        raise ValueError("Некорректный бюджет входа.")
    if type(max_response_bytes) is not int or not 1 <= max_response_bytes <= MAX_RESPONSE_BYTES:
        raise ValueError("Некорректный лимит ответа модели.")
    if mode == "api" and not allow_network:
        raise ValueError("Режим api требует явного разрешения сети (allow_network).")
    bundle = Path(bundle).resolve()
    result, passport_bytes = _load_passport(bundle)
    passport_sha256 = sha256(passport_bytes).hexdigest()
    profile = _profile_for(result, profile)
    document = _document_for(result)
    sources = _source_index(document)
    candidates = result["candidates"]
    if backend is None:
        backend = ApiBackend() if mode == "api" else MockBackend()
    _check_backend(backend, allow_network=allow_network)
    folder = bundle / "llm"
    _clear_artifacts(folder)
    artifacts = {}
    stages = []

    def record(name, status, used=(), **extra):
        stage = {"name": name, "status": status, "artifacts": list(used)}
        stage.update(extra)
        stages.append(stage)

    def artifact(name, value):
        data = _write_json(folder / name, value)
        artifacts[name] = {"sha256": sha256(data).hexdigest(), "bytes": len(data)}

    artifact("sources.json", {
        "schema_version": REVISION, "stage": "sources", "status": "ok",
        "publication_id": document["publication_id"], "document_sha256": document["sha256"],
        "pages": document["pages"], "source_count": len(sources),
        "sources_sha256": sha256(_json_bytes(document["sources"])).hexdigest(),
        "candidate_count": len(candidates),
        "candidates_sha256": sha256(_json_bytes(candidates)).hexdigest(),
    })
    record("sources", "ok", ["sources.json"])

    def reject(error_text):
        """Record a rejected interpretation (backend or validation failure)."""
        nonlocal interpretation
        interpretation = {
            "schema_version": REVISION, "stage": "interpret", "status": "rejected",
            "error": error_text,
        }
        artifact("interpretation.json", interpretation)
        used = ["request.json", "interpretation.json"]
        if RESPONSE_ARTIFACT in artifacts:
            used.append(RESPONSE_ARTIFACT)
        record("interpret", "rejected", used, error=interpretation["error"])

    interpretation = None
    request_artifact = None
    network_attempted = False
    semantic = None
    draft = None
    try:
        prompt = model_messages(document, profile, candidates, max_input_chars)
        if profile.get("adapter") == "solar_csv":
            prompt = _semantic_prompt(prompt, max_input_chars)
    except ValueError as error:
        interpretation = {
            "schema_version": REVISION, "stage": "interpret", "status": "rejected",
            "error": _bounded_text(error),
        }
        artifact("interpretation.json", interpretation)
        record("interpret", "rejected", ["interpretation.json"],
               error=interpretation["error"])
    else:
        adapter = profile.get("adapter", "fields")
        request_artifact = {
            "schema_version": REVISION, "stage": "interpret",
            "profile_id": profile["id"], "profile_version": profile["version"],
            "adapter": adapter,
            "prompt_sha256": sha256(_json_bytes(prompt)).hexdigest(),
            "input_chars": sum(len(message["content"]) for message in prompt),
            "max_input_chars": max_input_chars, "messages": prompt,
        }
        artifact("request.json", request_artifact)
        if adapter == "solar_csv" and mode == "mock":
            record("interpret", "skipped", ["request.json"],
                   reason="Mock-оболочка не формирует предметные таблицы solar; для них нужен режим api.")
        else:
            try:
                payload = {
                    "stage": "interpret", "adapter": adapter,
                    "profile": profile, "document": {
                        "publication_id": document["publication_id"], "pages": document["pages"],
                    },
                    "sources": document["sources"], "candidates": candidates,
                    "messages": prompt,
                }
                network_attempted = getattr(backend, "network", False) is True
                response = backend.complete("interpret", payload)
                candidate = _parse_response(response, max_response_bytes)
            except ValueError as error:
                reject(_bounded_text(error))
            except Exception as error:  # never leak provider details into artifacts
                reject(f"{type(error).__name__}: неожиданная ошибка бэкенда.")
            else:
                if adapter == "solar_csv":
                    # Keep the bounded parsed candidate before semantic
                    # extraction and finalize, so a later rejection can be
                    # diagnosed from what the model actually returned. Only the
                    # parsed object is stored: no keys, headers or config. This
                    # write is deliberately outside the rejection
                    # classification below: a persistence failure (OSError or a
                    # non-finite value) is an artifact error, not a rejected
                    # model response.
                    artifact(RESPONSE_ARTIFACT, {
                        "schema_version": REVISION, "stage": "interpret",
                        "publication_id": document["publication_id"],
                        "adapter": adapter, "response": candidate,
                    })
                try:
                    if adapter == "solar_csv":
                        # The semantic block is validated separately and removed
                        # before the seven-table contract sees the response.
                        semantic = validate_solar_semantic(candidate, document)
                        candidate.pop("semantic", None)
                        # The advisory per-row draft is computed before the strict
                        # contract runs, so a rejection still has a diagnosable
                        # view. It never feeds finalize, the tables or the CSV.
                        draft = validate_solar_draft(candidate, document)
                        solar = finalize(
                            candidate, document,
                            model=getattr(backend, "model", "") or mode)
                        tables = solar["tables"]
                        records = _solar_records(tables, solar["evidence"])
                        issues = tables["issues"]
                    else:
                        records, tables, issues = validate_fields(candidate, document, profile)
                except ValueError as error:
                    reject(_bounded_text(error))
                except Exception as error:  # never leak provider details into artifacts
                    reject(f"{type(error).__name__}: неожиданная ошибка бэкенда.")
                else:
                    interpretation = {
                        "schema_version": REVISION, "stage": "interpret", "status": "ok",
                        "adapter": adapter, "tables": tables, "records": records, "issues": issues,
                        "counts": {"tables": len(tables), "records": len(records), "issues": len(issues)},
                    }
                    if adapter == "solar_csv":
                        # Additive metadata: the bounded inventory of process and
                        # sample-size claims produced by finalize. It changes no
                        # table, record or status, and it is not a verification.
                        inventory = _attribution_inventory(solar)
                        if inventory is not None:
                            interpretation["attribution"] = inventory
                    artifact("interpretation.json", interpretation)
                    used = ["request.json", "interpretation.json"]
                    if RESPONSE_ARTIFACT in artifacts:
                        used.append(RESPONSE_ARTIFACT)
                    record("interpret", "ok", used)

    if profile.get("adapter") == "solar_csv":
        if semantic is None:
            semantic = validate_solar_semantic({}, document)
        artifact(SEMANTIC_ARTIFACT, semantic)
        # Written outside the interpretation try-block: a write failure must
        # surface as an I/O error, not be recorded as a validator rejection.
        if draft is None:
            draft = _draft_absent(document)
        artifact(DRAFT_ARTIFACT, draft)

    after_bytes = (bundle / "result.json").read_bytes()
    after_sha256 = sha256(after_bytes).hexdigest()
    if after_sha256 != passport_sha256:
        raise ValueError("scripts-паспорт изменился во время работы LLM-оболочки.")
    record("passport", "ok", read_only=True, sha256=passport_sha256)

    audit = None
    if interpretation is None or interpretation["status"] != "ok":
        record("audit", "skipped", reason="Нет проверенного черновика для аудита.")
    else:
        try:
            payload = {
                "stage": "audit",
                "draft": {
                    "tables": interpretation["tables"], "records": interpretation["records"],
                    "issues": interpretation["issues"],
                },
                "sources": document["sources"],
            }
            if getattr(backend, "network", False) is True:
                payload["messages"] = _audit_messages(
                    payload, document["sources"], max_input_chars)
            else:
                _check_budget(payload, max_input_chars)
            network_attempted = network_attempted or getattr(backend, "network", False) is True
            response = backend.complete("audit", payload)
            findings = _validate_findings(_parse_response(response, max_response_bytes),
                                          interpretation, sources)
        except ValueError as error:
            audit = {
                "schema_version": REVISION, "stage": "audit", "status": "rejected",
                "error": _bounded_text(error),
            }
            artifact("audit.json", audit)
            record("audit", "rejected", ["audit.json"], error=audit["error"])
        except Exception as error:  # never leak provider details into artifacts
            audit = {
                "schema_version": REVISION, "stage": "audit", "status": "rejected",
                "error": f"{type(error).__name__}: неожиданная ошибка бэкенда.",
            }
            artifact("audit.json", audit)
            record("audit", "rejected", ["audit.json"], error=audit["error"])
        else:
            audit = {
                "schema_version": REVISION, "stage": "audit", "status": "ok",
                "findings": findings, "counts": {"findings": len(findings)},
            }
            artifact("audit.json", audit)
            record("audit", "ok", ["audit.json"])

    record("export", "skipped",
           reason="CSV собирает код; официальные CSV этим модулем не формируются и не меняются.")
    stage_status = {stage["name"]: stage["status"] for stage in stages}
    solar = (
        solar_state(interpretation, candidates,
                    status=stage_status.get("interpret", "not_run"),
                    export_status=stage_status.get("export", "not_run"))
        if profile.get("adapter") == "solar_csv" else None)
    if solar is not None and semantic is not None:
        # Semantic records are reported next to the scientific state; they are
        # never counted as scientific records and never written to CSV.
        solar["semantic_status"] = semantic["status"]
        solar["semantic_counts"] = {
            **{kind: len(semantic["records"][kind]) for kind in SEMANTIC_RECORD_KINDS},
            "errors": len(semantic["errors"]),
        }
        # The advisory draft is reported next to the scientific state. Its rows
        # are never counted as official records.
        draft_status, draft_counts = _draft_state(draft if draft is not None else {})
        solar["draft_status"] = draft_status
        solar["draft_counts"] = draft_counts

    counts = {
        "interpreted_records": len(interpretation["records"])
        if interpretation and interpretation["status"] == "ok" else 0,
        "validation_issues": len(interpretation["issues"])
        if interpretation and interpretation["status"] == "ok" else 0,
        "audit_findings": len(audit["findings"]) if audit and audit["status"] == "ok" else 0,
    }
    network_used = network_attempted
    backend_name = getattr(backend, "name", "custom")
    backend_model = getattr(backend, "model", "") if network_used else ""
    manifest = {
        "schema_version": REVISION, "mode": mode,
        "status": "rejected" if any(stage["status"] == "rejected" for stage in stages) else "ok",
        "backend": {
            "name": backend_name, "network": network_used,
            **({"model": backend_model} if backend_model else {}),
        },
        "profile": {
            "id": profile["id"], "version": profile["version"],
            "adapter": profile.get("adapter", "fields"),
        },
        "document": {
            "publication_id": document["publication_id"],
            "sha256": document["sha256"], "pages": document["pages"],
        },
        "limits": {
            "max_input_chars": max_input_chars, "max_response_bytes": max_response_bytes,
            "max_findings": MAX_FINDINGS, "max_evidence": MAX_EVIDENCE,
        },
        "stages": stages, "artifacts": artifacts, "counts": counts,
        **({"solar": solar} if solar is not None else {}),
        "guarantees": {
            "passport_sha256_before": passport_sha256,
            "passport_sha256_after": after_sha256,
            "passport_unchanged": passport_sha256 == after_sha256,
            "passport_mutated": False, "official_csv_written": False,
            "network_used": network_used,
        },
    }
    _write_json(folder / "manifest.json", manifest)
    return manifest
