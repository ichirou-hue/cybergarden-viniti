"""Standalone solar extraction; no changes to the shared backend."""

import argparse
import json
import os
from pathlib import Path
import sys
import time
from tempfile import TemporaryDirectory

import llm_provider
from atomic_output import publish_directory
from llm_provider import (
    DEFAULT_MAX_TOKENS, DEFAULT_RETRIES, DEFAULT_TIMEOUT, ENDPOINT,
    MAX_RESPONSE_BYTES, NoRedirect, resolve_config,
)
from prompt_rules import DOCUMENT_RULES
from solar_facts import MAX_BYTES, export_bundle, finalize, prepare_pdf
from solar_schema import TABLES, VOCAB


# Kept as module globals so existing callers and tests can patch them.
urlopen = llm_provider.urlopen
SYSTEM = """You extract scientific data from thin-film solar-cell papers.
""" + DOCUMENT_RULES + """
Return ONLY a JSON object with the seven tables described by the user.
Each table is a list of rows; all CSV field values MUST be strings, unknowns "".
Do not guess missing quantities, conditions, units, aggregation or versions.
Include only experimental single-junction thin-film solar devices, no tandems
and no simulations without fabricated samples. Always provide one publication,
with in_scope "true" or "false" and an exclusion reason if false.
For excluded papers all other tables must be empty.

Use publication_id exactly as provided. Other IDs are unique within the paper.
Every materials/devices/layers/measurements/results row MUST additionally have
source_ids: a nonempty list of integer source IDs supporting the record.
Never generate coordinates: these are attached from the PDF by the application.
For every result, source_fragment is a verbatim substring of ONE supporting
source, including raw_value exactly, and source_page is that source's PDF page
as a string. Include header/units sources in source_ids as needed.
No paraphrases as source_fragment. Preserve all numeric signs and precision.
Preserve raw_value and raw_unit, do NOT calculate normalized values: leave
metric_value, metric_unit, normalized_value, normalized_unit empty.
Units may be canonical spellings (e.g. mV, V, %, fraction, mA/cm2), but only if
explicitly established by the paper. No unit means raw_unit "".
For scalar columns in materials/devices/layers preserve the original numeric
value and explicitly stated unit; the application normalizes them.
Ranges or uncertainties belong in raw_value/note, never invent a central value.
spread_value is an absolute uncertainty in raw_unit, explicitly printed with ±.
For relative or ambiguous uncertainty leave spread_value empty and use note.
Metric names include PCE, Voc, Jsc, FF, Rs, Rsh, HI, Jsc_EQE, stabilized_PCE,
T80, retained_PCE. Missing aggregation is "", not automatically champion.
Separate forward/reverse scans, champion/mean, stability time points and devices.
measurement.device_id can be semicolon-separated only if conditions explicitly
apply to those devices. Do not spread one device's conditions across others.
Own measured/reproduced results are primary. Borrowed numbers are cited;
for cited results leave device_id and measurement_id empty and provide the
paper's reference marker as cited_publication_id.
If a value is truly unavailable (e.g. missing supplement), use
present_not_extractable and an explicit source explaining its presence.
Do not manufacture absent rows for every conceivable metric.
Repeated mentions get their own result_id and source with duplicate_of pointing
to the original ONLY for the same conditions and value. Conflicting values
are NOT duplicates. Leave issues empty: checks are done by the application.
This is extraction, not a summary. Capture all supported records you can,
without inventing records to make the tables appear complete.
"""


def messages(document, max_input_chars=200000):
    sources = [
        {key: source[key] for key in ("id", "page", "kind", "quote", "table", "row") if key in source}
        for source in document["sources"]
    ]
    content = json.dumps({
        "publication_id": document["publication_id"], "columns": TABLES,
        "vocabularies": {f"{t}.{f}": sorted(values) for (t, f), values in VOCAB.items()},
        "sources": sources,
    }, ensure_ascii=False)
    if len(content) > max_input_chars:
        raise ValueError(
            "Документ превышает бюджет входа; текст не обрезан. "
            "Увеличьте --max-input-chars в пределах контекста модели. Chunking пока не реализован."
        )
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": content}]


def complete(messages, *, api_key, model, timeout=DEFAULT_TIMEOUT,
             retries=DEFAULT_RETRIES, max_tokens=DEFAULT_MAX_TOKENS,
             opener=None, sleep=time.sleep):
    """Backward-compatible wrapper over :func:`llm_provider.complete`.

    The wrapper exists so ``solar_extract.MAX_RESPONSE_BYTES`` and
    ``solar_extract.urlopen`` remain patch points for existing callers. New
    code should import :mod:`llm_provider` directly.
    """
    return llm_provider.complete(
        messages, api_key=api_key, model=model, timeout=timeout,
        retries=retries, max_tokens=max_tokens,
        opener=urlopen if opener is None else opener, sleep=sleep,
        response_limit=MAX_RESPONSE_BYTES,
    )


def analyze_pdf(name, data, *, api_key, model, max_input_chars=200000, **options):
    document = prepare_pdf(name, data)
    candidate = complete(messages(document, max_input_chars), api_key=api_key, model=model, **options)
    return finalize(candidate, document, model=model)


def main():
    parser = argparse.ArgumentParser(description="Извлечение solar: PDF → OpenRouter → проверенный JSON и 7 CSV.")
    parser.add_argument("pdf", type=Path)
    parser.add_argument("-o", "--output", required=True, type=Path, help="Новый каталог результата.")
    parser.add_argument("--model", default=os.environ.get("OPENROUTER_MODEL", ""))
    parser.add_argument("--max-input-chars", type=int, default=200000)
    parser.add_argument("--max-tokens", type=int, default=16000)
    parser.add_argument("--timeout", type=int, default=90)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--prepare", action="store_true", help="Подготовить запрос без вызова API и без CSV.")
    args = parser.parse_args()
    try:
        if args.output.exists():
            raise ValueError("Каталог результата уже существует; выберите новый путь.")
        if args.max_input_chars <= 0:
            raise ValueError("--max-input-chars должен быть положительным.")
        with args.pdf.open("rb") as stream:
            data = stream.read(MAX_BYTES + 1)
        document = prepare_pdf(args.pdf.name, data)
        prompt = messages(document, args.max_input_chars)
        if args.prepare:
            # Create exclusively; no overwrite and no API access in this mode.
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with TemporaryDirectory(prefix=".solar-", dir=args.output.parent) as temporary:
                folder = Path(temporary) / "prepared"
                folder.mkdir()
                (folder / "request.json").write_text(json.dumps({
                    "sha256": document["sha256"], "publication_id": document["publication_id"],
                    "model": args.model, "messages": prompt, "warnings": document["warnings"],
                    "status": "prepared_only",
                }, ensure_ascii=False, indent=2), encoding="utf-8")
                publish_directory(folder, args.output)
            print(f"Подготовлен запрос без API: {args.output / 'request.json'}")
            return 0
        api_key, model = resolve_config(model=args.model)
        candidate = complete(
            prompt, api_key=api_key, model=model,
            timeout=args.timeout, retries=args.retries, max_tokens=args.max_tokens,
        )
        result = finalize(candidate, document, model=model)
        export_bundle(result, args.output)
        print(f"Результат: {args.output}; записей: {len(result['tables']['results'])}; "
              f"замечаний: {len(result['tables']['issues'])}")
        for warning in result["warnings"]:
            print(f"Предупреждение: {warning}", file=sys.stderr)
        return 0
    except (OSError, ValueError) as error:
        print(f"solar: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
