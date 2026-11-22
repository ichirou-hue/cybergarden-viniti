"""Tests for the verifier gate and the window/filler seam.

Run:  python -X utf8 -m unittest test_extract -v
"""
from io import BytesIO
import hashlib
import os
import pathlib
import unittest
from tempfile import TemporaryDirectory

from viniti import db as storage, extract, verify


def source(identifier, page, quote, kind="text"):
    return {"id": identifier, "page": page, "bbox": [0, 0, 10, 10],
            "kind": kind, "quote": quote}


def tiny_pdf():
    """One page, two text lines, built with reportlab (already a dependency)."""
    from reportlab.pdfgen import canvas

    buffer = BytesIO()
    drawing = canvas.Canvas(buffer)
    drawing.drawString(72, 720, "The device showed a PCE of 12.5 % under reverse scan.")
    drawing.drawString(72, 700, "The pristine reference reached a PCE of 9.9 % only.")
    drawing.save()
    return buffer.getvalue()


def table_pdf():
    """One page carrying a table caption and a number, for locator tests."""
    from reportlab.pdfgen import canvas

    buffer = BytesIO()
    drawing = canvas.Canvas(buffer)
    drawing.drawString(72, 720, "Table 1. Photovoltaic parameters of the devices.")
    drawing.drawString(72, 700, "The champion device reached a PCE of 12.5 %.")
    drawing.save()
    return buffer.getvalue()


def blank_pdf():
    """One page with no text layer at all, like a scan without OCR."""
    from reportlab.pdfgen import canvas

    buffer = BytesIO()
    drawing = canvas.Canvas(buffer)
    drawing.showPage()
    drawing.save()
    return buffer.getvalue()


class ScriptedFiller:
    """A filler that returns fixed candidates and records the spec it was given."""

    def __init__(self, candidates):
        self.candidates = list(candidates)
        self.spec = None
        self.windows = 0

    def fill(self, window, spec):
        self.spec = spec
        self.windows += 1
        return list(self.candidates)


class VerifyTest(unittest.TestCase):
    def setUp(self):
        self.sources = [
            source(1, 3, "The device showed a PCE of 12.89% under reverse scan."),
            source(2, 4, "The pristine sample exhibited a Voc of 1,217 mV."),
        ]

    def test_fabricated_value_is_rejected_with_a_specific_reason(self):
        record = {"source_page": 3, "source_fragment": "PCE of 99.99%",
                  "metric_name": "PCE", "metric_value": "99.99"}
        ok, reason = verify.verify_record(record, self.sources)
        self.assertFalse(ok)
        self.assertIn("не найдена", reason)
        self.assertIn("странице 3", reason)
        self.assertIn("99.99", reason)

    def test_typography_and_whitespace_differences_are_accepted(self):
        sources = [
            source(1, 7, "CsPbI2Br\u2013doped film \u201cshowed\u201d a PCE of 12.89%... "
                         "the charge extrac-\ntion was stable."),
            source(2, 7, "The perov\u00adskite layer stayed stable."),
        ]
        record = {"source_page": "7", "source_fragment":
                  'CSPBI2BR-DOPED FILM "showed" a   PCE of 12.89%... the charge '
                  'extraction was stable', "metric_name": "PCE", "metric_value": "12.89"}
        ok, reason = verify.verify_record(record, sources)
        self.assertTrue(ok, reason)
        self.assertEqual(reason, "")
        self.assertTrue(verify.quote_present("perovskite layer", sources[1]["quote"]))

    def test_quote_without_the_value_is_rejected(self):
        """Цитата существует, но числа в ней нет - это и есть подмена числа."""
        record = {"source_page": 3,
                  "source_fragment": "The device showed a PCE",
                  "metric_name": "PCE", "metric_value": "24.1"}
        ok, reason = verify.verify_record(record, self.sources)
        self.assertFalse(ok)
        self.assertIn("не содержит значения", reason)
        self.assertIn("24.1", reason)

    def test_value_with_different_precision_is_accepted(self):
        """0.330 и 0.33 - одно число: точность записи не должна ломать проверку."""
        record = {"source_page": 3, "source_fragment": "PCE of 12.890%",
                  "metric_name": "PCE", "metric_value": "12.89"}
        self.assertTrue(verify.value_present("12.89", "PCE of 12.890%"))
        self.assertTrue(verify.value_present("0.330", "FF = 0.33"))

    def test_value_absent_from_fragment_is_caught(self):
        self.assertFalse(verify.value_present("24.1", "PCE of 21.4%"))
        self.assertFalse(verify.value_present("1.1", "Voc of 1.2 V"))

    def test_non_numeric_value_must_appear_verbatim(self):
        self.assertTrue(verify.value_present("n/a", "efficiency n/a for this sample"))
        self.assertFalse(verify.value_present("n/a", "efficiency 12.5%"))

    def test_quote_on_another_page_is_rejected(self):
        record = {"source_page": 5, "source_fragment": "Voc of 1,217 mV",
                  "metric_name": "Voc", "metric_value": "1,217"}
        ok, reason = verify.verify_record(record, self.sources)
        self.assertFalse(ok)
        self.assertIn("странице 4", reason)
        self.assertIn("страница 5", reason)

    def test_missing_locator_is_rejected(self):
        ok, reason = verify.verify_record(
            {"source_page": 3, "source_fragment": ""}, self.sources)
        self.assertFalse(ok)
        self.assertIn("source_fragment", reason)
        ok, reason = verify.verify_record(
            {"source_page": "нет", "source_fragment": "PCE of 12.89%"}, self.sources)
        self.assertFalse(ok)
        self.assertIn("source_page", reason)
        ok, reason = verify.verify_record({"source_page": 3}, self.sources)
        self.assertFalse(ok)
        self.assertIn("source_fragment", reason)

    def test_normalization_rules_are_the_documented_ones(self):
        self.assertEqual(verify.normalize_for_match("  PCE\u00a0 of\n12.89  "),
                         "pce of 12.89")
        self.assertEqual(verify.normalize_for_match("extrac-\ntion"), "extraction")
        self.assertEqual(verify.normalize_for_match("high-efficiency"), "high-efficiency")
        self.assertEqual(verify.normalize_for_match("\u2018A\u2019 \u2013 \u201cB\u201d"),
                         "'a' - \"b\"")
        self.assertEqual(verify.normalize_for_match("\u2026"), "...")
        self.assertEqual(verify.normalize_for_match("   "), "")
        # A space is not significant: PDF text and the reported quote disagree
        # about it ("cm-2,a VOC" vs "cm-2, a VOC"), and the check is about words
        # and numbers being present in order, not about typography.
        self.assertTrue(verify.quote_present("SnO2", "SnO 2"))
        self.assertTrue(verify.quote_present("SnO 2", "SnO2"))
        # ...but the words and their order still matter.
        self.assertFalse(verify.quote_present("SnO2", "Sn O3"))
        self.assertFalse(verify.quote_present("a PCE of 12.89", "12.89 a PCE of"))
        # A compound word split at a line break keeps its hyphen in the quote and
        # loses it on the page; both forms must match.
        self.assertTrue(verify.quote_present("high-efficiency", "high-\nefficiency"))
        self.assertTrue(verify.quote_present("high-\nefficiency", "high-efficiency"))

    # ------------------------------------------------------- локатор и цитата

    def test_locator_is_a_reference_and_a_sentence_is_a_quote(self):
        """Слово Table в начале ещё не делает фрагмент локатором.

        Для локатора число может лежать где угодно на странице, для цитаты - только
        внутри неё. Если считать локатором любую фразу, начинающуюся с "Table",
        любое число страницы подтвердит любую строку.
        """
        self.assertTrue(verify.is_locator("Table 1, строка pristine"))
        self.assertTrue(verify.is_locator("Table 2: Ablation study"))
        self.assertTrue(verify.is_locator("Figure 1:"))
        self.assertTrue(verify.is_locator("Supplementary Table 3"))
        self.assertTrue(verify.is_locator("TABLE I"))
        self.assertFalse(verify.is_locator("Table 1 lists the champion cell"))
        self.assertFalse(verify.is_locator("Figure 2 reveals a clear trend"))
        self.assertFalse(verify.is_locator("Supplementary information is available"))

    def test_sentence_starting_with_table_cannot_borrow_a_number_from_the_page(self):
        sources = [source(1, 5, "Table 1 lists the champion cell."),
                   source(2, 5, "the control cell reached 24.5 mA cm-2")]
        record = {"source_page": 5, "source_fragment": "Table 1 lists the champion cell",
                  "metric_name": "Jsc", "metric_value": "24.5"}
        ok, reason = verify.verify_record(record, sources)
        self.assertFalse(ok, "число другого прибора не должно подтверждать строку")
        self.assertIn("не содержит значения", reason)

    def test_supplementary_table_number_matches_the_printed_short_form(self):
        """В сдаче 'Supplementary Table 3', в статье 'Table S3' - это одна таблица."""
        sources = [source(1, 7, "Table S3. Parameters of the champion devices.")]
        texts = {7: "Table S3. Parameters of the champion devices. PCE 15.28 %"}
        ok, reason = verify.verify_record(
            {"source_page": 7, "source_fragment": "Supplementary Table 3",
             "metric_value": "15.28"}, sources, texts)
        self.assertTrue(ok, reason)

    def test_locator_without_its_label_on_the_page_is_rejected(self):
        sources = [source(1, 5, "TABLE 2 | Device parameters")]
        ok, reason = verify.verify_record(
            {"source_page": 5, "source_fragment": "Table 1, строка pristine",
             "metric_value": "15.28"}, sources, {5: "TABLE 2 | Device parameters"})
        self.assertFalse(ok)
        self.assertIn("Table 1", reason)

    # ------------------------------------------------------- пустое значение

    def test_empty_value_is_rejected_unless_the_status_says_it_is_absent(self):
        """Строка без числа - не результат; эталон помечает такие статусом."""
        sources = [source(1, 3, "The device showed a PCE under reverse scan.")]
        base = {"source_page": 3, "source_fragment": "The device showed a PCE",
                "metric_name": "PCE"}
        ok, reason = verify.verify_record(dict(base), sources)
        self.assertFalse(ok)
        self.assertIn("metric_value", reason)
        for status in ("present_not_extractable", "absent"):
            ok, reason = verify.verify_record({**base, "metric_value": "",
                                               "extraction_status": status}, sources)
            self.assertTrue(ok, f"{status}: {reason}")

    def test_overflowing_number_does_not_crash_the_gate(self):
        """Значение приходит от модели, поэтому мусор не должен ронять проверку."""
        self.assertFalse(verify.value_present("1e1000000", "PCE 12.5"))
        self.assertFalse(verify.value_present("99.99", "PCE 12.5"))
        self.assertTrue(verify.value_present("12.5", "PCE 12.5"))


class WindowTest(unittest.TestCase):
    def test_windows_respect_budget_and_drop_no_source(self):
        sources = [source(index, 1 + index // 10, "x" * 100)
                   for index in range(1, 41)]
        windows = extract.context_windows(sources, budget_chars=350, overlap=1)
        self.assertGreater(len(windows), 1)
        for window in windows:
            self.assertLessEqual(len(window["text"]), 350)
            self.assertEqual(len(window["sources"]), len(window["items"]))
            self.assertEqual(window["sources"], sorted(window["sources"]))
        used = {identifier for window in windows for identifier in window["sources"]}
        self.assertEqual(used, {item["id"] for item in sources})

    def test_window_never_spans_pages_and_never_splits_a_source(self):
        sources = [source(1, 1, "a" * 200), source(2, 2, "b" * 200)]
        windows = extract.context_windows(sources, budget_chars=1000, overlap=1)
        self.assertEqual([window["page"] for window in windows], [1, 2])
        self.assertEqual([window["sources"] for window in windows], [[1], [2]])

        huge = [source(1, 1, "y" * 10_000)]
        windows = extract.context_windows(huge, budget_chars=100)
        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0]["text"], "y" * 10_000)


class TableFillerTest(unittest.TestCase):
    def test_metric_in_prose_is_extracted_as_verifiable_evidence(self):
        item = {"id": 1, "page": 2, "kind": "text",
                "quote": "The champion device reached a PCE of 18.42 %."}
        rows = extract.TableFiller().fill(
            {"items": [item], "page": 2, "sources": [1], "text": ""}, {})
        self.assertEqual([(row["metric_name"], row["metric_value"], row["metric_unit"])
                          for row in rows], [("PCE", "18.42", "%")])
        ok, reason = verify.verify_record(rows[0], [item])
        self.assertTrue(ok, reason)

    def test_prose_does_not_invent_a_number_from_a_distant_line(self):
        items = [{"id": 1, "page": 2, "kind": "text", "quote": "PCE was reported."},
                 {"id": 2, "page": 2, "kind": "text", "quote": "The value was 18.42 %."}]
        rows = extract.TableFiller().fill(
            {"items": items, "page": 2, "sources": [1, 2], "text": ""}, {})
        self.assertEqual(rows, [])

    def test_prose_requires_an_explicit_solar_metric_phrase(self):
        item = {"id": 1, "page": 2, "kind": "text",
                "quote": "The material showed excellent efficiency close to 23%."}
        rows = extract.TableFiller().fill(
            {"items": [item], "page": 2, "sources": [1], "text": ""}, {})
        self.assertEqual(rows, [])

    def test_header_and_row_are_paired_and_verifiable(self):
        header = ("Sample |  | J | SC | (mA cm\u22122) | V | OC (mV) | FF | PCE (%)")
        row = "Pristine |  |  |  | 15.28 |  | 1,197 | 0.71 | 12.89"
        items = [
            {"id": 1, "page": 5, "kind": "table", "quote": header, "table": 1, "row": 2},
            {"id": 2, "page": 5, "kind": "table", "quote": row, "table": 1, "row": 3},
            # three numbers against four metrics: must be skipped, not guessed
            {"id": 3, "page": 5, "kind": "table",
             "quote": "Odd |  | 15.75 | 1,322 | 0.77", "table": 1, "row": 4},
        ]
        window = {"index": 1, "page": 5, "sources": [1, 2, 3], "text": "", "items": items}
        rows = extract.TableFiller().fill(window, {})
        self.assertEqual(
            [(row["metric_name"], row["metric_value"], row["metric_unit"]) for row in rows],
            [("Jsc", "15.28", "mA cm-2"), ("Voc", "1,197", "mV"),
             ("FF", "0.71", ""), ("PCE", "12.89", "%")])
        sources = [{"id": item["id"], "page": item["page"], "kind": item["kind"],
                    "quote": item["quote"]} for item in items]
        for row in rows:
            ok, reason = verify.verify_record(row, sources)
            self.assertTrue(ok, reason)
            self.assertEqual(row["source_page"], 5)

    def test_without_a_header_nothing_is_emitted(self):
        window = {"index": 1, "page": 5, "sources": [1], "text": "",
                  "items": [{"id": 1, "page": 5, "kind": "table",
                             "quote": "Pristine |  | 15.28 | 1,197", "table": 1, "row": 3}]}
        self.assertEqual(extract.TableFiller().fill(window, {}), [])


class RunTest(unittest.TestCase):
    def test_run_writes_only_verified_rows(self):
        good = {"source_page": 1, "source_fragment": "PCE of 12.5 %",
                "metric_name": "PCE", "metric_value": "12.5", "metric_unit": "%"}
        fake = {"source_page": 1, "source_fragment": "PCE of 44.4 %",
                "metric_name": "PCE", "metric_value": "44.4", "metric_unit": "%"}
        filler = ScriptedFiller([good, fake])
        with TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "test.sqlite3"
            summary = extract.run("solar", "D900", tiny_pdf(), filler, db_path=path)
            self.assertEqual(summary["candidates"], 2)
            self.assertEqual(summary["verified"], 1)
            self.assertEqual(summary["rejected"], 1)
            self.assertEqual(summary["written"], 1)
            self.assertEqual(sum(summary["rejected_reasons"].values()), 1)
            self.assertTrue(any("не найдена" in reason
                                for reason in summary["rejected_reasons"]))
            self.assertEqual(filler.spec["key"], "result_id")
            self.assertIn("metric_value", filler.spec["columns"])
            self.assertEqual(filler.windows, summary["windows"])

            connection = storage.connect(path)
            try:
                rows = connection.execute(
                    "SELECT result_id, publication_id, metric_value, metric_unit, "
                    "source_page, source_fragment, value_num FROM solar_results").fetchall()
            finally:
                connection.close()
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row["publication_id"], "D900")
            self.assertEqual(row["result_id"], "X001")
            self.assertEqual(row["metric_value"], "12.5")  # text, not float
            self.assertEqual(row["source_page"], "1")
            self.assertEqual(row["source_fragment"], "PCE of 12.5 %")
            self.assertAlmostEqual(row["value_num"], 12.5)

            # A rerun replaces its own rows instead of appending, and a run whose
            # candidates all fail the gate leaves no stale rows behind.
            again = extract.run("solar", "D900", tiny_pdf(), filler, db_path=path)
            self.assertEqual(again["written"], 1)
            connection = storage.connect(path)
            try:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM solar_results").fetchone()[0], 1)
            finally:
                connection.close()

            empty = extract.run("solar", "D900", tiny_pdf(), ScriptedFiller([fake]),
                                db_path=path)
            self.assertEqual(empty["written"], 0)
            connection = storage.connect(path)
            try:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM solar_results").fetchone()[0], 0)
            finally:
                connection.close()

    def test_dry_run_does_not_touch_the_database(self):
        good = {"source_page": 1, "source_fragment": "PCE of 12.5 %",
                "metric_name": "PCE", "metric_value": "12.5", "metric_unit": "%"}
        with TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "test.sqlite3"
            summary = extract.run("solar", "D901", tiny_pdf(), ScriptedFiller([good]),
                                  db_path=path, dry_run=True)
            self.assertTrue(summary["dry_run"])
            self.assertEqual(summary["verified"], 1)
            self.assertEqual(summary["written"], 0)
            self.assertFalse(path.exists())

    def test_locator_candidate_is_written_instead_of_crashing(self):
        """Локатор не цитата, его нельзя вырезать из текста.

        Раньше run безусловно звал locate_span, тот возвращал None, и вся статья
        терялась с RuntimeError. Локатор уже проверен по метке таблицы на странице.
        """
        locator = {"source_page": 1, "source_fragment": "Table 1, строка pristine",
                   "metric_name": "PCE", "metric_value": "12.5", "metric_unit": "%"}
        with TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "test.sqlite3"
            summary = extract.run("solar", "D902", table_pdf(), ScriptedFiller([locator]),
                                  db_path=path)
            self.assertEqual(summary["written"], 1)
            connection = storage.connect(path)
            try:
                row = connection.execute(
                    "SELECT source_fragment, source_page FROM solar_results").fetchone()
            finally:
                connection.close()
            self.assertEqual(row["source_fragment"], "Table 1, строка pristine")
            self.assertEqual(row["source_page"], "1")

    def test_locator_for_a_table_absent_from_the_page_is_rejected(self):
        locator = {"source_page": 1, "source_fragment": "Table 9, строка pristine",
                   "metric_name": "PCE", "metric_value": "12.5", "metric_unit": "%"}
        with TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "test.sqlite3"
            summary = extract.run("solar", "D903", tiny_pdf(), ScriptedFiller([locator]),
                                  db_path=path)
            self.assertEqual(summary["written"], 0)
            self.assertEqual(summary["rejected"], 1)

    def test_envelope_holds_one_document_and_one_block_per_page(self):
        """Цитату из source_fragment надо проверять по базе, не открывая PDF.

        Поэтому текст страниц лежит в blocks, а documents хранит sha256 и статус.
        Повторный прогон обновляет эти строки, а не добавляет вторые.
        """
        pdf = tiny_pdf()
        with TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "test.sqlite3"
            extract.run("solar", "D904", pdf, ScriptedFiller([]), db_path=path,
                        source_path="dev/D904.pdf")
            connection = storage.connect(path)
            try:
                document = connection.execute(
                    "SELECT * FROM documents WHERE area = 'solar'").fetchone()
                blocks = connection.execute(
                    "SELECT ordinal, page, section, kind, text, bbox_json "
                    "FROM blocks WHERE area = 'solar' ORDER BY ordinal").fetchall()
            finally:
                connection.close()
            self.assertEqual(document["publication_id"], "D904")
            self.assertEqual(document["sha256"], hashlib.sha256(pdf).hexdigest())
            self.assertEqual(document["parser_status"], "ok")
            self.assertEqual(document["parser_version"], extract.PARSER_VERSION)
            self.assertEqual(document["page_count"], 1)
            self.assertEqual(document["source_path"], "dev/D904.pdf")
            self.assertEqual(document["in_scope"], "true")
            self.assertEqual(len(blocks), 1)
            self.assertEqual(blocks[0]["ordinal"], 1)
            self.assertEqual(blocks[0]["page"], 1)
            self.assertEqual(blocks[0]["kind"], "page")
            self.assertEqual(blocks[0]["section"], "")
            self.assertEqual(blocks[0]["bbox_json"], "[]")
            self.assertIn("PCE of 12.5 %", blocks[0]["text"])
            self.assertEqual(document["text_chars"], len(blocks[0]["text"]))

            again = extract.run("solar", "D904", pdf, ScriptedFiller([]), db_path=path)
            self.assertEqual(again["blocks"], 1)
            connection = storage.connect(path)
            try:
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) FROM documents").fetchone()[0], 1)
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) FROM blocks").fetchone()[0], 1)
            finally:
                connection.close()

    def test_empty_text_layer_is_recorded_as_empty(self):
        with TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "test.sqlite3"
            summary = extract.run("solar", "D905", blank_pdf(), ScriptedFiller([]),
                                  db_path=path)
            self.assertEqual(summary["parser_status"], "empty")
            connection = storage.connect(path)
            try:
                row = connection.execute(
                    "SELECT parser_status FROM documents WHERE publication_id = 'D905'"
                ).fetchone()
            finally:
                connection.close()
            self.assertEqual(row["parser_status"], "empty")


class ProcessTest(unittest.TestCase):
    """process() - точка стыковки с batch.py."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.database = self.root / "process.sqlite3"
        self.saved = os.environ.get("VINITI_DATA_DB")
        os.environ["VINITI_DATA_DB"] = str(self.database)

    def tearDown(self):
        if self.saved is None:
            os.environ.pop("VINITI_DATA_DB", None)
        else:
            os.environ["VINITI_DATA_DB"] = self.saved
        self.tmp.cleanup()

    def write(self, name, data):
        path = self.root / name
        path.write_bytes(data)
        return path

    def test_process_writes_into_the_database_from_the_environment(self):
        path = self.write("D906.pdf", tiny_pdf())
        summary = extract.process("solar", "D906", str(path))
        self.assertEqual(summary["pages"], 1)
        self.assertTrue(self.database.exists())
        connection = storage.connect(self.database)
        try:
            document = connection.execute(
                "SELECT sha256, source_path FROM documents").fetchone()
        finally:
            connection.close()
        self.assertEqual(document["sha256"],
                         hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(document["source_path"], str(path))

    def test_process_refuses_a_pdf_without_a_text_layer(self):
        path = self.write("D907.pdf", blank_pdf())
        with self.assertRaises(ValueError) as caught:
            extract.process("solar", "D907", str(path))
        self.assertIn("текстовый слой", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
