"""Build passport.json, the machine-readable field catalogue for both areas.

The passport is derived from the organisers' validators, never hand-written:
  solar -> VOCAB / TABLES / NUMERIC of area1 tools/check_submission.py
  rag   -> submission_template/schema.json (tables, keys, enums) + NUMBERS

Run:  python passport.py            # regenerate passport.json
      python passport.py --check    # exit 1 if passport.json is stale
"""
import importlib.util
import json
import sys

from viniti import paths

ROOT = paths.REPO_ROOT
PASSPORT = paths.PASSPORT

SOLAR_KIT = paths.CORPUS_ROOTS["solar"]
RAG_KIT = paths.CORPUS_ROOTS["rag"]

SOLAR_KEYS = {
    "publications": "publication_id",
    "materials": "material_id",
    "devices": "device_id",
    "layers": "layer_id",
    "measurements": "measurement_id",
    "results": "result_id",
    "issues": "issue_id",
}

RAG_NUMBERS = {
    "publications": {"year"},
    "configurations": {"chunk_size", "chunk_overlap", "top_k"},
    "experiments": {"latency_ms", "tokens_per_query", "sample_size"},
    "results": {"metric_k", "metric_value", "baseline_value", "sample_size",
                "normalized_value", "source_page"},
    "issues": {"source_page"},
}


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build_solar():
    module = load_module(SOLAR_KIT / "tools" / "check_submission.py", "solar_check")
    tables = {}
    for table, columns in module.TABLES.items():
        names = columns.split()
        fields = {}
        for column in names:
            if (table, column) in module.VOCAB:
                fields[column] = {"type": "enum",
                                  "allowed": sorted(module.VOCAB[(table, column)])}
            elif (table, column) in module.NUMERIC:
                fields[column] = {"type": "number"}
            else:
                fields[column] = {"type": "text"}
        tables[table] = {"key": SOLAR_KEYS[table], "columns": names, "fields": fields}
    return {"bom": True, "newline": "\r\n", "tables": tables}


def build_rag():
    schema = json.loads((RAG_KIT / "submission_template" / "schema.json")
                        .read_text(encoding="utf-8"))
    enums = schema["enums"]
    tables = {}
    for table, columns in schema["tables"].items():
        fields = {}
        for column in columns:
            qualified = f"{table}.{column}"
            if qualified in enums:
                fields[column] = {"type": "enum", "allowed": list(enums[qualified])}
            elif column in RAG_NUMBERS.get(table, set()):
                fields[column] = {"type": "number"}
            else:
                fields[column] = {"type": "text"}
        tables[table] = {"key": schema["keys"][table], "columns": list(columns),
                         "fields": fields}
    return {"bom": False, "newline": "\r\n", "tables": tables}


def build():
    missing = [str(path) for path in (SOLAR_KIT, RAG_KIT) if not path.exists()]
    if missing:
        raise SystemExit("нет комплектов участников: " + ", ".join(missing)
                         + "\nпаспорт строится только при распакованном _corpus/")
    return {
        "version": 1,
        "generated_from": "area1 tools/check_submission.py + rag submission_template/schema.json",
        "areas": {"solar": build_solar(), "rag": build_rag()},
    }


def dump(document):
    return json.dumps(document, ensure_ascii=False, indent=1, sort_keys=False) + "\n"


def main(argv):
    document = build()
    text = dump(document)
    if "--check" in argv:
        current = PASSPORT.read_text(encoding="utf-8") if PASSPORT.exists() else ""
        if current != text:
            print("passport.json устарел: запустите python passport.py")
            return 1
        print("passport.json совпадает с валидаторами")
        return 0
    PASSPORT.write_text(text, encoding="utf-8")
    counts = {area: {t: len(v["columns"]) for t, v in data["tables"].items()}
              for area, data in document["areas"].items()}
    for area, tables in counts.items():
        total = sum(tables.values())
        print(f"{area}: {len(tables)} таблиц, {total} полей")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
