"""Build ``rag_metric_vocab.json`` from the organisers' rag gold set.

The rag area has 479 distinct ``metric_name`` values in the gold, and a table
header is only recognisable if the baseline knows those names. This generator
reads ``dev_gold/results.csv`` of the rag kit and writes a vocab file next to
``filler_rag.py``: for every metric name - how many gold rows use it, and the
most frequent ``metric_unit``, ``metric_group`` and ``metric_direction``.

This is a DELIBERATE, DOCUMENTED LEAK of the schema, and nothing else:

- metric names, groups, units and directions are the submission schema, the
  same thing the organisers publish in ``submission_template/schema.json`` and
  in every report header. Knowing them lets a baseline read a header row at
  all;
- metric VALUES, pages and fragments of the gold are NOT used anywhere: the
  detector only needs names, and ``measure_rag_filler.py`` compares exact
  text, never the other way around.

The generator is idempotent: the same ``results.csv`` always yields the same
JSON (sorted by metric name, deterministic tie-breaking). It fails loudly when
``dev_gold`` is missing.

Run:  python -X utf8 tools/rag_vocab.py
      python -X utf8 tools/rag_vocab.py --gold <path> --out <path>
"""
import argparse
import collections
import csv
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

DEFAULT_GOLD = (ROOT / "_corpus" / "rag"
                / "RAG_ВИНИТИ_участникам_2026-11-20" / "dev_gold" / "results.csv")
DEFAULT_OUT = ROOT / "rag_metric_vocab.json"

LEAK_NOTICE = (
    "Словарь построен по эталону dev_gold/results.csv области rag. Это осознанная "
    "утечка: имена метрик, единицы, группы и направления - это схема сдачи, а не "
    "ответ; значения и страницы из эталона не используются ни филлером, ни замером."
)

NORMALIZE = re.compile(r"\s+|-")


def normalize_metric(text):
    """The agreed key form: lower case, spaces and hyphens dropped, @ kept.

    ``NDCG@10``, ``nDCG@10`` and ``Recall@100`` / ``recall@100`` collapse to one
    key each; a header cell only has to reach the same key as a vocab name.
    """
    return NORMALIZE.sub("", (text or "").lower())


def read_gold(path):
    """Rows of dev_gold/results.csv (utf-8-sig), or a loud SystemExit."""
    path = pathlib.Path(path)
    if not path.exists():
        raise SystemExit(
            f"нет эталона {path}: запустите генератор из корня комплекта или "
            f"передайте --gold. dev_gold нужен только для имён метрик, это схема, "
            f"а не ответ.")
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def most_frequent(counter):
    """The first most common entry, or '' for a missing column."""
    found = counter.most_common(1)
    return found[0][0] if found else ""


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--gold", type=pathlib.Path, default=DEFAULT_GOLD)
    parser.add_argument("--out", type=pathlib.Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)

    rows = read_gold(args.gold)
    if not rows:
        raise SystemExit(f"{args.gold}: эталон пуст")

    units, groups, directions = (collections.defaultdict(collections.Counter)
                                 for _ in range(3))
    counts = collections.Counter()
    for row in rows:
        name = row.get("metric_name") or ""
        if not name:
            continue
        counts[name] += 1
        units[name][row.get("metric_unit") or ""] += 1
        groups[name][row.get("metric_group") or ""] += 1
        directions[name][row.get("metric_direction") or ""] += 1

    metrics = {}
    for name in sorted(counts):
        metrics[name] = {
            "rows": counts[name],
            "metric_unit": most_frequent(units[name]),
            "metric_group": most_frequent(groups[name]),
            "metric_direction": most_frequent(directions[name]),
        }

    normalized, collisions = {}, {}
    for name in metrics:
        key = normalize_metric(name)
        if key in normalized:
            # No collisions exist in the current gold (verified), but keep the
            # filler honest if a future regen introduces one: an ambiguous key
            # must not silently pick one of two names.
            collisions.setdefault(key, []).extend([normalized.pop(key), name])
            continue
        normalized[key] = name

    payload = {
        "_comment": LEAK_NOTICE,
        "_source": str(args.gold),
        "_generator": "tools/rag_vocab.py",
        "metrics": metrics,
        "normalized": normalized,
        "collisions": collisions,
    }
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=1)
                        + "\n", encoding="utf-8")

    top = sorted(metrics.items(), key=lambda item: -item[1]["rows"])[:10]
    print(f"словарь: {len(metrics)} метрик, {len(normalized)} нормализованных "
          f"ключей, коллизий: {len(collisions)}")
    print("записан:", args.out)
    print("топ-10 метрик по частоте:")
    for name, stats in top:
        print(f"  {stats['rows']:6} {name!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))