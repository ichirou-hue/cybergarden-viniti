"""Structured, reviewable LLM suggestions for the manual profile form."""

import json

from .prompt_rules import DOCUMENT_RULES_RU
from .llm_provider import complete, resolve_config

FIELD_TYPES = {"text", "number", "boolean", "reference"}
MAX_SOURCES = 24
MAX_SUGGESTIONS = 8


def suggest(payload, *, complete_fn=complete):
    if not isinstance(payload, dict):
        raise ValueError("Нужен запрос с вопросом и фрагментами PDF.")
    question = payload.get("question")
    if not isinstance(question, str) or not 1 <= len(question.strip()) <= 600:
        raise ValueError("Напишите вопрос длиной до 600 символов.")
    sources = payload.get("sources", [])
    if not isinstance(sources, list):
        raise ValueError("Некорректные фрагменты PDF.")
    safe_sources = []
    for item in sources[:MAX_SOURCES]:
        if not isinstance(item, dict):
            continue
        quote = item.get("quote", "")
        page = item.get("page")
        if isinstance(quote, str) and isinstance(page, int) and 1 <= page <= 10000:
            safe_sources.append({"page": page, "quote": quote[:600]})
    profile = payload.get("profile", {})
    if not isinstance(profile, dict):
        raise ValueError("Некорректный профиль.")
    current = []
    types = profile.get("types", [])
    if not isinstance(types, list):
        raise ValueError("Некорректный список сущностей профиля.")
    for entity in types[:30]:
        if isinstance(entity, dict):
            fields = entity.get("fields", [])
            if not isinstance(fields, list):
                fields = []
            current.append({"name": str(entity.get("name", ""))[:100],
                            "fields": [str(field.get("name", ""))[:100]
                                       for field in fields[:50] if isinstance(field, dict)]})
    key, model = resolve_config()
    if not key or not model:
        raise RuntimeError("Помощник не настроен. Нужны OPENROUTER_API_KEY и OPENROUTER_MODEL на сервере.")
    messages = [{"role": "system", "content": (
        "Ты помогаешь составить профиль извлечения научной статьи. "
        + DOCUMENT_RULES_RU + " "
        "Верни только JSON-объект "
        "с массивом suggestions до 8 элементов. Каждый элемент: entity (название типа сущности), "
        "field (название поля), type (text, number, boolean или reference), "
        "value (пример значения из PDF либо пустая строка), page (номер страницы либо null), "
        "quote (точная цитата из переданных фрагментов либо пустая строка). "
        "Не повторяй уже существующие поля. Не выдумывай значения и цитаты. "
        "Если данных не хватает, верни пустой массив. Не добавляй пояснительный текст."
    )}]
    history = payload.get("history", [])
    if isinstance(history, list):
        for turn in history[-4:]:
            if isinstance(turn, dict) and turn.get("role") in {"user", "assistant"} \
                    and isinstance(turn.get("content"), str):
                messages.append({"role": turn["role"], "content": turn["content"][:1200]})
    messages.append({"role": "user", "content": json.dumps({"question": question.strip(),
        "profile": current, "sources": safe_sources}, ensure_ascii=False)})
    response = complete_fn(messages, api_key=key, model=model, max_tokens=1200, timeout=60, retries=1)
    if not isinstance(response, dict) or not isinstance(response.get("suggestions"), list):
        raise ValueError("Помощник вернул неверный формат предложений.")
    suggestions = []
    for item in response["suggestions"][:MAX_SUGGESTIONS]:
        if not isinstance(item, dict):
            continue
        entity, field, kind = item.get("entity"), item.get("field"), item.get("type")
        if not isinstance(entity, str) or not 1 <= len(entity.strip()) <= 100:
            continue
        if not isinstance(field, str) or not 1 <= len(field.strip()) <= 100 or kind not in FIELD_TYPES:
            continue
        quote = item.get("quote", "")
        page = item.get("page")
        if not isinstance(quote, str) or not any(
            source["page"] == page and quote and quote in source["quote"] for source in safe_sources
        ):
            quote, page = "", None
        value = item.get("value", "")
        if not isinstance(value, str) or (value and not quote) or (value and value not in quote):
            value = ""
        suggestions.append({"entity": entity.strip(), "field": field.strip(), "type": kind,
                            "value": value[:160], "page": page, "quote": quote[:300]})
    return {"suggestions": suggestions}
