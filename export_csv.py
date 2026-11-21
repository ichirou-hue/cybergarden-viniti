"""Export submission CSV from the database.

Two distinct modes that must never be confused:

  official  full schema, column order from passport.json, byte-compatible with
            the organisers' template so their validator accepts it.
  custom    a user-chosen slice: selected columns, filters, sort order. This is
            what the filter UI offers. It is never submitted, because the
            validator rejects any deviation from the full column list.

source_page and source_fragment can never be dropped from a custom export: a
value without its locator is not evidence.

Run:  python export_csv.py --area solar --out out/solar
      python export_csv.py --area rag --out out/rag
      python export_csv.py --area rag --out out/slice.csv --columns metric_name,metric_value \
          --where "metric_unit=percent" --where "metric_value>20" --limit 500

A numeric clause may name a number field (``metric_value>20``) or its mirror
(``value_num>20``); both compare numbers, never text.
"""
import argparse
import csv
import pathlib
import sys

import db as storage

ALWAYS_KEPT = ("source_page", "source_fragment")


def area_spec(passport, area):
    if area not in passport["areas"]:
        raise SystemExit(f"неизвестная область {area}")
    return passport["areas"][area]


# Longest operators first: ">=" must win over ">", "!=" over "=".
OPERATORS = (">=", "<=", "!=", "=", ">", "<", "~")
NUMERIC_OPS = {">=", "<=", ">", "<", "!="}


def parse_clause(clause):
    """Split 'field<op>value' into (field, op, value). Raise on a malformed clause."""
    text = clause.strip()
    for op in OPERATORS:
        index = text.find(op)
        if index > 0:
            field = text[:index].strip()
            value = text[index + len(op):].strip()
            if not field:
                break
            return field, op, value
    raise SystemExit(f"не разобрать фильтр {clause!r}: нужен вид поле=значение, "
                     f"поле>число, поле<=число или поле~подстрока")


def field_set(table, meta):
    """Submission columns plus the numeric mirrors that exist for this table."""
    mirrors = set(storage.table_mirrors(table, meta).values())
    return set(meta["columns"]) | mirrors, mirrors


def numeric_argument(field, value):
    """Parse a filter value as a number, or stop: a numeric clause with text is a typo."""
    try:
        return float(value)
    except ValueError:
        raise SystemExit(f"поле {field!r} числовое, значение {value!r} не число") from None


def require_mirror(conn, area, table, mirror):
    """Stop with a fixable message when the database predates the mirror column."""
    name = storage.table_name(area, table)
    present = {row[1] for row in conn.execute(f"PRAGMA table_info({storage.quote(name)})")}
    if mirror not in present:
        raise SystemExit(f"в таблице {name} нет зеркала {mirror}: "
                         f"запустите python db.py --init, он добавит колонку и заполнит её")


def resolve(conn, area, table, meta, field, op, value):
    """Turn one clause into (sql_fragment, params). Unknown fields are an error, never a silent zero.

    A numeric comparison on a TEXT column would compare lexicographically, where '9.9' > '10.5'.
    Every number field of the passport is therefore compared through its REAL mirror, so range
    filters mean what they say. A clause may name the mirror directly as well (``value_num>20``).
    """
    allowed, mirrors = field_set(table, meta)
    if field not in allowed:
        raise SystemExit(f"нет такого поля {field!r} в таблице {table} области {area}. "
                         f"Доступны: {', '.join(sorted(allowed))}")

    quoted = storage.quote(field)
    if op == "~":
        return f"{quoted} LIKE ?", [f"%{value}%"]
    if field in mirrors:
        return f"{quoted} {op} ?", [numeric_argument(field, value)]
    mirror = storage.table_mirrors(table, meta).get(field)
    if mirror:
        require_mirror(conn, area, table, mirror)
        return f"{storage.quote(mirror)} {op} ?", [numeric_argument(field, value)]
    if op in NUMERIC_OPS:
        # A text field has no mirror, so the old CAST stays: year>2020 and raw_value>0
        # still work. Non-numeric text becomes 0.0 here, which count_unparsed reports.
        return f"CAST({quoted} AS REAL) {op} ?", [numeric_argument(field, value)]
    return f"{quoted} {op} ?", [value]


def count_unparsed(conn, area, table, meta, wheres):
    """How many rows a numeric filter silently excludes because the value is not a number.

    Non-numeric values are common: in the rag area 83% of raw_value is text like '<1' or 'n/a'.
    The caller reports this so a numeric slice never looks complete when it is not.
    """
    mirrors = storage.table_mirrors(table, meta)
    by_mirror = {mirror: column for column, mirror in mirrors.items()}
    name = storage.quote(storage.table_name(area, table))
    report = {}
    for clause in wheres:
        field, op, _ = parse_clause(clause)
        if op not in NUMERIC_OPS:
            continue
        column = by_mirror.get(field, field)
        mirror = mirrors.get(column)
        if mirror:
            total = conn.execute(
                f"SELECT COUNT(*) FROM {name} WHERE area = ?", (area,)).fetchone()[0]
            usable = conn.execute(
                f"SELECT COUNT(*) FROM {name} WHERE area = ? AND {storage.quote(mirror)} IS NOT NULL",
                (area,)).fetchone()[0]
            if usable < total:
                report[column] = total - usable
            continue
        # No mirror: CAST would call every text a 0.0, so count with the writers' rule.
        texts = conn.execute(
            f"SELECT {storage.quote(column)} FROM {name} WHERE area = ?", (area,)).fetchall()
        missing = sum(1 for (text,) in texts if storage.to_number(text) is None)
        if missing:
            report[column] = missing
    return report


def select_rows(conn, area, table, meta, columns, wheres, order, limit):
    name = storage.quote(storage.table_name(area, table))
    quoted = ", ".join(storage.quote(c) for c in columns)
    sql = f"SELECT {quoted} FROM {name} WHERE area = ?"
    params = [area]
    for clause in wheres:
        field, op, value = parse_clause(clause)
        fragment, values = resolve(conn, area, table, meta, field, op, value)
        sql += f" AND {fragment}"
        params.extend(values)
    if order:
        allowed, _ = field_set(table, meta)
        key = order.lstrip("-+").strip()
        if key not in allowed:
            raise SystemExit(f"нет такого поля {key!r} для сортировки в таблице {table}")
        direction = "DESC" if order.startswith("-") else "ASC"
        # Sort on the numeric mirror for a number field: as text, '9.9' sorts above '10.5'.
        sort_key = storage.table_mirrors(table, meta).get(key, key)
        if sort_key != key:
            require_mirror(conn, area, table, sort_key)
        sql += f" ORDER BY {storage.quote(sort_key)} {direction}"
    else:
        sql += " ORDER BY _line"
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))
    return conn.execute(sql, params).fetchall()


def write_table(path, columns, rows, bom, newline):
    encoding = "utf-8-sig" if bom else "utf-8"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding=encoding, newline="") as stream:
        writer = csv.writer(stream, lineterminator=newline)
        writer.writerow(columns)
        for row in rows:
            writer.writerow(["" if row[c] is None else str(row[c]) for c in columns])
    return len(rows)


def export_official(conn, area, spec, outdir):
    counts = {}
    for table, meta in spec["tables"].items():
        rows = select_rows(conn, area, table, meta, meta["columns"], [], None, None)
        counts[table] = write_table(outdir / f"{table}.csv", meta["columns"], rows,
                                    spec["bom"], spec["newline"])
    return counts


def export_custom(conn, area, spec, target, table, columns, wheres, order, limit):
    meta = spec["tables"].get(table)
    if meta is None:
        raise SystemExit(f"нет таблицы {table} в области {area}")
    chosen = [c.strip() for c in columns.split(",") if c.strip()] if columns \
        else list(meta["columns"])
    unknown = [c for c in chosen if c not in meta["columns"]]
    if unknown:
        raise SystemExit(f"нет таких полей: {', '.join(unknown)}")
    for required in ALWAYS_KEPT:
        if required in meta["columns"] and required not in chosen:
            chosen.append(required)
    rows = select_rows(conn, area, table, meta, chosen, wheres, order, limit)
    dropped = count_unparsed(conn, area, table, meta, wheres)
    for mirror, number in dropped.items():
        print(f"внимание: {number} строк таблицы {table} без числового значения в {mirror} "
              f"в фильтр не попали", file=sys.stderr)
    return write_table(target, chosen, rows, spec["bom"], spec["newline"])


def main(argv):
    parser = argparse.ArgumentParser(description="Export submission CSV")
    parser.add_argument("--area", required=True, choices=["solar", "rag"])
    parser.add_argument("--db", type=pathlib.Path, default=storage.DEFAULT_DB)
    parser.add_argument("--out", required=True, type=pathlib.Path)
    parser.add_argument("--table", default="results")
    parser.add_argument("--columns", default="")
    parser.add_argument("--where", action="append", default=[])
    parser.add_argument("--order", default="")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args(argv)
    passport = storage.load_passport()
    spec = area_spec(passport, args.area)
    conn = storage.connect(args.db)
    try:
        custom = bool(args.columns or args.where or args.order or args.limit)
        if custom:
            rows = export_custom(conn, args.area, spec, args.out, args.table,
                                 args.columns, args.where, args.order, args.limit)
            print(f"{args.area}/{args.table}: {rows} строк -> {args.out}")
        else:
            counts = export_official(conn, args.area, spec, args.out)
            print(f"{args.area}: " + ", ".join(f"{t}={n}" for t, n in counts.items()))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
