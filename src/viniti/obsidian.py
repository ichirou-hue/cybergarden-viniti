"""Obsidian export: a linked ZIP of Markdown notes.

The export is deliberately conservative. A link is created only when the
parsed envelope really contains the entity, the author or the result
reference. Solar device ids are positional, so the same ``DV1`` in two
articles may be different devices; their notes remain scoped to the document. RAG datasets and authors are merged by a normalized label,
because those names are meant to be shared across articles.

Nothing here touches a local vault. The caller gets a ZIP and imports it.
"""

import io
import json
import math
from hashlib import sha256
import re
import unicodedata
import zipfile
from datetime import datetime, timezone

MAX_ARTICLES = 200
MAX_CITATIONS = 40
MAX_QUOTE = 700

KIND_FOLDERS = {
    "authors": "Авторы",
    "materials": "Материалы",
    "devices": "Устройства",
    "measurements": "Измерения",
    "datasets": "Наборы данных",
    "configurations": "Конфигурации",
    "tasks": "Задачи",
    "experiments": "Эксперименты",
}

# Kinds whose label identifies the same object across articles.
SHARED_KINDS = {"authors", "datasets"}

GENERIC_LABELS = {"", "unspecified", "unknown", "n/a", "none", "без названия"}

INVALID_NAME = re.compile(r'[\\/:*?"<>|#^\[\]\x00-\x1f]')


def _text(value):
    if isinstance(value, str):
        return value
    if type(value) is int or (type(value) is float and math.isfinite(value)):
        return str(value)
    return ""


def _clean(value, limit=200):
    text = _text(value).replace("\r", " ").replace("\n", " ")
    text = "".join(char for char in text if not unicodedata.category(char).startswith("C"))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def _slug(value, limit=70):
    text = unicodedata.normalize("NFKC", _clean(value, limit * 2))
    text = INVALID_NAME.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    return (text[:limit].strip(" .") or "Без названия")


def _fold(value):
    return _clean(value, 300).casefold()


def _yaml(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_yaml(item) for item in value) + "]"
    return json.dumps(_text(value), ensure_ascii=False)


def _frontmatter(fields):
    lines = ["---"]
    for key, value in fields.items():
        lines.append(f"{key}: {_yaml(value)}")
    lines.append("---")
    return "\n".join(lines)


def _md(value):
    """Render source text literally, including table cells and Obsidian syntax."""
    text = _clean(value, 2000)
    return re.sub(r"([\\`*_{}\[\]()<>!|#])", r"\\\1", text)


def _wikilink(note, label=""):
    # Targets are generated filenames, while aliases can originate in a PDF.
    # A source alias must never close a link or introduce another target.
    safe_note = _slug(note, 150)
    label = INVALID_NAME.sub(" ", _clean(label, 120)).strip()
    return f"[[{safe_note}|{label}]]" if label and label != safe_note else f"[[{safe_note}]]"


def _stable_name(label, identity):
    digest = sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]
    return f"{_slug(label, 90)} - {digest}"


def _scope(envelope):
    return envelope.get("_export_identity") or envelope.get("document_id") or (
        _publication(envelope)["track"], _publication(envelope)["publication_id"])


def _publication(envelope):
    publication = envelope.get("publication") or {}
    authors = publication.get("authors") or envelope.get("authors") or []
    if isinstance(authors, str):
        authors = [authors]
    authors = [_clean(item, 120) for item in authors if _clean(item, 120)]
    return {
        "publication_id": _clean(publication.get("publication_id") or envelope.get("publication_id"), 80),
        "title": _clean(publication.get("title") or envelope.get("title"), 300),
        "authors": authors,
        "year": _clean(publication.get("year") or envelope.get("year"), 10),
        "doi": _clean(publication.get("DOI") or publication.get("doi"), 160),
        "track": _clean(envelope.get("track"), 20),
    }


def _article_name(publication, used, identity=None):
    # Identity rather than selection order resolves filename collisions.
    base = publication["publication_id"] or "Статья"
    title = publication["title"]
    label = f"{base} - {title}" if title else base
    return _stable_name(label, ("article", identity or publication))


def _entity_label(kind, row):
    label = _clean(row.get("label") or row.get("name") or row.get("title")
                   or row.get("material_name_raw") or row.get("device_label")
                   or row.get("measurement_type"), 160)
    if _fold(label) in GENERIC_LABELS:
        label = ""
    return label


def _entity_id(row):
    return _clean(row.get("id") or row.get("source_id") or row.get("material_id")
                  or row.get("device_id") or row.get("measurement_id"), 80)


def _entity_key(kind, row, publication_id):
    label = _entity_label(kind, row)
    if kind in SHARED_KINDS and label:
        return (kind, "label", _fold(label))
    identifier = _entity_id(row) or _fold(label) or sha256(
        json.dumps(row, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    return (kind, "document", publication_id, identifier)


def _entity_name(kind, row, publication_id, used, identity=None):
    label = _entity_label(kind, row)
    identifier = _entity_id(row)
    base = (_fold(label) if kind in SHARED_KINDS and label else label) or f"{publication_id} {identifier or 'запись'}"
    return _stable_name(base, identity or _entity_key(kind, row, publication_id))


def _parsed_entities(envelope):
    """Use actual entities/tables; solar result references supply only scoped IDs.

    Older solar catalogue envelopes intentionally omit the scientific tables.
    We can retain their result references in the graph but cannot reconstruct
    a device's composition or material from an identifier alone.
    """
    supplied = envelope.get("entities")
    entities = {kind: list(rows) for kind, rows in supplied.items()
                if kind in KIND_FOLDERS and isinstance(rows, list)} if isinstance(supplied, dict) else {}
    if envelope.get("track") != "solar":
        return entities
    tables = envelope.get("tables")
    tables = tables if isinstance(tables, dict) else {}
    for kind, field in (("devices", "device_id"), ("materials", "material_id"),
                        ("measurements", "measurement_id")):
        rows = entities.setdefault(kind, [])
        known = {_entity_id(row) for row in rows if isinstance(row, dict)}
        for row in tables.get(kind, []) if isinstance(tables.get(kind), list) else []:
            if not isinstance(row, dict):
                continue
            identifier = _clean(row.get(field), 80)
            if identifier and identifier not in known:
                rows.append({**row, "id": identifier})
                known.add(identifier)
        for result in envelope.get("results") or []:
            if not isinstance(result, dict):
                continue
            identifier = _clean(result.get(field), 80)
            if identifier and identifier not in known:
                rows.append({"id": identifier,
                             "detail": "Ссылка из результата статьи. Атрибуты сущности не предоставлены конвертом."})
                known.add(identifier)
    return entities


def _collect_entities(envelope, article_note):
    """Return ``{key: {kind, name, label, detail, aliases, backlinks}}``.

    Only supplied data and explicit result references become graph nodes.
    """
    entities = _parsed_entities(envelope)
    found = {}
    for kind, rows in entities.items():
        if not isinstance(rows, list):
            continue
        for row in rows[:200]:
            if not isinstance(row, dict):
                continue
            key = _entity_key(kind, row, _scope(envelope))
            item = found.setdefault(key, {
                "kind": kind,
                "label": _entity_label(kind, row),
                "detail": [],
                "aliases": set(),
                "backlinks": [],
                "id": _entity_id(row),
            })
            item["backlinks"].append(article_note)
            if item["id"]:
                item["aliases"].add(item["id"])
            if item["label"]:
                item["aliases"].add(item["label"])
            detail = _clean(row.get("detail") or row.get("note"), 400)
            if detail and detail not in item["detail"]:
                item["detail"].append(detail)
    return found


def _author_key(name):
    return ("authors", "label", _fold(name))


def _collect_authors(envelope, article_note):
    found = {}
    for name in _publication(envelope)["authors"]:
        key = _author_key(name)
        item = found.setdefault(key, {"kind": "authors", "label": name, "detail": [],
                                      "aliases": set(), "backlinks": [], "id": ""})
        item["backlinks"].append(article_note)
        item["aliases"].add(name)
    return found


def _entity_map(envelope):
    """Map a parsed entity id to its note key inside one article."""
    mapping = {}
    for kind, rows in _parsed_entities(envelope).items():
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            identifier = _entity_id(row)
            if identifier:
                mapping[(kind, identifier)] = _entity_key(kind, row, _scope(envelope))
    return mapping


def _result_value(row, *names):
    for name in names:
        value = _text(row.get(name)).strip()
        if value:
            return value
    return ""


def _results_table(envelope, entity_notes):
    results = envelope.get("results") or []
    if not results:
        return ["_В разборе нет извлечённых результатов._"]
    lines = ["| Показатель | Значение | Единица | Ссылки |", "| --- | --- | --- | --- |"]
    for row in results[:300]:
        if not isinstance(row, dict):
            continue
        metric = _result_value(row, "metric_name", "name", "metric")
        value = _result_value(row, "metric_value", "value")
        unit = _result_value(row, "metric_unit", "unit")
        if not value:
            value = _result_value(row, "raw_value")
            unit = _result_value(row, "raw_unit")
        links = []
        for kind, field in (("configurations", "config_id"), ("datasets", "dataset_id"),
                            ("tasks", "task_id"), ("experiments", "experiment_id"),
                            ("devices", "device_id"), ("materials", "material_id"),
                            ("measurements", "measurement_id")):
            identifier = _result_value(row, field)
            key = entity_notes.get((kind, identifier))
            if key and key.get("name"):
                links.append(_wikilink(key["name"], identifier).replace("|", "\\|"))
        lines.append(f"| {_md(metric) or '-'} | {_md(value) or '-'} | {_md(unit) or '-'} | {' '.join(links) or '-'} |")
    if len(results) > 300:
        lines.append("")
        lines.append(f"_Показаны первые 300 из {len(results)} строк._")
    return lines


def _issues_section(envelope):
    issues = envelope.get("issues") or []
    if not issues:
        return ["_Замечаний нет._"]
    lines = []
    for issue in issues[:100]:
        if not isinstance(issue, dict):
            continue
        identifier = _clean(issue.get("issue_id"), 40) or "замечание"
        check = _clean(issue.get("check_name") or issue.get("kind"), 120)
        description = _clean(issue.get("description") or issue.get("message"), 500)
        source = issue.get("source") if isinstance(issue.get("source"), dict) else {}
        page = source.get("page")
        suffix = f" (стр. {page})" if type(page) is int else ""
        lines.append(f"- **{_md(identifier)}** {_md(check)}: {_md(description)}{suffix}")
    if len(issues) > 100:
        lines.append(f"- Показаны первые 100 из {len(issues)} замечаний.")
    return lines


def _citations_section(envelope):
    raw = list(envelope.get("text_sources") or []) + list(envelope.get("recognized_sources") or [])
    raw.extend(row.get("source") for row in envelope.get("results") or [] if isinstance(row, dict))
    sources = [item for item in raw
               if isinstance(item, dict) and type(item.get("page")) is int and item["page"] > 0]
    if not sources:
        return ["_В разборе нет текстовых источников с номерами страниц._"]
    lines = []
    seen = set()
    for source in sources:
        text = _clean(source.get("fragment") or source.get("quote") or source.get("display_text"), MAX_QUOTE)
        marker = (source.get("page"), text[:120])
        if not text or marker in seen:
            continue
        seen.add(marker)
        kind = _clean(source.get("kind"), 20) or "текст"
        lines.append(f"> {_md(text)}")
        lines.append(f">")
        lines.append(f"> - стр. {source['page']}, {_md(kind)}")
        lines.append("")
        if len(seen) >= MAX_CITATIONS:
            break
    if len(seen) < len(sources):
        lines.append(f"_Показаны первые {len(seen)} из {len(sources)} фрагментов._")
    return lines


def _article_markdown(envelope, article_note, entity_links, entity_map):
    publication = _publication(envelope)
    tags = ["ВИНИТИ"]
    if publication["track"]:
        tags.append(publication["track"])
    if publication["year"]:
        tags.append(publication["year"])
    related = [_wikilink(link["name"]) for link in entity_links]
    aliases = [publication["publication_id"]] if publication["publication_id"] else []
    if publication["title"]:
        aliases.append(publication["title"])
    fields = {
        "type": "article",
        "title": publication["title"] or publication["publication_id"],
        "authors": publication["authors"],
        "year": publication["year"],
        "doi": publication["doi"],
        "tags": tags,
        "related": related,
        "aliases": aliases,
        "publication_id": publication["publication_id"],
        "source": "viniti",
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    counts = envelope.get("counts") or {}
    lines = [
        _frontmatter(fields),
        "",
        f"# {_md(publication['title'] or publication['publication_id'] or 'Статья')}",
        "",
        "## Сводка",
        "",
    ]
    for label, value in (("Публикация", publication["publication_id"]),
                         ("Авторы", ", ".join(publication["authors"])),
                         ("Год", publication["year"]),
                         ("DOI", publication["doi"]),
                         ("Область", publication["track"]),
                         ("Страниц", str(envelope.get("pages") or "")),
                         ("Результатов", str(counts.get("results", len(envelope.get("results") or [])))),
                         ("Замечаний", str(counts.get("issues", len(envelope.get("issues") or []))))):
        if value:
            lines.append(f"- **{label}:** {_md(value)}")
    if entity_links:
        lines.extend(["", "## Связи", ""])
        for link in entity_links:
            lines.append(f"- {_wikilink(link['name'], link.get('label') or link['name'])}")
    lines.extend(["", "## Результаты", "", *_results_table(envelope, entity_map)])
    lines.extend(["", "## Замечания", "", *_issues_section(envelope)])
    lines.extend(["", "## Цитаты", "", *_citations_section(envelope)])
    lines.extend(["", "## Ограничения", "",
                  "Связи построены только по данным паспорта и источника. "
                  "Экспорт не проверяет научную достоверность статьи и не заменяет официальную сдачу.", ""])
    return "\n".join(lines)


def _entity_markdown(item):
    folder = KIND_FOLDERS.get(item["kind"], "Сущности")
    aliases = sorted(item["aliases"])
    fields = {
        "type": "entity",
        "kind": item["kind"],
        "title": item["label"] or item["name"],
        "aliases": aliases,
        "tags": ["ВИНИТИ", folder],
        "source": "viniti",
    }
    lines = [
        _frontmatter(fields),
        "",
        f"# {_md(item['label'] or item['name'])}",
        "",
    ]
    if item["id"]:
        lines.append(f"- **Идентификатор:** {_md(item['id'])}")
    for detail in item["detail"]:
        lines.append(f"- {_md(detail)}")
    lines.extend(["", "## Статьи", ""])
    for note in dict.fromkeys(item["backlinks"]):
        lines.append(f"- {_wikilink(note)}")
    lines.append("")
    return "\n".join(lines)


def _readme(article_count, entity_count):
    return "\n".join([
        _frontmatter({"type": "readme", "title": "Экспорт ВИНИТИ", "source": "viniti"}),
        "",
        "# Экспорт ВИНИТИ",
        "",
        f"В архиве {article_count} заметок статей и {entity_count} заметок сущностей.",
        "",
        "## Как импортировать",
        "",
        "1. Распакуйте архив в отдельную папку внутри Obsidian vault.",
        "2. Откройте Graph View: связи построены по wikilinks.",
        "3. Не перезаписывайте существующие заметки автоматически: проверьте совпадения имён.",
        "",
        "## Что подтверждено",
        "",
        "- Связи статьи с сущностями взяты из разобранного паспорта, а не придуманы.",
        "- Авторы взяты из метаданных публикации.",
        "- Цитаты и номера страниц взяты из извлечённых источников. Распознанный текст требует сверки с PDF.",
        "- Позиционные идентификаторы устройств solar не превращаются в общие заметки: "
        "одинаковый `DV1` в разных статьях может обозначать разные приборы.",
        "- Если конверт содержит только ссылку на ID сущности, её заметка показывает это явно, без выдуманных атрибутов.",
        "",
        "Экспорт не синхронизируется с vault и не меняет существующие заметки.",
        "",
    ])


def export_zip(documents, *, title="viniti-obsidian"):
    """Build a ZIP with linked Markdown notes for ``documents``.

    ``documents`` is a list of ``{"document_id": str, "envelope": dict}``.
    """
    documents = [item for item in documents if isinstance(item, dict)
                 and isinstance(item.get("envelope"), dict)][:MAX_ARTICLES]
    if not documents:
        raise ValueError("Нет документов для экспорта.")
    used_names = set()
    articles = []
    entity_items = {}
    seen_documents = set()
    for document in documents:
        original = document["envelope"]
        identity = _text(document.get("document_id") or original.get("document_id")) or (
            _publication(original)["track"] + ":" +
            (_text(original.get("sha256")) or _publication(original)["publication_id"] or
             sha256(json.dumps(original, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()))
        if identity in seen_documents:
            continue
        seen_documents.add(identity)
        envelope = {**original, "_export_identity": identity}
        publication = _publication(envelope)
        article_note = _article_name(publication, used_names, identity)
        entities = _collect_entities(envelope, article_note)
        entities.update({key: value for key, value in _collect_authors(envelope, article_note).items()
                         if key not in entities})
        for key, item in entities.items():
            target = entity_items.setdefault(key, {**item, "name": "", "aliases": set(), "backlinks": []})
            target["aliases"].update(item["aliases"])
            target["backlinks"].extend(item["backlinks"])
            for detail in item["detail"]:
                if detail not in target["detail"]:
                    target["detail"].append(detail)
        articles.append((article_note, envelope, entities))

    # Allocate entity names after the article names, so a note cannot clash
    # with an article file in the ZIP.
    for key, item in entity_items.items():
        publication_id = ""
        if key[1] == "document":
            publication_id = key[2]
        elif articles:
            publication_id = _publication(articles[0][1])["publication_id"]
        item["name"] = _entity_name(item["kind"], {"label": item["label"], "id": item["id"]},
                                    publication_id, used_names, key)

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("README.md", _readme(len(articles), len(entity_items)))
        for article_note, envelope, entities in articles:
            links = [entity_items[key] for key in entities]
            entity_map = {local_id: entity_items[key]
                          for local_id, key in _entity_map(envelope).items()
                          if key in entity_items and key in entities}
            archive.writestr(f"Статьи/{article_note}.md",
                             _article_markdown(envelope, article_note, links, entity_map))
        for item in entity_items.values():
            folder = KIND_FOLDERS.get(item["kind"], "Сущности")
            archive.writestr(f"Сущности/{folder}/{item['name']}.md", _entity_markdown(item))
    return buffer.getvalue()
