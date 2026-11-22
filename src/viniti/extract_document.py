"""CLI for the provisional profile-driven ML contract."""

import argparse
import os
from pathlib import Path
import sys

from .extraction_core import export_result, load_profile, prepare_pdf, run
from .llm_provider import complete, resolve_config
from .solar_facts import MAX_BYTES


def main():
    parser = argparse.ArgumentParser(description="PDF + профиль → кандидаты или связанные данные с источниками.")
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--profile", default="solar", help="solar, universal, custom-example или путь к JSON-профилю.")
    parser.add_argument("--mode", choices=["scripts", "auto", "model"], default="scripts",
                        help="scripts — offline-кандидаты; auto/model отправляют текст внешнему API.")
    parser.add_argument("-o", "--output", required=True, type=Path)
    parser.add_argument("--model", default=os.environ.get("OPENROUTER_MODEL", ""))
    parser.add_argument("--max-input-chars", type=int, default=200000)
    parser.add_argument("--max-tokens", type=int, default=16000)
    parser.add_argument("--timeout", type=int, default=90)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--parser", choices=["pdfplumber", "docling"], default="pdfplumber",
                        help="Docling — optional layout/OCR/изображения; может загружать локальные модели.")
    parser.add_argument("--ocr", choices=["auto", "full", "off"], default="auto", help="Только для Docling.")
    parser.add_argument("--charts", action="store_true",
                        help="Только Docling: локальный chart2csv VLM, загрузка дополнительных весов. Результаты требуют проверки.")
    args = parser.parse_args()
    try:
        if args.output.exists():
            raise ValueError("Каталог результата уже существует.")
        if args.charts and args.parser != "docling":
            raise ValueError("--charts требует --parser docling.")
        if args.ocr != "auto" and args.parser != "docling":
            raise ValueError("--ocr full/off требует --parser docling.")
        profile = load_profile(args.profile)
        api_key = model = ""
        if args.mode != "scripts":
            api_key, model = resolve_config(model=args.model)
            if not api_key or not model:
                raise ValueError("Для API задайте OPENROUTER_API_KEY и OPENROUTER_MODEL или --model.")
        with args.pdf.open("rb") as stream:
            data = stream.read(MAX_BYTES + 1)
        assets = None
        if args.parser == "docling":
            from .docling_source import prepare_pdf as prepare_docling
            document, assets = prepare_docling(args.pdf.name, data, ocr=args.ocr, charts=args.charts)
        else:
            document = prepare_pdf(args.pdf.name, data)
        client = None
        if args.mode != "scripts":
            def client(messages):
                return complete(messages, api_key=api_key, model=model,
                                max_tokens=args.max_tokens, timeout=args.timeout, retries=args.retries)
        result = run(document, profile, mode=args.mode, model_client=client, model=model,
                     max_input_chars=args.max_input_chars)
        export_result(result, args.output, assets=assets)
        print(f"{args.output}: записей={len(result['records'])}, кандидатов={len(result['candidates'])}; "
              f"охват={result['coverage']['status']}, полнота={result['coverage']['completeness']}.")
        return 0
    except (OSError, ValueError) as error:
        print(f"extraction: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
