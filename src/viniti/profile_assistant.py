"""Structured, reviewable LLM suggestions for extraction profiles.

The assistant never edits a profile. It returns proposals with an entity, a
field, a type, an example, an explanation and, when the PDF really supports
it, a quote and a page. The client decides what to apply.

Built-in profiles are read-only. When the assistant is asked to change one,
``requires_copy`` is true and the UI must offer a user copy instead of a save.
"""

import json
import re

from .prompt_rules import DOCUMENT_RULES_RU
from .llm_provider import complete, resolve_config

FIELD_TYPES = {"text", "number", "boolean", "reference"}
MAX_SOURCES = 24
MAX_SUGGESTIONS = 8
MODES = {"suggest", "create", "audit"}
GENERIC_LABELS = {"", "unspecified", "unknown", "n/a", "none"}


def _text(value):
    return value if isinstance(value, str) else ""


def _fold(value):
    return re.sub(r"\s+", " ", _text(value)).strip().casefold()


def _profile_entities(profile):
    """Normalize the two profile shapes into ``[{name, fields}]``.

    The manual form sends ``types``; ``profiles_store`` profiles use
    ``tables``. Both are needed by the assistant.
    """
    entities = []
    if not isinstance(profile, dict):
        return entities
    if profile.get("adapter") == "solar_csv":
        # Solar declares its fields in the official adapter, not profile JSON.
        from .profiles_store import summary
        tables = summary(profile, source="builtin")["tables"]
        profile = {"tables": {table["name"]: {
            "key": table["key"],
            "fields": {field["name"]: field for field in table["fields"]},
        } for table in tables}}
    if isinstance(profile.get("types"), list):
        for entity in profile["types"][:30]:
            if not isinstance(entity, dict):
                continue
            fields = entity.get("fields", [])
            entities.append({
                "name": _text(entity.get("name"))[:100],
                "schema": "types",
                "fields": [{
                    "name": _text(field.get("name"))[:100],
                    "type": _text(field.get("type"))[:20],
                    "description": _text(field.get("description"))[:300],
                    "reference": _text(field.get("reference"))[:100],
                    "unit": _text(field.get("unit"))[:40],
                } for field in (fields[:50] if isinstance(fields, list) else [])
                    if isinstance(field, dict)],
            })
    elif isinstance(profile.get("tables"), dict):
        for name, table in list(profile["tables"].items())[:20]:
            if not isinstance(table, dict):
                continue
            fields = table.get("fields") or {}
            if not isinstance(fields, dict):
                continue
            entities.append({
                "name": _text(name)[:100],
                "key": _text(table.get("key"))[:100],
                "schema": "tables",
                "description": _text(table.get("description"))[:300],
                "fields": [{
                    "name": _text(field)[:100],
                    "type": _text(spec.get("type"))[:20] if isinstance(spec, dict) else "",
                    "description": _text(spec.get("description"))[:300] if isinstance(spec, dict) else "",
                    "reference": _text(spec.get("reference"))[:100] if isinstance(spec, dict) else "",
                    "unit": _text(spec.get("unit"))[:40] if isinstance(spec, dict) else "",
                    "required": spec.get("required") is True if isinstance(spec, dict) else False,
                } for field, spec in list(fields.items())[:50]],
            })
    return entities


def audit_profile(profile):
    """Deterministic duplicate and gap check. No model is involved."""
    entities = _profile_entities(profile)
    duplicates = []
    gaps = []
    seen_entities = {}
    for entity in entities:
        name = _fold(entity["name"])
        if not name:
            gaps.append({"entity": "", "field": "", "kind": "empty_entity",
                         "message": "У сущности нет названия."})
            continue
        if name in seen_entities:
            duplicates.append({"entity": entity["name"], "field": "", "kind": "duplicate_entity",
                               "message": f"Сущность «{entity['name']}» повторяется."})
        else:
            seen_entities[name] = entity
        if not entity["fields"]:
            gaps.append({"entity": entity["name"], "field": "", "kind": "empty_entity_fields",
                         "message": f"У сущности «{entity['name']}» нет полей."})
        seen_fields = {}
        strict = entity.get("schema") == "tables"
        for field in entity["fields"]:
            field_name = _fold(field["name"])
            if not field_name:
                gaps.append({"entity": entity["name"], "field": "", "kind": "empty_field",
                             "message": f"У сущности «{entity['name']}» есть поле без названия."})
                continue
            if field_name in seen_fields:
                duplicates.append({"entity": entity["name"], "field": field["name"],
                                   "kind": "duplicate_field",
                                   "message": f"Поле «{field['name']}» повторяется в «{entity['name']}»."})
            else:
                seen_fields[field_name] = field
            if not strict:
                continue
            if not field.get("type"):
                gaps.append({"entity": entity["name"], "field": field["name"], "kind": "missing_type",
                             "message": "Не указан тип поля."})
            elif field["type"] not in FIELD_TYPES | {"id", "enum"}:
                gaps.append({"entity": entity["name"], "field": field["name"], "kind": "unknown_type",
                             "message": f"Неизвестный тип «{field['type']}»."})
            if field.get("type") == "reference" and not field.get("reference"):
                gaps.append({"entity": entity["name"], "field": field["name"], "kind": "missing_reference",
                             "message": "Ссылочное поле не указывает на сущность."})
            if field.get("type") == "number" and not field.get("unit"):
                gaps.append({"entity": entity["name"], "field": field["name"], "kind": "missing_unit",
                             "message": "У числового поля нет единицы измерения."})
            if field.get("type") != "id" and not field.get("description"):
                gaps.append({"entity": entity["name"], "field": field["name"], "kind": "missing_description",
                             "message": "Нет описания поля: извлечение будет неоднозначным."})
    return {"duplicates": duplicates[:50], "gaps": gaps[:50]}


def _safe_sources(payload):
    sources = payload.get("sources", [])
    if not isinstance(sources, list):
        raise ValueError("Некорректные фрагменты PDF.")
    safe = []
    for item in sources[:MAX_SOURCES]:
        if not isinstance(item, dict):
            continue
        quote = item.get("quote", "")
        page = item.get("page")
        if isinstance(quote, str) and type(page) is int and 1 <= page <= 10000:
            safe.append({"page": page, "quote": quote[:600]})
    return safe


def _model_messages(mode, question, entities, sources, history):
    if mode == "create":
        task = ("Предложи структуру нового профиля извлечения: сущности и поля, которые "
                "подтверждаются переданными фрагментами. Значения и цитаты не нужны.")
    elif mode == "audit":
        task = ("Проверь профиль на дубли и пропуски. Верни только JSON с audit_notes: "
                "короткие замечания по структуре. Не предлагай значения из PDF.")
    else:
        task = ("Предложи добавить или изменить поля профиля. Если поле уже есть, "
                "используй action=modify и объясни изменение.")
    system = (
        "Ты помогаешь составить профиль извлечения научной статьи. "
        + DOCUMENT_RULES_RU + " " + task + " "
        "Верни только JSON-объект с массивом suggestions до 8 элементов. "
        "Каждый элемент: entity (название типа сущности), field (название поля), "
        "type (text, number, boolean или reference), action (add или modify), "
        "value (пример значения из PDF либо пустая строка), page (номер страницы либо null), "
        "quote (точная цитата из переданных фрагментов либо пустая строка), "
        "explanation (краткое пояснение, зачем поле). "
        "Не повторяй уже существующие поля без action=modify. Не выдумывай значения, цитаты и страницы. "
        "Если данных не хватает, верни пустой массив. Не добавляй пояснительный текст."
    )
    messages = [{"role": "system", "content": system}]
    if isinstance(history, list):
        for turn in history[-4:]:
            if isinstance(turn, dict) and turn.get("role") in {"user", "assistant"} \
                    and isinstance(turn.get("content"), str):
                messages.append({"role": turn["role"], "content": turn["content"][:1200]})
    messages.append({"role": "user", "content": json.dumps({
        "question": question.strip(),
        "mode": mode,
        "profile": entities,
        "sources": sources,
    }, ensure_ascii=False)})
    return messages


def _validate_suggestion(item, entities, sources, mode):
    if not isinstance(item, dict):
        return None
    entity, field, kind = item.get("entity"), item.get("field"), item.get("type")
    if not isinstance(entity, str) or not 1 <= len(entity.strip()) <= 100:
        return None
    if not isinstance(field, str) or not 1 <= len(field.strip()) <= 100 or not isinstance(kind, str) or kind not in FIELD_TYPES:
        return None
    action = item.get("action")
    if not isinstance(action, str) or action not in {"add", "modify"}:
        action = "add"
    quote = item.get("quote", "")
    page = item.get("page")
    if mode == "create":
        quote, page = "", None
    if not isinstance(quote, str) or not any(
        source["page"] == page and quote and quote in source["quote"] for source in sources
    ):
        quote, page = "", None
    value = item.get("value", "")
    if not isinstance(value, str) or (value and not quote) or (value and value not in quote):
        value = ""
    explanation = item.get("explanation", "")
    if not isinstance(explanation, str):
        explanation = ""
    current = {}
    for entity_item in entities:
        current[_fold(entity_item["name"])] = {
            _fold(field_item["name"]) for field_item in entity_item["fields"]
        }
    duplicate = action == "add" and _fold(field) in current.get(_fold(entity), set())
    return {
        "entity": entity.strip(),
        "field": field.strip(),
        "type": kind,
        "action": action,
        "value": value[:160],
        "example": value[:160],
        "page": page,
        "quote": quote[:600],
        "explanation": explanation.strip()[:400],
        "duplicate": duplicate,
    }


def suggest(payload, *, complete_fn=complete, profile_source=None):
    """Return validated suggestions and a deterministic profile audit.

    ``profile_source`` is ``"builtin"``, ``"user"`` or ``None``. For a built-in
    profile the response asks the client to create a copy before saving.
    """
    if not isinstance(payload, dict):
        raise ValueError("Нужен запрос с вопросом и фрагментами PDF.")
    question = payload.get("question")
    if not isinstance(question, str) or not 1 <= len(question.strip()) <= 600:
        raise ValueError("Напишите вопрос длиной до 600 символов.")
    mode = payload.get("mode", "suggest")
    if not isinstance(mode, str) or mode not in MODES:
        raise ValueError("Неизвестный режим помощника.")
    sources = _safe_sources(payload)
    profile = payload.get("profile", {})
    if not isinstance(profile, dict):
        raise ValueError("Некорректный профиль.")
    entities = _profile_entities(profile)
    audit = audit_profile(profile)
    source = profile_source or payload.get("profile_source")
    if not isinstance(source, str) or source not in {"builtin", "user"}:
        source = None
    requires_copy = source == "builtin"

    if mode == "audit":
        return {
            "suggestions": [],
            "audit": audit,
            "profile_source": source,
            "requires_copy": requires_copy,
            "mode": mode,
        }

    key, model = resolve_config()
    if not key or not model:
        raise RuntimeError("Помощник не настроен. Укажите ключ и модель OPENAI или OPENROUTER на сервере.")
    messages = _model_messages(mode, question, entities, sources, payload.get("history", []))
    # Let the provider resolve the configured host together with its key.
    # Passing explicit credentials to the legacy seam selects OpenRouter.
    options = {} if complete_fn is complete else {"api_key": key, "model": model}
    response = complete_fn(messages, **options, max_tokens=1400, timeout=60, retries=1)
    if not isinstance(response, dict) or not isinstance(response.get("suggestions"), list):
        raise ValueError("Помощник вернул неверный формат предложений.")
    suggestions = []
    for item in response["suggestions"][:MAX_SUGGESTIONS]:
        clean = _validate_suggestion(item, entities, sources, mode)
        if clean is not None:
            suggestions.append(clean)
    return {
        "suggestions": suggestions,
        "audit": audit,
        "profile_source": source,
        "requires_copy": requires_copy,
        "mode": mode,
    }
