"""Тесты pipeline.py: коды выхода, порядок стадий, dry-run, настоящий прогон.

Mock-тесты подменяют стадии через unittest.mock.patch, настоящий прогон строит
синтетический корпус в tempfile (как test_batch.py) и гоняет реальный
batch -> issues -> export с --skip-gate. Каталог _corpus/ не нужен.

Flow-тесты (FlowCase и наследники) идут дальше порядка вызовов: они проверяют,
что следующая стадия видит данные предыдущей. Разбор настоящий (solar_offline
по синтетическому PDF), находки и выгрузка тоже настоящие, подменён только
gate.check: он запускает валидаторы из _corpus/, а тесты корпуса не требуют.
Фейковый гейт не пустой - он читает CSV из переданного каталога.

Run:  python -X utf8 -m unittest test_pipeline -v
"""
from io import BytesIO
import contextlib
import csv
import io
import os
import pathlib
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import batch
import db as storage
import pipeline

PATCH_TARGETS = (
    "batch.run", "issues.generate", "issues.write",
    "export_csv.export_official", "gate.check",
)


def quiet():
    """Прятать печать стадий в mock-тестах."""
    return contextlib.redirect_stdout(io.StringIO())


def summary(ok=0, error=0, skipped=0, discovered=1, failed=None):
    return {"area": "solar", "split": "dev", "discovered": discovered,
            "selected": 1, "ok": ok, "error": error, "skipped": skipped,
            "failed": list(failed or [])}


def clean_gate(area, submission, split="dev", timeout=3600):
    return pipeline.gate.GateResult(area=area, submission=submission,
                                    status=pipeline.gate.CLEAN, errors=0)


def broken_gate(area, submission, split="dev", timeout=3600):
    return pipeline.gate.GateResult(area=area, submission=submission,
                                    status=pipeline.gate.BROKEN, errors=2)


def failing_gate(area, submission, split="dev", timeout=3600):
    return pipeline.gate.GateResult(area=area, submission=submission,
                                    failure="валидатор не запустился")


def table_pdf():
    """Одна страница с таблицей, которую TableFiller реально разбирает.

    pdfplumber видит сетку как таблицу: шапка с метриками и две строки чисел,
    по четыре числа в каждой - ровно четыре метрики (Jsc, Voc, FF, PCE).
    """
    from reportlab.pdfgen import canvas

    buffer = BytesIO()
    drawing = canvas.Canvas(buffer)
    drawing.setFont("Helvetica", 7)
    columns = [30, 90, 150, 205, 280, 330]
    rows = [170, 145, 120, 95]
    cells = [
        ["Sample", "J SC (mA cm\u22122)", "Voc (V)", "FF", "PCE (%)"],
        ["Pristine", "15.28", "1,197", "0.71", "12.89"],
        ["Control", "14.10", "1,150", "0.68", "11.02"],
    ]
    for x in columns:
        drawing.setLineWidth(0.6)
        drawing.line(x, rows[0], x, rows[-1])
    for y in rows:
        drawing.line(columns[0], y, columns[-1], y)
    for ri, row in enumerate(cells):
        for ci, text in enumerate(row):
            drawing.drawString(columns[ci] + 3, rows[ri] - 9, text)
    drawing.showPage()
    drawing.save()
    return buffer.getvalue()


def inconsistent_table_pdf():
    """Две строки одного замера: первая сходится, вторая - нет.

    ``solar_offline`` разбирает таблицу по координатам в 8 строк ``results``
    (по четыре метрики на прибор). У DV2 напечатано PCE 5.0 %, а Jsc * Voc * FF
    даёт 12.6 %, поэтому ``issues.jv_consistency`` обязан найти одну находку.
    Так тест может проследить одни и те же данные через все стадии, а не
    проверять только факт вызова.
    """
    from reportlab.pdfgen import canvas

    buffer = BytesIO()
    drawing = canvas.Canvas(buffer)
    drawing.setFont("Helvetica", 7)
    columns = [30, 90, 150, 205, 280, 330]
    rows = [170, 145, 120, 95]
    cells = [
        ["Sample", "J SC (mA cm-2)", "Voc (V)", "FF (%)", "PCE (%)"],
        ["Pristine", "15.28", "1.197", "71.0", "12.89"],
        ["Bad", "15.0", "1.200", "70.0", "5.0"],
    ]
    for x in columns:
        drawing.setLineWidth(0.6)
        drawing.line(x, rows[0], x, rows[-1])
    for y in rows:
        drawing.line(columns[0], y, columns[-1], y)
    for ri, row in enumerate(cells):
        for ci, text in enumerate(row):
            drawing.drawString(columns[ci] + 3, rows[ri] - 9, text)
    drawing.showPage()
    drawing.save()
    return buffer.getvalue()


def read_csv(path):
    """CSV сдачи как список строк; BOM solar снимается."""
    with pathlib.Path(path).open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.reader(stream))


class ExitCodeTest(unittest.TestCase):
    """Отображение кодов выхода при подменённых стадиях."""

    def setUp(self):
        self.tmp = TemporaryDirectory(prefix="viniti-pipeline-")
        self.addCleanup(self.tmp.cleanup)
        self.dir = pathlib.Path(self.tmp.name)
        self.db = self.dir / "viniti.sqlite3"
        storage.init(self.db)
        self.out = self.dir / "out"

    def run_with(self, batch_fake, gate_fake, area="all"):
        with quiet():
            with patch("batch.run", side_effect=batch_fake), \
                 patch("issues.generate", side_effect=lambda area, conn: []), \
                 patch("issues.write", side_effect=lambda area, conn, rows: 0), \
                 patch("export_csv.export_official",
                       side_effect=lambda conn, area, spec, outdir: {}), \
                 patch("gate.check", side_effect=gate_fake):
                return pipeline.main(["--area", area, "--db", str(self.db),
                                      "--out", str(self.out)])

    def test_clean_everything_exits_zero(self):
        code = self.run_with(
            lambda area, split, **kw: summary(ok=1),
            clean_gate,
        )
        self.assertEqual(code, 0)

    def test_parse_errors_with_clean_gate_exit_two(self):
        """Ошибки разбора при чистом гейте -> 2."""
        code = self.run_with(
            lambda area, split, **kw: summary(error=2,
                                              failed=[f"{area}D009"]),
            clean_gate,
        )
        self.assertEqual(code, 2)

    def test_structural_errors_exit_one(self):
        code = self.run_with(
            lambda area, split, **kw: summary(ok=1),
            broken_gate,
        )
        self.assertEqual(code, 1)

    def test_gate_failure_exit_three(self):
        code = self.run_with(
            lambda area, split, **kw: summary(ok=1),
            failing_gate,
        )
        self.assertEqual(code, 3)

    def test_structural_errors_win_over_parse_errors(self):
        """Худшая область побеждает: ошибки валидатора важнее ошибок разбора."""
        code = self.run_with(
            lambda area, split, **kw: summary(error=1),
            broken_gate,
        )
        self.assertEqual(code, 1)

    def test_skip_extract_without_database_exits_three(self):
        missing = self.dir / "net.sqlite3"
        with quiet():
            code = pipeline.main(["--area", "solar", "--db", str(missing),
                                  "--out", str(self.out), "--skip-extract"])
        self.assertEqual(code, 3)
        self.assertFalse(missing.exists())

    def test_parse_errors_skip_gate_exit_two(self):
        """Гейт пропущен, но разбор падал: тоже 2."""
        with quiet():
            with patch("batch.run",
                       side_effect=lambda area, split, **kw: summary(error=1)), \
                 patch("issues.generate", side_effect=lambda area, conn: []), \
                 patch("issues.write", side_effect=lambda area, conn, rows: 0), \
                 patch("export_csv.export_official",
                       side_effect=lambda conn, area, spec, outdir: {}):
                code = pipeline.main(["--area", "solar", "--db", str(self.db),
                                      "--out", str(self.out), "--skip-gate"])
        self.assertEqual(code, 2)


class StageOrderTest(unittest.TestCase):
    """Фейки пишут в общий список: порядок extract -> issues -> export -> gate."""

    def setUp(self):
        self.tmp = TemporaryDirectory(prefix="viniti-pipeline-")
        self.addCleanup(self.tmp.cleanup)
        self.dir = pathlib.Path(self.tmp.name)
        self.db = self.dir / "viniti.sqlite3"
        storage.init(self.db)
        self.out = self.dir / "out"

    def test_order_for_each_area(self):
        calls = []

        def fake_batch(area, split, **kw):
            calls.append("extract")
            return summary(ok=1)

        def fake_generate(area, conn):
            calls.append("issues")
            return []

        def fake_write(area, conn, rows):
            return 0

        def fake_export(conn, area, spec, outdir):
            calls.append("export")
            return {}

        def fake_gate(area, submission, split="dev", timeout=3600):
            calls.append("gate")
            return pipeline.gate.GateResult(
                area=area, submission=submission,
                status=pipeline.gate.CLEAN, errors=0)

        with quiet():
            with patch("batch.run", side_effect=fake_batch), \
                 patch("issues.generate", side_effect=fake_generate), \
                 patch("issues.write", side_effect=fake_write), \
                 patch("export_csv.export_official", side_effect=fake_export), \
                 patch("gate.check", side_effect=fake_gate):
                code = pipeline.main(["--area", "all", "--db", str(self.db),
                                      "--out", str(self.out)])
        self.assertEqual(code, 0)
        self.assertEqual(calls, ["extract", "issues", "export", "gate",
                                 "extract", "issues", "export", "gate"])


class DryRunTest(unittest.TestCase):
    """--dry-run: ни одна стадия не вызвана, база не создана."""

    def setUp(self):
        self.tmp = TemporaryDirectory(prefix="viniti-dry-")
        self.addCleanup(self.tmp.cleanup)
        self.dir = pathlib.Path(self.tmp.name)

    def test_dry_run_calls_nothing_and_creates_no_database(self):
        missing = self.dir / "db.sqlite3"
        calls = []

        def boom(*args, **kwargs):
            calls.append("called")
            raise AssertionError("стадия не должна вызываться при --dry-run")

        with quiet():
            with patch("batch.run", side_effect=boom), \
                 patch("issues.generate", side_effect=boom), \
                 patch("issues.write", side_effect=boom), \
                 patch("export_csv.export_official", side_effect=boom), \
                 patch("gate.check", side_effect=boom):
                code = pipeline.main(["--area", "all", "--db", str(missing),
                                      "--out", str(self.dir / "out"),
                                      "--dry-run"])
        self.assertEqual(code, 0)
        self.assertEqual(calls, [])
        self.assertFalse(missing.exists())


class RealRunTest(unittest.TestCase):
    """Настоящий прогон: batch -> issues -> export, гейт пропущен."""

    def test_solar_limited_run_writes_csv_and_results_rows(self):
        tmp = TemporaryDirectory(prefix="viniti-pipeline-real-")
        self.addCleanup(tmp.cleanup)
        root = pathlib.Path(tmp.name)
        kit = root / "kit"
        (kit / "dev").mkdir(parents=True)
        (kit / "dev" / "D900.pdf").write_bytes(table_pdf())

        db_path = root / "p.sqlite3"
        outdir = root / "out"
        saved_roots = dict(batch.CORPUS_ROOTS)
        saved_env = os.environ.get("VINITI_DATA_DB")
        try:
            batch.CORPUS_ROOTS["solar"] = kit
            with quiet():
                code = pipeline.main(["--area", "solar", "--split", "dev",
                                      "--limit", "1", "--db", str(db_path),
                                      "--out", str(outdir), "--skip-gate"])
        finally:
            batch.CORPUS_ROOTS.clear()
            batch.CORPUS_ROOTS.update(saved_roots)
            if saved_env is None:
                os.environ.pop("VINITI_DATA_DB", None)
            else:
                os.environ["VINITI_DATA_DB"] = saved_env

        self.assertEqual(code, 0)

        csvs = sorted(path.name for path in (outdir / "solar").glob("*.csv"))
        self.assertEqual(len(csvs), 7, csvs)

        connection = storage.connect(db_path)
        try:
            count = connection.execute(
                "SELECT COUNT(*) FROM solar_results").fetchone()[0]
            jobs = connection.execute(
                "SELECT status FROM jobs WHERE area = 'solar'").fetchall()
            issues = connection.execute(
                "SELECT COUNT(*) FROM solar_issues").fetchone()[0]
        finally:
            connection.close()
        self.assertGreater(count, 0)
        self.assertEqual([row["status"] for row in jobs], ["ok"])
        self.assertGreaterEqual(issues, 0)


def reading_gate(seen, *, status=None, errors=0):
    """Фейковый гейт, который читает то, что ему передали.

    Пустой заглушки недостаточно: если export ничего не записал или записал не
    те колонки, гейт обязан это увидеть. Поэтому фейк открывает ``results.csv``
    и ``issues.csv`` из каталога сдачи и возвращает результат по их строкам.
    """

    def check(area, submission, split="dev", timeout=3600):
        submission = pathlib.Path(submission)
        results = read_csv(submission / "results.csv")
        issues = read_csv(submission / "issues.csv")
        seen.append({"area": area, "submission": submission, "split": split,
                     "results": len(results) - 1, "issues": len(issues) - 1})
        return pipeline.gate.GateResult(
            area=area, submission=submission,
            status=status or pipeline.gate.CLEAN,
            tables=7, results=len(results) - 1, errors=errors)

    return check


class FlowCase(unittest.TestCase):
    """Temp-корпус и temp-база: настоящие batch, issues и export, фейковый гейт.

    Подменён только ``gate.check``: он запускает валидаторы организаторов из
    ``_corpus/``, а тесты не должны требовать корпуса. Всё остальное настоящее,
    включая ``solar_offline`` как процессор области solar.
    """

    def setUp(self):
        self.tmp = TemporaryDirectory(prefix="viniti-pipeline-flow-")
        self.addCleanup(self.tmp.cleanup)
        self.dir = pathlib.Path(self.tmp.name)
        self.kit = self.dir / "kit"
        (self.kit / "dev").mkdir(parents=True)
        (self.kit / "dev" / "D900.pdf").write_bytes(inconsistent_table_pdf())
        self.db = self.dir / "flow.sqlite3"
        self.out = self.dir / "out"
        # batch.run пишет в VINITI_DATA_DB, а CORPUS_ROOTS - общий словарь:
        # оба надо вернуть, иначе следующий тест увидит чужой корпус и базу.
        self.saved_roots = dict(batch.CORPUS_ROOTS)
        self.saved_env = os.environ.get("VINITI_DATA_DB")
        batch.CORPUS_ROOTS["solar"] = self.kit
        self.addCleanup(self.restore_environment)

    def restore_environment(self):
        batch.CORPUS_ROOTS.clear()
        batch.CORPUS_ROOTS.update(self.saved_roots)
        if self.saved_env is None:
            os.environ.pop("VINITI_DATA_DB", None)
        else:
            os.environ["VINITI_DATA_DB"] = self.saved_env

    def run_pipeline(self, *extra, gate_check=None):
        args = ["--area", "solar", "--split", "dev", "--db", str(self.db),
                "--out", str(self.out), *extra]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.ExitStack() as stack:
            if gate_check is not None:
                stack.enter_context(patch("gate.check", side_effect=gate_check))
            stack.enter_context(contextlib.redirect_stdout(out))
            stack.enter_context(contextlib.redirect_stderr(err))
            code = pipeline.main(args)
        return code, out.getvalue(), err.getvalue()

    def rows(self, sql, params=()):
        connection = storage.connect(self.db)
        try:
            return [dict(row) for row in connection.execute(sql, params)]
        finally:
            connection.close()

    def results_csv(self):
        return read_csv(self.out / "solar" / "results.csv")

    def issues_csv(self):
        return read_csv(self.out / "solar" / "issues.csv")


class FlowTest(FlowCase):
    """Одна и та же строка проходит разбор -> находки -> CSV -> гейт."""

    def test_stages_hand_real_rows_to_the_next_stage(self):
        seen = []
        code, out, err = self.run_pipeline(gate_check=reading_gate(seen))

        self.assertEqual(code, 0, err)
        # Сводка разбора отражает реально обработанный файл, а не заглушку.
        self.assertIn("[разбор] solar/dev: discovered 1, ok 1, error 0, skipped 0", out)
        # Находки считаются по строкам, которые batch только что записал.
        self.assertIn("[находки] solar: 1 находок (jv_consistency=1)", out)
        # Выгрузка читает ту же базу: 8 строк results и одна находка.
        self.assertIn("results=8", out)
        self.assertIn("issues=1", out)
        self.assertIn("[гейт] solar: clean", out)

        jobs = self.rows("SELECT status, attempts, extractor_revision, error "
                         "FROM jobs WHERE area = 'solar'")
        self.assertEqual([row["status"] for row in jobs], ["ok"])
        self.assertEqual(jobs[0]["attempts"], 1)
        self.assertEqual(jobs[0]["extractor_revision"],
                         batch.default_revision("solar"))
        self.assertEqual(jobs[0]["error"], "")

        results = self.rows("SELECT publication_id, result_id, metric_name "
                            "FROM solar_results ORDER BY result_id")
        self.assertEqual(len(results), 8, results)
        self.assertEqual([row["result_id"] for row in results],
                         [f"R{index:03d}" for index in range(1, 9)])
        self.assertTrue(all(row["publication_id"] == "D900" for row in results))

        stored = self.rows("SELECT issue_id, check_name, record_ids, "
                           "source_page, source_fragment FROM solar_issues")
        self.assertEqual(len(stored), 1, stored)
        self.assertEqual(stored[0]["issue_id"], "C01")
        self.assertEqual(stored[0]["check_name"], "jv_consistency")
        # Находка ссылается на строки разбора, а не на выдуманные id.
        self.assertEqual(stored[0]["record_ids"], "R005;R006;R007;R008")
        known = {row["result_id"] for row in results}
        for record_id in stored[0]["record_ids"].split(";"):
            self.assertIn(record_id, known)

        # Гейт получил именно каталог выгрузки и увидел в нём те же данные.
        self.assertEqual(len(seen), 1, seen)
        self.assertEqual(seen[0]["area"], "solar")
        self.assertEqual(seen[0]["split"], "dev")
        self.assertEqual(seen[0]["submission"].resolve(),
                         (self.out / "solar").resolve())
        self.assertEqual(seen[0]["results"], 8)
        self.assertEqual(seen[0]["issues"], 1)

        passport = storage.load_passport()
        results_csv = self.results_csv()
        issues_csv = self.issues_csv()
        self.assertEqual(results_csv[0],
                         passport["areas"]["solar"]["tables"]["results"]["columns"])
        self.assertEqual(issues_csv[0],
                         passport["areas"]["solar"]["tables"]["issues"]["columns"])
        self.assertEqual(len(results_csv) - 1, 8)
        self.assertEqual(len(issues_csv) - 1, 1)
        issue_row = dict(zip(issues_csv[0], issues_csv[1]))
        self.assertEqual(issue_row["record_ids"], stored[0]["record_ids"])
        self.assertEqual(issue_row["source_page"], stored[0]["source_page"])
        self.assertTrue(issue_row["source_fragment"])

    def test_resume_run_rebuilds_issues_and_export_from_stored_rows(self):
        """Второй прогон не разбирает PDF, но находки и CSV обязаны появиться.

        Между прогонами удаляем и находки, и готовые CSV: если бы issues и export
        зависели от первого запуска, а не от базы, второй прогон оставил бы
        пустую сдачу.
        """
        seen = []
        code, _, err = self.run_pipeline(gate_check=reading_gate(seen))
        self.assertEqual(code, 0, err)

        connection = storage.connect(self.db)
        try:
            with connection:
                connection.execute("DELETE FROM solar_issues")
        finally:
            connection.close()
        for path in (self.out / "solar").glob("*.csv"):
            path.unlink()

        seen = []
        code, out, err = self.run_pipeline(gate_check=reading_gate(seen))
        self.assertEqual(code, 0, err)
        self.assertIn("[разбор] solar/dev: discovered 1, ok 0, error 0, skipped 1", out)
        self.assertIn("[находки] solar: 1 находок", out)
        self.assertIn("results=8", out)
        self.assertEqual(seen[0]["results"], 8)
        self.assertEqual(seen[0]["issues"], 1)
        self.assertEqual(len(self.results_csv()) - 1, 8)
        self.assertEqual(len(self.issues_csv()) - 1, 1)

        jobs = self.rows("SELECT attempts FROM jobs WHERE area = 'solar'")
        self.assertEqual(jobs[0]["attempts"], 1, "resume не должен перезапускать разбор")
        result_ids = [row["result_id"] for row in
                      self.rows("SELECT result_id FROM solar_results")]
        self.assertEqual(len(result_ids), len(set(result_ids)), "дубли строк после resume")

    def test_skip_extract_reuses_database_and_still_builds_submission(self):
        """--skip-extract: разбора нет, но находки и выгрузка идут по базе."""
        seen = []
        code, _, err = self.run_pipeline(gate_check=reading_gate(seen))
        self.assertEqual(code, 0, err)
        for path in (self.out / "solar").glob("*.csv"):
            path.unlink()

        seen = []
        code, out, err = self.run_pipeline("--skip-extract",
                                           gate_check=reading_gate(seen))
        self.assertEqual(code, 0, err)
        self.assertIn("[разбор] solar: пропущено (--skip-extract)", out)
        self.assertIn("[находки] solar: 1 находок", out)
        self.assertIn("results=8", out)
        self.assertEqual(seen[0]["results"], 8)
        self.assertEqual(seen[0]["issues"], 1)
        self.assertEqual(len(self.results_csv()) - 1, 8)
        self.assertEqual(len(self.issues_csv()) - 1, 1)

    def test_gate_failure_keeps_the_built_submission(self):
        """Структурные ошибки гейта не стирают сдачу: её надо разобрать после прогона."""
        seen = []
        code, out, err = self.run_pipeline(
            gate_check=reading_gate(seen, status=pipeline.gate.BROKEN, errors=2))
        self.assertEqual(code, 1, err)
        self.assertIn("[гейт] solar: structural errors", out)
        self.assertEqual(seen[0]["results"], 8)
        self.assertEqual(len(self.results_csv()) - 1, 8)
        self.assertEqual(len(self.issues_csv()) - 1, 1)


class FlowFailureTest(FlowCase):
    """Ошибка стадии: что до неё успело записаться и что не запускается."""

    def test_one_broken_pdf_still_exports_the_good_one_and_exits_two(self):
        (self.kit / "dev" / "D901.pdf").write_bytes(b"not a pdf")
        seen = []
        code, out, err = self.run_pipeline(gate_check=reading_gate(seen))

        self.assertEqual(code, 2, err)
        self.assertIn("[разбор] solar/dev: discovered 2, ok 1, error 1, skipped 0", out)
        self.assertIn("D901 error", out)
        jobs = self.rows("SELECT publication_id, status, error FROM jobs "
                         "WHERE area = 'solar' ORDER BY publication_id")
        self.assertEqual([row["publication_id"] for row in jobs], ["D900", "D901"])
        self.assertEqual([row["status"] for row in jobs], ["ok", "error"])
        self.assertTrue(jobs[1]["error"].strip())

        # Битый файл не оставил полустрок: в выгрузке только данные D900.
        publications = {row["publication_id"] for row in
                        self.rows("SELECT publication_id FROM solar_results")}
        self.assertEqual(publications, {"D900"})
        self.assertEqual(len(self.results_csv()) - 1, 8)
        self.assertEqual(len(self.issues_csv()) - 1, 1)
        # Гейт всё равно получил полную сдачу по уцелевшей публикации.
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["results"], 8)

    def test_export_failure_stops_before_gate_and_leaves_no_submission(self):
        # Отдельный прогон с падающей выгрузкой: patch нужен только здесь.
        calls = []
        out, err = io.StringIO(), io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch("export_csv.export_official",
                                      side_effect=OSError("нет места на диске")))
            stack.enter_context(patch("gate.check",
                                      side_effect=lambda *a, **kw: calls.append(a)))
            stack.enter_context(contextlib.redirect_stdout(out))
            stack.enter_context(contextlib.redirect_stderr(err))
            code = pipeline.main(["--area", "solar", "--split", "dev",
                                  "--db", str(self.db), "--out", str(self.out)])

        self.assertEqual(code, 3, err.getvalue())
        self.assertIn("OSError: нет места на диске", err.getvalue())
        self.assertEqual(calls, [], "гейт не должен запускаться без сдачи")
        self.assertFalse(list((self.out / "solar").glob("*.csv")))
        # Находки идут до выгрузки, поэтому они уже в базе.
        self.assertEqual(len(self.rows("SELECT issue_id FROM solar_issues")), 1)
        self.assertEqual(len(self.rows("SELECT result_id FROM solar_results")), 8)

        # После сбоя база остаётся рабочей: повторный прогон без разбора проходит.
        seen = []
        code, out_text, err_text = self.run_pipeline(
            "--skip-extract", gate_check=reading_gate(seen))
        self.assertEqual(code, 0, err_text)
        self.assertEqual(len(self.results_csv()) - 1, 8)
        self.assertEqual(len(self.issues_csv()) - 1, 1)

    def test_issues_failure_stops_export_and_gate_but_keeps_extracted_rows(self):
        export_calls = []
        gate_calls = []
        out, err = io.StringIO(), io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch("issues.generate",
                                      side_effect=RuntimeError("сломанный запрос")))
            stack.enter_context(patch("export_csv.export_official",
                                      side_effect=lambda *a, **kw: export_calls.append(a)))
            stack.enter_context(patch("gate.check",
                                      side_effect=lambda *a, **kw: gate_calls.append(a)))
            stack.enter_context(contextlib.redirect_stdout(out))
            stack.enter_context(contextlib.redirect_stderr(err))
            code = pipeline.main(["--area", "solar", "--split", "dev",
                                  "--db", str(self.db), "--out", str(self.out)])

        self.assertEqual(code, 3, err.getvalue())
        self.assertIn("RuntimeError: сломанный запрос", err.getvalue())
        self.assertEqual(export_calls, [], "выгрузка не должна идти после сбоя находок")
        self.assertEqual(gate_calls, [], "гейт не должен запускаться после сбоя находок")
        self.assertFalse(list((self.out / "solar").glob("*.csv")))
        # Разбор завершился до сбоя и его строки уже зафиксированы.
        self.assertEqual(len(self.rows("SELECT result_id FROM solar_results")), 8)
        self.assertEqual([row["status"] for row in
                          self.rows("SELECT status FROM jobs WHERE area = 'solar'")],
                         ["ok"])


if __name__ == "__main__":
    unittest.main()