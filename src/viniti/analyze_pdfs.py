import argparse
import json
from pathlib import Path
import sys

from pdfminer.pdfdocument import PDFPasswordIncorrect
from pdfminer.pdfparser import PDFSyntaxError

from .library import MAX_BYTES, analyze_pdf


def main():
    parser = argparse.ArgumentParser(description="Analyze PDFs and write one JSON object per line.")
    parser.add_argument("pdf", nargs="+", type=Path)
    parser.add_argument("-o", "--output", type=Path, help="Write UTF-8 JSONL to a file.")
    args = parser.parse_args()
    if args.output and any(path.resolve() == args.output.resolve() for path in args.pdf):
        parser.error("output must be different from all input PDFs")

    if args.output:
        with args.output.open("x", encoding="utf-8", newline="\n") as stream:
            return analyze_files(args.pdf, stream)
    return analyze_files(args.pdf, sys.stdout)


def analyze_files(paths, stream):
    failed = False
    for path in paths:
        try:
            with path.open("rb") as source:
                result = analyze_pdf(path.name, source.read(MAX_BYTES + 1))
            print(json.dumps(result, ensure_ascii=stream is sys.stdout, allow_nan=False),
                  file=stream)
        except (OSError, ValueError, PDFPasswordIncorrect, PDFSyntaxError) as error:
            print(f"{path}: {error}", file=sys.stderr)
            failed = True
    return int(failed)


if __name__ == "__main__":
    sys.exit(main())
