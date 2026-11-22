#!/usr/bin/env python
"""Measure test coverage of the project's own modules and expose the GAPS.

This is not a score. The useful output is the missed-line list: every missed
range is behaviour that no test has ever executed, and a missed line in an
exporter or in the storage layer is a silent data-loss bug waiting to happen.

    python -X utf8 tools/coverage_report.py
        run the unittest suite under coverage, print a per-module table.
        Exit code 0 even when coverage is low: coverage must not turn CI red.

    python -X utf8 tools/coverage_report.py --gaps
        additionally print, per module, every missed line range with the actual
        source lines, so a human can see which behaviour is untested.

    python -X utf8 tools/coverage_report.py --gaps --module src/viniti/db.py,src/viniti/export_csv.py
        restrict the report to the named modules (table still lists everything).

    python -X utf8 tools/coverage_report.py --threshold 70
        exit 1 when any measured module is below 70 percent. Opt-in gate.

Modules are discovered in ``src/``. The root ``main.py`` is only a CLI entry point.
Excluded: ``test_*.py`` and ``tests/``
(they are the suite),
``__init__.py`` (package markers), ``demo.py`` and ``passport.py``
(fixtures/generator), ``analyze_pdfs.py`` (runs in a subprocess, so in-process
coverage would read 0), ``serve_data.py`` (a thin datasette wrapper), and
anything under ``.venv*``, ``_refs``, ``_corpus``, ``tools``, ``.slim``.

Modules that do not exist yet are simply absent from the table: other agents
are writing them concurrently.
"""
import argparse
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent

EXCLUDE_FILES = {
    "demo.py",            # generates a fixture PDF, needs reportlab
    "passport.py",        # generator for passport.json
    "analyze_pdfs.py",    # exercised through subprocess only
    "serve_data.py",      # thin wrapper around datasette
}
EXCLUDE_DIRS = {".venv", "_refs", "_corpus", "tools", "tests", ".slim", "__pycache__", ".git"}

# Written by other agents right now. Reported as "absent" when missing, never fatal.
WATCHLIST = (
    "src/viniti/norm.py", "src/viniti/extract.py", "src/viniti/verify.py",
)

# Source roots. A new source tree must be added here to be measured.
SOURCE_DIRS = ("src",)


def discover_modules():
    """Production modules under src/, in path order.

    ``tests/`` is not measured: those files are the suite itself, and
    ``run_suite`` already discovers them through the ``tests`` package.
    """
    candidates = []
    for source_root in SOURCE_DIRS:
        directory = ROOT / source_root
        if directory.is_dir():
            candidates.extend(sorted(directory.rglob("*.py")))
    found = []
    for path in candidates:
        if path.name.startswith("test_") or path.name == "__init__.py":
            continue
        if path.name in EXCLUDE_FILES:
            continue
        if any(part in EXCLUDE_DIRS for part in path.relative_to(ROOT).parts):
            continue
        found.append(path)
    return found


def read_lines(path):
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []


def ranges(numbers):
    """[1,2,3,7,9,10] -> [(1,3), (7,7), (9,10)]"""
    out = []
    for number in sorted(numbers):
        if out and number == out[-1][1] + 1:
            out[-1][1] = number
        else:
            out.append([number, number])
    return [(first, last) for first, last in out]


def format_ranges(pairs):
    return ", ".join(str(a) if a == b else f"{a}-{b}" for a, b in pairs)


def run_suite():
    """Run the project's unittest suite in-process. Returns (ok, summary)."""
    loader = unittest.TestLoader()
    try:
        suite = loader.discover(start_dir=str(ROOT), pattern="test_*.py",
                                top_level_dir=str(ROOT))
    except Exception as exc:  # a half-written module must not kill the harness
        return False, f"discovery failed: {exc!r}"
    runner = unittest.TextTestRunner(stream=sys.stderr, verbosity=0)
    result = runner.run(suite)
    ok = result.wasSuccessful()
    summary = (f"tests run={result.testsRun} failures={len(result.failures)} "
               f"errors={len(result.errors)} skipped={len(result.skipped)}")
    return ok, summary


def statement_lines(path):
    """Executable lines via co_lines(), used only by the stdlib fallback."""
    import types
    lines = set()
    try:
        code = compile(path.read_text(encoding="utf-8"), str(path), "exec")
    except (OSError, SyntaxError):
        return lines

    def walk(node):
        for _, _, line in node.co_lines():
            if line:
                lines.add(line)
        for const in node.co_consts:
            if isinstance(const, types.CodeType):
                walk(const)

    walk(code)
    return lines


class Tracer:
    """Minimal sys.settrace line collector, fallback when `coverage` is missing."""

    def __init__(self, files):
        self.files = {str(path): path for path in files}
        self.seen = {name: set() for name in self.files}

    def __enter__(self):
        self._previous = sys.gettrace()
        sys.settrace(self._trace)
        return self

    def __exit__(self, *exc):
        sys.settrace(self._previous)
        return False

    def _trace(self, frame, event, arg):
        name = frame.f_code.co_filename
        if name not in self.files:
            return None
        if event == "line":
            self.seen[name].add(frame.f_lineno)
        return self._trace


def measure_with_trace(modules):
    with Tracer(modules) as tracer:
        ok, summary = run_suite()
    measured = []
    for path in modules:
        name = str(path)
        statements = statement_lines(path)
        missed = statements - tracer.seen.get(name, set())
        measured.append(_row(path, statements, missed))
    return measured, ok, summary, "stdlib trace (coverage not installed)"


def measure_with_coverage(modules):
    import coverage

    cov = coverage.Coverage(
        include=[str(path) for path in modules],
        data_file=None,      # keep data in memory: write no .coverage file
        branch=False,
        config_file=False,
    )
    cov.start()
    try:
        ok, summary = run_suite()
    finally:
        cov.stop()
    measured = []
    for path in modules:
        try:
            _, statements, _, missing, _ = cov.analysis2(str(path))
        except Exception as exc:  # unparsable / vanished file: say so, do not vanish
            row = _row(path, (), ())
            row["error"] = f"{type(exc).__name__}: {exc}"
            measured.append(row)
            continue
        measured.append(_row(path, statements, missing))
    return measured, ok, summary, f"coverage {coverage.__version__}"


def _row(path, statements, missing):
    statements = set(statements)
    missing = set(missing) & statements
    total = len(statements)
    covered = total - len(missing)
    percent = 100.0 * covered / total if total else 0.0
    return {
        "module": path.relative_to(ROOT).as_posix(),
        "path": path,
        "statements": total,
        "missed": len(missing),
        "percent": percent,
        "ranges": ranges(missing),
        "error": None,
    }


def report(rows, engine, ok, summary, threshold, gaps, only):
    print(f"engine: {engine}")
    print(f"tests:  {'PASS' if ok else 'FAIL'} ({summary})")
    absent = [name for name in WATCHLIST if not (ROOT / name).exists()]
    if absent:
        print(f"absent modules skipped: {', '.join(absent)}")
    print()

    if not rows:
        print("no modules measured")
        return 0

    order = sorted(rows, key=lambda row: (row["percent"], row["module"]))
    print(f"{'module':24} {'stmt':>6} {'miss':>6} {'cover':>7}  missed lines")
    print("-" * 88)
    for row in order:
        print(f"{row['module']:24} {row['statements']:6d} {row['missed']:6d} "
              f"{row['percent']:6.1f}%  {format_ranges(row['ranges'])}")
        if row.get("error"):
            print(f"{'':24} {'':6} {'':6} {'':7}  !! unparsable: {row['error']}")
    print("-" * 88)
    total_stmt = sum(row["statements"] for row in rows)
    total_miss = sum(row["missed"] for row in rows)
    total_pct = 100.0 * (total_stmt - total_miss) / total_stmt if total_stmt else 100.0
    print(f"{'TOTAL':24} {total_stmt:6d} {total_miss:6d} {total_pct:6.1f}%")

    if gaps:
        selected = rows if not only else [r for r in rows if r["module"] in only]
        for row in sorted(selected, key=lambda r: (r["percent"], r["module"])):
            print()
            print(f"=== {row['module']}  {row['percent']:.1f}%  "
                  f"missed {format_ranges(row['ranges']) or 'nothing'}")
            lines = read_lines(row["path"])
            for first, last in row["ranges"]:
                print(f"  --- {first}-{last} ---")
                for number in range(first, last + 1):
                    text = lines[number - 1] if number <= len(lines) else ""
                    print(f"  {number:5d} | {text}")

    if threshold is not None:
        below = [row for row in order if row["percent"] < threshold]
        print()
        if below:
            print(f"threshold {threshold:g}% not met by "
                  f"{len(below)} module(s): "
                  + ", ".join(f"{r['module']} {r['percent']:.1f}%" for r in below))
            return 1
        print(f"threshold {threshold:g}% met by every measured module")
    return 0


def main(argv):
    parser = argparse.ArgumentParser(
        description="Test coverage of the project modules, gap-oriented")
    parser.add_argument("--gaps", action="store_true",
                        help="print missed line ranges with their source lines")
    parser.add_argument("--module", default="",
                        help="comma-separated module paths for --gaps output")
    parser.add_argument("--threshold", type=float, default=None,
                        help="exit 1 if any measured module is below this percent")
    args = parser.parse_args(argv)

    modules = discover_modules()
    if not modules:
        print("no modules found to measure")
        return 0
    only = {name.strip() for name in args.module.split(",") if name.strip()}

    try:
        rows, ok, summary, engine = measure_with_coverage(modules)
    except ImportError:
        rows, ok, summary, engine = measure_with_trace(modules)

    return report(rows, engine, ok, summary, args.threshold, args.gaps, only)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
