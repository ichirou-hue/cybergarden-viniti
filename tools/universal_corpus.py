"""Download and check a mixed-topic corpus and universal submission drafts.

Not an official solar/RAG submission and not a gold set. No LLM calls.
"""
import argparse
import csv
import hashlib
import json
import logging
from pathlib import Path
import shutil
from urllib.request import Request, urlopen

from viniti.extraction_core import load_profile, response_schema
from viniti.paths import REPO_ROOT

MANIFEST = REPO_ROOT / "tools" / "fixtures" / "universal-papers.json"
DEFAULT_ROOT = REPO_ROOT / "docs" / "materials" / "universal-participant-kit"
MAX_BYTES = 32 * 1024 * 1024


def text_counts(reader):
    messages = []

    class Capture(logging.Handler):
        def emit(self, record):
            messages.append(record.getMessage()[:300])

    logger = logging.getLogger("pypdf")
    handler = Capture()
    previous_propagate = logger.propagate
    logger.addHandler(handler)
    logger.propagate = False
    try:
        counts = [len(page.extract_text() or "") for page in reader.pages]
    finally:
        logger.removeHandler(handler)
        logger.propagate = previous_propagate
    return counts, sorted(set(messages))


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def download(item, path):
    if path.exists():
        return
    request = Request(item["pdf_url"], headers={"User-Agent": "Source-research-corpus/1.0"})
    with urlopen(request, timeout=90) as response:
        content = response.read(MAX_BYTES + 1)
    if len(content) > MAX_BYTES or not content.startswith(b"%PDF-"):
        raise ValueError(f"{item['id']}: response is not a PDF under 32 MiB")
    temporary = path.with_suffix(".download")
    temporary.write_bytes(content)
    temporary.replace(path)


def ensure_templates(root, papers, profile):
    folder = root / "submission_template"
    folder.mkdir(parents=True, exist_ok=True)
    for name, value in (("profile.json", profile), ("schema.json", response_schema(profile))):
        path = folder / name
        if not path.exists():
            write_json(path, value)
    for table, spec in profile["tables"].items():
        path = folder / f"{table}.csv"
        if not path.exists():
            with path.open("w", encoding="utf-8-sig", newline="") as stream:
                csv.writer(stream).writerow(spec["fields"])
    for paper in papers:
        destination = root / "submission_drafts" / paper["id"]
        destination.mkdir(parents=True, exist_ok=True)
        for source in folder.glob("*.csv"):
            path = destination / source.name
            if not path.exists():
                shutil.copyfile(source, path)


def verify(root, papers, profile):
    from pypdf import PdfReader
    template = root / "submission_template"
    expected = {"profile.json": profile, "schema.json": response_schema(profile)}
    for name, value in expected.items():
        if json.loads((template / name).read_text(encoding="utf-8")) != value:
            raise ValueError(f"{template / name}: schema does not match universal profile")
    report = []
    hashes = set()
    for item in papers:
        path = root / "test" / f"{item['id']}.pdf"
        data = path.read_bytes()
        if not data.startswith(b"%PDF-") or len(data) > MAX_BYTES:
            raise ValueError(f"{path}: not a PDF under 32 MiB")
        sha = hashlib.sha256(data).hexdigest()
        if item.get("sha256") and item["sha256"] != sha:
            raise ValueError(f"{path}: checksum mismatch")
        if sha in hashes:
            raise ValueError(f"{path}: duplicate paper content")
        hashes.add(sha)
        reader = PdfReader(path, strict=False)
        if reader.is_encrypted or not reader.pages:
            raise ValueError(f"{path}: encrypted or empty PDF")
        text_chars, warnings = text_counts(reader)
        if not any(text_chars):
            raise ValueError(f"{path}: no text to check; OCR is required")
        for folder in (root / "submission_template", root / "submission_drafts" / item["id"]):
            if {p.stem for p in folder.glob("*.csv")} != set(profile["tables"]):
                raise ValueError(f"{folder}: unexpected or missing CSV tables")
            for table, spec in profile["tables"].items():
                with (folder / f"{table}.csv").open(encoding="utf-8-sig", newline="") as stream:
                    header = next(csv.reader(stream), None)
                if header != list(spec["fields"]):
                    raise ValueError(f"{folder}/{table}.csv: wrong columns")
        report.append({**item, "file": f"test/{item['id']}.pdf", "bytes": len(data),
                       "sha256": sha, "pages": len(reader.pages),
                       "pages_with_text": sum(n > 0 for n in text_chars),
                       "text_chars": sum(text_chars), "template_headers": "ok",
                       "parser_warnings": warnings,
                       "semantic_extraction": "not_run", "gold": False})
        print(f"{item['id']}: {len(reader.pages)} pages, templates OK", flush=True)
    return report


def write_readme(root, papers):
    path = root / "README.md"
    if path.exists():
        return
    lines = ["# Разнотемный корпус универсального профиля", "",
        "10 открытых научных PDF вне RAG и тонкоплёночных солнечных элементов.",
        "Это подборка для технической проверки, не статистически случайная выборка и не эталон качества.", "",
        "- test/U001.pdf ... U010.pdf: исходные статьи без модификации.",
        "- submission_template/: profile.json, JSON Schema ответа модели и три пустых CSV.",
        "- submission_drafts/<ID>/: отдельные пустые CSV для каждой статьи.",
        "- manifest.json: первичные URL, SHA-256, число страниц и результаты проверки.",
        "- verification.json: технический отчёт; смысловое извлечение не выполнялось.", "",
        "## Как заполнять", "",
        "studies.csv описывает объект, метод и условия; results.csv связывает метрику и значение с study_id; findings.csv содержит выводы и ограничения.",
        "ID действуют внутри одного документа. Не объединяйте исследования по совпадению чисел. Пусто означает неизвестно, ноль сохраняется как 0.",
        "Исходные подтверждения каждого поля нужны в field_sources согласно schema.json. CSV без источников сами по себе не доказывают извлечение.",
        "Эти шаблоны независимы от официальных solar/RAG submission_template; tools/gate.py их не проверяет.", "",
        "## Воспроизведение", "", "Из корня репозитория:", "", "```sh",
        ".venv/bin/python -X utf8 tools/universal_corpus.py",
        ".venv/bin/python -X utf8 tools/universal_corpus.py --verify-only", "```", "",
        "Существующие PDF и заполненные CSV не перезаписываются; расхождение контрольной суммы или схемы является ошибкой.", "",
        "## Статьи и первичные источники", ""]
    for item in papers:
        lines.append(f"- {item['id']}: {item['topic']}. [{item['title']}]({item['source_url']}) ({item['year']}).")
    lines += ["", "Права на статьи остаются у авторов/издателей. Указание открытого доступа не означает универсальное разрешение на перепубликацию. Локальный корпус исключён из Git; URL и воспроизводимый загрузчик сохраняются в репозитории.", ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    papers = json.loads(MANIFEST.read_text(encoding="utf-8"))["papers"]
    if len(papers) != 10 or len({p["id"] for p in papers}) != 10:
        raise ValueError("Expected 10 distinct papers")
    profile = load_profile("universal")
    if not args.verify_only:
        (args.root / "test").mkdir(parents=True, exist_ok=True)
        for item in papers:
            print(f"Downloading {item['id']}: {item['topic']}", flush=True)
            download(item, args.root / "test" / f"{item['id']}.pdf")
        ensure_templates(args.root, papers, profile)
        write_readme(args.root, papers)
    report = verify(args.root, papers, profile)
    write_json(args.root / "manifest.json", {"profile": "universal", "papers": report})
    write_json(args.root / "verification.json", {"count": len(report), "status": "ok",
        "checks": ["PDF signature", "readable pages", "text extraction", "SHA-256", "CSV headers"],
        "semantic_extraction": "not_run", "papers": report})
    print(f"Ready: {args.root}")


if __name__ == "__main__":
    main()
