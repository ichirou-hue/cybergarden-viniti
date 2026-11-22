"""Launcher for a Datasette "filter constructor" over data/viniti.sqlite3.

Datasette turns the read-only SQLite database into a browsable, faceted web
interface with arbitrary SQL and CSV export of the *filtered* set (not just the
visible page). That is exactly the "pick your fields and rows, then export the
slice" workflow, with no custom frontend.

Datasette is an OPTIONAL dependency. It is intentionally NOT in requirements.txt
(CI runs without it, on Python 3.12). Install it from requirements-data.txt:

    python -m pip install -r requirements-data.txt

Run:
    python -X utf8 serve_data.py                 # serve on 127.0.0.1:8001
    python -X utf8 serve_data.py --port 8002
    python -X utf8 serve_data.py --print-only    # print URLs + SQL, no server

The server is read-only: the database is opened with Datasette's --immutable
flag, so no statement can modify the file.
"""
import argparse
import importlib.util
import pathlib
import shlex
import subprocess
import sys

from viniti import paths

ROOT = paths.REPO_ROOT
DB = paths.DATA_DIR / "viniti.sqlite3"
INSTALL_CMD = "python -m pip install -r requirements-data.txt"


def _datasette_available():
    return importlib.util.find_spec("datasette") is not None


def build_urls(port, db_path):
    """Return the ready-to-use URLs and the SQL they stand for."""
    base = f"http://127.0.0.1:{port}"
    db = db_path.stem  # Datasette names the database after the file stem

    table = f"{base}/{db}/solar_results"
    faceted = f"{base}/{db}/solar_results?_facet=metric_name"
    filtered = (
        f"{base}/{db}/solar_results"
        "?metric_name__exact=PCE&value_num__gt=20&_facet=metric_name"
    )
    csv_slice = (
        f"{base}/{db}/solar_results.csv"
        "?metric_name__exact=PCE&value_num__gt=20&_size=max&_stream=on&_dl=on"
    )
    rag_facet = (
        f"{base}/{db}/rag_results?publication_id__exact=D021&_facet=metric_name"
    )

    return {
        "table": table,
        "faceted": faceted,
        "filtered": filtered,
        "csv_slice": csv_slice,
        "rag_facet": rag_facet,
        "sql_filtered": (
            "SELECT * FROM solar_results "
            "WHERE metric_name = 'PCE' AND value_num > 20;"
        ),
        "sql_rag": (
            "SELECT * FROM rag_results "
            "WHERE publication_id = 'D021' ORDER BY metric_name;"
        ),
    }


def print_urls(port, db_path):
    u = build_urls(port, db_path)
    print("Datasette: конструктор выборки по data/viniti.sqlite3")
    print(f"Сервер: http://127.0.0.1:{port}")
    print()
    print("Готовые ссылки:")
    print(f"  Таблица:            {u['table']}")
    print(f"  Фасеты:             {u['faceted']}")
    print(f"  Фильтр + фасеты:    {u['filtered']}")
    print(f"  CSV всей выборки:   {u['csv_slice']}")
    print(f"  rag по статье:      {u['rag_facet']}")
    print()
    print("Эквивалентный SQL (вкладка /viniti?sql=... или SQL-панель):")
    print(f"  {u['sql_filtered']}")
    print(f"  {u['sql_rag']}")


def serve(port, db_path):
    cmd = [
        sys.executable, "-m", "datasette", "serve",
        "--host", "127.0.0.1",
        "--port", str(port),
        "--immutable", str(db_path),
        "--setting", "max_csv_mb", "0",
        "--setting", "sql_time_limit_ms", "15000",
        "--setting", "default_page_size", "50",
    ]
    print("Запуск:", " ".join(shlex.quote(c) for c in cmd))
    print("(Ctrl+C для остановки)")
    print()
    return subprocess.call(cmd)


def main(argv):
    parser = argparse.ArgumentParser(
        description="Datasette-конструктор выборки над data/viniti.sqlite3")
    parser.add_argument("--port", type=int, default=8001,
                        help="порт (по умолчанию 8001)")
    parser.add_argument("--db", type=pathlib.Path, default=DB,
                        help="путь к базе (по умолчанию data/viniti.sqlite3)")
    parser.add_argument("--print-only", action="store_true",
                        help="напечатать ссылки и SQL, не запуская сервер")
    args = parser.parse_args(argv)

    db_path = args.db.resolve()
    if not db_path.exists():
        print(f"Нет базы {db_path}. Создайте её: python -X utf8 db.py --init",
              file=sys.stderr)
        return 2

    if not _datasette_available():
        print("Datasette не установлен.", file=sys.stderr)
        print(f"Установите его: {INSTALL_CMD}", file=sys.stderr)
        print("(Это необязательная зависимость, в requirements.txt её нет.)",
              file=sys.stderr)
        return 1

    print_urls(args.port, db_path)
    print()

    if args.print_only:
        print("Режим --print-only: сервер не запущен.")
        return 0

    return serve(args.port, db_path)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
