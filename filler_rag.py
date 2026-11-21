"""Baseline extractor for the rag area: table rows only, nothing guessed.

``RagTableFiller`` is the rag counterpart of ``extract.TableFiller``: it reads
the table rows that ``pdf_source`` already structured, recognises a metric
header via ``rag_metric_vocab.json`` (the organisers' metric names, see
``tools/rag_vocab.py`` for the leak notice), and pairs each following row's
cells with the metric of their column.

Rules are deliberately literal:

- a row is a header when at least one of its cells matches a vocab metric name
  after normalization (lower case, spaces and hyphens dropped, ``@`` kept -
  ``NDCG@10`` and ``nDCG@10`` are the same key, ``Recall@100`` too);
- the value comes from the cell under the metric, verbatim as printed: no unit
  conversion, no leading-zero repair, ``58.5k`` stays ``58.5k``;
- the config name is the first non-empty, non-numeric cell of the row; without
  it the row is skipped;
- a metric column whose cell is empty or not number-like is skipped for that
  row;
- a metric that the vocab does not know is skipped, never guessed.

Like ``TableFiller`` this is honest and small, not good extraction: transposed
tables (metrics as rows), headers split across windows, merged cells, figures
and prose are all out of reach. The verifier drops anything that cannot prove
itself, and ``fill`` never raises on a crooked window - it returns ``[]``.
"""
import json
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent
DEFAULT_VOCAB = ROOT / "rag_metric_vocab.json"

PARENS = re.compile(r"\([^)]*\)")
NORMALIZE = re.compile(r"\s+|-")

# A cell that can carry a metric value: optional sign, digits with an optional
# comma/dot fraction (leading dot allowed: the rag PDFs print ".204"), an
# optional exponent ("1.316e20"), then an optional % or a short unit word
# ("58.5k", "0.4 ms"). The whole cell must match; "accuracy 0.9" is not a value.
NUMBER = r"(?:\d[\d\s]*(?:[.,]\d+)?|[.,]\d+)(?:[eE][+\-]?\d+)?"
VALUE_LIKE = re.compile(rf"^[−+\-]?\s*{NUMBER}\s*(?:%|[A-Za-z][A-Za-z0-9./]*)?\s*$")
PROSE_VALUE = re.compile(rf"(?P<value>[−+\-]?\s*{NUMBER}\s*(?:%|[A-Za-z][A-Za-z0-9./]*)?)")
ID_PATTERNS = {
    "config_id": re.compile(r"\b(?:config(?:uration)?|model)\s*[:=]?\s*([A-Za-z0-9][A-Za-z0-9_.-]*)", re.I),
    "dataset_id": re.compile(r"\bdataset\s*[:=]?\s*([A-Za-z0-9][A-Za-z0-9_.-]*)", re.I),
    "task_id": re.compile(r"\btask\s*[:=]?\s*([A-Za-z0-9][A-Za-z0-9_.-]*)", re.I),
    "experiment_id": re.compile(r"\bexperiment\s*[:=]?\s*([A-Za-z0-9][A-Za-z0-9_.-]*)", re.I),
}


def split_cells(quote):
    """Split a table row quote into stripped cell texts (same as extract)."""
    return [cell.strip() for cell in (quote or "").split("|")]


def normalize_metric(text):
    """Agreed key form for header cells and vocab names.

    Lower case, spaces and hyphens dropped, ``@`` untouched. A trailing
    parenthesised clause is dropped first, because PDF headers carry units that
    way ("EM (%)"): the unit in the parens belongs to the cell, not to the name.
    """
    text = PARENS.sub("", text or "")
    return NORMALIZE.sub("", text.lower())


def load_vocab(path=DEFAULT_VOCAB):
    """``{metric_name: {rows, metric_unit, metric_group, metric_direction}}``."""
    path = pathlib.Path(path)
    if not path.exists():
        raise SystemExit(
            f"нет словаря метрик {path}: запустите python -X utf8 tools/rag_vocab.py "
            f"из корня репозитория (нужен dev_gold области rag)")
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)["metrics"]


class RagTableFiller:
    """Model-free baseline for the rag area.

    ``vocab`` may be injected for tests; the default loads
    ``rag_metric_vocab.json`` from the repository root.
    """

    def __init__(self, vocab=None):
        stats = load_vocab() if vocab is None else vocab
        self._stats = stats
        lookup = {}
        for name in stats:
            key = normalize_metric(name)
            if key in lookup:
                # Ambiguous normalized key: neither name can be chosen honestly.
                lookup[key] = None
            else:
                lookup[key] = name
        self._lookup = lookup

    def _header_metrics(self, cells):
        """[(column_index, metric_name, unit, group, direction)] for a row, or []."""
        metrics = []
        for index, cell in enumerate(cells):
            if not cell or VALUE_LIKE.match(cell):
                continue
            name = self._lookup.get(normalize_metric(cell))
            if not name:
                continue
            stats = self._stats[name]
            metrics.append((index, name, stats.get("metric_unit") or "",
                            stats.get("metric_group") or "",
                            stats.get("metric_direction") or ""))
        return metrics

    def _row_candidates(self, item, cells, metrics):
        config_index = None
        config_name = ""
        for index, cell in enumerate(cells):
            if not cell or VALUE_LIKE.match(cell):
                continue  # first non-empty, non-numeric cell is the config
            config_index, config_name = index, cell
            break
        if config_index is None:
            return []  # no config name: nothing to attach the numbers to

        results = []
        for column, name, unit, group, direction in metrics:
            if column == config_index or column >= len(cells):
                continue
            value = cells[column]
            if not value or not VALUE_LIKE.match(value):
                continue  # empty or not a number: skip, never guess
            results.append({
                "metric_name": name,
                "metric_value": value,
                "metric_unit": unit,
                "metric_group": group,
                "metric_direction": direction,
                "config_name": config_name,
                "source_page": item.get("page"),
                "source_fragment": item.get("quote") or "",
                "source_table": f"таблица {item.get('table')}, строка {item.get('row')}",
                "extraction_status": "extracted",
                "note": "RagTableFiller (baseline)",
            })
        return results

    def _prose_candidates(self, item):
        """Extract known metric/value pairs from one text source only."""
        quote = (item.get("quote") or "").strip()
        if not quote:
            return []
        results = []
        for name, stats in self._stats.items():
            metric_pattern = re.escape(name).replace(r"\ ", r"\s+")
            pattern = re.compile(
                rf"(?<![\w@]){metric_pattern}(?![\w@])"
                rf"\s*(?:is|was|of|at|around|approximately|=|:)\s*"
                rf"{PROSE_VALUE.pattern}", re.I)
            for match in pattern.finditer(quote):
                row = {
                    "metric_name": name,
                    "metric_value": " ".join(match.group("value").split()),
                    "metric_unit": stats.get("metric_unit") or "",
                    "metric_group": stats.get("metric_group") or "",
                    "metric_direction": stats.get("metric_direction") or "",
                    "source_page": item.get("page"),
                    "source_fragment": quote,
                    "source_table": "", "source_figure": "",
                    "extraction_status": "extracted",
                    "note": "RagTableFiller prose rule",
                }
                for field, identifier in ID_PATTERNS.items():
                    found = identifier.search(quote)
                    row[field] = found.group(1) if found else ""
                results.append(row)
        return results

    def fill(self, window, spec):
        headers = {}
        results = []
        if not isinstance(window, dict):
            return results
        for item in window.get("items") or []:
            if item.get("kind") == "text":
                results.extend(self._prose_candidates(item))
                continue
            if item.get("kind") != "table":
                continue
            cells = split_cells(item.get("quote"))
            key = (item.get("page"), item.get("table"))
            mapped = self._header_metrics(cells)
            if mapped:
                headers[key] = mapped
                continue
            metrics = headers.get(key)
            if not metrics:
                continue  # a header must come first in the same window
            results.extend(self._row_candidates(item, cells, metrics))
        return results
