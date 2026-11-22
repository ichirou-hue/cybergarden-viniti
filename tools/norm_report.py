"""Read-only normalization report over data/viniti.sqlite3.

Answers four questions with real rows, so a human can judge the rules in
``norm.py``:

1. how solar measurements distribute over spectrum classes,
2. how many solar measurements are comparable under ``same_condition`` and
   which publications form the largest comparable group,
3. how many rag ``metric_value`` rows convert between ``fraction`` and ``%``,
   and how many cannot be converted at all,
4. which written values ``parse_quantity`` rejects, with the reason.

The database is opened with ``mode=ro``; this script writes nothing.  If the
database is missing it prints the two commands that create it and exits 2.

Run:  python -X utf8 tools/norm_report.py
      python -X utf8 tools/norm_report.py --db data/viniti.sqlite3
"""
import argparse
from collections import Counter, defaultdict
import pathlib
import sqlite3
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from viniti import norm  # noqa: E402  (path setup must come first)

DEFAULT_DB = ROOT / "data" / "viniti.sqlite3"
INIT_HINT = ("python -X utf8 -m viniti db --init; "
             "python -X utf8 -m viniti import-gold --area all")


def open_readonly(path):
    path = pathlib.Path(path)
    if not path.exists():
        print("нет базы %s" % path)
        print("создайте её: %s" % INIT_HINT)
        return None
    uri = "file:%s?mode=ro" % path.as_posix()
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def section(title):
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def solar_spectrum(db):
    section("1. SOLAR: spectrum classes over solar_measurements")
    rows = db.execute(
        "SELECT spectrum, COUNT(*) AS n FROM solar_measurements "
        "GROUP BY spectrum ORDER BY n DESC, spectrum").fetchall()
    by_class = Counter()
    print("%-40s %-26s %5s" % ("original text", "class", "rows"))
    print("-" * 78)
    for row in rows:
        klass = norm.spectrum_class(row["spectrum"])
        by_class[klass] += row["n"]
        print("%-40s %-26s %5d" % (_short(row["spectrum"]), klass, row["n"]))
    print("-" * 78)
    for klass, count in by_class.most_common():
        print("%-40s %-26s %5d" % ("", klass, count))
    print()
    print("variants whose class is not am15g cannot enter a comparable group:")
    for row in rows:
        info = norm.classify_spectrum(row["spectrum"])
        if info["class"] != norm.SPECTRUM_AM15G:
            print("  %-38s -> %-24s %s"
                  % (_short(row["spectrum"]), info["class"], info["reason"]))


def solar_comparable(db):
    section("2. SOLAR: measurements comparable under same_condition")
    rows = db.execute(
        "SELECT publication_id, measurement_id, spectrum, light_intensity, "
        "       intensity_unit, scan_direction, mask_used "
        "FROM solar_measurements ORDER BY publication_id, measurement_id").fetchall()
    total = len(rows)
    passing = []
    groups = defaultdict(set)
    for row in rows:
        report = norm.condition_report(dict(row))
        if report["ok"]:
            passing.append(row)
            groups[norm.condition_signature(dict(row))].add(row["publication_id"])

    print("measurements total: %d" % total)
    print("comparable (am15g + 1000 W/m2 + reverse + mask true): %d" % len(passing))
    for row in passing:
        print("  %s %s  spectrum=%r  %s %s  scan=%r  mask=%r"
              % (row["publication_id"], row["measurement_id"], row["spectrum"],
                 row["light_intensity"], row["intensity_unit"],
                 row["scan_direction"], row["mask_used"]))

    print()
    print("comparison of a measurement against itself must be True; "
          "against an unrecorded condition, False:")
    sample = dict(rows[0]) if rows else {}
    empty = {"spectrum": "", "light_intensity": "", "intensity_unit": "",
             "scan_direction": "", "mask_used": ""}
    print("  same_condition(standard, standard) = %s"
          % norm.same_condition(sample, sample))
    print("  same_condition(standard, empty)    = %s"
          % norm.same_condition(sample, empty))

    print()
    if not groups:
        print("largest comparable group: none")
        return
    ranked = sorted(groups.items(), key=lambda item: (-len(item[1]), sorted(item[1])))
    signature, publications = ranked[0]
    print("largest comparable group (by number of publications):")
    print("  signature: spectrum=%s irradiance=%s W/m2 scan=%s mask=%s"
          % signature)
    print("  publications: %s (%d)"
          % (", ".join(sorted(publications)), len(publications)))
    if len(ranked) > 1:
        print("  other groups: %s"
              % "; ".join("%s -> %s" % (sig[0], ",".join(sorted(pubs)))
                          for sig, pubs in ranked[1:]))
    expected = {"D001", "D003"}
    if set(publications) == expected:
        print("  matches the verified gold fact: D001 + D003")
    else:
        print("  WARNING: expected D001 + D003, got %s - the rule is wrong"
              % sorted(publications))


def rag_convertibility(db):
    section("3. RAG: metric_value convertibility between fraction and %")
    rows = db.execute(
        "SELECT metric_unit, metric_value FROM rag_results").fetchall()
    total = len(rows)
    convertible = Counter()
    not_convertible = Counter()
    reasons = Counter()
    unparsed = Counter()
    for row in rows:
        unit = norm.ratio_unit_kind(row["metric_unit"])
        value = norm.parse_quantity(row["metric_value"])[0]
        if unit is not None and value is not None:
            other = "percent" if unit == "fraction" else "fraction"
            if norm.convert_fraction_percent(value, unit, other) is not None:
                convertible[(unit, other)] += 1
                continue
        result = norm.normalize_metric("rag", "", row["metric_value"],
                                       row["metric_unit"])
        not_convertible[result["unit_canonical"]] += 1
        reasons[result["reason"] or "converted"] += 1
        if value is None:
            unparsed[norm.parse_status(row["metric_value"])] += 1

    converted_total = sum(convertible.values())
    print("metric_value rows total: %d" % total)
    print("convertible between fraction and %%: %d (%.1f%%)"
          % (converted_total, 100.0 * converted_total / total if total else 0))
    for (source, target), count in sorted(convertible.items()):
        print("  %-10s -> %-10s %6d" % (source, target, count))
    print()
    print("not convertible at all: %d (%.1f%%)"
          % (total - converted_total,
             100.0 * (total - converted_total) / total if total else 0))
    print("  by canonical unit:")
    for unit, count in not_convertible.most_common(12):
        print("    %-32s %6d" % (unit, count))
    print("  by reason:")
    for reason, count in reasons.most_common(6):
        if reason != "converted":
            print("    %-60s %6d" % (_short(reason, 60), count))
    if unparsed:
        print("  unparseable values inside the non-convertible set: %s"
              % ", ".join("%s=%d" % item for item in sorted(unparsed.items())))


def rejected_samples(db):
    section("4. Values that parse_quantity rejects (sample)")
    sources = [
        ("solar_results", "metric_value"),
        ("solar_measurements", "light_intensity"),
        ("rag_results", "metric_value"),
        ("rag_results", "raw_value"),
        ("solar_results", "raw_value"),
    ]
    for table, column in sources:
        rows = db.execute(
            "SELECT %s AS text, COUNT(*) AS n FROM %s GROUP BY %s"
            % (column, table, column)).fetchall()
        statuses = Counter()
        samples = []
        for row in rows:
            status = norm.parse_status(row["text"])
            statuses[status] += row["n"]
            if status != "ok":
                samples.append((row["n"], row["text"], status))
        print()
        print("%s.%s: %d distinct values, statuses %s"
              % (table, column, len(rows),
                 ", ".join("%s=%d" % item for item in sorted(statuses.items()))))
        for count, text, status in sorted(samples, reverse=True)[:10]:
            print("  x%-5d %-28r %s" % (count, _short(text, 28), status))
        accepted = [row for row in rows
                    if norm.parse_status(row["text"]) == "ok"
                    and not _looks_plain(row["text"])]
        for row in accepted[:4]:
            value, leftover = norm.parse_quantity(row["text"])
            print("  accepted: %-20r -> %s %r"
                  % (_short(row["text"], 20), value, leftover))


def _looks_plain(text):
    """True for a value a naive float() would also accept."""
    try:
        float(text)
    except (TypeError, ValueError):
        return False
    return True


def _short(text, limit=40):
    text = "" if text is None else str(text)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1] + "~"


def main(argv):
    parser = argparse.ArgumentParser(description="normalization report (read-only)")
    parser.add_argument("--db", type=pathlib.Path, default=DEFAULT_DB)
    args = parser.parse_args(argv)
    db = open_readonly(args.db)
    if db is None:
        return 2
    try:
        print("db: %s (read-only)" % args.db)
        solar_spectrum(db)
        solar_comparable(db)
        rag_convertibility(db)
        rejected_samples(db)
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
