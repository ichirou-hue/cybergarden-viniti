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
* official CSV export is a code-only stage and is not invoked here;
* artifacts live only under ``<bundle>/llm`` and are written atomically;
* a malformed or untrusted response becomes a recorded ``rejected`` stage, not
  a silent pass and not a crash;
* input and output sizes are bounded, and the document is never truncated.
"""

from hashlib import sha256
import json
import os
from pathlib import Path
import re
from tempfile import NamedTemporaryFile

from extraction_core import compile_profile, model_messages, validate_fields
from prompt_rules import DOCUMENT_RULES
from solar_facts import clean, finalize
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
ARTIFACT_NAMES = ("sources.json", "request.json", "interpretation.json", "audit.json", "manifest.json")
RULE = re.compile(r"[a-z][a-z0-9_]{0,39}")
CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
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
"""


def _json_bytes(value):
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def _bounded_text(value, limit=500):
    """Bounded, control-free text for artifact errors (never a raw response)."""
    return CONTROL.sub(" ", str(value))[:limit]


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
    """Compact status for the parse job and the CLI summary."""
    stages = {stage["name"]: stage["status"] for stage in manifest["stages"]}
    status = manifest.get("status") or (
        "rejected" if "rejected" in stages.values() else "ok")
    return {
        "status": status, "stages": stages, "counts": dict(manifest.get("counts", {})),
        "mode": manifest.get("mode", ""), "backend": dict(manifest.get("backend", {})),
    }


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

    interpretation = None
    request_artifact = None
    network_attempted = False
    try:
        prompt = model_messages(document, profile, candidates, max_input_chars)
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
                if adapter == "solar_csv":
                    solar = finalize(
                        candidate, document,
                        model=getattr(backend, "model", "") or mode)
                    tables = solar["tables"]
                    records = _solar_records(tables, solar["evidence"])
                    issues = tables["issues"]
                else:
                    records, tables, issues = validate_fields(candidate, document, profile)
            except ValueError as error:
                interpretation = {
                    "schema_version": REVISION, "stage": "interpret", "status": "rejected",
                    "error": _bounded_text(error),
                }
                artifact("interpretation.json", interpretation)
                record("interpret", "rejected", ["request.json", "interpretation.json"],
                       error=interpretation["error"])
            except Exception as error:  # never leak provider details into artifacts
                interpretation = {
                    "schema_version": REVISION, "stage": "interpret", "status": "rejected",
                    "error": f"{type(error).__name__}: неожиданная ошибка бэкенда.",
                }
                artifact("interpretation.json", interpretation)
                record("interpret", "rejected", ["request.json", "interpretation.json"],
                       error=interpretation["error"])
            else:
                interpretation = {
                    "schema_version": REVISION, "stage": "interpret", "status": "ok",
                    "adapter": adapter, "tables": tables, "records": records, "issues": issues,
                    "counts": {"tables": len(tables), "records": len(records), "issues": len(issues)},
                }
                artifact("interpretation.json", interpretation)
                record("interpret", "ok", ["request.json", "interpretation.json"])

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
