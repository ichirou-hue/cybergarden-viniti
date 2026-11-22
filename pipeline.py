"""Весь цикл одной командой: PDF -> база -> находки -> CSV -> гейт организаторов.

Стадии (для каждой области, по порядку):

1. ``batch.run`` - пакетный разбор PDF области в базу (таблица ``jobs``),
   сводка discovered/ok/error/skipped;
2. ``issues.generate`` + ``issues.write`` - машинные находки о несогласованности
   (таблица ``issues``), число находок по check_name;
3. ``export_csv.export_official`` - официальная выгрузка CSV области в
   ``<out>/<area>/``;
4. ``tools.gate.check`` - валидатор организаторов по готовой сдаче.

Области независимы: ошибка в solar не отменяет rag, итоговый код выхода
считается по худшей области.

Коды выхода:
  0 - все области чисто;
  1 - структурные ошибки валидатора хотя бы в одной области;
  2 - были ошибки разбора (batch error) при чистом гейте;
  3 - гейт или пайплайн не смог отработать.

Run:  python -X utf8 pipeline.py --area all --split dev --out out
      python -X utf8 pipeline.py --area solar --limit 1 --skip-gate
      python -X utf8 pipeline.py --area all --dry-run
"""
import argparse
import collections
import pathlib
import sys
import time
import traceback

import batch
import db as storage
import export_csv
import issues
from viniti import diagnostics, paths

logger = diagnostics.get_logger("pipeline")

ROOT = paths.REPO_ROOT

# tools/ не пакет (нет __init__.py), поэтому корень tools кладём в sys.path и
# импортируем gate модулем - так же поступают norm_report.py и spike_docling.py.
sys.path.insert(0, str(ROOT / "tools"))
import gate  # noqa: E402

AREAS = ("solar", "rag")


def run_area(area, args):
    """Все стадии одной области; возвращает исход для итоговой строки."""
    outcome = {
        "area": area,
        "summary": None,
        "batch_error": False,
        "gate": None,
        "gate_skipped": args.skip_gate,
        "pipeline_error": "",
    }
    diagnostics.event(logger, "pipeline.area.start", area=area, split=args.split,
                      db=str(args.db), out=str(args.out),
                      skip_extract=bool(args.skip_extract),
                      skip_issues=bool(args.skip_issues),
                      skip_export=bool(args.skip_export),
                      skip_gate=bool(args.skip_gate))

    if not args.skip_extract:
        started = time.perf_counter()
        summary = batch.run(
            area, args.split, db_path=args.db, resume=args.resume, limit=args.limit,
        )
        outcome["summary"] = summary
        outcome["batch_error"] = bool(summary["error"])
        print(f"[разбор] {area}/{args.split}: discovered {summary['discovered']}, "
              f"ok {summary['ok']}, error {summary['error']}, "
              f"skipped {summary['skipped']}")
        diagnostics.event(logger, "pipeline.stage", stage="extract", area=area,
                          duration_ms=int((time.perf_counter() - started) * 1000),
                          discovered=summary["discovered"], ok=summary["ok"],
                          error=summary["error"], skipped=summary["skipped"])
    else:
        print(f"[разбор] {area}: пропущено (--skip-extract)")

    outdir = args.out / area
    connection = None
    try:
        connection = storage.connect(args.db)
        if not args.skip_issues:
            started = time.perf_counter()
            with connection:
                rows = issues.generate(area, connection)
                written = issues.write(area, connection, rows)
            by_check = collections.Counter(row["check_name"] for row in rows)
            details = ", ".join(f"{name}={count}"
                                for name, count in sorted(by_check.items()))
            print(f"[находки] {area}: {written} находок"
                  + (f" ({details})" if details else ""))
            diagnostics.event(logger, "pipeline.stage", stage="issues", area=area,
                              duration_ms=int((time.perf_counter() - started) * 1000),
                              generated=len(rows), written=written,
                              by_check=dict(by_check))
        else:
            print(f"[находки] {area}: пропущено (--skip-issues)")

        if not args.skip_export:
            started = time.perf_counter()
            spec = export_csv.area_spec(storage.load_passport(), area)
            counts = export_csv.export_official(connection, area, spec, outdir)
            print(f"[выгрузка] {area}: "
                  + ", ".join(f"{table}={count}" for table, count in counts.items())
                  + f" -> {outdir}")
            diagnostics.event(logger, "pipeline.stage", stage="export", area=area,
                              duration_ms=int((time.perf_counter() - started) * 1000),
                              tables=len(counts), rows=sum(counts.values()),
                              out=str(outdir))
        else:
            print(f"[выгрузка] {area}: пропущено (--skip-export)")
    finally:
        if connection is not None:
            connection.close()

    if not args.skip_gate:
        started = time.perf_counter()
        result = gate.check(area, outdir, split=args.split)
        outcome["gate"] = result
        if result.failure:
            print(f"[гейт] {area}: валидатор не отработал: {result.failure}")
            diagnostics.error(logger, "pipeline.gate", area=area,
                              status=result.status)
        else:
            print(f"[гейт] {area}: {result.status} (таблиц {result.tables}, "
                  f"результатов {result.results}, ошибок {result.errors}, "
                  f"ревью {result.review})")
            diagnostics.event(logger, "pipeline.gate", area=area,
                              duration_ms=int((time.perf_counter() - started) * 1000),
                              status=result.status, tables=result.tables,
                              results=result.results, errors=result.errors,
                              review=result.review)
    else:
        print(f"[гейт] {area}: пропущено (--skip-gate)")
    return outcome


def describe(outcome):
    """Короткая оценка области для строки «итог:». """
    area = outcome["area"]
    if outcome.get("pipeline_error"):
        return f"{area}=ошибка пайплайна"
    result = outcome.get("gate")
    if result is None:
        if outcome.get("gate_skipped"):
            return f"{area}=гейт пропущен"
        return f"{area}=без гейта"
    if result.failure:
        return f"{area}=gate failure"
    return f"{area}={result.status}"


def exit_code(outcomes):
    """0 чисто, 1 структурные ошибки, 2 ошибки разбора при чистом гейте, 3 не отработало."""
    if any(outcome.get("pipeline_error") for outcome in outcomes):
        return 3
    if any(outcome.get("gate") is not None and outcome["gate"].failure
           for outcome in outcomes):
        return 3
    if any(outcome.get("gate") is not None and outcome["gate"].errors
           for outcome in outcomes):
        return 1
    if any(outcome.get("batch_error") for outcome in outcomes):
        return 2
    return 0


def plan(args, areas):
    """Печать плана стадий; ничего не выполняется и не пишется."""
    print(f"[план] области: {', '.join(areas)}; split: {args.split}; "
          f"база: {args.db}; сдача: {args.out}")
    for area in areas:
        outdir = args.out / area
        print(f"  {area}:")
        if args.skip_extract:
            print("    1. разбор    пропущен (--skip-extract)")
        else:
            limit = args.limit if args.limit is not None else "без ограничения"
            print(f"    1. разбор    batch.run('{area}', '{args.split}', "
                  f"db_path={args.db}, resume={args.resume}, limit={limit})")
        if args.skip_issues:
            print("    2. находки   пропущен (--skip-issues)")
        else:
            print("    2. находки   issues.generate + issues.write (таблица issues)")
        if args.skip_export:
            print("    3. выгрузка  пропущен (--skip-export)")
        else:
            print(f"    3. выгрузка  export_csv.export_official -> {outdir}")
        if args.skip_gate:
            print("    4. гейт      пропущен (--skip-gate)")
        else:
            print(f"    4. гейт      tools.gate.check('{area}', {outdir}, "
                  f"split='{args.split}')")
    print("сухо: ничего не выполняется и не пишется")


def main(argv):
    diagnostics.configure()
    parser = argparse.ArgumentParser(
        description="Весь цикл одной командой: PDF -> база -> находки -> CSV -> гейт")
    parser.add_argument("--area", choices=("solar", "rag", "all"), default="all",
                        help="область: solar, rag или all (по умолчанию)")
    parser.add_argument("--split", default="dev",
                        help="каталог раздела внутри комплекта (dev, test)")
    parser.add_argument("--db", type=pathlib.Path, default=storage.DEFAULT_DB,
                        help="файл SQLite обеих областей")
    parser.add_argument("--out", type=pathlib.Path, default=pathlib.Path("out"),
                        help="каталог сдачи: <out>/solar и <out>/rag")
    parser.add_argument("--limit", type=int, default=None,
                        help="обработать не больше N публикаций на область (проба)")
    parser.add_argument("--resume", dest="resume", action="store_true", default=True,
                        help="не пересчитывать готовые публикации (по умолчанию)")
    parser.add_argument("--no-resume", dest="resume", action="store_false",
                        help="перезапустить готовые публикации")
    parser.add_argument("--skip-extract", action="store_true",
                        help="не разбирать PDF, брать готовую базу")
    parser.add_argument("--skip-issues", action="store_true",
                        help="не пересчитывать находки")
    parser.add_argument("--skip-export", action="store_true",
                        help="не выгружать CSV")
    parser.add_argument("--skip-gate", action="store_true",
                        help="не гонять валидатор организаторов")
    parser.add_argument("--dry-run", action="store_true",
                        help="показать план стадий, ничего не писать")
    args = parser.parse_args(argv)
    areas = AREAS if args.area == "all" else (args.area,)

    if args.dry_run:
        plan(args, areas)
        return 0

    if args.skip_extract and not args.db.exists():
        hint = (f"python -X utf8 db.py --init --db {args.db}"
                if args.db != storage.DEFAULT_DB
                else "python -X utf8 db.py --init")
        print(f"pipeline: базы {args.db} нет, а стадия разбора пропущена "
              f"(--skip-extract): работать не с чем. Создайте базу ({hint}) "
              f"или снимите --skip-extract.", file=sys.stderr)
        return 3

    outcomes = []
    for area in areas:
        print(f"[область] {area}")
        try:
            with diagnostics.context(area=area):
                outcomes.append(run_area(area, args))
        except KeyboardInterrupt:
            raise
        except SystemExit as exc:
            message = str(exc) or f"код {exc.code}"
            print(f"pipeline: {area}: {message}", file=sys.stderr)
            diagnostics.error(logger, "pipeline.area.failed", area=area,
                              kind="SystemExit")
            outcomes.append({"area": area, "summary": None, "batch_error": False,
                             "gate": None, "gate_skipped": args.skip_gate,
                             "pipeline_error": message})
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            print(f"pipeline: {area}: {message}", file=sys.stderr)
            traceback.print_exc()
            diagnostics.exception(logger, "pipeline.area.failed", area=area,
                                  kind=type(exc).__name__)
            outcomes.append({"area": area, "summary": None, "batch_error": False,
                             "gate": None, "gate_skipped": args.skip_gate,
                             "pipeline_error": message})

    code = exit_code(outcomes)
    print(f"итог: {', '.join(describe(outcome) for outcome in outcomes)} "
          f"-> exit {code}")
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))