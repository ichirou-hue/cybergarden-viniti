"""Create and validate deterministic, LLM-ready parsing audit artifacts.

This module is intentionally read-only with respect to the application database.  It
records the output of ``pdf_source`` and ``extract.context_windows`` so parser changes
can be inspected before an LLM is introduced.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

# When invoked as ``python tools/parse_audit.py`` Python puts ``tools`` rather
# than the repository root on sys.path.  Keep the script usable both as a
# module and as the documented root-level entrypoint.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import extract
import pdf_source


SCHEMA_VERSION = 1


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _source_item(source):
    return {key: source[key] for key in ("id", "page", "kind", "bbox", "quote", "table", "row")
            if key in source}


def _atomic_json(path, value, pretty=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2 if pretty else None,
                      sort_keys=True, separators=None if pretty else (",", ":"))
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _atomic_jsonl(path, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True,
                                       separators=(",", ":")) + "\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _quality(parsed):
    sources = parsed["sources"]
    warnings = parsed.get("warnings") or []
    fallback_pages = set(parsed.get("fallback_pages") or [])
    pages = []
    for page in range(1, parsed["pages"] + 1):
        page_sources = [item for item in sources if item.get("page") == page]
        tables = {(item.get("table"), item.get("page")) for item in page_sources
                  if item.get("kind") == "table"}
        empty = any(str(warning).startswith("Страница %s:" % page) for warning in warnings)
        pages.append({
            "page": page,
            "text_chars": len(parsed.get("page_texts", {}).get(page, "") or ""),
            "source_count": len(page_sources),
            "table_count": len(tables),
            # Every source emitted on a fallback page came from the fallback
            # line extractor; source shape remains unchanged.
            "fallback_source_count": len(page_sources) if page in fallback_pages else 0,
            "empty_detection": "empty" if empty else "nonempty",
        })
    return pages


def _validate_quality(quality, pages, sources, warnings):
    """Validate the auditable, source-derived portions of page quality."""
    if not isinstance(quality, list) or len(quality) != pages:
        raise ValueError("page_quality must contain one row per page")
    by_page = {page: [] for page in range(1, pages + 1)}
    for source in sources:
        by_page[source["page"]].append(source)
    for expected_page, row in enumerate(quality, 1):
        if not isinstance(row, dict):
            raise ValueError("page_quality row must be an object")
        required = ("page", "text_chars", "source_count", "table_count",
                    "fallback_source_count", "empty_detection")
        if any(key not in row for key in required) or row["page"] != expected_page:
            raise ValueError("page_quality row is malformed")
        integer_fields = ("text_chars", "source_count", "table_count", "fallback_source_count")
        if any(isinstance(row[key], bool) or not isinstance(row[key], int) or row[key] < 0
               for key in integer_fields):
            raise ValueError("page_quality counts must be non-negative integers")
        page_sources = by_page[expected_page]
        table_count = len({(item.get("table"), item.get("page")) for item in page_sources
                           if item.get("kind") == "table"})
        if row["source_count"] != len(page_sources) or row["table_count"] != table_count:
            raise ValueError("page_quality source counts do not match sources")
        if row["fallback_source_count"] > row["source_count"]:
            raise ValueError("page_quality fallback count exceeds source count")
        expected_empty = "empty" if any(str(w).startswith("Страница %s:" % expected_page)
                                         for w in warnings) else "nonempty"
        if row["empty_detection"] not in ("empty", "nonempty") or row["empty_detection"] != expected_empty:
            raise ValueError("page_quality empty detection is inconsistent")


def build_artifacts(pdf_path, budget_chars=extract.DEFAULT_BUDGET,
                    overlap=extract.DEFAULT_OVERLAP):
    """Parse *pdf_path* and return the four serializable artifact objects."""
    path = Path(pdf_path)
    data = path.read_bytes()
    parsed = pdf_source.extract(data)
    windows = extract.context_windows(parsed["sources"], budget_chars=budget_chars,
                                      overlap=overlap)
    source_map = {source["id"]: source for source in parsed["sources"]}
    # Hash the stable semantic window payload, not dict insertion order or filesystem data.
    for window in windows:
        window["source_ids"] = list(window["sources"])
        window["items"] = [_source_item(source_map[source_id])
                           for source_id in window["source_ids"]]
        window["window_hash"] = hashlib.sha256(_json_bytes({
            "page": window["page"], "source_ids": window["source_ids"],
            "text": window["text"],
        })).hexdigest()
    quality = _quality(parsed)
    document = {
        "schema_version": SCHEMA_VERSION,
        "parser_version": extract.PARSER_VERSION,
        "input": {"path": str(path), "sha256": _sha256(data), "bytes": len(data)},
        "title": parsed.get("title", ""), "author": parsed.get("author", ""),
        "page_count": parsed["pages"], "source_count": len(parsed["sources"]),
        "window_count": len(windows), "warnings": list(parsed.get("warnings") or []),
        "budget_chars": int(budget_chars), "overlap": max(0, int(overlap)),
        "page_quality": quality,
    }
    summary = {
        "schema_version": SCHEMA_VERSION,
        "parser_version": extract.PARSER_VERSION,
        "input_sha256": document["input"]["sha256"],
        "page_count": document["page_count"], "source_count": document["source_count"],
        "window_count": document["window_count"],
        "budget_chars": int(budget_chars), "overlap": max(0, int(overlap)),
        "warnings": document["warnings"], "page_quality": quality,
        "source_ids": [source["id"] for source in parsed["sources"]],
        "window_hashes": [window["window_hash"] for window in windows],
    }
    return document, parsed["sources"], windows, summary


def write_artifacts(pdf_path, out_dir, budget_chars=extract.DEFAULT_BUDGET,
                    overlap=extract.DEFAULT_OVERLAP, pretty=False):
    document, sources, windows, summary = build_artifacts(pdf_path, budget_chars, overlap)
    out = Path(out_dir)
    _atomic_json(out / "document.json", document, pretty)
    _atomic_jsonl(out / "sources.jsonl", sources)
    _atomic_jsonl(out / "windows.jsonl", windows)
    _atomic_json(out / "summary.json", summary, pretty)
    return out


def _load_jsonl(path):
    with Path(path).open(encoding="utf-8") as handle:
        rows = []
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except (json.JSONDecodeError, TypeError) as exc:
                raise ValueError("invalid JSONL at line %s" % line_number) from exc
            if not isinstance(row, dict):
                raise ValueError("JSONL row %s must be an object" % line_number)
            rows.append(row)
        return rows


def validate_artifact(path):
    """Raise ``ValueError`` for a malformed artifact directory, otherwise return True."""
    root = Path(path)
    required = [root / name for name in ("document.json", "sources.jsonl", "windows.jsonl", "summary.json")]
    if not all(item.is_file() for item in required):
        raise ValueError("artifact directory is missing one or more required files")
    try:
        document = json.loads((root / "document.json").read_text(encoding="utf-8"))
        sources = _load_jsonl(root / "sources.jsonl")
        windows = _load_jsonl(root / "windows.jsonl")
        summary = json.loads((root / "summary.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError("invalid JSON artifact") from exc
    if not isinstance(document, dict) or not isinstance(summary, dict):
        raise ValueError("document and summary must be objects")
    if document.get("schema_version") != SCHEMA_VERSION or summary.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported schema_version")
    if document.get("parser_version") != extract.PARSER_VERSION or summary.get("parser_version") != document.get("parser_version"):
        raise ValueError("parser_version mismatch")
    document_input = document.get("input")
    if not isinstance(document_input, dict) or not isinstance(document_input.get("sha256"), str):
        raise ValueError("invalid document input")
    if summary.get("input_sha256") != document_input["sha256"]:
        raise ValueError("input sha256 mismatch")
    pages = document.get("page_count")
    if not isinstance(pages, int) or pages < 1 or document.get("source_count") != len(sources):
        raise ValueError("invalid document counts")
    ids = [source.get("id") for source in sources]
    if any(not isinstance(item, int) for item in ids) or len(set(ids)) != len(ids):
        raise ValueError("source ids must be unique integers")
    source_map = dict(zip(ids, sources))
    for source in sources:
        if any(key not in source for key in ("id", "page", "bbox", "quote", "kind")):
            raise ValueError("source is missing required fields")
        if not isinstance(source.get("page"), int) or not 1 <= source["page"] <= pages:
            raise ValueError("source points to an invalid page")
        if not isinstance(source.get("quote"), str):
            raise ValueError("source quote must be text")
        if not isinstance(source.get("bbox"), list) or len(source["bbox"]) != 4:
            raise ValueError("source bbox is malformed")
    budget = summary.get("budget_chars")
    overlap = summary.get("overlap")
    if not isinstance(budget, int) or budget <= 0 or not isinstance(overlap, int) or overlap < 0:
        raise ValueError("invalid window budget")
    if document.get("budget_chars") != budget or document.get("overlap") != overlap:
        raise ValueError("window configuration mismatch")
    if summary.get("page_count") != pages or summary.get("source_count") != len(sources):
        raise ValueError("summary counts do not match document")
    if document.get("window_count") != len(windows):
        raise ValueError("invalid window count")
    _validate_quality(document.get("page_quality"), pages, sources, document.get("warnings") or [])
    _validate_quality(summary.get("page_quality"), pages, sources, summary.get("warnings") or [])
    for number, window in enumerate(windows, 1):
        if not isinstance(window, dict) or any(key not in window for key in
                                               ("index", "page", "sources", "source_ids", "text", "items", "window_hash")):
            raise ValueError("window is missing required fields")
        source_ids = window.get("source_ids", window.get("sources"))
        if not isinstance(source_ids, list) or not source_ids or any(item not in source_map for item in source_ids):
            raise ValueError("window has invalid source_ids")
        if source_ids != window.get("sources"):
            raise ValueError("sources and source_ids disagree")
        items = window.get("items")
        if not isinstance(items, list) or len(items) != len(source_ids):
            raise ValueError("window items are malformed")
        for source_id, item in zip(source_ids, items):
            if not isinstance(item, dict):
                raise ValueError("window item must be an object")
            canonical = _source_item(source_map[source_id])
            if item != canonical:
                raise ValueError("window item does not match source")
        if any(source_map[item]["page"] != window.get("page") for item in source_ids):
            raise ValueError("window crosses pages")
        expected_text = "\n".join(source_map[item].get("quote") or "" for item in source_ids)
        if window.get("text") != expected_text:
            raise ValueError("window text does not match sources")
        size = sum(len(source_map[item].get("quote") or "") + 1 for item in source_ids)
        if size > budget and not (len(source_ids) == 1 and len(window["text"]) + 1 > budget):
            raise ValueError("window exceeds budget")
        expected_hash = hashlib.sha256(_json_bytes({"page": window["page"],
            "source_ids": source_ids, "text": window["text"]})).hexdigest()
        if window.get("window_hash") != expected_hash or window.get("index") != number:
            raise ValueError("invalid window hash or index")
    if summary.get("source_ids") != ids or summary.get("window_count") != len(windows):
        raise ValueError("summary links do not match artifacts")
    if summary.get("window_hashes") != [window.get("window_hash") for window in windows]:
        raise ValueError("summary window hashes do not match artifacts")
    if summary.get("warnings") != document.get("warnings") or summary.get("page_quality") != document.get("page_quality"):
        raise ValueError("summary quality does not match document")
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", nargs="?", help="input PDF")
    parser.add_argument("--out", help="artifact output directory")
    parser.add_argument("--budget-chars", type=int, default=extract.DEFAULT_BUDGET)
    parser.add_argument("--overlap", type=int, default=extract.DEFAULT_OVERLAP)
    parser.add_argument("--pretty", action="store_true")
    parser.add_argument("--validate", metavar="ARTIFACT_DIR", help="validate existing artifacts")
    args = parser.parse_args(argv)
    try:
        if args.validate:
            validate_artifact(args.validate)
            print("valid")
            return 0
        if not args.pdf or not args.out:
            parser.error("pdf and --out are required unless --validate is used")
        write_artifacts(args.pdf, args.out, args.budget_chars, args.overlap, args.pretty)
        return 0
    except (OSError, ValueError) as exc:
        parser.exit(1, "error: %s\n" % exc)


if __name__ == "__main__":
    raise SystemExit(main())
