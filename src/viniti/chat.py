"""Grounded chat over parsed articles and the user's library.

The chat is read-only. It never writes the passport, ``result.json`` or the
official CSV: the only persistent state is the per-user conversation history
in ``chat_messages``.

Every factual claim returned to the user must be backed by a source that
existed in the parsed envelope. The model can only point at numbered context
sources; the server then checks that the quote is really a substring of that
source. A citation that fails the check is dropped, and an answer that lost
all citations is marked ``unverified`` instead of being presented as proven.
"""

import hashlib
import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote as urlquote

from .llm_provider import chat as llm_chat, config_from_env
from .prompt_rules import DOCUMENT_RULES_RU
from .verify import compact

MODES = ("document", "selection", "library", "general")
MAX_DOCUMENTS = 12
MAX_QUESTION = 2000
MAX_ANSWER = 8000
MAX_CONTEXT_CHARS = 28000
MAX_SOURCES = 80
MAX_CITATIONS = 12
MAX_CONTRADICTIONS = 5
HISTORY_TURNS = 6
HISTORY_LIMIT = 40
MIN_SOURCE_CHARS = 24

KIND_LABELS = {
    "text": "текст",
    "table": "таблица",
    "caption": "подпись",
    "equation": "формула",
    "figure": "рисунок",
}


def context_key(mode, document_ids=()):
    """Stable history key: one conversation per user and context."""
    if mode not in MODES:
        raise ValueError("Неизвестный режим чата.")
    if mode == "document":
        if not document_ids:
            raise ValueError("Не указана статья.")
        return "doc:" + str(document_ids[0])
    if mode == "selection":
        if not document_ids:
            raise ValueError("Не выбраны статьи.")
        digest = hashlib.sha256("\n".join(sorted(map(str, document_ids))).encode()).hexdigest()
        return "sel:" + digest[:20]
    if mode == "library":
        return "library"
    return "general"


def _text(value):
    return value if isinstance(value, str) else ""


def _source_text(source):
    for name in ("fragment", "quote", "display_text", "text"):
        text = source.get(name)
        if isinstance(text, str) and text.strip():
            return text.strip()
    return ""


def _document_meta(document):
    envelope = document.get("envelope") or {}
    publication = envelope.get("publication") or {}
    title = _text(publication.get("title")) or _text(envelope.get("title"))
    authors = publication.get("authors") or envelope.get("authors") or []
    if isinstance(authors, str):
        authors = [authors]
    authors = [str(item) for item in authors if str(item).strip()][:6]
    year = publication.get("year") or envelope.get("year")
    doi = publication.get("DOI") or publication.get("doi") or ""
    return {
        "document_id": document.get("document_id", ""),
        "title": title[:300],
        "authors": authors,
        "year": str(year)[:10] if year else "",
        "doi": str(doi)[:120] if doi else "",
        "publication_id": str(publication.get("publication_id") or envelope.get("publication_id") or "")[:80],
    }


def _candidate_sources(document):
    """Order sources so tables and captions are represented first."""
    envelope = document.get("envelope") or {}
    document_id = document.get("document_id", "")
    order = {"table": 0, "caption": 1, "text": 2, "equation": 3}
    items = []
    seen = set()
    candidates = list(envelope.get("text_sources") or [])
    candidates.extend(envelope.get("recognized_sources") or [])
    candidates.extend(row.get("source") for row in envelope.get("results") or [] if isinstance(row, dict))
    for source in candidates:
        if not isinstance(source, dict):
            continue
        page = source.get("page")
        text = _source_text(source)
        if type(page) is not int or not 1 <= page <= 10000 or len(text) < MIN_SOURCE_CHARS:
            continue
        kind = source.get("kind") if source.get("kind") in KIND_LABELS else "text"
        marker = (page, compact(text))
        if marker in seen:
            continue
        seen.add(marker)
        items.append({
            "document_id": document_id,
            "page": page,
            "kind": kind,
            "text": text,
            "source_id": source.get("source_id", source.get("id")),
            "provenance": "recognized" if source in (envelope.get("recognized_sources") or []) else "pdf_text",
            "priority": order.get(kind, 4),
        })
    items.sort(key=lambda item: (item["priority"], item["page"]))
    return items


def build_context(documents, *, max_sources=MAX_SOURCES, max_chars=MAX_CONTEXT_CHARS, question=""):
    """Numbered, bounded context. Only these sources may be cited."""
    documents = list(documents)
    if not documents:
        return [], "", False
    quota = max(4, max_sources // len(documents))
    selected = []
    terms = set(re.findall(r"[\w]+", question.casefold()))
    available = 0
    clipped = False
    for document in documents:
        candidates = _candidate_sources(document)
        available += len(candidates)
        if terms:
            candidates.sort(key=lambda item: -sum(term in item["text"].casefold() for term in terms if len(term) > 2))
        # Divide the character budget as well, so one long article cannot
        # consume the whole prompt before the next article is represented.
        for item in candidates[:quota]:
            limit = max(24, min(1800, (max_chars // len(documents) - 500) // quota - 80))
            clipped = clipped or len(item["text"]) > limit
            positions = [item["text"].casefold().find(term) for term in terms if len(term) > 2]
            matches = [position for position in positions if position >= 0]
            start = max(0, min(matches) - limit // 3) if matches else 0
            item["text"] = item["text"][start:start + limit]
        selected.extend(candidates[:quota])
    selected = selected[:max_sources]

    blocks = []
    sources = []
    used = 0
    truncated = clipped or available > len(selected)
    for document in documents:
        meta = _document_meta(document)
        lines = [f"Документ {meta['document_id']}: {meta['title'] or 'без названия'}"]
        detail = []
        if meta["authors"]:
            detail.append("авторы: " + ", ".join(meta["authors"]))
        if meta["year"]:
            detail.append("год: " + meta["year"])
        if meta["doi"]:
            detail.append("DOI: " + meta["doi"])
        if detail:
            lines.append("  " + "; ".join(detail))
        header_size = len("\n".join(lines)) + 2
        if used + header_size > max_chars:
            truncated = True
            break
        used += header_size
        own = [item for item in selected if item["document_id"] == meta["document_id"]]
        for item in own:
            text = item["text"][:1800]
            label = KIND_LABELS.get(item["kind"], item["kind"])
            block = f"[S{len(sources) + 1}] стр. {item['page']}, {label}: {text}"
            if used + len(block) + 1 > max_chars:
                truncated = True
                break
            used += len(block) + 1
            sources.append({
                "ref": f"S{len(sources) + 1}",
                "document_id": item["document_id"],
                "page": item["page"],
                "kind": item["kind"],
                "text": text,
                "source_id": item["source_id"],
                "provenance": item["provenance"],
            })
            lines.append(block)
        if own:
            blocks.append("\n".join(lines))
        if used >= max_chars:
            break
    return sources, "\n\n".join(blocks), truncated


def context_label(mode, documents, *, total=None):
    if mode == "general":
        return "Общий вопрос без документов"
    if mode == "document":
        meta = _document_meta(documents[0]) if documents else {}
        return f"Текущая статья: {meta.get('publication_id') or meta.get('document_id') or 'без номера'}"
    if mode == "selection":
        return f"Выбранные статьи: {len(documents)}"
    count = len(documents) if total is None else total
    suffix = f" (в контексте {len(documents)})" if total is not None and total > len(documents) else ""
    return f"Библиотека пользователя: {count} документов{suffix}"


def _history_for_model(history):
    messages = []
    for turn in (history or [])[-HISTORY_TURNS * 2:]:
        if not isinstance(turn, dict):
            continue
        role = turn.get("role")
        content = turn.get("content")
        if role in {"user", "assistant"} and isinstance(content, str) and content.strip():
            messages.append({"role": role, "content": content[:2000]})
    return messages


def _system_prompt(mode):
    if mode == "general":
        return (
            "Ты научный ассистент ВИНИТИ. Пользователь задал общий вопрос без документов. "
            "Отвечай из общих знаний модели и явно помечай это в поле answer словами "
            "\"Общие знания модели, не вывод по статье\". Не придумывай цитаты, страницы "
            "и документы: citations всегда пустой массив, contradictions тоже пустой. "
            + DOCUMENT_RULES_RU
            + " Верни только JSON-объект с полями answer (строка), citations (массив), "
            "not_found (boolean), contradictions (массив). Не добавляй пояснительный текст."
        )
    return (
        "Ты научный ассистент ВИНИТИ. Отвечай только по предоставленным источникам. "
        "Каждый факт в answer должен опираться на citation с точной цитатой из источника. "
        "Ссылайся на источники по номерам S1, S2 и так далее. "
        "Если в источниках нет ответа, поставь not_found=true и напиши, каких данных не хватает. "
        "Если источники противоречат друг другу, перечисли противоречия отдельно, не объединяй их молча. "
        "Не используй общие знания как факт о статье. Не выдумывай цитаты, страницы и документы. "
        "История нужна только для понимания вопроса; номера S в ней относятся к прошлым ответам. "
        "Все цитаты для нового ответа заново выбирай из текущего контекста. "
        + DOCUMENT_RULES_RU
        + " Верни только JSON-объект: answer (строка), citations (массив объектов ref, quote), "
        "not_found (boolean), contradictions (массив объектов topic, refs, explanation). "
        "quote обязан быть буквальной подстрокой указанного источника. Не добавляй пояснительный текст."
    )


def _validate_citations(raw, sources):
    by_ref = {source["ref"]: source for source in sources}
    valid = []
    seen = set()
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        ref = item.get("ref")
        quote = item.get("quote")
        source = by_ref.get(ref) if isinstance(ref, str) else None
        if source is None or not isinstance(quote, str):
            continue
        quote = quote.strip()
        if not 1 <= len(quote) <= 800 or not compact(quote) or compact(quote) not in compact(source["text"]):
            continue
        marker = (ref, compact(quote))
        if marker in seen:
            continue
        seen.add(marker)
        document_id = source["document_id"]
        is_parse = document_id.startswith("parse:")
        base = (f"/api/parse/{urlquote(document_id[6:], safe='')}" if is_parse else
                f"/api/documents/{urlquote(document_id, safe='')}")
        valid.append({
            "ref": ref,
            "quote": quote,
            "page": source["page"],
            "document_id": source["document_id"],
            "kind": source["kind"],
            "source_id": source["source_id"],
            "provenance": source.get("provenance", "pdf_text"),
            "pdf_url": f"{base}/pdf#page={source['page']}",
            "anchor_url": (f"{base}/anchors/{urlquote(str(source['source_id']), safe='')}.json")
                          if not is_parse and source["source_id"] is not None else None,
        })
        if len(valid) >= MAX_CITATIONS:
            break
    return valid


def _validate_contradictions(raw, sources):
    by_ref = {source["ref"]: source for source in sources}
    result = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        topic = item.get("topic")
        refs = item.get("refs")
        explanation = item.get("explanation")
        if not isinstance(topic, str) or not topic.strip() or not isinstance(refs, list):
            continue
        clean = [ref for ref in refs if isinstance(ref, str) and ref in by_ref]
        clean = list(dict.fromkeys(clean))
        if len(clean) < 2:
            continue
        documents = {by_ref[ref]["document_id"] for ref in clean}
        result.append({
            "topic": topic.strip()[:200],
            "refs": clean[:6],
            "documents": sorted(documents),
            "explanation": explanation.strip()[:500] if isinstance(explanation, str) else "",
        })
        if len(result) >= MAX_CONTRADICTIONS:
            break
    return result


def ask(question, mode, documents, *, history=None, total_documents=None, complete_fn=None):
    """Answer one question with verified citations.

    ``documents`` is a list of ``{"document_id": str, "envelope": dict}``
    already checked for ownership by the caller. ``complete_fn`` is a test
    seam with the ``llm_provider.chat`` signature.
    """
    if not isinstance(question, str) or not 1 <= len(question.strip()) <= MAX_QUESTION:
        raise ValueError(f"Напишите вопрос длиной до {MAX_QUESTION} символов.")
    if mode not in MODES:
        raise ValueError("Неизвестный режим чата.")
    documents = list(documents or [])
    if mode != "general" and not documents:
        raise ValueError("Для этого режима нужен хотя бы один документ.")
    if len(documents) > MAX_DOCUMENTS:
        raise ValueError(f"В контекст можно добавить не больше {MAX_DOCUMENTS} документов.")
    if mode == "general":
        documents = []

    sources, context_text, truncated = build_context(documents, question=question)
    if mode != "general" and not sources:
        # There is nothing to ground an answer on; do not call the model and
        # pretend that the empty result is an answer.
        return {
            "answer": "В разборе этих документов нет пригодных текстовых источников, "
                      "поэтому ответить по статье нельзя.",
            "citations": [],
            "not_found": True,
            "unverified": False,
            "contradictions": [],
            "context": {
                "mode": mode,
                "label": context_label(mode, documents, total=total_documents),
                "documents": [_document_meta(document) for document in documents],
                "source_count": 0,
                "truncated": truncated,
            },
            "general_knowledge": False,
        }

    if complete_fn is None:
        try:
            config = config_from_env()
        except ValueError as error:
            raise RuntimeError("Чат не настроен: " + str(error)) from None
        complete_fn = lambda messages, **options: llm_chat(messages, config, **options)

    request = {
        "mode": mode,
        "question": question.strip(),
        "context": context_text if mode != "general" else "",
        "sources": [{"ref": source["ref"], "document_id": source["document_id"],
                     "page": source["page"], "kind": source["kind"]} for source in sources],
    }
    messages = [{"role": "system", "content": _system_prompt(mode)}]
    messages.extend(_history_for_model(history))
    messages.append({"role": "user", "content": json.dumps(request, ensure_ascii=False)})

    response = complete_fn(messages, max_tokens=1800)
    if not isinstance(response, dict):
        raise ValueError("Чат вернул неверный формат ответа.")
    answer = response.get("answer")
    if not isinstance(answer, str) or not answer.strip():
        raise ValueError("Чат вернул пустой ответ.")
    answer = answer.strip()[:MAX_ANSWER]
    if mode == "general":
        answer = "Общие знания модели, не вывод по статье.\n\n" + answer
    citations = _validate_citations(response.get("citations"), sources)
    cited_refs = {item["ref"] for item in citations}
    contradictions = _validate_contradictions(response.get("contradictions"),
                                               [item for item in sources if item["ref"] in cited_refs])
    not_found = response.get("not_found") is True
    raw_citations = response.get("citations")
    unverified = mode != "general" and not not_found and (
        not citations or not isinstance(raw_citations, list) or len(citations) != len(raw_citations))

    return {
        "answer": answer,
        "citations": citations,
        "not_found": not_found,
        "unverified": unverified,
        "contradictions": contradictions,
        "context": {
            "mode": mode,
            "label": context_label(mode, documents, total=total_documents),
            "documents": [_document_meta(document) for document in documents],
            "source_count": len(sources),
            "truncated": truncated,
        },
        "general_knowledge": mode == "general",
    }


class ChatStore:
    """Per-user conversation history in the shared SQLite file.

    The table is created idempotently, like the auth tables. It does not touch
    the result tables, so an existing database keeps working unchanged.
    """

    def __init__(self, path):
        self.path = Path(path)

    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        ensure_schema(conn)
        return conn

    def history(self, user_id, scope, key, limit=HISTORY_LIMIT):
        with closing(self.connect()) as conn:
            rows = conn.execute(
                "SELECT role, content, payload, created_at FROM chat_messages "
                "WHERE user_id=? AND scope=? AND context_key=? ORDER BY id DESC LIMIT ?",
                (str(user_id), scope, key, int(limit)),
            ).fetchall()
        items = []
        for row in reversed(rows):
            try:
                payload = json.loads(row["payload"])
            except (TypeError, ValueError):
                payload = {}
            items.append({
                "role": row["role"],
                "content": row["content"],
                "payload": payload if isinstance(payload, dict) else {},
                "created_at": row["created_at"],
            })
        return items

    def append(self, user_id, scope, key, role, content, payload=None):
        if role not in {"user", "assistant"}:
            raise ValueError("Неизвестная роль сообщения.")
        created = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with closing(self.connect()) as conn, conn:
            conn.execute(
                "INSERT INTO chat_messages "
                "(user_id, scope, context_key, role, content, payload, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (str(user_id), scope, key, role, str(content)[:MAX_ANSWER],
                 json.dumps(payload or {}, ensure_ascii=False), created),
            )

    def clear(self, user_id, scope, key):
        with closing(self.connect()) as conn, conn:
            conn.execute(
                "DELETE FROM chat_messages WHERE user_id=? AND scope=? AND context_key=?",
                (str(user_id), scope, key),
            )


def ensure_schema(conn):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS chat_messages ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "user_id TEXT NOT NULL,"
        "scope TEXT NOT NULL,"
        "context_key TEXT NOT NULL,"
        "role TEXT NOT NULL,"
        "content TEXT NOT NULL,"
        "payload TEXT NOT NULL DEFAULT '{}',"
        "created_at TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS chat_messages_lookup "
        "ON chat_messages(user_id, scope, context_key, id)"
    )
