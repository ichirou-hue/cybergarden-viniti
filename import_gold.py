"""Import the organisers' dev_gold into the local database.

Used as a known-answer test for the whole pipe: gold in, gold out, validator
clean. Nothing here is extraction logic.

Run:  python import_gold.py --area solar
      python import_gold.py --area rag
      python import_gold.py --area all
"""
import argparse
import csv
import pathlib
import sys

import db as storage
from viniti import paths

ROOT = paths.REPO_ROOT
GOLD = {area: root / "dev_gold" for area, root in paths.CORPUS_ROOTS.items()}


def read_csv(path, columns):
    """Read one submission table, keeping every value exactly as written."""
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        present = reader.fieldnames or []
        missing = [c for c in columns if c not in present]
        if missing:
            raise SystemExit(f"{path.name}: нет колонок {', '.join(missing)}")
        extra = [c for c in present if c not in columns]
        if extra:
            # Silently dropping a column would lose data the organisers asked for.
            raise SystemExit(f"{path.name}: лишние колонки {', '.join(extra)} "
                             f"- проверьте, не обновилась ли схема")
        return [{c: (row.get(c) or "") for c in columns} for row in reader]


def to_number(text):
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def check_keys(area, table, meta, rows):
    """Refuse a CSV whose local key repeats inside one publication.

    Keys like L1 or D001 are unique only within a publication, so a repeat means the
    source is broken; INSERT OR REPLACE would otherwise drop a row without a word.
    """
    key = meta["key"]
    if key not in meta["columns"]:
        return
    seen = set()
    for index, row in enumerate(rows, 2):
        identity = (row.get("publication_id", ""), row[key])
        if identity in seen:
            raise SystemExit(f"{area}/{table}: ключ {identity} повторяется "
                             f"(строка {index} в CSV) - импорт остановлен")
        seen.add(identity)


def insert_rows(conn, area, table, meta, rows):
    name = storage.table_name(area, table)
    columns = ["area", "_line"] + meta["columns"]
    mirrors = storage.table_mirrors(table, meta)
    columns += list(mirrors.values())
    placeholders = ", ".join("?" for _ in columns)
    quoted = ", ".join(storage.quote(c) for c in columns)
    statement = f"INSERT INTO {storage.quote(name)} ({quoted}) VALUES ({placeholders})"
    payload = []
    for index, row in enumerate(rows, 1):
        values = [area, index] + [row[c] for c in meta["columns"]]
        values += [to_number(row[column]) for column in mirrors]
        payload.append(values)
    conn.executemany(statement, payload)
    return len(payload)


def import_area(area, gold_dir, db_path, passport):
    spec = passport["areas"][area]
    conn = storage.connect(db_path)
    counts = {}
    try:
        with conn:
            for table, meta in spec["tables"].items():
                path = pathlib.Path(gold_dir) / f"{table}.csv"
                # Clear the area first: a re-import must not leave rows that vanished
                # from the source, or the official export silently grows stale rows.
                conn.execute(
                    f"DELETE FROM {storage.quote(storage.table_name(area, table))} "
                    f"WHERE area = ?", (area,))
                if not path.exists():
                    counts[table] = 0
                    continue
                rows = read_csv(path, meta["columns"])
                check_keys(area, table, meta, rows)
                counts[table] = insert_rows(conn, area, table, meta, rows)
            conn.execute("DELETE FROM documents WHERE area = ?", (area,))
            publications = conn.execute(
                f"SELECT publication_id, title, year, in_scope, exclusion_reason "
                f"FROM {storage.quote(storage.table_name(area, 'publications'))} "
                f"WHERE area = ?", (area,)).fetchall()
            for row in publications:
                conn.execute(
                    "INSERT OR REPLACE INTO documents "
                    "(area, publication_id, title, year, in_scope, exclusion_reason, "
                    " parser_status, source_path) "
                    "VALUES (?, ?, ?, ?, ?, ?, 'imported', ?)",
                    (area, row["publication_id"], row["title"] or "", row["year"] or "",
                     row["in_scope"] or "true", row["exclusion_reason"] or "",
                     str(gold_dir)))
    finally:
        conn.close()
    return counts


def main(argv):
    parser = argparse.ArgumentParser(description="Import dev_gold into the database")
    parser.add_argument("--area", choices=["solar", "rag", "all"], default="all")
    parser.add_argument("--db", type=pathlib.Path, default=storage.DEFAULT_DB)
    args = parser.parse_args(argv)
    passport = storage.load_passport()
    storage.init(args.db, passport)
    areas = ["solar", "rag"] if args.area == "all" else [args.area]
    for area in areas:
        gold = GOLD[area]
        if not gold.exists():
            print(f"{area}: нет {gold}")
            return 1
        counts = import_area(area, gold, args.db, passport)
        total = sum(counts.values())
        print(f"{area}: " + ", ".join(f"{t}={n}" for t, n in counts.items())
              + f" | всего {total}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
