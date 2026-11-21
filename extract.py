"""Window selection, the filler seam, and the verified write.

Pipeline (one publication):

    pdf_source.extract(pdf_bytes)        -> located text sources
    context_windows(sources)             -> windows a model could read
    filler.fill(window, spec)            -> candidate records
    verify.verify_record(record, sources)-> keep or drop, with a reason
    <area>_results                       -> survivors only

The model is deliberately absent. A model-backed filler plugs into ``Filler``
without touching this module; ``TableFiller`` is the model-free baseline that keeps
the pipeline end-to-end testable today.

Window shape (``context_windows``)::

    {
      "index": 3,                 # 1-based window number
      "page": 5,                  # every source in the window is from this page
      "sources": [198, 199, 200], # ids from pdf_source, in reading order
      "text": "line\\nline\\n",    # source quotes joined by newlines, nothing added
      "items": [ {...source dict without "cells"...} ],
    }

Windows never span pages, so ``window["page"]`` is exact and a filler that trusts
it still gets caught by the verifier if it invents a page. ``text`` is the plain
concatenation of the quotes: no markers, no numbering, so anything a model quotes
from it is still a literal substring. ``items`` keeps the structured view
(``id``, ``page``, ``kind``, ``bbox``, and ``table``/``row`` for table rows) for
fillers that need more than flat text.

Filler protocol. ``Filler.fill(window, spec)`` returns a list of candidate dicts.
``spec`` is the passport entry for the target table::

    {"area": "solar", "table": "results", "key": "result_id",
     "columns": [...], "fields": {...}}

Each candidate MUST carry:

    source_page      int or str, 1-based page the claim comes from
    source_fragment  str, text copied from that page; this is the evidence

and MAY carry any column of the target table (``metric_name``, ``metric_value``,
``metric_unit``, ``source_table``, ...). ``metric_value`` must be the text exactly
as printed - ``"1,197"`` and ``"0.71"`` stay text, never ``float``. Columns a
filler omits are written as ``''``. ``area``, ``publication_id`` and ``result_id``
are assigned by :func:`run` and are ignored if a filler sets them.

What :func:`run` guarantees:

- a candidate that fails ``verify.verify_record`` is never written;
- ``source_page`` comes from the located source, never from the candidate;
- ``source_fragment`` is replaced by the verbatim slice of the source that
  matched, so even a quote that matched only after normalization is stored as
  literal text;
- re-running replaces this publication's previous extractor rows (ids ``X001``...)
  instead of appending to them, and imported gold rows (ids ``R...``) are untouched.

Besides the area table, :func:`run` fills the document envelope: one ``documents``
row per publication and one ``blocks`` row per page. The page blocks hold the pypdf
page text, so a ``results.source_fragment`` can be checked against the database
without reopening the PDF.

Run:  python -X utf8 extract.py _corpus/area1/dev/D001.pdf --publication-id D001 \
          --db data/scratch.sqlite3
"""
import argparse
from collections import Counter
import hashlib
import json
import os
import pathlib
import re
import sys
from typing import Protocol

import db as storage
import pdf_source
import verify

DEFAULT_BUDGET = 6000
DEFAULT_OVERLAP = 1
# Bump when the parse itself changes (pdf_source, the envelope), so a stored
# parser_version says which code produced the documents/blocks rows.
PARSER_VERSION = 1
# Extractor rows use their own id space so a rerun can clear them without ever
# touching rows imported from dev_gold (which are R### / R#-#).
RESULT_PREFIX = "X"
RESULT_ID_GLOB = "X[0-9][0-9][0-9]"


# ---------------------------------------------------------------- windows

def _size(group):
    """Characters a group of sources adds to the window text."""
    return sum(len(source.get("quote") or "") + 1 for source in group)


def _window(index, group):
    return {
        "index": index,
        "page": group[0]["page"],
        "sources": [source["id"] for source in group],
        "text": "\n".join(source.get("quote") or "" for source in group),
        "items": [{key: value for key, value in source.items() if key != "cells"}
                  for source in group],
    }


def context_windows(sources, budget_chars=DEFAULT_BUDGET, overlap=DEFAULT_OVERLAP):
    """Group consecutive sources into windows a model could read.

    - a window never spans pages, so ``page`` is unambiguous;
    - a window stays under ``budget_chars`` characters of text, except when a
      single source is larger than the whole budget: that source gets a window of
      its own rather than being cut, because cutting it would destroy the quote;
    - ``overlap`` sources from the end of a window repeat at the start of the next
      one, so a sentence split across the boundary is still readable in one piece;
    - no source is dropped: every id appears in at least one window.
    """
    if budget_chars <= 0:
        raise ValueError("budget_chars должен быть положительным")
    overlap = max(0, int(overlap))
    windows, group = [], []

    def tail(items, page):
        return [item for item in items[-overlap:] if item["page"] == page] if overlap else []

    for source in sources:
        if group and source["page"] != group[0]["page"]:
            windows.append(group)
            group = []
        if group and _size(group + [source]) > budget_chars:
            windows.append(group)
            group = tail(group, source["page"])
            if group and _size(group + [source]) > budget_chars:
                group = []
        group.append(source)
    if group:
        windows.append(group)
    return [_window(index, group) for index, group in enumerate(windows, 1)]


# ---------------------------------------------------------------- filler seam

class Filler(Protocol):
    """Anything that can turn a window into candidate records.

    See the module docstring for the exact candidate shape. ``fill`` may return an
    empty list; it must not write to the database and must not invent ``source_page``
    values it cannot defend, because the verifier will drop them.
    """

    def fill(self, window: dict, spec: dict) -> list:
        ...


# ---------------------------------------------------------------- baseline filler

# Metric words as they appear in table headers of the solar area.
METRIC_HEADS = {
    "jsc": "Jsc", "voc": "Voc", "ff": "FF", "pce": "PCE", "eff": "PCE",
    "efficiency": "PCE", "eta": "PCE", "\u03b7": "PCE",
    "rs": "Rs", "rsh": "Rsh", "eqe": "EQE", "iqe": "IQE",
}
TWO_LETTER_HEADS = {"j": ("sc", "Jsc"), "v": ("oc", "Voc")}
TOKEN = re.compile(r"[A-Za-z\u03b7]+")
# A whole cell that is a number, allowing the spacing and minus sign pdfplumber
# leaves behind: "0.71", "1,197", "\u2212 0.5", "12.89 %".
NUMERIC = re.compile(r"^[-\u2212+]?\s*\d[\d\s]*(?:[.,]\d+)?\s*%?$")
PARENS = re.compile(r"\(([^)]*)\)")
UNIT_HINT = re.compile(r"^(%|mV|V|A/W|mA\s*/?\s*cm\s*[-\u2212]?\s*2|cm\s*2\s*/\s*Vs)$", re.I)


def split_cells(quote):
    """Split a table row quote into stripped cell texts."""
    return [cell.strip() for cell in (quote or "").split("|")]


def _unit(cells, cell_index, stop_index):
    span = " ".join(cells[cell_index:stop_index])
    found = PARENS.search(span)
    if found:
        return " ".join(found.group(1).replace("\u2212", "-").split())
    for text in cells[cell_index:stop_index]:
        if UNIT_HINT.match(text):
            return text
    return ""


def parse_header(cells):
    """Read metric names and units out of a table header row.

    Returns ``[(name, unit), ...]`` in column order, or ``[]`` when the row is not a
    metric header. Handles both ``J | SC | (mA cm-2)`` and ``J SC`` spellings.
    """
    tokens = [(token.lower(), index)
              for index, text in enumerate(cells)
              for token in TOKEN.findall(text)]
    metrics = []
    position = 0
    while position < len(tokens):
        word, cell = tokens[position]
        name = None
        if word in TWO_LETTER_HEADS:
            suffix, name = TWO_LETTER_HEADS[word]
            following = tokens[position + 1] if position + 1 < len(tokens) else None
            if following and following[0] == suffix:
                position += 1
            else:
                name = None
        elif word in METRIC_HEADS:
            name = METRIC_HEADS[word]
        position += 1
        if name:
            metrics.append((name, cell))
    if len(metrics) < 2:
        return []
    if len({name for name, _ in metrics}) != len(metrics):
        return []  # repeated metric: the row is not a header we can align
    stop = [cell for _, cell in metrics] + [len(cells)]
    return [(name, _unit(cells, cell, stop[index + 1]))
            for index, (name, cell) in enumerate(metrics)]


def is_header(cells):
    if any(NUMERIC.match(text) for text in cells if text):
        return False
    return bool(parse_header(cells))


class TableFiller:
    """Model-free baseline: read table rows pdf_source already structured.

    It maps a header row to metric names and units, then pairs the numeric cells of
    the following rows with those metrics **in column order**, and only when the
    count of numeric cells equals the count of metrics. Rows it cannot align are
    skipped rather than guessed.

    This is honest and small, not good extraction: it ignores merged cells, device
    identity, scan direction, units written outside parentheses, tables whose header
    lands in a different window, and every number that lives in running text. It
    exists so the seam and the verifier can be tested end to end today. It is not a
    solution for the solar area.
    """

    def fill(self, window, spec):
        rows, headers = [], {}
        for item in window.get("items") or []:
            if item.get("kind") != "table":
                continue
            quote = item.get("quote") or ""
            cells = split_cells(quote)
            key = (item.get("page"), item.get("table"))
            if is_header(cells):
                headers[key] = parse_header(cells)
                continue
            metrics = headers.get(key)
            if not metrics:
                continue
            values = [cell for cell in cells if NUMERIC.match(cell)]
            if len(values) != len(metrics):
                continue
            for (name, unit), value in zip(metrics, values):
                rows.append({
                    "metric_name": name,
                    "metric_value": value,
                    "metric_unit": unit,
                    "source_page": item.get("page"),
                    "source_fragment": quote,
                    "source_table": f"таблица {item.get('table')}, строка {item.get('row')}",
                    "extraction_status": "extracted",
                    "note": "TableFiller (baseline)",
                })
        return rows


# ---------------------------------------------------------------- write

def _number(text):
    """Same conversion import_gold.py uses: plain float, None when it is not one."""
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _insert(connection, name, area, table, meta, rows):
    mirrors = storage.table_mirrors(table, meta)
    columns = ["area", "_line"] + list(meta["columns"]) + list(mirrors.values())
    statement = (f"INSERT OR REPLACE INTO {storage.quote(name)} "
                 f"({', '.join(storage.quote(column) for column in columns)}) "
                 f"VALUES ({', '.join('?' for _ in columns)})")
    payload = []
    for line, row in enumerate(rows, 1):
        values = [area, line] + [row[column] for column in meta["columns"]]
        values += [_number(row[column]) for column in mirrors]
        payload.append(values)
    connection.executemany(statement, payload)
    return len(payload)


def _write_envelope(connection, area, publication_id, pdf_bytes, document, source_path):
    """Record one publication in ``documents`` and its pages in ``blocks``.

    ``documents`` is keyed on ``(area, publication_id)``, so INSERT OR REPLACE
    makes a rerun an update instead of a duplicate-key error. ``in_scope`` is
    ``true`` because the area comes from the corpus kit the file was taken from,
    not from the article text; ``year`` stays empty because pdf_source does not
    read it.

    ``blocks`` gets one row per page with the pypdf page text (``page_texts``),
    ``kind='page'``, ``section=''`` and ``bbox_json='[]'``: the whole page, not a
    located block. This is what makes a ``results.source_fragment`` checkable
    against the database without reopening the PDF. The rows of this publication
    are deleted first, so a rerun replaces the pages rather than appending them.

    Returns the number of block rows written.
    """
    page_texts = document.get("page_texts") or {}
    sources = document.get("sources") or []
    connection.execute(
        "INSERT OR REPLACE INTO documents "
        "(area, publication_id, sha256, title, year, in_scope, exclusion_reason, "
        " parser_status, parser_version, page_count, text_chars, source_path) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (area, publication_id, hashlib.sha256(pdf_bytes).hexdigest(),
         document.get("title") or "", "", "true", "",
         "ok" if sources else "empty", PARSER_VERSION,
         int(document.get("pages") or 0),
         sum(len(text or "") for text in page_texts.values()),
         str(source_path or "")))
    if not page_texts:
        # No page text at all means the parse told us nothing about the pages;
        # leave any previous blocks alone instead of wiping them on a bad parse.
        return 0
    connection.execute("DELETE FROM blocks WHERE area = ? AND publication_id = ?",
                       (area, publication_id))
    payload = [(area, publication_id, int(page), int(page), "", "page", text or "", "[]")
               for page, text in sorted(page_texts.items(), key=lambda item: int(item[0]))]
    connection.executemany(
        "INSERT OR REPLACE INTO blocks "
        "(area, publication_id, ordinal, page, section, kind, text, bbox_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", payload)
    return len(payload)


def run(area, publication_id, pdf_bytes, filler, db_path=storage.DEFAULT_DB,
        dry_run=False, table="results", budget_chars=DEFAULT_BUDGET,
        overlap=DEFAULT_OVERLAP, source_path=""):
    """Extract one publication and write only verified rows.

    Also fills the envelope: one ``documents`` row (status ``ok`` when the parse
    produced sources, ``empty`` when it did not) and one ``blocks`` row per page,
    so a stored ``source_fragment`` can be checked against the database.
    ``source_path`` is recorded in ``documents`` as the origin of the file.

    Returns a summary dict::

        {"area", "publication_id", "table", "pages", "sources",
         "windows", "candidates", "verified", "duplicates", "rejected", "written",
         "blocks", "parser_status", "rejected_reasons": {reason: count},
         "rejected_examples": [...], "dry_run"}

    ``verified`` counts candidates that passed the gate; ``written`` is what landed
    in the table, so ``verified == written + duplicates``.
    """
    passport = storage.load_passport()
    meta = passport["areas"][area]["tables"][table]
    document = pdf_source.extract(pdf_bytes)
    sources = document.get("sources") or []
    page_texts = document.get("page_texts") or {}
    windows = context_windows(sources, budget_chars=budget_chars, overlap=overlap)
    spec = {"area": area, "table": table, "key": meta["key"],
            "columns": list(meta["columns"]), "fields": meta["fields"]}

    candidates = []
    for window in windows:
        produced = filler.fill(window, spec)
        if produced:
            candidates.extend(produced)

    rows, seen = [], set()
    reasons = Counter()
    examples = []
    verified = duplicates = rejected = 0
    for candidate in candidates:
        ok, reason = verify.verify_record(candidate, sources, page_texts)
        if not ok:
            rejected += 1
            reasons[reason] += 1
            if len(examples) < 20:
                fragment = candidate.get("source_fragment") if isinstance(candidate, dict) else ""
                examples.append({"reason": reason, "fragment": verify.short(fragment or "")})
            continue
        page = verify.parse_page(candidate["source_page"])
        if verify.is_locator(candidate["source_fragment"]):
            # A locator is not a quote, so it cannot be cut out of the text; it was
            # already accepted by label_on_page for this page. Running it through
            # locate_span used to raise RuntimeError and lose the whole article.
            source, literal = {"page": page}, candidate["source_fragment"].strip()
        else:
            found = verify.locate_span(candidate["source_fragment"], sources, page, page_texts)
            if found is None:  # verify_record passed, so this is a programming error
                raise RuntimeError(
                    f"verify_record принял цитату, locate_span не нашёл: {candidate!r}")
            source, literal = found
        verified += 1
        row = {column: "" for column in meta["columns"]}
        for column, value in candidate.items():
            if column in row and column not in ("publication_id", "result_id") and value is not None:
                row[column] = value if isinstance(value, str) else str(value)
        row["publication_id"] = publication_id
        row["source_page"] = str(source["page"])
        row["source_fragment"] = literal
        if not row["extraction_status"]:
            row["extraction_status"] = "extracted"
        key = (row["source_page"], row["source_fragment"], row["metric_name"],
               row["metric_value"], row["metric_unit"])
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        rows.append(row)

    for index, row in enumerate(rows, 1):
        row["result_id"] = f"{RESULT_PREFIX}{index:03d}"

    written = 0
    blocks = 0
    # A parse that produced no sources is a failure, not an empty result: it must
    # not wipe rows a previous run wrote. A parse that succeeded replaces them,
    # even when nothing survived the gate. The envelope is written either way
    # (with parser_status 'empty' for a text-layer-less file), because that row is
    # the record of the failure.
    if not dry_run and document.get("pages"):
        path = pathlib.Path(db_path)
        if not path.exists():
            storage.init(path)
        name = storage.table_name(area, table)
        connection = storage.connect(path)
        try:
            with connection:
                blocks = _write_envelope(connection, area, publication_id, pdf_bytes,
                                         document, source_path)
                if sources:
                    connection.execute(
                        f"DELETE FROM {storage.quote(name)} "
                        f"WHERE area = ? AND publication_id = ? AND result_id GLOB ?",
                        (area, publication_id, RESULT_ID_GLOB))
                    if rows:
                        written = _insert(connection, name, area, table, meta, rows)
        finally:
            connection.close()

    return {
        "area": area, "publication_id": publication_id, "table": table,
        "pages": document.get("pages", 0), "sources": len(sources),
        "windows": len(windows), "candidates": len(candidates),
        "verified": verified, "duplicates": duplicates, "rejected": rejected,
        "written": written, "blocks": blocks,
        "parser_status": "ok" if sources else "empty",
        "rejected_reasons": dict(reasons),
        "rejected_examples": examples, "dry_run": bool(dry_run),
    }


# ---------------------------------------------------------------- batch seam

def process(area, publication_id, pdf_path):
    """Processor for ``batch.run``: read one PDF and extract it into the database.

    The signature is exactly what ``batch.py`` calls, ``process(area,
    publication_id, str(path))``, and batch passes no database path. The target is
    therefore read from the ``VINITI_DATA_DB`` environment variable, which
    ``batch.main`` (and ``batch.run``) set from ``--db`` before processing; a
    standalone call falls back to ``storage.DEFAULT_DB``.

    Returns the summary dict of :func:`run`; the batch ignores it, but tests and a
    human reading the log can see what was found and what was rejected.

    Raises ValueError when the PDF yields no text at all (empty text layer, no
    pages). The batch records the message in ``jobs.error``, which is better than
    reporting an empty success.
    """
    path = pathlib.Path(pdf_path)
    data = path.read_bytes()
    db_path = os.environ.get("VINITI_DATA_DB") or storage.DEFAULT_DB
    summary = run(area, publication_id, data, TableFiller(), db_path=db_path,
                  table="results", source_path=str(path))
    if not summary["pages"] or not summary["sources"]:
        raise ValueError(
            f"{publication_id}: PDF не разобран - страниц {summary['pages']}, "
            f"текстовых источников {summary['sources']}; текстовый слой пуст "
            f"или отсутствует, извлекать нечего")
    return summary


# ---------------------------------------------------------------- CLI

def main(argv):
    parser = argparse.ArgumentParser(description="Extract solar results from one PDF")
    parser.add_argument("pdf", type=pathlib.Path)
    parser.add_argument("--publication-id", default=None)
    parser.add_argument("--area", default="solar")
    parser.add_argument("--table", default="results")
    parser.add_argument("--db", type=pathlib.Path, default=storage.DEFAULT_DB)
    parser.add_argument("--budget", type=int, default=DEFAULT_BUDGET)
    parser.add_argument("--overlap", type=int, default=DEFAULT_OVERLAP)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    publication_id = args.publication_id or args.pdf.stem
    summary = run(args.area, publication_id, args.pdf.read_bytes(), TableFiller(),
                  db_path=args.db, dry_run=args.dry_run, table=args.table,
                  budget_chars=args.budget, overlap=args.overlap,
                  source_path=str(args.pdf))
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    if not args.dry_run and summary["written"]:
        name = storage.table_name(args.area, args.table)
        connection = storage.connect(args.db)
        try:
            rows = connection.execute(
                f"SELECT result_id, metric_name, metric_value, metric_unit, "
                f"source_page, source_fragment FROM {storage.quote(name)} "
                f"WHERE area = ? AND publication_id = ? AND result_id GLOB ? "
                f"ORDER BY result_id LIMIT 5",
                (args.area, publication_id, RESULT_ID_GLOB)).fetchall()
        finally:
            connection.close()
        print("\nпервые записанные строки:")
        for row in rows:
            print(f"  {row['result_id']} {row['metric_name']} = {row['metric_value']} "
                  f"{row['metric_unit']} | стр. {row['source_page']} | {row['source_fragment']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
