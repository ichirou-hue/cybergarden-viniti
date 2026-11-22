"""Command dispatcher for the installed VINITI application."""

import argparse
import runpy
import sys


COMMANDS = {
    "analyze": "viniti.analyze_pdfs",
    "batch": "viniti.batch",
    "db": "viniti.db",
    "evaluate-extraction": "viniti.evaluate_extraction",
    "export-csv": "viniti.export_csv",
    "extract": "viniti.extract",
    "extract-document": "viniti.extract_document",
    "import-gold": "viniti.import_gold",
    "issues": "viniti.issues",
    "parse": "viniti.parse_document",
    "passport": "viniti.passport",
    "pipeline": "viniti.pipeline",
    "rag": "rag.cli",
    "score": "viniti.score",
    "serve-data": "viniti.serve_data",
    "server": "viniti.server",
    "solar-extract": "viniti.solar_extract",
    "solar-offline": "viniti.solar_offline",
}


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(
        prog="viniti",
        description="Scientific PDF processing commands.",
    )
    parser.add_argument("command", choices=sorted(COMMANDS))
    if not arguments or arguments[0] in {"-h", "--help"}:
        parser.print_help()
        return 0

    command = arguments.pop(0)
    if command not in COMMANDS:
        parser.error(f"unknown command: {command}")

    sys.argv = [f"{parser.prog} {command}", *arguments]
    runpy.run_module(COMMANDS[command], run_name="__main__")
    return 0
