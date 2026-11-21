"""Тесты пакетного прогона: возобновление по ревизии, изоляция сбоев.

Самодостаточно: подменяет корень корпуса на tempfile и кладёт туда пустые .pdf.
Каталог _corpus/ не нужен - в CI его нет.
"""
from io import BytesIO
import hashlib
import os
import pathlib
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import batch
import db as storage


def tiny_pdf():
    """Одна страница с текстом, чтобы настоящий экстрактор было на чём гонять."""
    from reportlab.pdfgen import canvas

    buffer = BytesIO()
    drawing = canvas.Canvas(buffer)
    drawing.drawString(72, 720, "The device showed a PCE of 12.5 % under reverse scan.")
    drawing.save()
    return buffer.getvalue()


def quiet(_message):
    """Заглушка вместо print, чтобы тест не засорял вывод."""


def failing(area, publication_id, pdf_path):
    raise RuntimeError(f"нет экстрактора для {publication_id}")


def working(area, publication_id, pdf_path):
    return {"publication_id": publication_id}


class BatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.kit = self.root / "kit"
        (self.kit / "dev").mkdir(parents=True)
        for name in ("D001", "D002", "D003"):
            (self.kit / "dev" / f"{name}.pdf").write_bytes(b"%PDF-1.4")
        self.database = self.root / "batch.sqlite3"
        storage.init(self.database)
        # Корень корпуса подменяется: в CI каталога _corpus/ нет.
        self.saved_roots = dict(batch.CORPUS_ROOTS)
        batch.CORPUS_ROOTS["solar"] = self.kit
        batch.CORPUS_ROOTS["rag"] = self.kit
        # batch.run выставляет VINITI_DATA_DB, вернём окружение как было.
        self.saved_env = os.environ.get("VINITI_DATA_DB")

    def tearDown(self):
        batch.CORPUS_ROOTS.clear()
        batch.CORPUS_ROOTS.update(self.saved_roots)
        if self.saved_env is None:
            os.environ.pop("VINITI_DATA_DB", None)
        else:
            os.environ["VINITI_DATA_DB"] = self.saved_env
        self.tmp.cleanup()

    def run_batch(self, **options):
        options.setdefault("db_path", self.database)
        options.setdefault("log", quiet)
        return batch.run("solar", "dev", **options)

    def jobs(self, area="solar"):
        conn = storage.connect(self.database)
        try:
            return {row["publication_id"]: dict(row) for row in conn.execute(
                "SELECT * FROM jobs WHERE area = ?", (area,))}
        finally:
            conn.close()

    def test_dry_run_touches_nothing(self):
        summary = self.run_batch(dry_run=True, process=working)
        self.assertEqual(summary["discovered"], 3)
        self.assertEqual(summary["selected"], 3)
        self.assertEqual(self.jobs(), {})

    def test_limit_caps_the_run(self):
        summary = self.run_batch(limit=2, process=working)
        self.assertEqual(summary["selected"], 2)
        self.assertEqual(summary["ok"], 2)
        self.assertEqual(len(self.jobs()), 2)

    def test_one_failure_does_not_abort_the_run(self):
        def sometimes(area, publication_id, pdf_path):
            if publication_id == "D002":
                raise RuntimeError("сломалось")
            return {"publication_id": publication_id}

        summary = self.run_batch(process=sometimes)
        self.assertEqual(summary["ok"], 2)
        self.assertEqual(summary["error"], 1)
        jobs = self.jobs()
        self.assertEqual(jobs["D002"]["status"], "error")
        self.assertIn("сломалось", jobs["D002"]["error"])
        self.assertEqual(jobs["D001"]["status"], "ok")

    def test_resume_skips_done_publications(self):
        self.run_batch(process=working)
        summary = self.run_batch(resume=True, process=working)
        self.assertEqual(summary["skipped"], 3)
        self.assertEqual(summary["selected"], 0)

    def test_new_extractor_revision_reruns_done_publications(self):
        """Смена ревизии обязана перезапустить: кэш по sha256 молча отдаёт старое."""
        self.run_batch(process=working, revision=1)
        summary = self.run_batch(resume=True, process=working, revision=2)
        self.assertEqual(summary["skipped"], 0)
        self.assertEqual(summary["selected"], 3)
        self.assertEqual({row["extractor_revision"] for row in self.jobs().values()}, {2})

    def test_resume_retries_a_failed_publication(self):
        self.run_batch(process=failing)
        summary = self.run_batch(resume=True, process=working)
        self.assertEqual(summary["skipped"], 0)
        self.assertEqual(summary["ok"], 3)
        self.assertEqual(self.jobs()["D001"]["attempts"], 2)

    def test_areas_do_not_share_jobs(self):
        self.run_batch(process=working)
        self.assertEqual(len(self.jobs("solar")), 3)
        self.assertEqual(self.jobs("rag"), {})

    def test_negative_limits_are_refused(self):
        with self.assertRaises(ValueError):
            self.run_batch(limit=-1, process=working)
        with self.assertRaises(ValueError):
            self.run_batch(timeout=-1, process=working)

    def test_split_must_be_a_directory_name(self):
        with self.assertRaises(ValueError):
            batch.run("solar", "../dev", db_path=self.database, process=working, log=quiet)

    def test_default_processor_is_the_area_extractor(self):
        """Без --processor solar идёт через solar_offline.process и пишет конверт.

        documents и blocks пишутся в ту же базу, которую видит batch: процессор
        узнаёт её из VINITI_DATA_DB, потому что сигнатура process его не получает.
        """
        pdf = tiny_pdf()
        (self.kit / "dev" / "D001.pdf").write_bytes(pdf)
        summary = self.run_batch(limit=1)
        self.assertEqual(summary["ok"], 1)
        self.assertEqual(summary["error"], 0)
        self.assertEqual(os.environ["VINITI_DATA_DB"], str(self.database))

        connection = storage.connect(self.database)
        try:
            document = connection.execute(
                "SELECT sha256, page_count, parser_status FROM documents "
                "WHERE area = 'solar' AND publication_id = 'D001'").fetchone()
            blocks = connection.execute(
                "SELECT COUNT(*) FROM blocks WHERE area = 'solar' "
                "AND publication_id = 'D001'").fetchone()[0]
            results = connection.execute(
                "SELECT COUNT(*) FROM solar_results WHERE area = 'solar' "
                "AND publication_id = 'D001'").fetchone()[0]
        finally:
            connection.close()
        self.assertIsNotNone(document)
        self.assertEqual(document["sha256"], hashlib.sha256(pdf).hexdigest())
        self.assertEqual(document["parser_status"], "ok")
        self.assertEqual(blocks, document["page_count"])
        self.assertEqual(results, 1)

    def test_default_processor_dispatches_by_area(self):
        with patch("solar_offline.process", return_value="solar") as solar, \
                patch("extract.process", return_value="extract") as legacy:
            self.assertEqual(batch.default_process("solar", "D001", "a.pdf"), "solar")
            self.assertEqual(batch.default_process("rag", "P001", "b.pdf"), "extract")
        solar.assert_called_once_with("solar", "D001", "a.pdf")
        legacy.assert_called_once_with("rag", "P001", "b.pdf")

    def test_solar_has_its_own_extractor_revision(self):
        import solar_offline

        self.assertEqual(batch.default_revision("solar"), solar_offline.PARSER_VERSION)
        self.assertEqual(batch.default_revision("rag"), batch.DEFAULT_EXTRACTOR_REVISION)
        self.assertNotEqual(solar_offline.PARSER_VERSION,
                            batch.DEFAULT_EXTRACTOR_REVISION)


if __name__ == "__main__":
    unittest.main()
