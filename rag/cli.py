"""Командная строка RAG-модуля.

Примеры::

    python -m rag info
    python -m rag pages D001 --page 6
    python -m rag extract --pubs D001 --out out/submission
    python -m rag evaluate --pred out/submission --per-publication
    python -m rag selfcheck
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from . import __version__
from .csvio import read_submission, write_submission
from .evaluate import evaluate, render_text, report_to_dict
from .extract import extract_document
from .kit import TABLE_ORDER, Kit, load_kit
from .pdf import load_document, ocr_available


def _kit(args: argparse.Namespace) -> Kit:
    return load_kit(Path(args.root) if args.root else None)


def _merge(extractions: list) -> dict[str, list[dict[str, str]]]:
    merged: dict[str, list[dict[str, str]]] = {name: [] for name in TABLE_ORDER}
    for extraction in extractions:
        for table, rows in extraction.tables.items():
            merged[table].extend(rows)
    return merged


def cmd_info(args: argparse.Namespace) -> int:
    kit = _kit(args)
    schema = kit.schema()
    print(f"rag {__version__}")
    print(f"комплект: {kit.root}")
    print(f"схема:    {schema.raw.get('version')}")
    print(f"OCR:      {'доступен' if ocr_available() else 'tesseract не найден'}")
    print()
    for split in ("dev", "test", "gold"):
        ids = kit.publication_ids(split)
        if ids:
            print(f"{split:5s}: {len(ids)} публикаций")
    print()
    for table, columns in schema.tables.items():
        print(f"{table:15s} {len(columns):2d} колонок, ключ {schema.key(table)}")
    return 0


def cmd_pages(args: argparse.Namespace) -> int:
    kit = _kit(args)
    document = load_document(kit.pdf(args.publication_id, args.split))
    pages = (
        [document.page(args.page)]
        if args.page
        else list(document.pages)
    )
    for page in pages:
        print(f"===== стр. {page.number} ({page.source}) =====")
        print(page.text)
    return 0


def cmd_extract(args: argparse.Namespace) -> int:
    kit = _kit(args)
    schema = kit.schema()
    publication_ids = args.pubs or kit.publication_ids(args.split)
    if not publication_ids:
        print(f"нет публикаций в split={args.split}", file=sys.stderr)
        return 1

    extractions = []
    for publication_id in publication_ids:
        path = kit.pdf(publication_id, args.split)
        document = load_document(path)
        if document.is_scan and args.ocr:
            from .pdf import with_ocr

            document = with_ocr(document)
        extraction = extract_document(publication_id, document)
        extractions.append(extraction)
        counts = ", ".join(
            f"{table}={len(rows)}" for table, rows in extraction.tables.items() if rows
        )
        print(f"{publication_id}: {counts}")

    merged = _merge(extractions)
    written = write_submission(args.out, schema, merged)
    print()
    print(f"записано в {Path(args.out).resolve()}")
    for table, count in written.items():
        print(f"  {table:15s} {count}")
    return 0


def cmd_sample(args: argparse.Namespace) -> int:
    """Собрать sample-rag.json для согласования конверта с backend."""
    kit = _kit(args)
    document = load_document(kit.pdf(args.publication_id, args.split))
    extraction = extract_document(args.publication_id, document)

    limit = args.limit
    payload = {
        "schema_version": kit.schema().raw.get("version"),
        "status": "proposal-for-backend",
        "generated_by": f"rag {__version__}",
        "publication": extraction.tables["publications"][0]
        if extraction.tables["publications"]
        else {},
        "entities": {
            "configurations": extraction.tables["configurations"][:limit],
            "datasets": extraction.tables["datasets"],
            "tasks": extraction.tables["tasks"],
            "experiments": extraction.tables["experiments"],
        },
        "results": extraction.tables["results"][: args.results],
        "issues": extraction.tables["issues"],
        "provenance": {
            "pdf": str(kit.pdf(args.publication_id, args.split).name),
            "pages_checked": sorted(
                {
                    row.get("source_page", "")
                    for row in extraction.tables["results"]
                    if row.get("source_page")
                }
            ),
            "verified_by": "manual",
        },
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"записано {out} ({len(payload['results'])} results)")
    return 0


def cmd_evaluate(args: argparse.Namespace) -> int:
    kit = _kit(args)
    schema = kit.schema()
    gold = read_submission(args.gold or kit.dev_gold, schema)
    pred = read_submission(args.pred, schema)
    report = evaluate(
        gold,
        pred,
        gold_dir=args.gold or kit.dev_gold,
        pred_dir=args.pred,
        tolerance=args.tolerance,
    )
    print(render_text(report, per_publication=args.per_publication))
    if args.json:
        path = Path(args.json)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(report_to_dict(report), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print()
        print(f"JSON-отчёт: {path}")
    return 0


def cmd_selfcheck(args: argparse.Namespace) -> int:
    """Проверка самого измерителя: эталон против себя обязан дать 100 %."""
    kit = _kit(args)
    schema = kit.schema()
    gold = read_submission(kit.dev_gold, schema)
    report = evaluate(gold, gold, gold_dir=kit.dev_gold, pred_dir=kit.dev_gold)
    ok = True
    for table, score in report.by_table.items():
        good = score.matched == score.gold == score.pred
        ok = ok and good
        print(f"{table:15s} {'ok' if good else 'FAIL'}  {score.matched}/{score.gold}")
        if not good:
            print(f"  missing: {score.missing_examples[:3]}")
            print(f"  extra:   {score.extra_examples[:3]}")
    print()
    print("измеритель исправен" if ok else "измеритель некорректен")
    return 0 if ok else 1


def cmd_check(args: argparse.Namespace) -> int:
    """Запустить официальный чекер организаторов."""
    kit = _kit(args)
    checker = kit.tools / "check_submission.py"
    if not checker.is_file():
        print(f"чекер не найден: {checker}", file=sys.stderr)
        return 1
    command = [
        sys.executable,
        str(checker),
        str(args.submission),
        str(kit.split_dir(args.split)),
    ]
    if args.ocr:
        command.append("--ocr")
    result = subprocess.run(command)
    return result.returncode


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rag",
        description="Извлечение сведений о RAG-системах из научных публикаций.",
    )
    parser.add_argument("--root", help="корень комплекта участника (иначе автопоиск)")
    sub = parser.add_subparsers(dest="command", required=True)

    info = sub.add_parser("info", help="показать комплект, схему и доступность OCR")
    info.set_defaults(func=cmd_info)

    pages = sub.add_parser("pages", help="вывести текст страниц публикации")
    pages.add_argument("publication_id")
    pages.add_argument("--page", type=int)
    pages.add_argument("--split", default="dev", choices=["dev", "test"])
    pages.set_defaults(func=cmd_pages)

    extract = sub.add_parser("extract", help="извлечь строки и записать сдачу")
    extract.add_argument("--pubs", nargs="*", help="список publication_id (по умолчанию все)")
    extract.add_argument("--split", default="dev", choices=["dev", "test"])
    extract.add_argument("--out", default="out/submission")
    extract.add_argument("--ocr", action="store_true", help="включить OCR для сканов")
    extract.set_defaults(func=cmd_extract)

    sample = sub.add_parser("sample", help="собрать sample-rag.json для backend")
    sample.add_argument("publication_id")
    sample.add_argument("--split", default="dev", choices=["dev", "test"])
    sample.add_argument("--out", default="out/sample-rag.json")
    sample.add_argument("--limit", type=int, default=3, help="сколько конфигураций включить")
    sample.add_argument("--results", type=int, default=8, help="сколько results включить")
    sample.set_defaults(func=cmd_sample)

    evaluate_cmd = sub.add_parser("evaluate", help="сравнить сдачу с dev_gold")
    evaluate_cmd.add_argument("--pred", default="out/submission")
    evaluate_cmd.add_argument("--gold", help="каталог эталона (по умолчанию dev_gold)")
    evaluate_cmd.add_argument("--tolerance", type=float, default=0.0)
    evaluate_cmd.add_argument("--per-publication", action="store_true")
    evaluate_cmd.add_argument("--json", help="куда записать JSON-отчёт")
    evaluate_cmd.set_defaults(func=cmd_evaluate)

    selfcheck = sub.add_parser("selfcheck", help="проверить измеритель на самом эталоне")
    selfcheck.set_defaults(func=cmd_selfcheck)

    check = sub.add_parser("check", help="запустить официальный чекер")
    check.add_argument("--submission", default="out/submission")
    check.add_argument("--split", default="dev", choices=["dev", "test"])
    check.add_argument("--ocr", action="store_true")
    check.set_defaults(func=cmd_check)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
