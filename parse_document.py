"""Разбор одного PDF по профилю: источники, кандидаты и артефакты парсера.

По умолчанию модель не вызывается. `--llm-shell mock` прогоняет офлайн-оболочку
LLM (`llm_shell`) поверх готового scripts-паспорта; `--llm-shell api` вызывает
реальную модель через [OI]-совместимый endpoint, ключ и адрес берутся только из
окружения (`OPENAI_API_KEY`, `OPENAI_MODEL`, `OPENAI_BASE_URL`). Паспорт и
официальные CSV при этом не меняются.
"""

import argparse
import json
from pathlib import Path
import sys

import llm_provider
import llm_shell
from extraction_core import export_result, load_profile, prepare_pdf, run
from solar_facts import MAX_BYTES


def parse(args):
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
    profile = load_profile(args.profile)
    if args.llm_shell == "api":
        llm_provider.config_from_env()
    if args.parser == "docling":
        from docling_source import prepare_pdf as prepare_docling
        document, assets = prepare_docling(path.name, data, ocr=args.ocr, charts=args.charts)
    else:
        document, assets = prepare_pdf(path.name, data), None
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
        "Извлечение структуры моделью не выполнялось, кандидаты не являются научными фактами."
    )
    export_result(result, args.output, assets=assets)
    summary = {
        "output": str(args.output),
        "pages": result["document"]["pages"],
        "sources": len(result["sources"]),
        "candidates": len(result["candidates"]),
        "warnings": len(result["warnings"]),
    }
    if args.llm_shell != "off":
        mode = args.llm_shell
        summary["llm"] = llm_shell.summarize(llm_shell.run_bundle(
            args.output, mode=mode, allow_network=mode == "api"))
    print(json.dumps(summary, ensure_ascii=False))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="PDF + профиль -> разбор: источники, кандидаты и артефакты; "
                    "опционально оболочка LLM (mock или api).")
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--profile", required=True, help="solar, custom-example или путь к JSON-профилю.")
    parser.add_argument("--publication-id", default="", help="Исходное имя публикации без расширения.")
    parser.add_argument("-o", "--output", required=True, type=Path)
    parser.add_argument("--parser", choices=["pdfplumber", "docling"], default="pdfplumber")
    parser.add_argument("--ocr", choices=["auto", "full", "off"], default="auto", help="Только для Docling.")
    parser.add_argument("--charts", action="store_true",
                        help="Только Docling: локальный chart2csv VLM, результаты требуют проверки.")
    parser.add_argument("--llm-shell", choices=["off", "mock", "api"], default="off",
                        help="Оболочка LLM: mock пишет кандидаты и аудит офлайн; "
                             "api вызывает модель по OPENAI_API_KEY/OPENAI_MODEL/OPENAI_BASE_URL. "
                             "Паспорт и официальные CSV не меняются.")
    args = parser.parse_args(argv)
    try:
        return parse(args)
    except (OSError, ValueError) as error:
        print(f"parse: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
