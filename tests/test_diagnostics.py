"""Тесты диагностического слоя: конфигурация, безопасные поля, контуры.

Модуль ``viniti.diagnostics`` ничего не настраивает при импорте: пока точка
входа не вызвала ``configure()``, слой молчит. Здесь это проверяется явно,
вместе с редакцией секретов и тел документов, форматами text/json, контекстом
и двумя реальными контурами (fallback разбора и поля job в batch).
"""
import builtins
import contextlib
import io
import json
import logging
import os
import pathlib
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

from viniti import batch, db as storage, pdf_source
from viniti import diagnostics


def tiny_pdf(text="The device showed a PCE of 12.5 % under reverse scan."):
    """Одна страница с текстом: настоящий PDF для pdfplumber/pypdf."""
    from reportlab.pdfgen import canvas

    buffer = io.BytesIO()
    drawing = canvas.Canvas(buffer)
    drawing.drawString(72, 720, text)
    drawing.save()
    return buffer.getvalue()


class LayerResetMixin:
    """Каждый тест начинает с чистого слоя и не оставляет за собой хендлер."""

    def setUp(self):
        diagnostics.reset()
        self.addCleanup(diagnostics.reset)

    def capture(self, **options):
        stream = io.StringIO()
        options.setdefault("level", "DEBUG")
        options.setdefault("fmt", "text")
        diagnostics.configure(force=True, stream=stream, **options)
        return stream


class DiagnosticsTestCase(LayerResetMixin, unittest.TestCase):

    def test_unconfigured_layer_is_silent(self):
        stderr = io.StringIO()
        stdout = io.StringIO()
        with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(stdout):
            logger = diagnostics.get_logger("silent")
            diagnostics.event(logger, "should.not.print", area="solar")
            diagnostics.warning(logger, "still.silent")
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(stdout.getvalue(), "")
        handlers = logging.getLogger("viniti").handlers
        self.assertTrue(handlers)
        self.assertTrue(all(isinstance(handler, logging.NullHandler)
                            for handler in handlers))

    def test_get_logger_does_not_configure(self):
        logger = diagnostics.get_logger("plain")
        self.assertEqual(logger.name, "viniti.plain")
        handlers = logging.getLogger("viniti").handlers
        self.assertTrue(all(isinstance(handler, logging.NullHandler)
                            for handler in handlers))

    def test_level_and_format_come_from_environment(self):
        with mock.patch.dict(os.environ, {"VINITI_LOG_LEVEL": "debug",
                                          "VINITI_LOG_FORMAT": "json"}, clear=False):
            stream = io.StringIO()
            diagnostics.configure(force=True, stream=stream)
            logger = diagnostics.get_logger("env")
            diagnostics.debug(logger, "env.debug", area="rag")
        record = json.loads(stream.getvalue().strip())
        self.assertEqual(record["level"], "DEBUG")
        self.assertEqual(record["event"], "env.debug")
        self.assertEqual(record["fields"]["area"], "rag")

    def test_unknown_level_and_format_fall_back(self):
        with mock.patch.dict(os.environ, {"VINITI_LOG_LEVEL": "chatty",
                                          "VINITI_LOG_FORMAT": "xml"}, clear=False):
            stream = io.StringIO()
            diagnostics.configure(force=True, stream=stream)
            logger = diagnostics.get_logger("fallback")
            diagnostics.debug(logger, "hidden")
            diagnostics.event(logger, "info.hidden")
            diagnostics.warning(logger, "visible")
        text = stream.getvalue()
        self.assertNotIn("hidden", text)
        self.assertNotIn("info.hidden", text)
        self.assertIn("visible", text)
        self.assertEqual(diagnostics.resolve_level("chatty"), logging.WARNING)

    def test_default_level_is_warning(self):
        """Без переменных окружения INFO не печатается: stdout уже занят CLI."""
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("VINITI_LOG_LEVEL", None)
            os.environ.pop("VINITI_LOG_FORMAT", None)
            stream = io.StringIO()
            diagnostics.configure(force=True, stream=stream)
            logger = diagnostics.get_logger("default")
            diagnostics.event(logger, "info.hidden")
            diagnostics.warning(logger, "warning.visible")
        text = stream.getvalue()
        self.assertNotIn("info.hidden", text)
        self.assertIn("warning.visible", text)
        self.assertEqual(diagnostics.resolve_level(None), logging.WARNING)

    def test_text_format_redacts_secrets_and_document_bodies(self):
        stream = self.capture()
        logger = diagnostics.get_logger("redact")
        diagnostics.event(
            logger, "document.ingest.done", area="solar", publication_id="D001",
            password="hunter2", api_key="sk-secret", csrf_token="token-value",
            cookie="session=abc", pdf_bytes=b"%PDF-1.4 payload",
            source_fragment="verbatim page text", raw_value="12.5",
            note="first line\nsecond line")
        text = stream.getvalue()
        for leaked in ("hunter2", "sk-secret", "token-value", "session=abc",
                       "%PDF-1.4", "verbatim page text", "12.5"):
            self.assertNotIn(leaked, text)
        self.assertIn("password=<redacted>", text)
        self.assertIn("publication_id=D001", text)
        self.assertEqual(len(text.strip().splitlines()), 1)  # one record per line

    def test_long_values_are_truncated(self):
        stream = self.capture()
        logger = diagnostics.get_logger("truncate")
        diagnostics.event(logger, "long", message="x" * 5000)
        text = stream.getvalue()
        self.assertLess(len(text), 1000)
        self.assertIn("...", text)

    def test_json_format_is_one_object_per_line(self):
        stream = self.capture(fmt="json")
        logger = diagnostics.get_logger("json")
        with diagnostics.context(area="rag", publication_id="P001", parser="pdfplumber"):
            diagnostics.event(logger, "job.ok", duration_ms=12, rows=3)
        lines = [line for line in stream.getvalue().splitlines() if line.strip()]
        self.assertEqual(len(lines), 1)
        record = json.loads(lines[0])
        self.assertEqual(record["event"], "job.ok")
        self.assertEqual(record["fields"]["area"], "rag")
        self.assertEqual(record["fields"]["publication_id"], "P001")
        self.assertEqual(record["fields"]["duration_ms"], 12)

    def test_context_nests_and_restores(self):
        stream = self.capture()
        logger = diagnostics.get_logger("ctx")
        with diagnostics.context(area="solar"):
            with diagnostics.context(publication_id="D001", job_id="solar:D001"):
                diagnostics.event(logger, "job.start")
            diagnostics.event(logger, "after.inner")
        diagnostics.event(logger, "after.outer")
        lines = stream.getvalue().strip().splitlines()
        self.assertIn("publication_id=D001", lines[0])
        self.assertIn("job_id=solar:D001", lines[0])
        self.assertIn("area=solar", lines[1])
        self.assertNotIn("publication_id", lines[1])
        self.assertNotIn("area", lines[2])
        self.assertEqual(diagnostics.current_context(), {})

    def test_context_is_restored_after_exception(self):
        logger = diagnostics.get_logger("ctx.error")
        self.capture()
        with self.assertRaises(RuntimeError):
            with diagnostics.context(area="solar"):
                raise RuntimeError("boom")
        self.assertEqual(diagnostics.current_context(), {})
        diagnostics.event(logger, "still.works", area="rag")
        self.assertEqual(diagnostics.current_context(), {})

    def test_exception_writes_traceback(self):
        stream = self.capture()
        logger = diagnostics.get_logger("boom")
        try:
            raise ValueError("bad pdf")
        except ValueError:
            diagnostics.exception(logger, "job.failed", publication_id="D002")
        text = stream.getvalue()
        self.assertIn("event=job.failed", text)
        self.assertIn("Traceback (most recent call last)", text)
        self.assertIn("ValueError: bad pdf", text)
        self.assertEqual(len(text.strip().splitlines()), 1)

    def test_multiline_exception_message_stays_one_line_and_scrubbed(self):
        """Многострочное сообщение с секретом не рвёт запись и не течёт."""
        stream = self.capture()
        logger = diagnostics.get_logger("boom.secret")
        page = "PCE 12.5 % under reverse scan; " * 40
        try:
            raise RuntimeError(f"token=sk-live-abcdefgh\n{page}\x1b[31m")
        except RuntimeError:
            diagnostics.exception(logger, "job.failed", publication_id="D003")
        text = stream.getvalue()
        lines = text.strip().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertIn("exception=", lines[0])
        self.assertIn("\\n", lines[0])
        self.assertNotIn("sk-live-abcdefgh", text)
        self.assertNotIn("\x1b", text)
        self.assertLess(len(lines[0]), diagnostics.MAX_EXCEPTION_CHARS + 400)
        self.assertIn("publication_id=D003", lines[0])

    def test_exception_in_json_is_one_line_and_scrubbed(self):
        stream = self.capture(fmt="json")
        logger = diagnostics.get_logger("boom.json")
        try:
            raise RuntimeError("password=hunter2\nsecond line")
        except RuntimeError:
            diagnostics.exception(logger, "job.failed")
        lines = [line for line in stream.getvalue().splitlines() if line.strip()]
        self.assertEqual(len(lines), 1)
        record = json.loads(lines[0])
        self.assertNotIn("hunter2", record["exception"])
        self.assertIn("\\n", record["exception"])

    def test_control_characters_do_not_split_or_leak_records(self):
        stream = self.capture()
        logger = diagnostics.get_logger("control")
        diagnostics.event(logger, "document.name", area="solar",
                          value="a\x00b\x1bc\u2028d")
        text = stream.getvalue()
        self.assertEqual(len(text.strip().splitlines()), 1)
        self.assertIn("\\x00", text)
        self.assertIn("\\x1b", text)
        self.assertIn("\\x2028", text)
        for raw in ("\x00", "\x1b", "\u2028"):
            self.assertNotIn(raw, text)

    def test_narrow_console_stream_does_not_raise(self):
        """cp1252-поток: непечатный символ экранируется, а не даёт logging error."""
        buffer = io.BytesIO()
        stream = io.TextIOWrapper(buffer, encoding="cp1252", errors="strict")
        self.addCleanup(stream.detach)
        diagnostics.configure(force=True, stream=stream, level="DEBUG")
        logger = diagnostics.get_logger("narrow")
        diagnostics.event(logger, "разбор", area="solar")
        stream.flush()
        data = buffer.getvalue()
        self.assertIn(b"event=", data)
        self.assertNotIn("--- Logging error ---".encode("ascii"), data)

    def test_safe_fields_bound_sequences_and_depth(self):
        fields = diagnostics.safe_fields({
            "pages": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14],
            "nested": {"token": "secret", "count": 2},
            "deep": {"a": {"b": {"c": {"d": 1}}}},
        })
        self.assertEqual(len(fields["pages"]), diagnostics.MAX_ITEMS + 1)
        self.assertEqual(fields["nested"]["token"], "<redacted>")
        self.assertEqual(fields["nested"]["count"], 2)
        self.assertEqual(fields["deep"]["a"]["b"], {"c": "<dict>"})


class ParserFallbackTest(LayerResetMixin, unittest.TestCase):
    """Fallback разбора обязан оставлять след в логе, а не молчать."""

    def test_page_texts_falls_back_to_pdfplumber_with_warning(self):
        stream = self.capture()
        data = tiny_pdf()
        real_import = builtins.__import__

        def without_pypdf(name, *args, **kwargs):
            if name == "pypdf":
                raise ImportError("pypdf is not installed for this test")
            return real_import(name, *args, **kwargs)

        with mock.patch("builtins.__import__", without_pypdf):
            texts = pdf_source.page_texts(data)
        self.assertTrue(texts, "pdfplumber fallback returned no page text")
        text = stream.getvalue()
        self.assertIn("pdf.text.fallback", text)
        self.assertIn("parser=pdfplumber", text)
        self.assertIn("reason=pypdf_missing", text)


class BatchJobFieldsTest(LayerResetMixin, unittest.TestCase):
    """События batch несут area/publication_id/job_id и не пачкают stdout."""

    def setUp(self):
        super().setUp()
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = pathlib.Path(self.tmp.name)
        self.kit = root / "kit"
        (self.kit / "dev").mkdir(parents=True)
        for name in ("D001", "D002"):
            (self.kit / "dev" / f"{name}.pdf").write_bytes(b"%PDF-1.4")
        self.database = root / "viniti.batch.sqlite3"
        storage.init(self.database)
        self.saved_roots = dict(batch.CORPUS_ROOTS)
        self.addCleanup(batch.CORPUS_ROOTS.update, self.saved_roots)
        batch.CORPUS_ROOTS["solar"] = self.kit
        self.saved_env = os.environ.get("VINITI_DATA_DB")
        self.addCleanup(self.restore_env)

    def restore_env(self):
        if self.saved_env is None:
            os.environ.pop("VINITI_DATA_DB", None)
        else:
            os.environ["VINITI_DATA_DB"] = self.saved_env

    def test_job_events_carry_context_and_keep_stdout_clean(self):
        stream = self.capture()
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            summary = batch.run("solar", "dev", db_path=self.database,
                                process=lambda area, pid, path: {"ok": pid})
        self.assertEqual(summary["ok"], 2)
        text = stream.getvalue()
        self.assertIn("event=batch.start", text)
        self.assertIn("event=job.ok", text)
        self.assertIn("publication_id=D001", text)
        self.assertIn("job_id=solar:D001", text)
        self.assertIn("duration_ms=", text)
        self.assertIn("event=batch.summary", text)
        # CLI печатает по-прежнему в stdout и без полей диагностики.
        printed = stdout.getvalue()
        self.assertIn("[batch] solar/dev", printed)
        self.assertNotIn("event=", printed)


if __name__ == "__main__":
    unittest.main()
