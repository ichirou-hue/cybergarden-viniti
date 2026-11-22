"""Batch harness over one corpus split, tracked in the shared ``jobs`` table.

This file owns scheduling and bookkeeping only. The extraction itself is
injected through ``process(area, publication_id, pdf_path)``; by default that is
``solar_offline.process`` for solar and ``extract.process`` for rag, and
``--processor module:callable`` replaces it.

Every file gets a ``jobs`` row that moves ``queued`` -> ``running`` ->
``ok`` / ``error`` / ``skipped``. A failure is recorded and the batch continues.

``--resume`` skips a publication only when its last row is ``ok`` AND was
produced by the same ``extractor_revision``. A bumped revision therefore
re-runs everything; unlike ``library.py``, which keys its cache on sha256 only.

Run:  python -m viniti batch --area rag --split test --resume
      python -m viniti batch --area solar --split dev --dry-run
"""
import argparse
import contextvars
import importlib
import os
import pathlib
import sqlite3
import sys
import threading
import time
import traceback
from datetime import datetime, timezone

from . import db
from viniti import diagnostics, paths

logger = diagnostics.get_logger("batch")

ROOT = paths.REPO_ROOT

AREAS = ("solar", "rag")

# Corpus kit roots. Both kits use the same split names (dev, test, dev_gold).
# A copy: tests and one-off runs swap entries, and the shared mapping stays as is.
CORPUS_ROOTS = dict(paths.CORPUS_ROOTS)

# Keep resume invalidation tied to the actual extractor revision. A separate
# constant silently lets batch reuse rows after extract.py changes. The import
# stays lazy: extract pulls in pdfplumber, and the harness must import without it.
def default_extractor_revision():
    from . import extract
    return extract.PARSER_VERSION

# Return this from a processor to record the publication as deliberately
# skipped instead of ok.
SKIP = object()

UPSERT_JOB = """
INSERT INTO jobs (area, publication_id, status, attempts, started_at,
                  duration_ms, extractor_revision, error)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(area, publication_id) DO UPDATE SET
    status = excluded.status,
    attempts = excluded.attempts,
    started_at = excluded.started_at,
    duration_ms = excluded.duration_ms,
    extractor_revision = excluded.extractor_revision,
    error = excluded.error
"""


def split_dir(area, split):
    if area not in CORPUS_ROOTS:
        raise ValueError(f"неизвестная область {area!r}, ожидалось {AREAS}")
    if not split or "/" in split or "\\" in split or split in (".", ".."):
        raise ValueError(f"--split должен быть именем каталога, получено {split!r}")
    return CORPUS_ROOTS[area] / split


def discover(area, split):
    """Return PDF paths of the split, sorted by file name."""
    directory = split_dir(area, split)
    if not directory.is_dir():
        raise FileNotFoundError(f"нет каталога {directory}")
    return sorted(directory.glob("*.pdf"), key=lambda p: p.name.lower())


def default_process(area, publication_id, pdf_path):
    """Run the area's real extractor for one publication.

    Solar is served by ``solar_offline``, which fills all seven submission
    tables; RAG keeps the filler-based ``extract``. Both are imported lazily so
    the harness itself stays importable without pdfplumber.
    """
    if area == "solar":
        from . import solar_offline

        return solar_offline.process(area, publication_id, pdf_path)
    from . import extract

    return extract.process(area, publication_id, pdf_path)


def default_revision(area):
    """Extractor revision a fresh run of the area records in ``jobs``."""
    if area == "solar":
        from .solar_version import PARSER_VERSION

        return PARSER_VERSION
    return default_extractor_revision()


def load_processor(spec):
    """Resolve ``module:callable`` into a processor, or return the default one."""
    if not spec:
        return default_process
    module_name, sep, attr = spec.partition(":")
    if not sep or not module_name or not attr:
        raise ValueError(f"--processor ожидает module:callable, получено {spec!r}")
    module = importlib.import_module(module_name)
    try:
        processor = getattr(module, attr)
    except AttributeError as exc:
        raise ValueError(f"в модуле {module_name} нет {attr!r}") from exc
    if not callable(processor):
        raise ValueError(f"{spec!r} не вызывается")
    return processor


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _job_row(conn, area, publication_id):
    return conn.execute(
        "SELECT * FROM jobs WHERE area = ? AND publication_id = ?",
        (area, publication_id),
    ).fetchone()


def _write_job(conn, area, publication_id, *, status, attempts, started_at,
               duration_ms, revision, error):
    with conn:
        conn.execute(UPSERT_JOB, (
            area, publication_id, status, attempts, started_at,
            duration_ms, revision, error[:4000],
        ))


def _call_with_timeout(processor, args, timeout):
    """Run the processor, optionally with a soft per-file deadline.

    A Python thread cannot be killed, so on timeout the call is abandoned and
    the batch moves on; the abandoned daemon thread dies with the process.
    """
    box = {}

    def target():
        try:
            box["value"] = processor(*args)
        except BaseException as exc:  # re-raised in the caller below
            box["error"] = exc

    thread = threading.Thread(target=contextvars.copy_context().run, args=(target,),
                              daemon=True)
    thread.start()
    thread.join(timeout if timeout and timeout > 0 else None)
    if thread.is_alive():
        raise TimeoutError(f"превышен таймаут {timeout:g} с")
    if "error" in box:
        raise box["error"]
    return box.get("value")


def _existing_jobs(db_path, area):
    """Read-only view of jobs for --resume --dry-run. No writes."""
    if not db_path.exists():
        return {}
    conn = sqlite3.connect(str(db_path), timeout=30)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM jobs WHERE area = ?", (area,)
        ).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    return {row["publication_id"]: row for row in rows}


def _is_done(row, revision):
    return bool(row) and row["status"] == "ok" and row["extractor_revision"] == revision


def run(area, split, *, db_path=db.DEFAULT_DB, resume=False, limit=None,
        timeout=0.0, dry_run=False, process=None, revision=None,
        log=print, tracebacks=False):
    """Process one split. Returns a summary dict; never raises per-file errors."""
    process = process or default_process
    if revision is None:
        revision = default_revision(area)
    if limit is not None and limit < 0:
        raise ValueError("--limit не может быть отрицательным")
    if timeout is not None and timeout < 0:
        raise ValueError("--timeout не может быть отрицательным")
    db_path = pathlib.Path(db_path)
    # The processor receives only (area, publication_id, path), so the database
    # travels through the environment: extract.process reads VINITI_DATA_DB. Set
    # here as well as in main, so a programmatic call (a test, another script)
    # writes its rows into the same database this batch keeps its jobs in.
    os.environ["VINITI_DATA_DB"] = str(db_path)

    files = discover(area, split)
    summary = {
        "area": area, "split": split, "discovered": len(files),
        "selected": 0, "ok": 0, "error": 0, "skipped": 0, "failed": [],
    }
    diagnostics.event(logger, "batch.start", area=area, split=split,
                      discovered=len(files), resume=bool(resume), limit=limit,
                      timeout=timeout, dry_run=bool(dry_run), revision=revision,
                      processor=getattr(process, "__name__", type(process).__name__))

    if dry_run:
        existing = _existing_jobs(db_path, area) if resume else {}
        planned = []
        for path in files:
            publication_id = path.stem
            if resume and _is_done(existing.get(publication_id), revision):
                summary["skipped"] += 1
                continue
            planned.append(path)
        if limit is not None:
            planned = planned[:limit]
        summary["selected"] = len(planned)
        diagnostics.event(logger, "batch.plan", area=area, split=split,
                          discovered=len(files), planned=len(planned),
                          skipped=summary["skipped"])
        log(f"[dry-run] {area}/{split}: {len(files)} файлов, "
            f"к запуску {len(planned)}, пропущено {summary['skipped']}")
        for path in planned:
            log(f"  {path.stem}\t{path}")
        return summary

    if not db_path.exists():
        log(f"база {db_path} отсутствует, создаю схему")
        db.init(db_path)

    conn = db.connect(db_path)
    try:
        queue = []
        for path in files:
            publication_id = path.stem
            row = _job_row(conn, area, publication_id)
            if resume and _is_done(row, revision):
                summary["skipped"] += 1
                continue
            queue.append((path, row))
        if limit is not None:
            queue = queue[:limit]
        summary["selected"] = len(queue)

        log(f"[batch] {area}/{split}: {len(files)} файлов, "
            f"к запуску {len(queue)}, пропущено {summary['skipped']}")

        for index, (path, row) in enumerate(queue, 1):
            publication_id = path.stem
            attempts = (row["attempts"] if row else 0) + 1
            started_at = _now()
            _write_job(conn, area, publication_id, status="queued",
                       attempts=attempts, started_at="", duration_ms=0,
                       revision=revision, error="")
            _write_job(conn, area, publication_id, status="running",
                       attempts=attempts, started_at=started_at,
                       duration_ms=0, revision=revision, error="")
            started = time.perf_counter()
            status, error = "ok", ""
            # The context also reaches the processor thread: _call_with_timeout
            # copies it, so an extractor event carries the same job fields.
            with diagnostics.context(area=area, publication_id=publication_id,
                                     job_id=f"{area}:{publication_id}",
                                     attempt=attempts, position=index,
                                     total=len(queue)):
                # No file name here: the publication id is already in the
                # context, and a file name can repeat the article title.
                diagnostics.debug(logger, "job.start")
                try:
                    value = _call_with_timeout(
                        process, (area, publication_id, str(path)), timeout)
                    if value is SKIP:
                        status = "skipped"
                except KeyboardInterrupt:
                    _write_job(conn, area, publication_id, status="error",
                               attempts=attempts, started_at=started_at,
                               duration_ms=int((time.perf_counter() - started) * 1000),
                               revision=revision, error="прервано пользователем")
                    diagnostics.warning(logger, "job.interrupted")
                    raise
                except TimeoutError as exc:
                    status, error = "error", str(exc)
                    diagnostics.error(logger, "job.timeout", timeout_s=timeout)
                except NotImplementedError as exc:
                    status, error = "error", f"NotImplementedError: {exc}"
                    diagnostics.exception(logger, "job.failed", kind="NotImplementedError")
                except Exception as exc:
                    status, error = "error", f"{type(exc).__name__}: {exc}"
                    diagnostics.exception(logger, "job.failed",
                                          kind=type(exc).__name__)
                    if tracebacks:
                        traceback.print_exc()
                duration_ms = int((time.perf_counter() - started) * 1000)
                _write_job(conn, area, publication_id, status=status,
                           attempts=attempts, started_at=started_at,
                           duration_ms=duration_ms, revision=revision, error=error)
                # No error text here: the same string goes to jobs.error, and an
                # extractor exception may quote document text. The failure itself
                # was already logged above with its traceback.
                if status == "ok":
                    diagnostics.event(logger, "job.ok", duration_ms=duration_ms)
                elif status == "skipped":
                    diagnostics.event(logger, "job.skipped", duration_ms=duration_ms)

            if status == "ok":
                summary["ok"] += 1
                log(f"[{index}/{len(queue)}] {publication_id} ok {duration_ms} ms")
            elif status == "skipped":
                summary["skipped"] += 1
                log(f"[{index}/{len(queue)}] {publication_id} skipped")
            else:
                summary["error"] += 1
                summary["failed"].append(publication_id)
                log(f"[{index}/{len(queue)}] {publication_id} error: {error}")
    finally:
        conn.close()

    diagnostics.event(logger, "batch.summary", area=area, split=split,
                      discovered=summary["discovered"], selected=summary["selected"],
                      ok=summary["ok"], error=summary["error"],
                      skipped=summary["skipped"], failed=len(summary["failed"]))
    log(f"[batch] итог: ok {summary['ok']}, ошибок {summary['error']}, "
        f"пропущено {summary['skipped']}")
    return summary


def main(argv):
    diagnostics.configure()
    parser = argparse.ArgumentParser(
        description="Пакетная обработка одного раздела корпуса с журналом jobs")
    parser.add_argument("--area", required=True, choices=AREAS)
    parser.add_argument("--split", required=True,
                        help="каталог раздела внутри комплекта: dev, test, dev_gold")
    parser.add_argument("--db", type=pathlib.Path, default=db.DEFAULT_DB)
    parser.add_argument("--resume", action="store_true",
                        help="пропускать ok при текущей ревизии экстрактора")
    parser.add_argument("--limit", type=int, default=None,
                        help="не больше N файлов за запуск, после пропуска готовых "
                             "по --resume")
    parser.add_argument("--timeout", type=float, default=0.0,
                        help="таймаут на файл в секундах, 0 - без ограничения")
    parser.add_argument("--dry-run", action="store_true",
                        help="только показать, что было бы обработано")
    parser.add_argument("--extractor-revision", type=int, default=None,
                        help="ревизия экстрактора для jobs; по умолчанию "
                             "своя для каждой области")
    parser.add_argument("--processor", default=None, metavar="MODULE:CALLABLE",
                        help="заменить встроенный экстрактор области: process(area, id, path)")
    args = parser.parse_args(argv)

    # The processor is called as process(area, publication_id, path) and cannot
    # be handed --db, so the target database travels through the environment that
    # extract.process reads. Without this the jobs row would land in --db while
    # the extracted rows went to storage.DEFAULT_DB.
    os.environ["VINITI_DATA_DB"] = str(args.db)

    try:
        processor = load_processor(args.processor)
        summary = run(
            args.area, args.split, db_path=args.db, resume=args.resume,
            limit=args.limit, timeout=args.timeout, dry_run=args.dry_run,
            process=processor, revision=args.extractor_revision,
        )
    except (FileNotFoundError, ValueError) as exc:
        print(f"batch: {exc}", file=sys.stderr)
        return 2

    # A completed run with failed files is a failed run: CI should notice.
    return 1 if summary["error"] else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
