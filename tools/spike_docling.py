"""Spike: measure table extraction quality per parser on a folder of real PDFs.

Two parsers:

* ``pdfplumber`` - the baseline. Imports ``pdf_source.find_tables`` so it measures
  exactly the path the app uses today (ruling-line tables plus the text-strategy
  fallback), never a reimplementation.
* ``docling`` - optional. Only importable inside an isolated venv, e.g.
  ``.venv-docling\\Scripts\\python.exe tools\\spike_docling.py --parser docling``.

Nothing here is imported by the application. The script only reads PDFs and
prints numbers.

Usage
-----
    python -X utf8 tools/spike_docling.py --parser pdfplumber --pdfs _corpus/area1/dev --limit 3
    python -X utf8 tools/spike_docling.py --parser both --pdfs _corpus/area1/dev --limit 3

Exit codes: 0 measured, 2 requested parser is not installed, 1 bad input.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import time
from dataclasses import asdict, dataclass, field

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


@dataclass
class TableInfo:
    page: int
    rows: int
    cols: int
    cells: int
    non_empty: int

    @property
    def fill(self) -> float:
        return self.non_empty / self.cells if self.cells else 0.0

    @property
    def shape(self) -> str:
        return "%dx%d" % (self.rows, self.cols)


@dataclass
class PdfResult:
    name: str
    parser: str
    tables: list[TableInfo] = field(default_factory=list)
    seconds: float = 0.0
    pages: int = 0
    error: str = ""

    @property
    def table_count(self) -> int:
        return len(self.tables)

    @property
    def mean_fill(self) -> float:
        if not self.tables:
            return 0.0
        return sum(table.fill for table in self.tables) / len(self.tables)


def collect_pdfs(target: str, limit: int) -> list[str]:
    if os.path.isdir(target):
        paths = sorted(glob.glob(os.path.join(target, "*.pdf")))
        paths += sorted(glob.glob(os.path.join(target, "*.PDF")))
    elif os.path.isfile(target):
        paths = [target]
    else:
        paths = sorted(glob.glob(target))
    unique = sorted({os.path.abspath(path) for path in paths})
    if limit and limit > 0:
        unique = unique[:limit]
    return unique


def grid_fill(grid) -> tuple[int, int, int, int]:
    """Rows, columns, total cells and non-empty cells for a list-of-rows grid."""
    rows = len(grid)
    cols = max((len(row) for row in grid), default=0)
    total = 0
    filled = 0
    for row in grid:
        for cell in row:
            total += 1
            text = cell if isinstance(cell, str) else ("" if cell is None else str(cell))
            if text.strip():
                filled += 1
    return rows, cols, total, filled


# --------------------------------------------------------------------------- #
# pdfplumber baseline
# --------------------------------------------------------------------------- #

def run_pdfplumber(path: str) -> PdfResult:
    import pdfplumber

    import pdf_source

    result = PdfResult(name=os.path.basename(path), parser="pdfplumber")
    started = time.perf_counter()
    try:
        with pdfplumber.open(path) as pdf:
            result.pages = len(pdf.pages)
            for page in pdf.pages:
                page = page.dedupe_chars()
                if not page.extract_words(x_tolerance=pdf_source.X_TOLERANCE):
                    page.close()
                    continue
                for table in pdf_source.find_tables(page):
                    rows = table.extract(x_tolerance=pdf_source.X_TOLERANCE)
                    n_rows, n_cols, total, filled = grid_fill(rows)
                    result.tables.append(TableInfo(
                        page=page.page_number, rows=n_rows, cols=n_cols,
                        cells=total, non_empty=filled,
                    ))
                page.close()
    except Exception as exc:
        # pdf_source.find_tables raises on some real corpus pages (see the report);
        # record it per PDF so one bad file does not hide the other measurements.
        result.error = "%s: %s" % (type(exc).__name__, exc)
    result.seconds = time.perf_counter() - started
    return result


# --------------------------------------------------------------------------- #
# Docling (optional)
# --------------------------------------------------------------------------- #

def docling_available() -> tuple[bool, str]:
    try:
        import docling  # noqa: F401
    except Exception as exc:  # pragma: no cover - depends on the environment
        return False, "%s: %s" % (type(exc).__name__, exc)
    try:
        from importlib.metadata import version

        return True, version("docling")
    except Exception:
        return True, "unknown"


def docling_grid(table) -> list[list[str]]:
    """Best-effort text grid out of a Docling TableItem, across API versions."""
    data = getattr(table, "data", None)
    if data is None:
        return []
    grid = getattr(data, "grid", None)
    if grid:
        rows = []
        for row in grid:
            rows.append([
                (getattr(cell, "text", "") or "").strip()
                for cell in row
                if cell is not None
            ])
        return rows
    raw = getattr(data, "data", None)
    if raw:
        return [[("" if cell is None else str(cell)) for cell in row] for row in raw]
    cells = getattr(data, "table_cells", None) or []
    n_cols = getattr(data, "num_cols", 0) or 0
    n_rows = getattr(data, "num_rows", 0) or 0
    if n_rows and n_cols:
        rows = [["" for _ in range(n_cols)] for _ in range(n_rows)]
        for cell in cells:
            try:
                rows[cell.start_row_offset_idx][cell.start_col_offset_idx] = cell.text or ""
            except (IndexError, AttributeError):
                continue
        return rows
    return []


def run_docling(paths: list[str], use_ocr: bool) -> list[PdfResult]:
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption

    options = PdfPipelineOptions()
    options.do_ocr = use_ocr
    options.do_table_structure = True
    converter = DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)}
    )

    results = []
    for path in paths:
        result = PdfResult(name=os.path.basename(path), parser="docling")
        started = time.perf_counter()
        try:
            converted = converter.convert(path)
            document = converted.document
            result.pages = len(getattr(document, "pages", {}) or {})
            for table in getattr(document, "tables", []) or []:
                grid = docling_grid(table)
                n_rows, n_cols, total, filled = grid_fill(grid)
                provenance = getattr(table, "prov", None) or []
                page = getattr(provenance[0], "page_no", 0) if provenance else 0
                result.tables.append(TableInfo(
                    page=int(page or 0), rows=n_rows, cols=n_cols,
                    cells=total, non_empty=filled,
                ))
        except Exception as exc:  # pragma: no cover - depends on models
            result.error = "%s: %s" % (type(exc).__name__, exc)
        result.seconds = time.perf_counter() - started
        results.append(result)
    return results


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #

def print_results(results: list[PdfResult]) -> None:
    for result in results:
        print("%s  [%s]" % (result.name, result.parser))
        if result.error:
            print("  ERROR: %s" % result.error)
            continue
        print("  pages=%d  tables=%d  time=%.1fs" % (
            result.pages, result.table_count, result.seconds,
        ))
        if not result.tables:
            print("  (no tables)")
        for index, table in enumerate(result.tables, 1):
            print("    #%-2d page=%-3d %-7s cells=%-4d non-empty=%.2f" % (
                index, table.page, table.shape, table.cells, table.fill,
            ))


def print_comparison(baseline: list[PdfResult], other: list[PdfResult], other_name: str) -> None:
    by_name = {result.name: result for result in other}
    header = "%-14s %8s %8s %8s %8s %8s %8s" % (
        "pdf", "pl_tab", "doc_tab", "pl_fill", "doc_fill", "pl_s", "doc_s",
    )
    print()
    print("COMPARISON pdfplumber vs %s" % other_name)
    print(header)
    print("-" * len(header))
    for base in baseline:
        rival = by_name.get(base.name)
        print("%-14s %8d %8d %8.2f %8.2f %8.1f %8.1f" % (
            base.name[:14], base.table_count,
            0 if rival is None else rival.table_count,
            base.mean_fill, 0.0 if rival is None else rival.mean_fill,
            base.seconds, 0.0 if rival is None else rival.seconds,
        ))
    print("-" * len(header))
    base_tables = sum(result.table_count for result in baseline)
    other_tables = sum(result.table_count for result in other)
    base_fill = [r.mean_fill for r in baseline if r.tables]
    other_fill = [r.mean_fill for r in other if r.tables]
    print("%-14s %8d %8d %8.2f %8.2f %8.1f %8.1f" % (
        "TOTAL",
        base_tables, other_tables,
        sum(base_fill) / len(base_fill) if base_fill else 0.0,
        sum(other_fill) / len(other_fill) if other_fill else 0.0,
        sum(r.seconds for r in baseline), sum(r.seconds for r in other),
    ))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--parser", choices=["pdfplumber", "docling", "both"],
                        default="pdfplumber")
    parser.add_argument("--pdfs", required=True, help="directory, glob or single PDF")
    parser.add_argument("--limit", type=int, default=3, help="max PDFs, 0 = all")
    parser.add_argument("--ocr", action="store_true",
                        help="docling: enable OCR (off by default, as in the benchmark)")
    parser.add_argument("--json", default="", help="optional path to write raw results")
    args = parser.parse_args(argv)

    paths = collect_pdfs(args.pdfs, args.limit)
    if not paths:
        print("no PDFs matched %r" % args.pdfs, file=sys.stderr)
        return 1

    payload: dict[str, list[dict]] = {}
    results: dict[str, list[PdfResult]] = {}

    if args.parser in ("pdfplumber", "both"):
        results["pdfplumber"] = [run_pdfplumber(path) for path in paths]
        print_results(results["pdfplumber"])
        payload["pdfplumber"] = [asdict(result) for result in results["pdfplumber"]]

    if args.parser in ("docling", "both"):
        installed, detail = docling_available()
        if not installed:
            print(
                "docling is not installed in this interpreter (%s).\n"
                "Install it isolated and rerun with that interpreter.\n"
                "Pin the version: an unpinned install backtracks through every\n"
                "docling release and then tries to build lxml from source.\n"
                "  python -m venv .venv-docling\n"
                "  .\\.venv-docling\\Scripts\\python.exe -m pip install --prefer-binary "
                "\"docling==2.151.1\"\n"
                "  .\\.venv-docling\\Scripts\\python.exe tools\\spike_docling.py "
                "--parser docling --pdfs %s --limit %d" % (detail, args.pdfs, args.limit),
                file=sys.stderr,
            )
            return 2
        print("docling %s" % detail)
        if args.parser == "both":
            print()
        results["docling"] = run_docling(paths, args.ocr)
        print_results(results["docling"])
        payload["docling"] = [asdict(result) for result in results["docling"]]

    if len(results) == 2:
        print_comparison(results["pdfplumber"], results["docling"], "docling")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=1)
        print("\nwrote %s" % args.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
