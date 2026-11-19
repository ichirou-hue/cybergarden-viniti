from collections import defaultdict
from decimal import Decimal
import re
import unicodedata


ALIASES = {
    "recall": r"recall",
    "precision": r"precision",
    "ndcg": r"n\s*dcg",
    "mrr": r"mrr",
    "map": r"map",
    "hit_rate": r"hit[\s-]*rate",
    "exact_match": r"exact[\s-]*match|em",
    "f1": r"f[\s-]*1",
    "accuracy": r"accuracy",
}
METRIC = re.compile(
    r"(?<![\w@])(?P<name>" + "|".join(f"(?:{alias})" for alias in ALIASES.values())
    + r")(?:\s*(?:@|at\s+)\s*(?P<cutoff>[1-9]\d{0,4}))?(?!\w)", re.I,
)
NUMBER = r"[−+\-]?(?:\d+(?:[.,]\d+)?|[.,]\d+)"
MEASURE = re.compile(rf"(?P<number>{NUMBER})\s*(?P<percent>%)?")
PROSE = re.compile(
    METRIC.pattern + rf"\s*(?P<header_unit>\(%\))?"
    rf"\s*(?:=|:|\bof\b|\bwas\b|\bis\b|\breached\b)?\s*"
    rf"(?P<number>{NUMBER})\s*(?P<percent>%)?(?![\w.,+−%-])"
    r"(?!\s*(?:±|to\b|→|–|−|\+/-))", re.I,
)
CONTEXT = re.compile(r"\b(?:using|evaluated on|on)\s+([\w.-]+)", re.I)


def clean(text):
    return unicodedata.normalize("NFKC", text).strip()


def metric_name(raw):
    return next(name for name, alias in ALIASES.items() if re.fullmatch(alias, raw, re.I))


def finding(match, raw_value, source, evidence, group, context, header_percent=False,
            header=None):
    raw = match["number"].replace("−", "-").replace(",", ".")
    value = Decimal(raw)
    percent = bool(match["percent"]) or header_percent
    issues = []
    unit = "ratio"
    if percent:
        value /= 100
    elif value > 1:
        unit = "unspecified"
        issues.append("missing_unit")
    if value < 0 or (percent and value > 1) or (unit == "unspecified" and value > 100):
        issues.append("out_of_range")
    return {
        "metric": metric_name(raw_value["name"]),
        "cutoff": int(raw_value["cutoff"]) if raw_value["cutoff"] else None,
        "value": float(value), "unit": unit, "raw_value": match[0].strip(),
        "source_id": source["id"], "evidence": evidence, "header": header,
        "group": group, "context": context, "issues": issues,
    }


def table_findings(sources):
    headers = {}
    for source in sources:
        if source["kind"] != "table":
            continue
        key = (source["page"], source["table"])
        cells = source["cells"]
        labels = [clean(cell["quote"]) if cell else "" for cell in cells]
        labels_and_units = [re.sub(r"\s*\(%\)$", "", label).strip() for label in labels]
        metrics = {i: match for i, label in enumerate(labels_and_units)
                   if (match := METRIC.fullmatch(label))}
        if metrics:
            headers[key] = (metrics, cells)
            continue
        if key not in headers:
            continue
        metrics, header_cells = headers[key]
        context = " · ".join(label for i, label in enumerate(labels)
                             if label and i not in metrics)
        for i, metric in metrics.items():
            if i >= len(labels) or not (value := MEASURE.fullmatch(labels[i])):
                continue
            header = header_cells[i]
            yield finding(value, metric, source, cells[i],
                          f"table:{key[0]}:{key[1]}:{source['row']}", context,
                          clean(header["quote"]).endswith("(%)"), header)


def extract_facts(sources):
    facts = list(table_findings(sources))
    for source in sources:
        if source["kind"] not in {"text", "caption"}:
            continue
        quote = clean(source["quote"])
        for match in PROSE.finditer(quote):
            if match.start() and quote[match.start() - 1] in {"Δ", "δ"}:
                continue
            context = CONTEXT.search(quote)
            # Prose mentions do not establish comparable experimental conditions.
            facts.append(finding(match, match, source, source, None,
                                 context[1].rstrip(".") if context else "",
                                 bool(match["header_unit"])))
    facts.sort(key=lambda fact: (fact["source_id"], fact["evidence"]["bbox"][0]))
    for index, fact in enumerate(facts, 1):
        fact["id"] = index
    return facts


def check_consistency(facts):
    groups = defaultdict(list)
    for fact in facts:
        if fact["group"] and not fact["issues"]:
            groups[fact["group"]].append(fact)

    for group in groups.values():
        metrics = defaultdict(list)
        for fact in group:
            metrics[fact["metric"], fact["cutoff"]].append(fact)
        for (metric, _), duplicates in metrics.items():
            if len({fact["value"] for fact in duplicates}) >= 2:
                for fact in duplicates:
                    fact["issues"].append("conflicting_values")
        recall = sorted(
            (fact for fact in group if fact["metric"] == "recall"
             and fact["cutoff"] and not fact["issues"]),
            key=lambda fact: fact["cutoff"],
        )
        highest = None
        for fact in recall:
            if highest and fact["value"] + 1e-9 < highest["value"]:
                for affected in (highest, fact):
                    if "recall_decreases" not in affected["issues"]:
                        affected["issues"].append("recall_decreases")
            if highest is None or fact["value"] > highest["value"]:
                highest = fact


def analyze(document):
    facts = extract_facts(document["sources"])
    check_consistency(facts)
    return {**document, "schema_version": 1, "track": "rag", "facts": facts}
