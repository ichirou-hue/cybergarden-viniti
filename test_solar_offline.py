"""Tests for the deterministic solar extractor (``solar_offline``).

The extractor reads a PDF with pdfplumber, finds table rows and prose results,
and hands the candidate tables to :mod:`solar_facts` for validation. These
tests build synthetic PDFs with reportlab, so they need no corpus on disk.
"""

import contextlib
from io import BytesIO, StringIO
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from reportlab.pdfgen import canvas

import db as storage
import solar_offline
from solar_schema import TABLES


def make_pdf(lines, table=False, rows=None):
    """One page: free text lines, then an optional fixed-column table.

    The header puts every symbol and its unit on its own line, which is how a
    typeset table declares its columns; the rows then carry one value per
    column at fixed x positions, so the column detector sees clean centres.
    """
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=(595, 842))
    y = 800
    for line in lines:
        pdf.drawString(60, y, line)
        y -= 16
    if table:
        pdf.drawString(60, y, "Device")
        for x, text in ((180, "VOC"), (215, "(mV)"), (280, "JSC"),
                        (315, "(mA/cm2)"), (390, "FF"), (415, "(%)"),
                        (490, "PCE"), (515, "(%)")):
            pdf.drawString(x, y, text)
        y -= 18
        for label, voc, jsc, ff, pce in rows or (
            ("Control", "1000", "20.0", "0.70", "14.0"),
            ("Target", "1050", "21.0", "0.75", "16.5"),
        ):
            for x, text in ((60, label), (180, voc), (280, jsc), (390, ff), (490, pce)):
                pdf.drawString(x, y, text)
            y -= 18
    pdf.showPage()
    pdf.save()
    return buffer.getvalue()


TITLE = "Improved Photovoltaic Performance of CsPbI2Br Perovskite Films"
# A page of body text keeps the table from being read as two newspaper columns:
# the detector splits a page when most lines share a vertical gap, and a real
# paper page always has prose above the table.
TABLE_PAGE = [
    TITLE, "Abstract",
    "The devices were measured under AM1.5G illumination at room temperature.",
    "Current density and voltage were recorded with a reverse scan.",
    "Stability was tracked at the maximum power point for one thousand hours.",
    "Each sample was encapsulated before the outdoor measurement.",
    "The absorber was spin-coated and annealed at one hundred degrees.",
    "All values are averaged over twenty cells of the same batch.",
    "The target device showed a PCE of 16.5% under AM1.5G illumination.",
    "Table 1 Photovoltaic parameters of the devices",
]


def extract(lines, table=False, rows=None):
    data = make_pdf(lines, table=table, rows=rows)
    with TemporaryDirectory() as directory:
        path = Path(directory, "D001.pdf")
        path.write_bytes(data)
        return solar_offline.extract_pdf(path)


class TableExtractionTest(unittest.TestCase):
    def setUp(self):
        self.result = extract(TABLE_PAGE, table=True)
        self.tables = self.result["tables"]

    def test_devices_and_measurement(self):
        devices = {row["device_id"]: row for row in self.tables["devices"]}
        self.assertEqual(set(devices), {"DV1", "DV2"})
        self.assertEqual(devices["DV1"]["device_label"], "Control")
        self.assertEqual(devices["DV1"]["device_role"], "control")
        self.assertEqual(devices["DV2"]["device_role"], "target")
        self.assertEqual(len(self.tables["measurements"]), 1)
        self.assertEqual(self.tables["measurements"][0]["measurement_type"], "J-V")

    def test_table_rows_are_read_and_normalized(self):
        values = {(row["device_id"], row["metric_name"]): row
                  for row in self.tables["results"]}
        self.assertEqual(values[("DV1", "Voc")]["normalized_value"], "1")
        self.assertEqual(values[("DV1", "Jsc")]["normalized_value"], "20")
        self.assertEqual(values[("DV1", "FF")]["normalized_value"], "70")
        self.assertEqual(values[("DV2", "PCE")]["normalized_value"], "16.5")
        self.assertEqual(values[("DV1", "PCE")]["raw_value"], "14.0")

    def test_every_row_has_a_real_source(self):
        sources = {source["id"] for source in self.result["sources"]}
        for key, anchors in self.result["evidence"].items():
            if key.startswith("publications/"):
                continue  # a publication is identified by the document itself
            self.assertTrue(anchors, key)
            for anchor in anchors:
                self.assertIn(anchor["id"], sources)

    def test_comparison_bandgap_is_not_taken_for_the_absorber(self):
        value, _, _ = solar_offline._bandgap(
            {"1": "A wide bandgap, higher than 3 eV is preferred. "
                  "The absorber has a band gap of 1.65 eV."})
        self.assertEqual(value, "1.65")
        value, _, _ = solar_offline._bandgap(
            {"1": "The transport layer has a wide bandgap, higher than 3 eV."})
        self.assertEqual(value, "")

    def test_bandgap_is_attached_to_the_material(self):
        tables = extract(TABLE_PAGE + ["The absorber has a band gap of 1.65 eV."],
                         table=True)["tables"]
        self.assertEqual(len(tables["materials"]), 2)
        for row in tables["materials"]:
            self.assertEqual(row["bandgap_value"], "1.65")
            self.assertEqual(row["bandgap_unit"], "eV")

    def test_publication_is_in_scope(self):
        self.assertEqual(len(self.tables["publications"]), 1)
        self.assertIn("CsPbI2Br", self.tables["publications"][0]["title"])
        self.assertEqual(self.tables["publications"][0]["in_scope"], "true")


class ProseExtractionTest(unittest.TestCase):
    def test_prose_value_is_kept_with_its_device(self):
        tables = extract([
            TITLE,
            "The Cs5 device showed a PCE of 16.5% under AM1.5G illumination.",
            "The Cs5 device kept 93% of its initial efficiency after 1000 h.",
        ])["tables"]
        pce = [row for row in tables["results"] if row["metric_name"] == "PCE"]
        self.assertEqual(len(pce), 1)
        self.assertEqual(pce[0]["normalized_value"], "16.5")
        self.assertEqual(pce[0]["device_id"], "DV-Cs5")

    def test_excluded_paper_with_results_keeps_only_its_row(self):
        tables = extract([
            "Band Gap Narrowing in Lead-Halide Perovskites by Dynamic Defect Self-Doping",
            "A theoretical study based on density functional theory.",
            "The reported PCE of 21.3% is taken from the literature.",
        ])["tables"]
        publication = tables["publications"][0]
        self.assertEqual(publication["in_scope"], "false")
        self.assertIn("Моделирование", publication["exclusion_reason"])
        for table in ("materials", "devices", "layers", "measurements", "results", "issues"):
            self.assertFalse(tables[table], table)

    def test_paper_without_numbers_stays_in_scope_and_empty(self):
        tables = extract([
            TITLE,
            "This review discusses the history of thin-film photovoltaics.",
        ])["tables"]
        publication = tables["publications"][0]
        self.assertEqual(publication["in_scope"], "true")
        self.assertEqual(publication["exclusion_reason"], "")
        self.assertFalse(tables["results"])

    def test_modelling_paper_is_excluded_with_a_reason(self):
        tables = extract([
            "Band Gap Narrowing in Lead-Halide Perovskites by Dynamic Defect Self-Doping",
            "A theoretical study based on density functional theory.",
        ])["tables"]
        publication = tables["publications"][0]
        self.assertEqual(publication["in_scope"], "false")
        self.assertIn("Моделирование", publication["exclusion_reason"])
        self.assertFalse(tables["results"])

    def test_ligature_hides_no_metric(self):
        parts = solar_offline._sentences("a power conversion ef\ufb01ciency of 23.0%")
        self.assertIn("efficiency", parts[0][1])

    def test_labelled_list_pairs_in_order(self):
        records = solar_offline.prose_records(
            "The device had a VOC of 1.05 V, a JSC of 21.0 mA cm-2 and a PCE of 16.5%.",
            [],
        )
        found = {(record["metric_name"], record["raw_value"]) for record in records}
        self.assertIn(("Voc", "1.05"), found)
        self.assertIn(("Jsc", "21.0"), found)
        self.assertIn(("PCE", "16.5"), found)


class DatabaseProcessorTest(unittest.TestCase):
    """``process`` is the default solar processor for ``batch.run``."""

    def test_process_writes_every_table_and_the_envelope(self):
        data = make_pdf(TABLE_PAGE, table=True)
        with TemporaryDirectory() as directory:
            pdf = Path(directory, "D001.pdf")
            pdf.write_bytes(data)
            database = Path(directory, "solar.sqlite3")
            storage.init(database)
            saved = os.environ.get("VINITI_DATA_DB")
            os.environ["VINITI_DATA_DB"] = str(database)
            try:
                first = solar_offline.process("solar", "D001", pdf)
                second = solar_offline.process("solar", "D001", pdf)
            finally:
                if saved is None:
                    os.environ.pop("VINITI_DATA_DB", None)
                else:
                    os.environ["VINITI_DATA_DB"] = saved
            connection = storage.connect(database)
            try:
                counts = {
                    table: connection.execute(
                        f"SELECT COUNT(*) FROM {storage.table_name('solar', table)}"
                    ).fetchone()[0]
                    for table in TABLES
                }
                document = connection.execute(
                    "SELECT sha256, parser_status, parser_version, page_count, in_scope "
                    "FROM documents WHERE area = 'solar' AND publication_id = 'D001'"
                ).fetchone()
                blocks = connection.execute(
                    "SELECT COUNT(*) FROM blocks WHERE area = 'solar' "
                    "AND publication_id = 'D001'").fetchone()[0]
            finally:
                connection.close()

        self.assertEqual(first["tables"], second["tables"])
        self.assertEqual(counts["publications"], 1)
        self.assertEqual(counts["devices"], 2)
        self.assertEqual(counts["measurements"], 1)
        self.assertEqual(counts["results"], 8)
        self.assertEqual(document["parser_status"], "ok")
        self.assertEqual(document["parser_version"], solar_offline.PARSER_VERSION)
        self.assertEqual(document["in_scope"], "true")
        self.assertEqual(blocks, document["page_count"])

    def test_rows_without_documents_are_not_overwritten(self):
        data = make_pdf(TABLE_PAGE, table=True)
        with TemporaryDirectory() as directory:
            pdf = Path(directory, "D001.pdf")
            pdf.write_bytes(data)
            database = Path(directory, "solar.sqlite3")
            storage.init(database)
            connection = storage.connect(database)
            try:
                with connection:
                    connection.execute(
                        "INSERT INTO solar_results (area, publication_id, result_id) "
                        "VALUES ('solar', 'D001', 'R001')")
            finally:
                connection.close()
            saved = os.environ.get("VINITI_DATA_DB")
            os.environ["VINITI_DATA_DB"] = str(database)
            try:
                with self.assertRaises(ValueError) as raised:
                    solar_offline.process("solar", "D001", pdf)
            finally:
                if saved is None:
                    os.environ.pop("VINITI_DATA_DB", None)
                else:
                    os.environ["VINITI_DATA_DB"] = saved
            connection = storage.connect(database)
            try:
                kept = connection.execute(
                    "SELECT COUNT(*) FROM solar_results WHERE area = 'solar' "
                    "AND publication_id = 'D001'").fetchone()[0]
            finally:
                connection.close()
        self.assertIn("documents", str(raised.exception))
        self.assertEqual(kept, 1)

    def test_imported_gold_is_not_overwritten(self):
        data = make_pdf(TABLE_PAGE, table=True)
        with TemporaryDirectory() as directory:
            pdf = Path(directory, "D001.pdf")
            pdf.write_bytes(data)
            database = Path(directory, "solar.sqlite3")
            storage.init(database)
            connection = storage.connect(database)
            try:
                with connection:
                    connection.execute(
                        "INSERT INTO documents (area, publication_id, parser_status) "
                        "VALUES ('solar', 'D001', 'imported')")
                    connection.execute(
                        "INSERT INTO solar_results (area, publication_id, result_id) "
                        "VALUES ('solar', 'D001', 'R001')")
            finally:
                connection.close()
            saved = os.environ.get("VINITI_DATA_DB")
            os.environ["VINITI_DATA_DB"] = str(database)
            try:
                with self.assertRaises(ValueError) as raised:
                    solar_offline.process("solar", "D001", pdf)
            finally:
                if saved is None:
                    os.environ.pop("VINITI_DATA_DB", None)
                else:
                    os.environ["VINITI_DATA_DB"] = saved
            connection = storage.connect(database)
            try:
                kept = connection.execute(
                    "SELECT COUNT(*) FROM solar_results WHERE area = 'solar' "
                    "AND publication_id = 'D001'").fetchone()[0]
            finally:
                connection.close()
        self.assertIn("эталон", str(raised.exception))
        self.assertEqual(kept, 1)

    def test_other_areas_are_refused(self):
        with self.assertRaises(ValueError):
            solar_offline.process("rag", "P001", "missing.pdf")


class CliTest(unittest.TestCase):
    def test_missing_directory_is_a_clean_error(self):
        with TemporaryDirectory() as directory:
            stdout, stderr = StringIO(), StringIO()
            with self.assertRaises(SystemExit) as raised, \
                    contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                solar_offline.main(["--pdf-dir", str(Path(directory, "absent")),
                                    "--out", str(Path(directory, "out"))])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("нет каталога", stderr.getvalue())

    def test_broken_first_file_does_not_block_a_valid_namesake(self):
        data = make_pdf([TITLE, "The target device showed a PCE of 16.5% under AM1.5G."])
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a").mkdir()
            (root / "b").mkdir()
            (root / "a" / "D001.pdf").write_bytes(b"not a pdf")
            (root / "b" / "D001.pdf").write_bytes(data)
            out = root / "out"
            stdout = StringIO()
            with contextlib.redirect_stdout(stdout):
                code = solar_offline.main([
                    str(root / "a" / "D001.pdf"), str(root / "b" / "D001.pdf"),
                    "--out", str(out), "--skip-errors",
                ])
            with (out / "publications.csv").open(encoding="utf-8") as stream:
                publication_rows = list(stream)
        self.assertEqual(code, 0)
        self.assertEqual(len(publication_rows), 2)  # header + one valid publication


class HelperTest(unittest.TestCase):
    def test_printed_value_keeps_printed_spelling(self):
        self.assertEqual(solar_offline.printed_value("1,197"), "1197")
        self.assertEqual(solar_offline.printed_value("0,71"), "0,71")
        self.assertEqual(solar_offline.printed_value("23.0%"), "23.0")

    def test_number_text_reads_thousands_and_decimal_comma(self):
        self.assertEqual(solar_offline.number_text("1,197"), "1197")
        self.assertEqual(solar_offline.number_text("0,71"), "0.71")

    def test_foreign_unit_rejected(self):
        self.assertTrue(solar_offline.plausible("FF", 0.71, "fraction"))
        self.assertTrue(solar_offline.plausible("PCE", 16.5, "%"))
        self.assertFalse(solar_offline.plausible("PCE", 0.73, "V"))
        self.assertFalse(solar_offline.plausible("Voc", 1.05, "%"))

    def test_series_prefix_dropped_when_shared(self):
        devices = [{"id": "DV-DIO5", "label": "DIO-5"},
                   {"id": "DV-DIO10", "label": "DIO-10"}]
        solar_offline._simplify_series(devices)
        self.assertEqual([device["id"] for device in devices], ["DV5", "DV10"])

    def test_series_prefix_kept_when_letters_differ(self):
        devices = [{"id": "DV-G1", "label": "G1"},
                   {"id": "DV-I1", "label": "I1"}]
        solar_offline._simplify_series(devices)
        self.assertEqual([device["id"] for device in devices], ["DV-G1", "DV-I1"])

    def test_section_heading_is_not_a_device(self):
        self.assertFalse(solar_offline._device_ok("4. Conclusions"))
        self.assertFalse(solar_offline._device_ok("FB-SC"))
        self.assertTrue(solar_offline._device_ok("0.001 M SrCl2"))

    def test_value_forms_are_read_the_same(self):
        cases = {
            "14.0": ("14.0", False, False),
            "14.0%": ("14.0", True, False),
            "(16.5)": ("16.5", False, True),
            "(16.5%)": ("16.5", True, True),
            "(16.5)%": ("16.5", True, True),
        }
        for text, (literal, percent, parenthesized) in cases.items():
            with self.subTest(text=text):
                line = {"words": [{"text": text, "x0": 0, "x1": 40, "top": 0}]}
                token = solar_offline._value_tokens(line)[0]
                self.assertEqual(token["literal"], literal)
                self.assertEqual(token["percent"], percent)
                self.assertEqual(token["parenthesized"], parenthesized)

    def test_percent_glued_to_a_table_cell_is_read(self):
        tables = extract(TABLE_PAGE, table=True, rows=(
            ("Control", "1000", "20.0", "0.70", "14.0%"),
            ("Target", "1050", "21.0", "0.75", "16.5"),
        ))["tables"]
        pce = [row for row in tables["results"]
               if row["metric_name"] == "PCE" and row["device_id"] == "DV1"]
        self.assertEqual(len(pce), 1)
        self.assertEqual(pce[0]["raw_value"], "14.0")
        self.assertEqual(pce[0]["raw_unit"], "%")
        self.assertEqual(pce[0]["normalized_value"], "14")

    def test_double_percent_is_not_a_value(self):
        line = {"words": [{"text": "16.5%%", "x0": 0, "x1": 40, "top": 0}]}
        self.assertEqual(solar_offline._value_tokens(line), [])

    def test_percent_as_a_separate_word(self):
        line = {"words": [
            {"text": "(16.5)", "x0": 0, "x1": 40, "top": 0},
            {"text": "%", "x0": 45, "x1": 55, "top": 0},
        ]}
        token = solar_offline._value_tokens(line)[0]
        self.assertEqual(token["text"], "(16.5)%")
        self.assertEqual(token["literal"], "16.5")
        self.assertTrue(token["percent"])
        self.assertTrue(token["parenthesized"])

    def test_measurement_for_keeps_the_direction(self):
        measurements = [
            {"measurement_id": "MS1", "measurement_type": "J-V", "scan_direction": "reverse"},
            {"measurement_id": "MS2", "measurement_type": "J-V", "scan_direction": "forward"},
        ]
        self.assertEqual(solar_offline._measurement_for(
            "PCE", measurements, {"scan": "reverse"}), "MS1")
        self.assertEqual(solar_offline._measurement_for(
            "PCE", measurements, {"scan": "forward"}), "MS2")
        self.assertEqual(solar_offline._measurement_for(
            "PCE", measurements, {"scan": ""}), "MS1")
        self.assertEqual(solar_offline._measurement_for(
            "PCE", measurements[:1], {"scan": "forward"}), "")

    def test_scan_named_on_a_row_gets_its_own_measurement(self):
        lines = [line for line in TABLE_PAGE
                 if "reverse scan" not in line]
        lines.append("The values were recorded under illumination.")
        tables = extract(lines, table=True, rows=(
            ("Control", "1000", "20.0", "0.70", "14.0"),
            ("Target", "1050", "21.0", "0.75", "16.5"),
            ("FB-SC", "1050", "21.0", "0.75", "16.5"),
        ))["tables"]
        self.assertEqual([row["scan_direction"] for row in tables["measurements"]],
                         ["reverse"])

    def test_scan_direction(self):
        self.assertEqual(solar_offline._scan_direction("FB-SC"), "reverse")
        self.assertEqual(solar_offline._scan_direction("SC-FB"), "forward")
        self.assertEqual(solar_offline._scan_direction("Target"), "")

    def test_project_keeps_template_columns(self):
        row = {"publication_id": "D001", "result_id": "R001", "metric_name": "PCE",
               "_source_ids": [1, 2]}
        table = solar_offline.project({"results": [row]})["results"][0]
        self.assertEqual(set(table), set(TABLES["results"]) | {"source_ids"})
        self.assertEqual(table["source_ids"], [1, 2])


if __name__ == "__main__":
    unittest.main()
