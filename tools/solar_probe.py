"""Read-only probe of the solar baseline extractor.

The probe deliberately calls :func:`extract.run` with ``dry_run=True``.  It
does not create a database, and it does not change the parser or TableFiller.
Besides the normal candidate/verify counters it reports how often the baseline
can supply result identity fields and a value normalizable by ``norm.py``.

Examples::

    python -X utf8 tools/solar_probe.py _corpus/area1/dev/D001.pdf
    python -X utf8 tools/solar_probe.py --dev _corpus/area1/dev --limit 5
    python -X utf8 tools/solar_probe.py --dev _corpus/area1/dev --gold _corpus/area1/dev_gold/results.csv
"""
import argparse
from collections import Counter
import csv
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import extract  # noqa: E402
import norm  # noqa: E402
import pdf_source  # noqa: E402
import verify  # noqa: E402

DEFAULT_DEV = ROOT / "_corpus" / "area1" / "dev"
DEFAULT_GOLD = ROOT / "_corpus" / "area1" / "dev_gold" / "results.csv"


def metric_key(text):
    """Small comparison key, not a replacement for the passport vocabulary."""
    text = re.sub(r"[^a-z0-9]+", "", (text or "").lower())
    return {"jsc": "jsc", "jsc": "jsc", "voc": "voc",
            "eff": "pce", "eta": "pce", "efficiency": "pce"}.get(text, text)


def normalized_key(row):
    """Return a relaxed (metric, canonical unit, Decimal value) key.

    Gold rows prefer their explicitly stored normalized value.  Extracted rows
    are normalized from metric_value using the same pure function used by the
    rest of the project.  Rows that cannot be normalized fall back to a
    whitespace-normalized literal value, so they remain countable in reports.
    """
    value = row.get("normalized_value") or row.get("metric_value") or ""
    unit = row.get("normalized_unit") or row.get("metric_unit") or ""
    # The solar passport permits mV in normalized_unit although norm.py keeps
    # its deliberately small solar vocabulary at volts.  This is only a probe
    # comparison convenience; stored text is never rewritten.
    if (unit or "").strip().lower() == "mv":
        try:
            value = str(float(value) / 1000)
            unit = "V"
        except (TypeError, ValueError):
            pass
    result = norm.normalize_metric("solar", row.get("metric_name", ""), value, unit)
    if result["comparable"] and result["value"] is not None:
        return (metric_key(row.get("metric_name")), result["unit_canonical"],
                str(result["value"]))
    return (metric_key(row.get("metric_name")), "literal", " ".join(str(value).split()))


def read_gold(path):
    if not path or not pathlib.Path(path).exists():
        return []
    with pathlib.Path(path).open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


class CaptureFiller:
    """Capture TableFiller output while retaining the normal filler seam."""

    def __init__(self):
        self.inner = extract.TableFiller()
        self.rows = []

    def fill(self, window, spec):
        rows = self.inner.fill(window, spec)
        self.rows.extend(rows)
        return rows


def inspect(path):
    data = pathlib.Path(path).read_bytes()
    filler = CaptureFiller()
    summary = extract.run("solar", pathlib.Path(path).stem, data, filler,
                          dry_run=True, source_path=str(path))
    document = pdf_source.extract(data)
    accepted = []
    for row in filler.rows:
        ok, _ = verify.verify_record(row, document.get("sources") or [],
                                     document.get("page_texts") or {})
        if ok:
            accepted.append(row)
    # Keep the same duplicate notion as extract.run for field coverage and
    # matching, rather than counting repeated overlap windows twice.
    unique = []
    seen = set()
    for row in accepted:
        key = (row.get("source_page"), row.get("source_fragment"),
               row.get("metric_name"), row.get("metric_value"), row.get("metric_unit"))
        if key not in seen:
            seen.add(key)
            unique.append(row)
    return summary, unique


def coverage(rows):
    total = len(rows)
    fields = {}
    for field in ("normalized_value", "device_id", "measurement_id"):
        fields[field] = sum(bool(str(row.get(field) or "").strip()) for row in rows)
    fields["normalizable_value"] = sum(
        norm.normalize_metric("solar", row.get("metric_name", ""),
                              row.get("metric_value", ""), row.get("metric_unit", ""))[
                                  "comparable"] for row in rows)
    return {"rows": total, "filled": fields,
            "rates": {key: (value / total if total else 0.0)
                      for key, value in fields.items()}}


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("pdfs", nargs="*", type=pathlib.Path)
    parser.add_argument("--dev", type=pathlib.Path, default=DEFAULT_DEV)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--gold", type=pathlib.Path, default=None,
                        help="optional gold CSV for relaxed metric/value matching")
    args = parser.parse_args(argv)
    paths = sorted(args.pdfs) if args.pdfs else sorted(args.dev.glob("*.pdf"))
    if args.limit:
        paths = paths[:args.limit]
    if not paths:
        parser.error("PDF files not found")

    totals = Counter()
    all_rows = []
    for path in paths:
        try:
            summary, rows = inspect(path)
        except Exception as error:  # one bad PDF should not hide the rest
            print(f"{path.stem}: ERROR {error}", file=sys.stderr)
            continue
        all_rows.extend(rows)
        for key in ("candidates", "verified", "rejected", "duplicates"):
            totals[key] += summary.get(key, 0)
        print(f"{path.stem}: candidates={summary['candidates']} verified={summary['verified']} "
              f"rejected={summary['rejected']} unique_verified={len(rows)}")

    print("totals: " + " ".join(f"{key}={totals[key]}"
                               for key in ("candidates", "verified", "rejected", "duplicates")))
    print("verified field coverage:", coverage(all_rows))

    gold_path = args.gold if args.gold is not None else (DEFAULT_GOLD if DEFAULT_GOLD.exists() else None)
    gold = read_gold(gold_path)
    if gold:
        predicted = Counter(normalized_key(row) for row in all_rows)
        reference = Counter(normalized_key(row) for row in gold
                            if not paths or row.get("publication_id") in {p.stem for p in paths})
        matched = sum((predicted & reference).values())
        print("relaxed metric/value: predicted=%d gold=%d matched=%d recall=%.4f precision=%.4f" %
              (sum(predicted.values()), sum(reference.values()), matched,
               matched / sum(reference.values()) if reference else 0.0,
               matched / sum(predicted.values()) if predicted else 0.0))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
