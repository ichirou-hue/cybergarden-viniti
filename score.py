"""Score a predicted submission against a reference: precision, recall, F1.

This is a SCORER, not the organisers' validator. `tools/check_submission.py`
checks format, cross-references and whether a quote literally occurs in the PDF;
it never opens dev_gold and it reports `semantic_completeness_certified: false`.
This script answers a different question: how many gold rows did the prediction
reproduce, and how many rows did it invent.

What is compared
----------------
Every table of the area (7 tables) is matched independently:

* `results` is matched by identity, never by `result_id` (ids are arbitrary per
  publication) and never by the number alone:

      solar: (publication_id, device_id, measurement_id, metric_name, normalized_value)
      rag:   (publication_id, config_id, dataset_id, task_id, metric_name,
              metric_variant, normalized_value)

  The non-value part is a bucket; inside a bucket the values are matched as a
  multiset, so two identical keys with the same value count as two results.
  In rag, D001 alone holds 61 (metric, value) pairs where one number appears
  under different `config_id` values; merging on value would hide 131 rows.

* every other table is matched by (publication_id, <table key>).

Values are compared as written. A coarser explicitly reported rounding still
matches (`0.33` predicted against `0.330` gold), which is an independent
implementation of the rag validator's `same_at_reported_precision` rule
(Decimal, ROUND_HALF_UP). Nothing is imported from the validator and no value
is ever rewritten.

Near matches
------------
For `results` a second pass reports pairs inside the same bucket that are not
equal but differ only in the last digit (`0.330` vs `0.331`) or by unit scale
(`71.0` vs `0.71`, percent against fraction). They are reported separately and
never counted as matches.

What this does NOT measure
--------------------------
* Quote fidelity: whether `source_fragment` occurs on `source_page` of the PDF.
  That is the validator's job and needs the PDFs.
* Schema conformance, enum membership, cross-references between tables, BOM and
  column order. Again the validator.
* The payload of the non-`results` tables: those are matched by
  (publication_id, key) identity only, so a row with the right id and a wrong
  title still counts as a match.
* Whether a gold row is itself correct or complete.
* Ordering, formatting and `result_id` consistency of the prediction.
* Coverage of publications that are in the corpus but absent from the gold.

Usage
-----
    python -X utf8 score.py --area solar --pred out/solar --ref _corpus/area1/dev_gold
    python -X utf8 score.py --area rag --pred out/rag --ref db
    python -X utf8 score.py --area solar --pred out/solar --ref db --json

`--area all` scores both areas; then `--pred` must contain `solar/` and `rag/`
subdirectories and `--ref` is either `db` or a directory with the same layout.

Exit codes: 0 scored, 1 unreadable input or missing path, 2 bad arguments.
"""
import argparse
import csv
import json
import pathlib
import sqlite3
import sys
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from viniti import paths

ROOT = paths.REPO_ROOT
PASSPORT_PATH = paths.PASSPORT
DEFAULT_DB = paths.DATA_DIR / "viniti.sqlite3"

AREAS = ("solar", "rag")

# Identity of a measured result. `result_id` is deliberately absent.
RESULT_KEY = {
    "solar": ("publication_id", "device_id", "measurement_id", "metric_name"),
    "rag": ("publication_id", "config_id", "dataset_id", "task_id",
            "metric_name", "metric_variant"),
}
RESULT_VALUE = "normalized_value"
METRIC_COLUMN = "metric_name"

# Above this many pairs inside one bucket the matching falls back to greedy
# pairing instead of maximum bipartite matching. Real buckets hold < 20 rows.
GREEDY_LIMIT = 250_000
RECURSION_LIMIT = 800

UNIT_SCALE = (Decimal(100), Decimal("0.01"))

NOT_MEASURED = [
    "quote fidelity against the PDF text (validator's job)",
    "schema conformance, enums, cross-references, BOM and column order",
    "values of non-key columns in tables other than results "
    "(only row identity is compared there)",
    "correctness or completeness of the gold itself",
    "ordering, formatting and result_id consistency of the prediction",
    "publications present in the corpus but absent from the gold",
]


# --------------------------------------------------------------------------- #
# input
# --------------------------------------------------------------------------- #

def load_passport(path=PASSPORT_PATH):
    return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))


class DirSource:
    """A directory of the 7 submission CSVs."""

    kind = "dir"

    def __init__(self, folder, passport):
        self.folder = pathlib.Path(folder)
        self.passport = passport
        self.missing = []
        if not self.folder.is_dir():
            raise SystemExit(f"нет каталога {self.folder}")

    def rows(self, area, table):
        columns = self.passport["areas"][area]["tables"][table]["columns"]
        path = self.folder / f"{table}.csv"
        if not path.exists():
            self.missing.append(table)
            return []
        with path.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            fieldnames = reader.fieldnames or []
            rows = []
            for raw in reader:
                row = {c: (raw.get(c) or "").strip() for c in columns}
                if not any(row.values()) and not any(raw.get(c) for c in fieldnames):
                    continue  # blank line
                rows.append(row)
            return rows

    def label(self):
        return str(self.folder)


class DbSource:
    """The imported organisers' gold in data/viniti.sqlite3."""

    kind = "db"

    def __init__(self, path, passport):
        self.path = pathlib.Path(path)
        self.passport = passport
        self.missing = []
        if not self.path.exists():
            raise SystemExit(
                f"нет базы {self.path}: запустите python -X utf8 db.py --init "
                f"и python -X utf8 import_gold.py --area all")

    def rows(self, area, table):
        columns = self.passport["areas"][area]["tables"][table]["columns"]
        quoted = ", ".join('"' + c.replace('"', '""') + '"' for c in columns)
        name = f"{area}_{table}"
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            cursor = connection.execute(
                f'SELECT {quoted} FROM "{name}" WHERE area = ? ORDER BY _line', (area,))
            return [{c: (row[c] or "").strip() for c in columns} for row in cursor]
        except sqlite3.OperationalError as exc:
            raise SystemExit(f"{name}: {exc}; запустите python -X utf8 db.py --init")
        finally:
            connection.close()

    def label(self):
        return "db"


# --------------------------------------------------------------------------- #
# value comparison
# --------------------------------------------------------------------------- #

def to_decimal(text):
    try:
        value = Decimal(str(text).strip())
    except (InvalidOperation, ValueError):
        return None
    return value if value.is_finite() else None


def values_equal(predicted, reference):
    """Accept a coarser explicitly reported rounding; never rewrite either value.

    Independent implementation of the rule used by the rag validator's
    `same_at_reported_precision`, kept local on purpose: the validator is not a
    dependency of this repo.
    """
    if predicted == reference:
        return True
    left, right = to_decimal(predicted), to_decimal(reference)
    if left is None or right is None:
        return False
    if left == right:
        return True
    left_exp, right_exp = left.as_tuple().exponent, right.as_tuple().exponent
    if left_exp == right_exp:
        return False
    coarse, fine = (left, right) if left_exp > right_exp else (right, left)
    try:
        step = Decimal(1).scaleb(coarse.as_tuple().exponent)
        return fine.quantize(step, rounding=ROUND_HALF_UP) == coarse
    except (InvalidOperation, ValueError):
        return False


def near_kind(predicted, reference):
    """Return 'unit_scale', 'last_digit' or None for a pair that is not equal."""
    left, right = to_decimal(predicted), to_decimal(reference)
    if left is None or right is None or left == right:
        return None
    if left != 0 and right != 0:
        try:
            if (left / right) in UNIT_SCALE:
                return "unit_scale"
        except (InvalidOperation, ZeroDivisionError):
            pass
    left_exp, right_exp = left.as_tuple().exponent, right.as_tuple().exponent
    if left_exp == right_exp and abs(left - right) == Decimal(1).scaleb(left_exp):
        return "last_digit"
    return None


# --------------------------------------------------------------------------- #
# matching
# --------------------------------------------------------------------------- #

def _group(rows, key_columns):
    groups = {}
    for row in rows:
        groups.setdefault(tuple(row.get(c, "") for c in key_columns), []).append(row)
    return groups


def _bipartite(left, right, compatible):
    """Maximum bipartite matching (Kuhn). Returns a list of (i, j) pairs."""
    if not left or not right:
        return []
    if len(left) * len(right) > GREEDY_LIMIT or max(len(left), len(right)) > RECURSION_LIMIT:
        used, pairs = set(), []
        for i in range(len(left)):
            for j in range(len(right)):
                if j not in used and compatible(left[i], right[j]):
                    used.add(j)
                    pairs.append((i, j))
                    break
        return pairs
    adjacency = [[j for j in range(len(right)) if compatible(left[i], right[j])]
                 for i in range(len(left))]
    match_right = [-1] * len(right)

    def augment(i, seen):
        for j in adjacency[i]:
            if seen[j]:
                continue
            seen[j] = True
            if match_right[j] == -1 or augment(match_right[j], seen):
                match_right[j] = i
                return True
        return False

    for i in range(len(left)):
        augment(i, [False] * len(right))
    return [(i, j) for j, i in enumerate(match_right) if i != -1]


def _match_bucket(preds, refs, value_field, near_counts, near_pairs):
    """Pair rows inside one bucket: exact text, then tolerance, then near misses."""
    pairs = []
    by_value = {}
    for j, row in enumerate(refs):
        by_value.setdefault(row[value_field], []).append(j)
    used = set()
    remaining_preds = []
    for row in preds:
        candidates = by_value.get(row[value_field])
        if candidates:
            j = candidates.pop()
            used.add(j)
            pairs.append((row, refs[j]))
        else:
            remaining_preds.append(row)
    remaining_refs = [refs[j] for j in range(len(refs)) if j not in used]

    tolerant = _bipartite(remaining_preds, remaining_refs,
                          lambda p, r: values_equal(p[value_field], r[value_field]))
    taken_pred = {i for i, _ in tolerant}
    taken_ref = {j for _, j in tolerant}
    pairs.extend((remaining_preds[i], remaining_refs[j]) for i, j in tolerant)

    left = [p for i, p in enumerate(remaining_preds) if i not in taken_pred]
    right = [r for j, r in enumerate(remaining_refs) if j not in taken_ref]
    near = _bipartite(left, right,
                      lambda p, r: near_kind(p[value_field], r[value_field]) is not None)
    for i, j in near:
        kind = near_kind(left[i][value_field], right[j][value_field])
        near_counts[kind] = near_counts.get(kind, 0) + 1
        near_pairs.append((kind, left[i], right[j]))

    # Near misses stay in the leftovers: they are diagnostics, not matches, so
    # they must still count against precision and recall.
    return pairs, left, right


def match_table(pred_rows, ref_rows, key_columns, value_field=None):
    """Match two row lists. Returns matched pairs plus the unmatched leftovers."""
    pred_groups = _group(pred_rows, key_columns)
    ref_groups = _group(ref_rows, key_columns)
    matched, pred_only, ref_only = [], [], []
    near_counts, near_pairs = {}, []

    for bucket in set(pred_groups) | set(ref_groups):
        preds = pred_groups.get(bucket, [])
        refs = ref_groups.get(bucket, [])
        if not preds or not refs:
            pred_only.extend(preds)
            ref_only.extend(refs)
            continue
        if value_field is None:
            count = min(len(preds), len(refs))
            matched.extend(zip(preds[:count], refs[:count]))
            pred_only.extend(preds[count:])
            ref_only.extend(refs[count:])
            continue
        pairs, leftover_preds, leftover_refs = _match_bucket(
            preds, refs, value_field, near_counts, near_pairs)
        matched.extend(pairs)
        pred_only.extend(leftover_preds)
        ref_only.extend(leftover_refs)

    return {"matched": matched, "pred_only": pred_only, "ref_only": ref_only,
            "near": near_pairs, "near_counts": near_counts}


# --------------------------------------------------------------------------- #
# scoring
# --------------------------------------------------------------------------- #

def _prf(matched, pred_count, ref_count):
    if pred_count:
        precision = matched / pred_count
    else:
        precision = 1.0 if ref_count == 0 else 0.0
    if ref_count:
        recall = matched / ref_count
    else:
        recall = 1.0 if pred_count == 0 else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return precision, recall, f1


def _sample(row, key_columns, value_field=None):
    parts = [f"{c}={row.get(c, '')}" for c in key_columns]
    if value_field:
        parts.append(f"{value_field}={row.get(value_field, '')}")
    return " | ".join(parts)


def _table_key(area, table, passport):
    meta = passport["areas"][area]["tables"][table]
    key = meta["key"]
    return ("publication_id", key) if key != "publication_id" else ("publication_id",)


def score_table(area, table, pred_rows, ref_rows, passport, samples=3):
    meta = passport["areas"][area]["tables"][table]
    is_results = table == "results"
    key_columns = (RESULT_KEY[area] if is_results
                   else _table_key(area, table, passport))
    value_field = RESULT_VALUE if is_results else None

    result = match_table(pred_rows, ref_rows, key_columns, value_field)
    matched = len(result["matched"])
    precision, recall, f1 = _prf(matched, len(pred_rows), len(ref_rows))

    report = {
        "pred_rows": len(pred_rows),
        "ref_rows": len(ref_rows),
        "matched": matched,
        "pred_only": len(result["pred_only"]),
        "ref_only": len(result["ref_only"]),
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "key": list(key_columns) + ([value_field] if value_field else []),
        "pred_only_samples": [_sample(r, key_columns, value_field)
                              for r in result["pred_only"][:samples]],
        "ref_only_samples": [_sample(r, key_columns, value_field)
                             for r in result["ref_only"][:samples]],
    }

    if is_results:
        counts = result["near_counts"]
        report["near_match"] = {
            "total": sum(counts.values()),
            "unit_scale": counts.get("unit_scale", 0),
            "last_digit": counts.get("last_digit", 0),
            "samples": [f"{kind}: " + _sample(p, key_columns, value_field)
                        + "  vs  " + _sample(r, key_columns, value_field)
                        for kind, p, r in result["near"][:samples]],
        }
        report["by_metric"] = _by_metric(
            result, key_columns, value_field, METRIC_COLUMN)
    return report


def _by_metric(result, key_columns, value_field, metric_column):
    buckets = {}

    def bucket(row):
        return buckets.setdefault(row.get(metric_column, ""), {
            "metric_name": row.get(metric_column, ""), "matched": 0,
            "pred_only": 0, "ref_only": 0})

    for pred_row, _ in result["matched"]:
        bucket(pred_row)["matched"] += 1
    for row in result["pred_only"]:
        bucket(row)["pred_only"] += 1
    for row in result["ref_only"]:
        bucket(row)["ref_only"] += 1

    rows = []
    for entry in buckets.values():
        pred_count = entry["matched"] + entry["pred_only"]
        ref_count = entry["matched"] + entry["ref_only"]
        precision, recall, f1 = _prf(entry["matched"], pred_count, ref_count)
        rows.append({**entry, "pred_rows": pred_count, "ref_rows": ref_count,
                     "precision": precision, "recall": recall, "f1": f1})
    rows.sort(key=lambda r: (-(r["pred_rows"] + r["ref_rows"]), r["metric_name"]))
    return rows


def score_area(area, pred_source, ref_source, passport, samples=3):
    if area not in AREAS:
        raise SystemExit(f"неизвестная область {area}: ожидается {', '.join(AREAS)}")
    tables = list(passport["areas"][area]["tables"])
    order = {name: index for index, name in enumerate(tables)}
    tables.sort(key=lambda t: (t != "results", order[t]))

    report = {"area": area, "pred": pred_source.label(), "ref": ref_source.label(),
              "key": list(RESULT_KEY[area]) + [RESULT_VALUE], "tables": {}}
    totals = {"matched": 0, "pred_rows": 0, "ref_rows": 0}
    for table in tables:
        pred_rows = pred_source.rows(area, table)
        ref_rows = ref_source.rows(area, table)
        entry = score_table(area, table, pred_rows, ref_rows, passport, samples)
        report["tables"][table] = entry
        totals["matched"] += entry["matched"]
        totals["pred_rows"] += entry["pred_rows"]
        totals["ref_rows"] += entry["ref_rows"]

    precision, recall, f1 = _prf(
        totals["matched"], totals["pred_rows"], totals["ref_rows"])
    report["totals"] = {**totals, "precision": precision,
                        "recall": recall, "f1": f1}
    report["missing_pred_tables"] = sorted(pred_source.missing)
    return report


# --------------------------------------------------------------------------- #
# output
# --------------------------------------------------------------------------- #

def _table_lines(headers, rows, left_columns=2):
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))

    def render(cells):
        parts = []
        for i, cell in enumerate(cells):
            text = str(cell)
            parts.append(text.ljust(widths[i]) if i < left_columns
                         else text.rjust(widths[i]))
        return "  ".join(parts).rstrip()

    return [render(headers)] + [render(row) for row in rows]


def _score_cells(entry):
    return [f"{entry['precision']:.4f}", f"{entry['recall']:.4f}",
            f"{entry['f1']:.4f}"]


def format_human(report, top=10):
    lines = []
    for area, data in report["areas"].items():
        lines.append(f"area {area}   pred {data['pred']}   ref {data['ref']}")
        lines.append("key " + ", ".join(data["key"])
                     + " (coarser reported rounding accepted)")
        if data["missing_pred_tables"]:
            lines.append("missing in prediction: " + ", ".join(data["missing_pred_tables"]))
        lines.append("")

        rows = []
        for table, entry in data["tables"].items():
            rows.append([table, entry["pred_rows"], entry["ref_rows"],
                         entry["matched"], entry["pred_only"], entry["ref_only"]]
                        + _score_cells(entry))
        totals = data["totals"]
        rows.append(["TOTAL", totals["pred_rows"], totals["ref_rows"],
                     totals["matched"],
                     totals["pred_rows"] - totals["matched"],
                     totals["ref_rows"] - totals["matched"]]
                    + _score_cells(totals))
        lines.extend(_table_lines(
            ["table", "pred", "ref", "matched", "pred_only", "ref_only",
             "precision", "recall", "f1"], rows))
        lines.append("")

        results = data["tables"].get("results")
        if results:
            lines.append(f"results by {METRIC_COLUMN} (top {top})")
            metric_rows = []
            for entry in results["by_metric"][:top]:
                metric_rows.append([entry["metric_name"], entry["pred_rows"],
                                    entry["ref_rows"], entry["matched"],
                                    entry["pred_only"], entry["ref_only"]]
                                   + _score_cells(entry))
            lines.extend(_table_lines(
                [METRIC_COLUMN, "pred", "ref", "matched", "pred_only",
                 "ref_only", "precision", "recall", "f1"], metric_rows, 1))
            near = results["near_match"]
            lines.append(f"near matches: {near['total']} "
                         f"(unit_scale {near['unit_scale']}, "
                         f"last_digit {near['last_digit']})")
            for sample in near["samples"]:
                lines.append("  " + sample)
            lines.append("")

    lines.append("not measured: " + "; ".join(NOT_MEASURED))
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def build_report(area, pred, ref, db_path, passport, samples=3):
    areas = list(AREAS) if area == "all" else [area]
    reports = {}
    for name in areas:
        pred_source = _resolve_pred(pred, name, area == "all", passport)
        ref_source = _resolve_ref(ref, name, area == "all", db_path, passport)
        reports[name] = score_area(name, pred_source, ref_source, passport, samples)
    return {"pred": pred, "ref": ref, "not_measured": list(NOT_MEASURED),
            "areas": reports}


def _resolve_pred(pred, area, multi, passport):
    root = pathlib.Path(pred)
    if multi:
        folder = root / area
        if not folder.is_dir():
            raise SystemExit(f"--area all ожидает подкаталог {folder}")
        return DirSource(folder, passport)
    return DirSource(root, passport)


def _resolve_ref(ref, area, multi, db_path, passport):
    if str(ref).lower() == "db":
        return DbSource(db_path, passport)
    root = pathlib.Path(ref)
    if multi:
        folder = root / area
        if not folder.is_dir():
            raise SystemExit(f"--area all ожидает подкаталог {folder} или --ref db")
        return DirSource(folder, passport)
    return DirSource(root, passport)


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Score a predicted submission against a reference (gold).",
        epilog="Not measured: " + "; ".join(NOT_MEASURED))
    parser.add_argument("--area", choices=["solar", "rag", "all"], required=True)
    parser.add_argument("--pred", required=True,
                        help="directory with the 7 submission CSVs")
    parser.add_argument("--ref", required=True,
                        help="reference directory of CSVs, or 'db'")
    parser.add_argument("--db", type=pathlib.Path, default=DEFAULT_DB,
                        help=f"database used by --ref db (default {DEFAULT_DB})")
    parser.add_argument("--json", action="store_true", help="machine readable output")
    parser.add_argument("--top", type=int, default=10,
                        help="how many metric_name rows to break down (default 10)")
    parser.add_argument("--samples", type=int, default=3,
                        help="unmatched samples kept per table (default 3)")
    return parser.parse_args(argv)


def main(argv):
    args = parse_args(argv)
    passport = load_passport()
    report = build_report(args.area, args.pred, args.ref, args.db, passport,
                          samples=max(0, args.samples))
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(format_human(report, top=max(0, args.top)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
