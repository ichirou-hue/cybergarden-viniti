"""Разбор PDF через Docling с OCR и формулами, без связывания фактов LLM."""

import argparse
import json
from pathlib import Path
import sys
import time

from extraction_core import export_result, load_profile, prepare_pdf, run
from solar_facts import MAX_BYTES
from viniti import diagnostics

logger = diagnostics.get_logger("parse_document")


def parse(args):
    started = time.perf_counter()
    if args.parser == "auto":
        args.parser = "docling"
    if args.output.exists():
        raise ValueError("Каталог результата уже существует.")
    if args.charts and args.parser != "docling":
        raise ValueError("--charts требует --parser docling.")
    if args.ocr != "auto" and args.parser != "docling":
        raise ValueError("--ocr full/off требует --parser docling.")
    path = Path(args.pdf)
    with path.open("rb") as stream:
        data = stream.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("PDF больше 40 МиБ.")
    diagnostics.event(logger, "parse.start", parser=args.parser,
                      publication_id=args.publication_id or path.stem,
                      ocr=args.ocr if args.parser == "docling" else "off",
                      charts=bool(args.charts), bytes=len(data))
    profile = load_profile(args.profile)
    if args.parser == "vision":
        from vision_source import prepare_pdf as prepare_vision
        document, assets = prepare_vision(path.name, data)
    elif args.parser == "docling":
        from docling_source import prepare_pdf as prepare_docling
        document, assets = prepare_docling(path.name, data, ocr=args.ocr, charts=args.charts)
    else:
        document, assets = prepare_pdf(path.name, data), None
    diagnostics.event(logger, "parse.prepared", parser=args.parser,
                      pages=document.get("pages", 0),
                      sources=len(document.get("sources") or []))
    if args.publication_id:
        document["publication_id"] = args.publication_id
    result = run(document, profile, mode="scripts")
    result["profile"] = profile
    result["run"]["parser"] = {
        "name": args.parser,
        "ocr": args.ocr if args.parser == "docling" else "off",
        "charts": bool(args.charts),
    }
    result["coverage"]["reason"] = (
        f"Парсер {args.parser}; OCR {args.ocr if args.parser == 'docling' else 'off'}; "
        f"графики {'запрошены' if args.charts else 'выключены'}. "
        "Смысловое связывание научных фактов не выполнялось, кандидаты требуют проверки."
    )
    export_result(result, args.output, assets=assets)
    print(json.dumps({
        "output": str(args.output),
        "pages": result["document"]["pages"],
        "sources": len(result["sources"]),
        "candidates": len(result["candidates"]),
        "warnings": len(result["warnings"]),
    }, ensure_ascii=False))
    diagnostics.event(logger, "parse.done", parser=args.parser,
                      duration_ms=int((time.perf_counter() - started) * 1000),
                      pages=result["document"]["pages"],
                      sources=len(result["sources"]),
                      candidates=len(result["candidates"]),
                      warnings=len(result["warnings"]))
    return 0


def main(argv=None):
    diagnostics.configure()
    parser = argparse.ArgumentParser(
        description="PDF + профиль -> источники, кандидаты и артефакты; auto использует локальный Docling.")
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--profile", required=True, help="solar, custom-example или путь к JSON-профилю.")
    parser.add_argument("--publication-id", default="", help="Исходное имя публикации без расширения.")
    parser.add_argument("-o", "--output", required=True, type=Path)
    parser.add_argument("--parser", choices=["auto", "vision", "pdfplumber", "docling"], default="auto")
    parser.add_argument("--ocr", choices=["auto", "full", "off"], default="auto", help="Только для Docling.")
    parser.add_argument("--charts", action="store_true",
                        help="Только Docling: локальный chart2csv VLM, результаты требуют проверки.")
    args = parser.parse_args(argv)
    try:
        return parse(args)
    except (OSError, ValueError) as error:
        # Emit before the contract line: the server reads the last stderr line
        # as the job error, so ``parse: ...`` must stay last.
        diagnostics.error(logger, "parse.failed", kind=type(error).__name__)
        print(f"parse: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
