"""SQLite storage driven by passport.json.

One database holds both areas. Every area gets its own set of tables, named
``<area>_<table>``, so that D001 of solar and D001 of rag never collide.

All submission columns are stored as TEXT. This is deliberate: the organisers'
validators compare values as written (``0.330`` must stay ``0.330``, not
``0.33``), and ``raw_value`` is frequently not a number at all.

Every column whose passport type is ``number`` gets a REAL mirror for range
filters and sorting: ``metric_value`` -> ``value_num``, ``source_page`` ->
``page_num``, and ``<column>_num`` for the rest. The three historical names
(``value_num``, ``norm_num``, ``page_num``) are frozen because exports, the
server and the tests already use them.

Mirrors are schema, not data: an existing database created before this rule
would keep refusing inserts, so ``init()`` adds the missing mirror columns with
ALTER TABLE and backfills them from the TEXT column. That migration is
idempotent.

Run:  python db.py --init        # create schema in data/viniti.sqlite3
      python db.py --stats       # row counts per area and table
"""
import argparse
import json
import pathlib
import sqlite3
import sys

ROOT = pathlib.Path(__file__).resolve().parent
PASSPORT = ROOT / "passport.json"
DEFAULT_DB = ROOT / "data" / "viniti.sqlite3"

# Historical mirror names that exports, the server and the tests already rely on.
LEGACY_MIRRORS = {
    ("results", "metric_value"): "value_num",
    ("results", "normalized_value"): "norm_num",
    ("results", "source_page"): "page_num",
}

# Tables that carry the document envelope; the rest are area payload.
COMMON_TABLES = ("documents", "jobs", "blocks")


def load_passport(path=PASSPORT):
    return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))


def table_name(area, table):
    return f"{area}_{table}"


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def to_number(text):
    """The writers' rule for a mirror: float(text), or None when the text is not a number.

    import_gold.to_number and extract._number keep their own copies; the migration
    must agree with them exactly, otherwise a backfilled mirror and a freshly
    written one would disagree about the same text.
    """
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def mirror_name(table, column):
    """Name of the REAL mirror for one column.

    The three historical names are frozen; everything else is ``<column>_num``.
    """
    return LEGACY_MIRRORS.get((table, column), f"{column}_num")


def numeric_columns(meta):
    """Columns the passport declares as numbers, in schema order."""
    fields = meta.get("fields") or {}
    return [column for column in meta["columns"]
            if (fields.get(column) or {}).get("type") == "number"]


def table_mirrors(table, meta):
    """{column: mirror} for every number field of one table spec.

    Empty when the spec carries no ``fields`` map, which is what a hand-built
    test passport looks like: no types, no mirrors.
    """
    return {column: mirror_name(table, column) for column in numeric_columns(meta)}


def mirror_map(passport=None):
    """{(table, column): mirror} across every area, the shape NUMERIC_MIRROR had.

    Prefer table_mirrors(): a bare (table, column) pair cannot say which area it
    belongs to, and the two areas disagree (``publications.year`` is a number in
    rag and text in solar).
    """
    passport = passport or load_passport()
    mapping = {}
    for spec in passport["areas"].values():
        for table, meta in spec["tables"].items():
            for column, mirror in table_mirrors(table, meta).items():
                mapping.setdefault((table, column), mirror)
    return mapping


def __getattr__(name):
    """Keep ``NUMERIC_MIRROR`` working for outside callers without a literal table.

    Computed on demand from passport.json instead of at import: db.py must stay
    importable even when the passport is unreadable, and a module-level dict
    would go stale as soon as the passport changed.
    """
    if name == "NUMERIC_MIRROR":
        return mirror_map()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def create_common(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS documents (
            area TEXT NOT NULL,
            publication_id TEXT NOT NULL,
            sha256 TEXT,
            title TEXT NOT NULL DEFAULT '',
            year TEXT NOT NULL DEFAULT '',
            in_scope TEXT NOT NULL DEFAULT 'true',
            exclusion_reason TEXT NOT NULL DEFAULT '',
            parser_status TEXT NOT NULL DEFAULT 'pending',
            parser_version INTEGER NOT NULL DEFAULT 0,
            page_count INTEGER NOT NULL DEFAULT 0,
            text_chars INTEGER NOT NULL DEFAULT 0,
            source_path TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (area, publication_id)
        );
        CREATE TABLE IF NOT EXISTS blocks (
            area TEXT NOT NULL,
            publication_id TEXT NOT NULL,
            ordinal INTEGER NOT NULL,
            page INTEGER NOT NULL,
            section TEXT NOT NULL DEFAULT '',
            kind TEXT NOT NULL DEFAULT 'text',
            text TEXT NOT NULL,
            bbox_json TEXT NOT NULL DEFAULT '[]',
            PRIMARY KEY (area, publication_id, ordinal)
        );
        CREATE INDEX IF NOT EXISTS blocks_doc_idx ON blocks (area, publication_id, page);
        CREATE TABLE IF NOT EXISTS jobs (
            area TEXT NOT NULL,
            publication_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued',
            attempts INTEGER NOT NULL DEFAULT 0,
            started_at TEXT NOT NULL DEFAULT '',
            duration_ms INTEGER NOT NULL DEFAULT 0,
            extractor_revision INTEGER NOT NULL DEFAULT 0,
            error TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (area, publication_id)
        );
        CREATE INDEX IF NOT EXISTS jobs_status_idx ON jobs (area, status);
    """)


def create_area(db, area, spec):
    for table, meta in spec["tables"].items():
        name = table_name(area, table)
        columns = ["area TEXT NOT NULL"]
        columns.append("_line INTEGER NOT NULL DEFAULT 0")
        for column in meta["columns"]:
            columns.append(f"{quote(column)} TEXT NOT NULL DEFAULT ''")
        for mirror in table_mirrors(table, meta).values():
            columns.append(f"{quote(mirror)} REAL")
        key = meta["key"]
        if key in meta["columns"]:
            # publications keys on publication_id itself, which is already part of the key.
            parts = ["area", "publication_id"]
            if key != "publication_id":
                parts.append(quote(key))
            columns.append(f"PRIMARY KEY ({', '.join(parts)})")
        db.execute(f"CREATE TABLE IF NOT EXISTS {quote(name)} ({', '.join(columns)})")
        db.execute(f"CREATE INDEX IF NOT EXISTS {quote(name + '_pid_idx')} "
                   f"ON {quote(name)} (area, publication_id)")


def table_columns(db, name):
    """Existing column names of one table, from the schema itself."""
    return {row[1] for row in db.execute(f"PRAGMA table_info({quote(name)})")}


def backfill_mirror(db, name, column, mirror):
    """Copy the parseable numbers of a TEXT column into its REAL mirror.

    The obvious ``UPDATE ... SET mirror = CAST(col AS REAL)`` is wrong in SQLite:
    CAST('EM decrease' AS REAL) is 0.0, not NULL, so every text row would claim a
    number and slip into a numeric filter. The fill therefore uses the writers'
    rule (float() or NULL) row by row.
    """
    rows = db.execute(f"SELECT rowid, {quote(column)} FROM {quote(name)}").fetchall()
    payload = []
    for rowid, text in rows:
        number = to_number(text)
        if number is not None:
            payload.append((number, rowid))
    if payload:
        db.executemany(f"UPDATE {quote(name)} SET {quote(mirror)} = ? WHERE rowid = ?", payload)
    return len(payload)


def migrate_mirrors(db, area, table, meta):
    """Add mirrors this database is missing and fill them from the TEXT columns.

    CREATE TABLE IF NOT EXISTS never alters a table that already exists, so a
    database built before mirrors were generated from the passport would keep
    rejecting every insert. Returns the [(column, mirror)] pairs that were added;
    a second run on a migrated database adds nothing.
    """
    name = table_name(area, table)
    present = table_columns(db, name)
    added = []
    for column, mirror in table_mirrors(table, meta).items():
        if mirror in present:
            continue
        db.execute(f"ALTER TABLE {quote(name)} ADD COLUMN {quote(mirror)} REAL")
        added.append((column, mirror))
    for column, mirror in added:
        backfill_mirror(db, name, column, mirror)
    return added


def init(path=DEFAULT_DB, passport=None):
    passport = passport or load_passport()
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    try:
        with db:
            create_common(db)
            for area, spec in passport["areas"].items():
                create_area(db, area, spec)
                for table, meta in spec["tables"].items():
                    migrate_mirrors(db, area, table, meta)
    finally:
        db.close()
    return path


def connect(path=DEFAULT_DB):
    path = pathlib.Path(path)
    if not path.exists():
        raise SystemExit(f"нет базы {path}: запустите python db.py --init")
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    return db


def stats(path=DEFAULT_DB, passport=None):
    passport = passport or load_passport()
    db = connect(path)
    try:
        for area, spec in passport["areas"].items():
            print(f"[{area}]")
            for table in spec["tables"]:
                count = db.execute(
                    f"SELECT COUNT(*) FROM {quote(table_name(area, table))}").fetchone()[0]
                print(f"  {table:16} {count:7}")
    finally:
        db.close()


def main(argv):
    parser = argparse.ArgumentParser(description="VINITI storage")
    parser.add_argument("--db", type=pathlib.Path, default=DEFAULT_DB)
    parser.add_argument("--init", action="store_true")
    parser.add_argument("--stats", action="store_true")
    args = parser.parse_args(argv)
    if args.init:
        print("создано:", init(args.db))
        return 0
    if args.stats:
        stats(args.db)
        return 0
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
