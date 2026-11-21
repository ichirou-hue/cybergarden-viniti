"""One gate over the organisers' validators for both areas.

The two kits disagree on interface, so this wrapper hides the difference:

- solar: exit code is only 0 or 1 and there is no JSON report. The summary
  line ``таблиц: N, результатов: N, ошибок: N, предупреждений: N`` is parsed
  from stdout.
- rag: exit code is 0, 1 or 2 (2 = structurally clean, quotes need review)
  and ``--json-report PATH`` gives structured counts.

Gate exit codes:
  0  no structural errors (clean, or clean but needs review)
  1  structural errors
  3  the gate itself could not run/parse a validator

The distinction between "clean" and "clean but needs review" is printed per
area; the exit code deliberately reflects structure only.

Both validators must run with the kit directory as cwd because they resolve
relative paths from there. Submission paths are passed absolute.

Run:  python -X utf8 tools/gate.py --area solar --submission out/solar
      python -X utf8 tools/gate.py --area all --submission out
"""
import argparse
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field

ROOT = pathlib.Path(__file__).resolve().parent.parent

KITS = {
    "solar": ROOT / "_corpus" / "area1",
    "rag": ROOT / "_corpus" / "rag" / "RAG_ВИНИТИ_участникам_2026-11-20",
}

AREAS = ("solar", "rag")

SOLAR_SUMMARY = re.compile(
    r"таблиц:\s*(\d+),\s*результатов:\s*(\d+),\s*"
    r"ошибок:\s*(\d+),\s*предупреждений:\s*(\d+)"
)

CLEAN = "clean"
REVIEW = "clean but needs review"
BROKEN = "structural errors"


@dataclass
class GateResult:
    area: str
    submission: pathlib.Path
    status: str = BROKEN
    tables: int = 0
    results: int = 0
    errors: int = 0
    review: int = 0
    validator_exit: int = 0
    failure: str = ""
    error_lines: list = field(default_factory=list)
    review_lines: list = field(default_factory=list)
    extra: list = field(default_factory=list)

    @property
    def gate_exit(self):
        if self.failure:
            return 3
        return 1 if self.errors else 0


def _env():
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def _run(cmd, cwd, timeout):
    return subprocess.run(
        cmd, cwd=str(cwd), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout, env=_env(),
    )


def _lines(text, prefixes):
    found = []
    for line in text.splitlines():
        stripped = line.strip()
        for prefix in prefixes:
            if stripped.startswith(prefix):
                found.append(stripped)
                break
    return found


def _failure(area, submission, message, stdout="", stderr=""):
    tail = "\n".join((stdout + "\n" + stderr).strip().splitlines()[-15:])
    if tail:
        message = f"{message}\n{tail}"
    return GateResult(area=area, submission=submission, failure=message)


def check_solar(submission, split, timeout):
    kit = KITS["solar"]
    script = kit / "tools" / "check_submission.py"
    cmd = [sys.executable, "-X", "utf8", str(script), str(submission), split]
    try:
        proc = _run(cmd, kit, timeout)
    except subprocess.TimeoutExpired:
        return _failure("solar", submission, f"валидатор не ответил за {timeout:g} с")
    except OSError as exc:
        return _failure("solar", submission, f"не удалось запустить валидатор: {exc}")

    match = SOLAR_SUMMARY.search(proc.stdout)
    if not match:
        return _failure(
            "solar", submission,
            "не найдена строка итога «таблиц: ..., ошибок: ...»; "
            f"код валидатора {proc.returncode}",
            proc.stdout, proc.stderr,
        )
    tables, results, errors, review = (int(x) for x in match.groups())
    if proc.returncode not in (0, 1):
        return _failure(
            "solar", submission,
            f"неожиданный код валидатора {proc.returncode}",
            proc.stdout, proc.stderr,
        )
    return GateResult(
        area="solar", submission=submission,
        status=BROKEN if errors else (REVIEW if review else CLEAN),
        tables=tables, results=results, errors=errors, review=review,
        validator_exit=proc.returncode,
        error_lines=_lines(proc.stdout, ("ОШИБКА:",)),
        review_lines=_lines(proc.stdout, ("внимание:",)),
    )


def check_rag(submission, split, timeout):
    kit = KITS["rag"]
    script = kit / "tools" / "check_submission.py"
    with tempfile.TemporaryDirectory(prefix="viniti_gate_") as tmp:
        report_path = pathlib.Path(tmp) / "report.json"
        cmd = [sys.executable, "-X", "utf8", str(script), str(submission), split,
               "--json-report", str(report_path)]
        try:
            proc = _run(cmd, kit, timeout)
        except subprocess.TimeoutExpired:
            return _failure("rag", submission, f"валидатор не ответил за {timeout:g} с")
        except OSError as exc:
            return _failure("rag", submission, f"не удалось запустить валидатор: {exc}")

        if not report_path.exists():
            return _failure(
                "rag", submission,
                "валидатор не создал JSON-отчёт; "
                f"код валидатора {proc.returncode}",
                proc.stdout, proc.stderr,
            )
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return _failure("rag", submission, f"JSON-отчёт не читается: {exc}")

    if proc.returncode not in (0, 1, 2):
        return _failure(
            "rag", submission,
            f"неожиданный код валидатора {proc.returncode}",
            proc.stdout, proc.stderr,
        )

    errors = len(report.get("errors") or [])
    warnings = len(report.get("warnings") or [])
    quotes = report.get("quotes") or {}
    rows = report.get("rows") or {}
    expected = report.get("exit_code")
    extra = [f"quotes: requested {quotes.get('requested', 0)}, "
             f"matched {quotes.get('matched', 0)}, "
             f"unresolved {quotes.get('unresolved', 0)}"]
    if rows:
        extra.append("rows: " + ", ".join(f"{k} {v}" for k, v in rows.items()))
    if expected is not None and expected != proc.returncode:
        extra.append(f"note: report exit_code {expected} != validator exit {proc.returncode}")

    return GateResult(
        area="rag", submission=submission,
        status=BROKEN if errors else (REVIEW if warnings else CLEAN),
        results=int(rows.get("results", 0)),
        tables=sum(1 for value in rows.values() if value),
        errors=errors, review=warnings,
        validator_exit=proc.returncode,
        error_lines=[f"ERROR: {e}" for e in (report.get("errors") or [])],
        review_lines=[f"REVIEW: {w}" for w in (report.get("warnings") or [])],
        extra=extra,
    )


def check(area, submission, split="dev", timeout=3600):
    if area not in AREAS:
        raise ValueError(f"неизвестная область {area!r}, ожидалось {AREAS}")
    submission = pathlib.Path(submission).resolve()
    if not submission.is_dir():
        return _failure(area, submission, f"нет каталога сдачи {submission}")
    if area == "solar":
        return check_solar(submission, split, timeout)
    return check_rag(submission, split, timeout)


def resolve_submissions(area, submission):
    """For --area all, --submission is a base dir with solar/ and rag/ inside."""
    base = pathlib.Path(submission)
    if area == "all":
        return {name: base / name for name in AREAS}
    return {area: base}


def _print_result(result, verbose):
    print(f"[{result.area}] {result.submission}")
    if result.failure:
        print("  gate failure: " + result.failure.replace("\n", "\n    "))
        return
    print(f"  status: {result.status}")
    print(f"  tables: {result.tables}  results: {result.results}  "
          f"errors: {result.errors}  review items: {result.review}")
    for line in result.extra:
        print(f"  {line}")

    errors = result.error_lines if verbose else result.error_lines[:20]
    for line in errors:
        print(f"  {line}")
    if len(result.error_lines) > len(errors):
        print(f"  ... ещё ошибок: {len(result.error_lines) - len(errors)}")

    reviews = result.review_lines if verbose else result.review_lines[:5]
    for line in reviews:
        print(f"  {line}")
    if len(result.review_lines) > len(reviews):
        print(f"  ... ещё замечаний: {len(result.review_lines) - len(reviews)} "
              f"(полный список: --verbose)")


def main(argv):
    parser = argparse.ArgumentParser(
        description="Единый гейт по валидаторам обеих областей")
    parser.add_argument("--area", required=True, choices=("solar", "rag", "all"))
    parser.add_argument("--submission", required=True,
                        help="каталог сдачи; для --area all - базовый каталог с solar/ и rag/")
    parser.add_argument("--split", default="dev",
                        help="каталог PDF, который видит валидатор (dev, test)")
    parser.add_argument("--timeout", type=float, default=3600,
                        help="таймаут валидатора в секундах")
    parser.add_argument("--verbose", action="store_true",
                        help="печатать все ошибки и замечания")
    args = parser.parse_args(argv)

    submissions = resolve_submissions(args.area, args.submission)
    results = []
    for area, submission in submissions.items():
        results.append(check(area, submission, args.split, args.timeout))

    for result in results:
        _print_result(result, args.verbose)

    summary = ", ".join(f"{r.area}={r.status if not r.failure else 'gate failure'}"
                        for r in results)
    if any(r.failure for r in results):
        code = 3
    elif any(r.errors for r in results):
        code = 1
    else:
        code = 0
    print(f"\nитог: {summary} -> exit {code}")
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
