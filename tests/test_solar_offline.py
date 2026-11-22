"""Tests for the deterministic solar extractor (``solar_offline``).

The extractor reads a PDF with pdfplumber, finds table rows and prose results,
and hands the candidate tables to :mod:`solar_facts` for validation. These
tests build synthetic PDFs with reportlab, so they need no corpus on disk.
"""

import contextlib
from io import BytesIO, StringIO
import inspect
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
        # An unrelated comparison in the same sentence must not veto a real gap.
        value, _, _ = solar_offline._bandgap(
            {"1": "The deposition increased the absorption of the CZTS film to "
                  "more than 104 cm-1 with a band gap between 1.62 eV and 1.51 eV."},
            "CZTS")
        self.assertEqual(value, "1.62")
        value, _, _ = solar_offline._bandgap(
            {"1": "The bulk bandgap of CdS is just above 2.42 eV for this stack."},
            "Cu(In,Ga)Se2")
        self.assertEqual(value, "")
        value, _, _ = solar_offline._bandgap(
            {"1": "The transport layer has a wide bandgap, higher than 3 eV."})
        self.assertEqual(value, "")
        value, _, _ = solar_offline._bandgap(
            {"1": "The absorber has a wide bandgap of 1.8 eV."}, "CdTe")
        self.assertEqual(value, "1.8")
        value, _, _ = solar_offline._bandgap(
            {"1": "CuCrOx is a wide bandgap (3.2 eV) material."}, "CdTe")
        self.assertEqual(value, "")
        value, _, _ = solar_offline._bandgap(
            {"1": "The band-gap of 1.8 eV matches the absorber."}, "CdTe")
        self.assertEqual(value, "1.8")
        value, _, _ = solar_offline._bandgap(
            {"1": "The CuCrOx wide-bandgap buffer has 3.2 eV."}, "CdTe")
        self.assertEqual(value, "")

    def test_bandgap_is_attached_to_the_material(self):
        tables = extract(TABLE_PAGE + ["The absorber has a band gap of 1.65 eV."],
                         table=True)["tables"]
        self.assertEqual(len(tables["materials"]), 2)
        for row in tables["materials"]:
            self.assertEqual(row["bandgap_value"], "1.65")
            self.assertEqual(row["bandgap_unit"], "eV")

    def test_bandgap_range_reaches_the_material_row(self):
        # The quote of a range covers both bounds, so the value still has to be
        # found in it and the eV unit still has to be attached to that source.
        tables = extract(
            TABLE_PAGE + ["The absorber has a band gap between 1.62 eV and 1.51 eV."],
            table=True)["tables"]
        self.assertEqual(len(tables["materials"]), 2)
        for row in tables["materials"]:
            self.assertEqual(row["bandgap_value"], "1.62")
            self.assertEqual(row["bandgap_unit"], "eV")
            self.assertEqual(row["bandgap_method"], "cited")

    def test_bandgap_range_takes_the_first_bound(self):
        # The template carries one absorber value, so a stated range contributes
        # its first bound - both with the unit repeated and with it written once.
        for text in (
            "The CZTS absorber has a band gap between 1.62 eV and 1.51 eV.",
            "The CZTS absorber has a band gap between 1.62 and 1.51 eV.",
            "The CZTS absorber has a band gap from 1.62 to 1.51 eV.",
        ):
            with self.subTest(text=text):
                value, page, quote = solar_offline._bandgap({"1": text}, "CZTS")
                self.assertEqual(value, "1.62")
                self.assertEqual(page, "1")
                self.assertIn("1.62", quote)

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

    def test_delta_list_after_improves_is_not_a_measurement(self):
        # D002: these are gains over the single-layer device, not its values.
        records = solar_offline.prose_records(
            "The NiO/GO as a double hole transport layer improves Voc of 0.127 V, "
            "Jsc of 3.1 mAcm-2, fill factor of 25.4%, and PCE of 7.5%.",
            [],
        )
        self.assertEqual(records, [])

    def test_delta_list_wrapped_to_the_next_line_is_still_a_delta(self):
        # D002 prints the list over two lines; the second line starts with the
        # fill-factor value and has no delta verb of its own.
        page = ("improves Voc of 0.127 V, Jsc of 3.1 mAcm-2, fill factor of\n"
                "25.4%, and PCE of 7.5%. Note those equivalent PCE\n")
        self.assertEqual(solar_offline.prose_records(page, []), [])

    def test_delta_after_increase_is_not_a_measurement(self):
        for phrase in (
            "The PCE increased by 1.2% after the treatment.",
            "The increase in PCE of 1.2% is due to the transport layer.",
            "An increase of 1.2% in PCE was observed.",
            "The device showed a PCE gain of 0.8%.",
        ):
            with self.subTest(phrase=phrase):
                self.assertEqual(solar_offline.prose_records(phrase, []), [])

    def test_metric_delta_verb_before_by_or_of_is_not_a_measurement(self):
        # "PCE increased by 1.2 %" is a change, not the value of the metric. A
        # decrease is as much a change as an increase, and the adverb between
        # the link and the number does not turn it into a measurement.
        for phrase in (
            "The PCE increased by 1.2% after the treatment.",
            "The PCE reduced by 1.2% after the treatment.",
            "The PCE gained by 1.2% after the treatment.",
            "The PCE gained of 1.2% after the treatment.",
            "The PCE decreased by 1.2% after the treatment.",
            "The PCE dropped by 1.2% after the treatment.",
            "The PCE increased by roughly 1.2% after the treatment.",
            "The Voc decreased by 0.05 V after the treatment.",
        ):
            with self.subTest(phrase=phrase):
                self.assertEqual(solar_offline.prose_records(phrase, []), [])

    def test_delta_verb_with_to_keeps_the_final_value(self):
        # "improved to 16.5 %" names the value reached, not the size of the gain.
        records = solar_offline.prose_records(
            "The NiO/GO device improved to a PCE of 16.5%.", [])
        self.assertEqual([(row["metric_name"], row["raw_value"]) for row in records],
                         [("PCE", "16.5")])

    def test_threshold_after_metric_is_not_a_pce(self):
        for phrase in (
            "A mesoscopic solar cell with efficiency exceeding 9% was reported.",
            "The champion device had a PCE above 20%.",
            "Reported PCE over 22% in the literature.",
            "Devices with PCE more than 18% are rare.",
        ):
            with self.subTest(phrase=phrase):
                records = solar_offline.prose_records(phrase, [])
                self.assertFalse([row for row in records if row["metric_name"] == "PCE"],
                                 records)

    def test_adjective_improved_does_not_hide_a_real_value(self):
        records = solar_offline.prose_records(
            "The improved NiO/GO device showed a PCE of 16.5%.", [])
        self.assertEqual([(row["metric_name"], row["raw_value"]) for row in records],
                         [("PCE", "16.5")])

    def test_delta_does_not_leak_into_the_next_sentence_on_one_line(self):
        # A printed line may hold two sentences; the delta verb of the first one
        # must not swallow the measurement of the second.
        records = solar_offline.prose_records(
            "The bilayer improves Voc of 0.12 V. The PCE of 15.3% is the average.", [])
        self.assertEqual([(row["metric_name"], row["raw_value"]) for row in records],
                         [("PCE", "15.3")])

    def test_delta_does_not_wrap_past_a_finished_sentence(self):
        # The same rule has to hold across printed lines: a line that ends a
        # sentence closes the delta, a comma or an open list keeps it going.
        page = ("The bilayer improves Voc of 0.12 V.\n"
                "The PCE of 15.3% is the average.\n")
        records = solar_offline.prose_records(page, [])
        self.assertEqual([(row["metric_name"], row["raw_value"]) for row in records],
                         [("PCE", "15.3")])

    def test_sentence_end_rule_is_defined_once(self):
        # Two competing SENTENCE_END definitions used to disagree about a quote
        # after the full stop, and the later one silently won at runtime.
        source = inspect.getsource(solar_offline)
        self.assertEqual(source.count("SENTENCE_END = re.compile"), 1)
        self.assertIsNotNone(solar_offline.SENTENCE_END.search('a PCE of 16.5 %."'))
        self.assertIsNotNone(solar_offline.SENTENCE_END.search("the metrics were:"))
        self.assertIsNone(solar_offline.SENTENCE_END.search("Voc of 0.12 V,"))
        self.assertIsNone(solar_offline.SENTENCE_END.search("fill factor of"))

    def test_explicit_longer_label_wins_over_its_prefix(self):
        devices = [{"id": "DV1", "label": "SC"}, {"id": "DV2", "label": "SC-1"}]
        self.assertEqual(
            solar_offline._nearby_label("The SC-1 cell reached a PCE", 26, devices),
            "SC-1")

    def test_nearest_full_label_wins_over_a_shorter_prefix(self):
        # D002: "NiO/GO and NiO are 15.3 % and 7.8 %". The names come in the
        # same order as the values, so the second value stays with NiO even
        # though the compound label also starts with the token NiO.
        devices = [{"id": "DV1", "label": "NiO"}, {"id": "DV2", "label": "NiO/GO"}]
        records = solar_offline.prose_records(
            "The efficiency of NiO/GO and NiO are 15.3% and 7.8%.", devices)
        self.assertEqual([(row["raw_value"], row["device_label"]) for row in records],
                         [("15.3", "NiO/GO"), ("7.8", "NiO")])

    def test_label_order_pairing_needs_matching_counts(self):
        # One name and two values is not a list: the nearest label is kept and
        # no device is invented for the second value.
        devices = [{"id": "DV1", "label": "NiO"}]
        records = solar_offline.prose_records(
            "The NiO cell gave a PCE of 15.3% and 7.8% under illumination.", devices)
        self.assertEqual([row["device_label"] for row in records], ["NiO", "NiO"])

    def test_prose_value_without_a_named_device_keeps_an_empty_id(self):
        records = solar_offline.prose_records(
            "The reported PCE of 21.3% is taken from the literature.",
            [{"id": "DV1", "label": "NiO"}])
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["device_label"], "")


class ProseTableDedupeTest(unittest.TestCase):
    """The prose/table duplicate check must not merge distinct results."""

    ROWS = (("Control", "1000", "20.0", "0.70", "16.5"),
            ("Target", "1050", "21.0", "0.75", "17.0"))
    # The prose sentence is added per test, so the shared page must not carry a
    # second one that would be read as a duplicate of the target.
    PAGE = [line for line in TABLE_PAGE if "target device showed" not in line]

    def _pce(self, lines):
        tables = extract(self.PAGE + lines, table=True, rows=self.ROWS)["tables"]
        return [(row["device_id"], row["raw_value"])
                for row in tables["results"] if row["metric_name"] == "PCE"]

    def test_same_value_on_two_devices_is_kept(self):
        # 16.5 % printed in the table for the control and in a sentence for Cs5
        # are two results of two devices; dropping the prose row loses data.
        found = self._pce([
            "The Cs5 device showed a PCE of 16.5% under AM1.5G illumination.",
            "The Cs5 device was annealed at one hundred degrees.",
        ])
        self.assertIn(("DV1", "16.5"), found)
        self.assertIn(("DV2", "17.0"), found)
        self.assertIn(("DV-Cs5", "16.5"), found)

    def test_prose_repeat_of_the_same_table_row_is_dropped(self):
        # The same metric, value and device said twice is one result.
        found = self._pce([
            "The Control device showed a PCE of 16.5% under AM1.5G illumination.",
            "The Control device was annealed at one hundred degrees.",
        ])
        self.assertEqual(found, [("DV1", "16.5"), ("DV2", "17.0")])


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

    def test_any_table_without_documents_is_not_overwritten(self):
        with TemporaryDirectory() as directory:
            pdf = Path(directory, "D001.pdf")
            pdf.write_bytes(make_pdf(TABLE_PAGE, table=True))
            database = Path(directory, "solar.sqlite3")
            storage.init(database)
            connection = storage.connect(database)
            try:
                with connection:
                    connection.execute(
                        "INSERT INTO solar_materials (area, publication_id, material_id) "
                        "VALUES ('solar', 'D001', 'M1')")
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
                    "SELECT COUNT(*) FROM solar_materials WHERE area = 'solar' "
                    "AND publication_id = 'D001'").fetchone()[0]
            finally:
                connection.close()
        self.assertIn("documents", str(raised.exception))
        self.assertEqual(kept, 1)

    def test_imported_is_refused_before_parsing(self):
        with TemporaryDirectory() as directory:
            pdf = Path(directory, "D001.pdf")
            pdf.write_bytes(b"not a pdf at all")
            database = Path(directory, "solar.sqlite3")
            storage.init(database)
            connection = storage.connect(database)
            try:
                with connection:
                    connection.execute(
                        "INSERT INTO documents (area, publication_id, parser_status) "
                        "VALUES ('solar', 'D001', 'imported')")
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
        self.assertIn("эталон", str(raised.exception))
        self.assertNotIn("не является PDF", str(raised.exception))

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
                if text == "(16.5%)":
                    self.assertEqual(token["text"], "(16.5%)")

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

    def test_exponent_keeps_the_percent_of_the_whole_value(self):
        cases = [
            (["6.7", "×", "10", "-3%"], "6.7e-3", "6.7e-3%"),
            (["6.7", "×", "10", "-3", "%"], "6.7e-3", "6.7e-3%"),
        ]
        for words, literal, text in cases:
            with self.subTest(words=words):
                line = {"words": [
                    {"text": item, "x0": position * 30, "x1": position * 30 + 20, "top": 0}
                    for position, item in enumerate(words)
                ]}
                token = solar_offline._value_tokens(line)[0]
                self.assertEqual(token["literal"], literal)
                self.assertEqual(token["text"], text)
                self.assertTrue(token["percent"])

    def test_double_percent_in_two_words_is_one_unit(self):
        line = {"words": [
            {"text": "14.0%", "x0": 0, "x1": 40, "top": 0},
            {"text": "%", "x0": 45, "x1": 55, "top": 0},
        ]}
        token = solar_offline._value_tokens(line)[0]
        self.assertEqual(token["text"], "14.0%")
        self.assertEqual(token["literal"], "14.0")

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
