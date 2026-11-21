"""Тесты pipeline.py: коды выхода, порядок стадий, dry-run, настоящий прогон.

Mock-тесты подменяют стадии через unittest.mock.patch, настоящий прогон строит
синтетический корпус в tempfile (как test_batch.py) и гоняет реальный
batch -> issues -> export с --skip-gate. Каталог _corpus/ не нужен.

Run:  python -X utf8 -m unittest test_pipeline -v
"""
from io import BytesIO
import contextlib
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


if __name__ == "__main__":
    unittest.main()