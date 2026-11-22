"""Measure the rag baseline against the organisers' dev gold.

Runs ``extract.run`` with ``filler_rag.RagTableFiller`` over ``dev`` PDFs of
the rag kit (or a subset with ``--limit``), writes every publication into a
temporary database, then compares the written rows with ``dev_gold/results.csv``
on the exact tuple (``publication_id``, ``metric_name``, ``metric_value``,
``source_page``) - verbatim text, no unit conversion, no numeric equivalence.

Comparison is a multiset intersection per publication: the gold legitimately
repeats a tuple across configs/datasets (one .204 of D001 serves several rows),
so ``found = sum(min(gold_count, predicted_count))`` per tuple.

Also prints the aggregated candidate/verified/rejected counters and the top
``verify`` rejection reasons from the ``run`` summaries, so a zero recall is
explainable: either the filler produced nothing, or the verifier dropped it.

The temporary database is deleted on exit, in every path.

Run:  python -X utf8 tools/measure_rag_filler.py
      python -X utf8 tools/measure_rag_filler.py --limit 5
"""
import argparse
import collections
import csv
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from viniti import db as storage  # noqa: E402
from viniti import extract  # noqa: E402
from viniti.filler_rag import RagTableFiller  # noqa: E402

KIT = ROOT / "_corpus" / "rag" / "RAG_ВИНИТИ_участникам_2026-11-20"
DEFAULT_DEV = KIT / "dev"
DEFAULT_GOLD = KIT / "dev_gold" / "results.csv"

COLUMNS = ("публикация", "эталон", "найдено", "записано", "лишних",
           "recall", "precision")


def read_gold(path):
    """Rows are kept verbatim: comparison is exact text, not float."""
    path = pathlib.Path(path)
    if not path.exists():
        raise SystemExit(f"нет эталона {path}: запустите из корня комплекта rag")
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def tuple_of(row, prefix=""):
    """(publication_id, metric_name, metric_value, source_page), all strip()ed."""
    values = []
    for column in ("publication_id", "metric_name",
                   "metric_value", "source_page"):
        if isinstance(row, dict):
            values.append((row.get(prefix + column) or "").strip())
        else:  # sqlite3.Row indexes by column name too
            values.append((row[prefix + column] or "").strip())
    return tuple(values)


def run_publication(pid, pdf_bytes, db_path):
    return extract.run("rag", pid, pdf_bytes, RagTableFiller(), db_path=db_path)


def collect_summaries(dev, limit, db_path):
    """Run the filler over PDFs, return {pid: summary}."""
    pdfs = sorted(dev.glob("*.pdf"))
    if limit:
        pdfs = pdfs[:limit]
    summaries = {}
    for path in pdfs:
        pid = path.stem
        try:
            summaries[pid] = run_publication(pid, path.read_bytes(), db_path)
        except Exception as error:  # a record, not a crash
            summaries[pid] = {"error": str(error)}
        print(f"  {pid}: {summaries[pid].get('error') or 'ok'}",
              file=sys.stderr)
    return summaries


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dev", type=pathlib.Path, default=DEFAULT_DEV)
    parser.add_argument("--gold", type=pathlib.Path, default=DEFAULT_GOLD)
    parser.add_argument("--limit", type=int, default=0,
                        help="прогнать только первые N PDF (0 = все)")
    args = parser.parse_args(argv)

    gold = read_gold(args.gold)
    gold_by_pub = collections.defaultdict(collections.Counter)
    for row in gold:
        gold_by_pub[row["publication_id"]][tuple_of(row)] += 1

    with tempfile.TemporaryDirectory() as tmp:
        db_path = pathlib.Path(tmp) / "measure.sqlite3"
        summaries = collect_summaries(args.dev, args.limit, db_path)

        connection = None
        predicted = collections.defaultdict(collections.Counter)
        try:
            if db_path.exists():
                connection = storage.connect(db_path)
                for row in connection.execute(
                        "SELECT publication_id, metric_name, metric_value, "
                        "source_page FROM rag_results"):
                    predicted[row["publication_id"]][tuple_of(row)] += 1
        finally:
            if connection is not None:
                connection.close()

    pids = sorted(summaries)  # only publications this run actually processed
    rows = []
    for pid in pids:
        gold_rows = sum(gold_by_pub[pid].values())
        written = sum(predicted[pid].values())
        found = sum(min(gold_by_pub[pid][key], count)
                    for key, count in predicted[pid].items())
        extra = written - found
        recall = found / gold_rows if gold_rows else 0.0
        precision = found / written if written else 0.0
        rows.append({"pid": pid, "gold": gold_rows, "found": found,
                     "written": written, "extra": extra,
                     "recall": recall, "precision": precision})

    header = COLUMNS
    widths = {"публикация": max(len(row["pid"]) for row in rows)}
    rows_out = []
    for row in rows:
        fields = [row["pid"], row["gold"], row["found"], row["written"],
                  row["extra"], f"{row['recall']:.3f}", f"{row['precision']:.3f}"]
        rows_out.append(fields)
        for key, text in zip(COLUMNS, fields):
            widths.setdefault(key, len(key))
            widths[key] = max(widths[key], len(str(text)))

    print("таблица по публикациям:")
    print("  " + " | ".join(str(header[i]).ljust(widths[header[i]])
                            for i in range(len(header))))
    for fields in rows_out:
        print("  " + " | ".join(str(fields[i]).ljust(widths[COLUMNS[i]])
                                for i in range(len(COLUMNS))))

    total_gold = sum(row["gold"] for row in rows)
    total_written = sum(row["written"] for row in rows)
    total_found = sum(row["found"] for row in rows)
    recall = total_found / total_gold if total_gold else 0.0
    precision = total_found / total_written if total_written else 0.0
    print(f"\nитого: эталон {total_gold}, найдено {total_found}, "
          f"записано {total_written}, лишних {total_written - total_found}")
    print(f"recall {recall:.4f}, precision {precision:.4f}")

    candidates = sum((summary.get("candidates") or 0)
                     for summary in summaries.values())
    verified = sum((summary.get("verified") or 0)
                   for summary in summaries.values())
    rejected = sum((summary.get("rejected") or 0)
                   for summary in summaries.values())
    errors = [pid for pid, summary in summaries.items()
              if "error" in summary]
    reasons = collections.Counter()
    for summary in summaries.values():
        for reason, count in (summary.get("rejected_reasons") or {}).items():
            reasons[reason] += count
    print(f"\nкандидаты {candidates}, прошли verify {verified}, "
          f"отклонены {rejected}, публикаций с ошибкой разбора: {len(errors)}")
    if reasons:
        print("причины отказов verify:")
        for reason, count in reasons.most_common(10):
            print(f"  {count:4} {reason}")
    if errors:
        print("не разобранные публикации:", ", ".join(sorted(errors)))

    worst = sorted(rows, key=lambda row: (row["recall"], -row["gold"]))[:5]
    print("\n5 худших публикаций по recall:")
    for row in worst:
        print(f"  {row['pid']}: эталон {row['gold']}, найдено {row['found']}, "
              f"записано {row['written']}, recall {row['recall']:.3f}, "
              f"precision {row['precision']:.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))