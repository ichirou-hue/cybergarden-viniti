"""Offline, mention-level evaluation of solar CSV bundles (stdlib only)."""

import argparse
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
from fractions import Fraction
import json
import os
from pathlib import Path
import csv
import re
import sys
from tempfile import TemporaryDirectory

from solar_schema import KEYS, TABLES, UNITS


BASIC_KEY = ["metric_name", "normalized_unit", "source_page"]
CONTEXT_KEY = BASIC_KEY + [
    "result_origin", "aggregation", "extraction_status", "time_point", "time_unit",
    "device.device_label", "device.architecture", "measurement.measurement_type",
    "measurement.scan_direction", "measurement.light_intensity",
    "measurement.intensity_unit", "measurement.spectrum",
]


def number(value, location):
    if not isinstance(value, str) or len(value) > 256:
        raise ValueError(f"{location}: numeric input exceeds supported size")
    try:
        result = Decimal(value)
    except (InvalidOperation, ValueError):
        raise ValueError(f"{location}: invalid decimal {value!r}") from None
    if not result.is_finite():
        raise ValueError(f"{location}: nonfinite decimal {value!r}")
    if len(result.as_tuple().digits) > 200 or abs(result.adjusted()) > 200:
        raise ValueError(f"{location}: numeric precision/exponent exceeds supported limits")
    return result


def load_bundle(directory):
    """Require the seven template tables, including headers on empty tables."""
    directory = Path(directory)
    tables = {}
    for table, columns in TABLES.items():
        path = directory / f"{table}.csv"
        try:
            with path.open(encoding="utf-8-sig", newline="") as stream:
                reader = csv.DictReader(stream, strict=True)
                headers = reader.fieldnames or []
                if len(headers) != len(set(headers)) or set(columns) - set(headers):
                    raise ValueError(f"{path}: missing or duplicate columns")
                rows = []
                seen = set()
                for line, row in enumerate(reader, 2):
                    if None in row or any(value is None for value in row.values()):
                        raise ValueError(f"{path}:{line}: malformed CSV row")
                    row = {key: value.strip() for key, value in row.items()}
                    identity = (row["publication_id"], row[KEYS[table]])
                    if not all(identity) or identity in seen:
                        raise ValueError(f"{path}:{line}: missing or duplicate row ID")
                    seen.add(identity)
                    rows.append(row)
                tables[table] = rows
        except (OSError, UnicodeError, csv.Error) as error:
            raise ValueError(f"{path}: {error}") from error
    publications = {row["publication_id"] for row in tables["publications"]}
    for table, rows in tables.items():
        for row in rows:
            if row["publication_id"] not in publications:
                raise ValueError(f"{table}: undeclared publication {row['publication_id']}")
    return tables


def collect(tables, publications, metrics):
    indexes = {
        table: {(r["publication_id"], r[KEYS[table]]): r for r in tables[table]}
        for table in ("devices", "measurements")
    }
    entries = {pid: [] for pid in publications}
    excluded = Counter()
    for row in tables["results"]:
        pid = row["publication_id"]
        if pid not in publications:
            excluded["outside_selected_publications"] += 1
            continue
        status = row["extraction_status"]
        if status not in {"extracted", "absent", "present_not_extractable"}:
            raise ValueError(f"{pid}/{row['result_id']}: invalid extraction_status")
        if status != "extracted":
            excluded[f"status:{status}"] += 1
            continue
        if row["metric_name"] not in metrics:
            excluded[f"metric:{row['metric_name']}"] += 1
            continue
        location = f"{pid}/{row['result_id']}"
        if not re.fullmatch(r"[1-9][0-9]*", row["source_page"]):
            raise ValueError(f"{location}: source_page must be a canonical ASCII positive integer")
        # Validate nonempty numeric claims even when their unit is missing.
        value = number(row["normalized_value"], location) if row["normalized_value"] else None
        if value is None or not row["normalized_unit"]:
            excluded["unnormalized"] += 1
            continue

        def resolve(table, field):
            reference = row[field]
            if not reference:
                return {}
            entity = indexes[table].get((pid, reference))
            if entity is None:
                raise ValueError(f"{location}: unresolved {field} {reference!r}")
            return entity

        measurement = resolve("measurements", "measurement_id")
        device = resolve("devices", "device_id")
        measurement_device = measurement.get("device_id", "")
        if measurement_device:
            device_ids = [item.strip() for item in measurement_device.split(";")]
            linked = [indexes["devices"].get((pid, item)) for item in device_ids]
            if any(item is None for item in linked):
                raise ValueError(f"{location}: unresolved measurement device")
            if device and device["device_id"] not in device_ids:
                raise ValueError(f"{location}: conflicting result and measurement devices")
            if not device and len(linked) == 1:
                device = linked[0]
        time = number(row["time_point"], location + "/time_point") if row["time_point"] else ""
        intensity = measurement.get("light_intensity", "")
        if intensity:
            intensity = number(intensity, location + "/light_intensity")
        basic = tuple(row[field] for field in BASIC_KEY)
        context = basic + (
            row["result_origin"], row["aggregation"], status, time, row["time_unit"],
            device.get("device_label", ""), device.get("architecture", ""),
            measurement.get("measurement_type", ""), measurement.get("scan_direction", ""),
            intensity, measurement.get("intensity_unit", ""), measurement.get("spectrum", ""),
        )
        entries[pid].append({
            "basic": basic, "context": context, "value": value,
            "example": {key: row[key] for key in (
                "publication_id", "result_id", "metric_name", "normalized_value",
                "normalized_unit", "source_page", "duplicate_of",
            )},
        })
    return entries, dict(sorted(excluded.items()))


def scores(tp, fp, fn):
    # An empty denominator is reported as zero, not perfect extraction.
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    return {
        "tp": tp, "fp": fp, "fn": fn, "precision": precision, "recall": recall,
        "f1": 2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else 0.0,
    }


def compare(predicted, gold, key, tolerance, example_limit):
    """Counter matching at zero tolerance; sorted maximum-cardinality at >0."""
    def groups(rows):
        result = defaultdict(list)
        for row in rows:
            result[row[key]].append(row)
        return result

    pred_groups, gold_groups = groups(predicted), groups(gold)
    unmatched_pred, unmatched_gold = [], []
    tp = 0
    for identity in sorted(pred_groups.keys() | gold_groups.keys(), key=repr):
        pred = sorted(pred_groups[identity], key=lambda r: r["value"])
        expected = sorted(gold_groups[identity], key=lambda r: r["value"])
        if not tolerance:
            pcounts = Counter(row["value"] for row in pred)
            gcounts = Counter(row["value"] for row in expected)
            tp += sum((pcounts & gcounts).values())
            for rows, counts, output in (
                (pred, pcounts - gcounts, unmatched_pred),
                (expected, gcounts - pcounts, unmatched_gold),
            ):
                for row in rows:
                    if counts[row["value"]]:
                        output.append(row)
                        counts[row["value"]] -= 1
            continue
        i = j = 0
        while i < len(pred) and j < len(expected):
            if abs(Fraction(pred[i]["value"]) - Fraction(expected[j]["value"])) <= Fraction(tolerance):
                tp += 1
                i += 1
                j += 1
            elif pred[i]["value"] < expected[j]["value"]:
                unmatched_pred.append(pred[i])
                i += 1
            else:
                unmatched_gold.append(expected[j])
                j += 1
        unmatched_pred.extend(pred[i:])
        unmatched_gold.extend(expected[j:])
    result = scores(tp, len(unmatched_pred), len(unmatched_gold))
    result["unmatched_predicted_examples"] = [
        dict(row["example"], match_context=[str(v) for v in row[key]])
        for row in unmatched_pred[:example_limit]
    ]
    result["unmatched_gold_examples"] = [
        dict(row["example"], match_context=[str(v) for v in row[key]])
        for row in unmatched_gold[:example_limit]
    ]
    return result


def evaluate(predicted_dir, gold_dir, publications=None, tolerance="0",
             metrics=None, example_limit=10):
    tolerance = number(str(tolerance), "tolerance")
    if tolerance < 0:
        raise ValueError("tolerance must be nonnegative")
    if example_limit < 0:
        raise ValueError("example_limit must be nonnegative")
    metrics = set(UNITS if metrics is None else metrics)
    if not metrics or any(not metric for metric in metrics):
        raise ValueError("metrics must not be empty")
    predicted, gold = load_bundle(predicted_dir), load_bundle(gold_dir)
    gold_pubs = {r["publication_id"]: r for r in gold["publications"]}
    pred_pubs = {r["publication_id"]: r for r in predicted["publications"]}
    unexpected = sorted(pred_pubs.keys() - gold_pubs.keys())
    if unexpected:
        raise ValueError(f"predicted publications absent from gold: {unexpected}")
    selected = set(gold_pubs if publications is None else publications)
    if not selected or selected - gold_pubs.keys():
        raise ValueError("publication selection is empty or absent from gold")
    pred_entries, pred_excluded = collect(predicted, selected, metrics)
    gold_entries, gold_excluded = collect(gold, selected, metrics)
    report = {
        "schema_version": "solar-evaluation-1",
        "publications": sorted(selected),
        "policy": {
            "unit_of_evaluation": "all extracted result mentions, including duplicate_of",
            "normalization": "scalar normalized only; missing normalized_value or normalized_unit is an unnormalized abstention in either bundle",
            "abstention": "excluded_results.unnormalized; never matched; normalized gold still contributes FN",
            "source_page": "canonical ASCII positive integer, including for unnormalized extracted allowed-metric rows",
            "macro": "unweighted mean of per-publication precision, recall and f1 over all selected publications",
            "empty_denominators": "zero, including empty gold and empty prediction; empty papers remain in macro",
            "metrics": sorted(metrics), "absolute_value_tolerance": str(tolerance),
            "numeric_comparison": "finite Decimal; 1 equals 1.0; no unit conversion",
            "numeric_limits": {"max_significant_digits": 200, "max_abs_adjusted_exponent": 200,
                               "tolerance_arithmetic": "exact rational"},
            "basic_key": BASIC_KEY + ["normalized_value"],
            "context_key": CONTEXT_KEY + ["normalized_value"],
            "publication_partitioned": True,
            "matching": "one-to-one multiset; maximum cardinality within each key",
            "missing_context": "empty matches empty only; no wildcard or inferred scientific equivalence",
            "references": "local IDs resolved within publication, never compared across bundles",
            "limitations": [
                "No PDF or quote verification; correct quote alone does not prove context.",
                "Context uses literal labels; equivalent labels may yield false negatives.",
                "Material, layer, cited-publication identity and other unlisted context are not scored.",
                "Only extracted allowed-metric rows are scored; scope is separate.",
                "Unnormalized mentions are excluded, not correct normalized claims; empty-denominator scores are zero.",
                "Absolute tolerance applies only to normalized_value, not time or intensity.",
                "Shared measurement device lists use the explicit result device; no device is inferred from a multi-device list.",
            ],
        },
        "excluded_results": {"predicted": pred_excluded, "gold": gold_excluded},
        "predicted_publications_outside_selection": sorted(pred_pubs.keys() - selected),
        "scope": {"per_publication": {}},
        "per_publication": {},
        "micro": {},
        "macro": {},
    }
    for pid in sorted(selected):
        expected_scope = gold_pubs[pid]["in_scope"]
        actual_scope = pred_pubs.get(pid, {}).get("in_scope")
        if expected_scope not in {"true", "false"} or actual_scope not in {None, "true", "false"}:
            raise ValueError(f"{pid}: invalid in_scope")
        report["scope"]["per_publication"][pid] = {
            "gold": expected_scope, "predicted": actual_scope,
            "match": actual_scope == expected_scope,
        }
        report["per_publication"][pid] = {
            name: compare(pred_entries[pid], gold_entries[pid], name, tolerance, example_limit)
            for name in ("basic", "context")
        }
    report["scope"]["matched"] = sum(
        item["match"] for item in report["scope"]["per_publication"].values()
    )
    report["scope"]["total"] = len(selected)
    for name in ("basic", "context"):
        counts = [
            sum(report["per_publication"][pid][name][field] for pid in selected)
            for field in ("tp", "fp", "fn")
        ]
        report["micro"][name] = scores(*counts)
        report["macro"][name] = {
            field: sum(report["per_publication"][pid][name][field] for pid in sorted(selected)) / len(selected)
            for field in ("precision", "recall", "f1")
        }
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("predicted", type=Path)
    parser.add_argument("gold", type=Path)
    parser.add_argument("--publication", action="append")
    parser.add_argument("--tolerance", default="0", help="absolute normalized-value tolerance")
    parser.add_argument("--metric", action="append", help="replace the official metric allowlist")
    parser.add_argument("--example-limit", type=int, default=10)
    parser.add_argument("-o", "--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.output.exists():
            raise ValueError(f"output already exists: {args.output}")
        report = evaluate(
            args.predicted, args.gold, args.publication, args.tolerance,
            args.metric, args.example_limit,
        )
        write_report(report, args.output)
    except (ValueError, OSError) as error:
        parser.exit(2, f"error: {error}\n")
    return 0


def write_report(report, output):
    """Publish a closed complete file, atomically and without overwriting."""
    output = Path(output)
    text = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    with TemporaryDirectory(prefix=".evaluation-", dir=output.parent) as temporary:
        staging = Path(temporary) / "report.json"
        staging.write_text(text, encoding="utf-8")
        # Same-filesystem hard link is atomic and fails if output already exists.
        os.link(staging, output)


if __name__ == "__main__":
    sys.exit(main())
