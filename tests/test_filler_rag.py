"""Tests for the rag baseline filler (filler_rag.RagTableFiller).

Run:  python -X utf8 -m unittest test_filler_rag -v
"""
import pathlib
import unittest
from io import BytesIO
from tempfile import TemporaryDirectory

import db as storage
import extract
from filler_rag import RagTableFiller

# A small vocab, independent of the repository file: the tests must not depend
# on dev_gold having been processed. NDCG@10 and Recall@100 are not in the real
# vocab (it has NDCG@3/5/6, Recall@3/6), which is fine: recognition is about
# the normalized key, and the injected vocab owns its keys.
TEST_VOCAB = {
    "NDCG@10": {"rows": 1, "metric_unit": "score",
                "metric_group": "retrieval ranking",
                "metric_direction": "higher_is_better"},
    "Recall@100": {"rows": 1, "metric_unit": "fraction",
                   "metric_group": "retrieval quality",
                   "metric_direction": "higher_is_better"},
    "MRR": {"rows": 1, "metric_unit": "score",
            "metric_group": "retrieval quality",
            "metric_direction": "higher_is_better"},
}


def window(items):
    return {"index": 1, "page": 5, "sources": [item["id"] for item in items],
            "text": "", "items": items}


def table_item(identifier, page, table, row, quote):
    return {"id": identifier, "page": page, "kind": "table", "quote": quote,
            "table": table, "row": row}


def table_pdf():
    """One page with a real detected table: header + two data rows."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.platypus import (SimpleDocTemplate, Spacer, Table,
                                    TableStyle)

    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4)
    table = Table([["Model", "NDCG@10", "MRR"],
                   ["Alpha", "0.42", "0.88"],
                   ["Beta", "0.39", "0.91"]],
                  colWidths=[120, 90, 90], rowHeights=[24, 24, 24])
    table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.8, colors.black),
        ("FONT", (0, 0), (-1, -1), "Helvetica", 10),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
    ]))
    doc.build([Spacer(1, 40), table])
    return buffer.getvalue()


class RagTableFillerTest(unittest.TestCase):
    def test_header_and_rows_are_paired(self):
        items = [
            table_item(1, 5, 1, 1, "Model | NDCG@10 | Recall@100 | MRR"),
            table_item(2, 5, 1, 2, "ConfigA | 0.42 | 0.61 | 0.88"),
            table_item(3, 5, 1, 3, "ConfigB | 0.39 | 0.57 | 0.91"),
        ]
        rows = RagTableFiller(TEST_VOCAB).fill(window(items), {})
        self.assertEqual(len(rows), 6)
        first = rows[0]
        self.assertEqual(first["metric_name"], "NDCG@10")
        self.assertEqual(first["metric_value"], "0.42")  # as printed, not float
        self.assertEqual(first["metric_unit"], "score")
        self.assertEqual(first["metric_group"], "retrieval ranking")
        self.assertEqual(first["metric_direction"], "higher_is_better")
        self.assertEqual(first["config_name"], "ConfigA")
        self.assertEqual(first["source_page"], 5)
        self.assertEqual(first["source_fragment"], items[1]["quote"])
        self.assertEqual(first["source_table"], "таблица 1, строка 2")
        self.assertEqual(first["extraction_status"], "extracted")
        self.assertEqual(first["note"], "RagTableFiller (baseline)")
        self.assertNotIn("publication_id", first)
        self.assertNotIn("result_id", first)
        self.assertEqual([r["metric_name"] for r in rows],
                         ["NDCG@10", "Recall@100", "MRR"] * 2)
        self.assertEqual([r["metric_value"] for r in rows],
                         ["0.42", "0.61", "0.88", "0.39", "0.57", "0.91"])

    def test_unknown_metric_column_is_skipped(self):
        items = [
            table_item(1, 5, 1, 1, "Model | NDCG@10 | Blargh Metric | MRR"),
            table_item(2, 5, 1, 2, "ConfigA | 0.42 | 12.5 | 0.88"),
        ]
        rows = RagTableFiller(TEST_VOCAB).fill(window(items), {})
        self.assertEqual([(r["metric_name"], r["metric_value"]) for r in rows],
                         [("NDCG@10", "0.42"), ("MRR", "0.88")])

    def test_header_case_and_spacing_do_not_matter(self):
        items = [
            table_item(1, 5, 1, 1, "Model | nDCG@10 | Recall @ 100 | mrr"),
            table_item(2, 5, 1, 2, "Alpha | .42 | .61 | 0.88"),
        ]
        rows = RagTableFiller(TEST_VOCAB).fill(window(items), {})
        self.assertEqual([r["metric_name"] for r in rows],
                         ["NDCG@10", "Recall@100", "MRR"])
        # Values stay exactly as printed: ".42" keeps its leading dot.
        self.assertEqual([r["metric_value"] for r in rows], [".42", ".61", "0.88"])

    def test_row_without_config_name_is_skipped(self):
        items = [
            table_item(1, 5, 1, 1, "Model | NDCG@10 | MRR"),
            table_item(2, 5, 1, 2, " | 0.42 | 0.88"),
            table_item(3, 5, 1, 3, "42 | 0.39 | 0.91"),  # numeric label, not a name
        ]
        rows = RagTableFiller(TEST_VOCAB).fill(window(items), {})
        self.assertEqual(len(rows), 0)

    def test_empty_or_non_numeric_value_cells_are_skipped(self):
        items = [
            table_item(1, 5, 1, 1, "Model | NDCG@10 | MRR"),
            table_item(2, 5, 1, 2, "Alpha |  | 0.88"),
            table_item(3, 5, 1, 3, "Beta | n/a | 0.91"),
        ]
        rows = RagTableFiller(TEST_VOCAB).fill(window(items), {})
        self.assertEqual([(r["config_name"], r["metric_name"]) for r in rows],
                         [("Alpha", "MRR"), ("Beta", "MRR")])

    def test_headers_are_per_table_not_global(self):
        items = [
            table_item(1, 5, 1, 1, "Model | NDCG@10 | MRR"),
            table_item(2, 5, 1, 2, "Alpha | 0.42 | 0.88"),
            table_item(3, 5, 2, 1, "0.99 | 0.98 | 0.97"),  # no header: skipped
            table_item(4, 5, 2, 2, "Beta | 0.39 | 0.91"),
        ]
        rows = RagTableFiller(TEST_VOCAB).fill(window(items), {})
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["config_name"] == "Alpha" for row in rows))

    def test_window_without_tables_returns_empty_list(self):
        filler = RagTableFiller(TEST_VOCAB)
        self.assertEqual(filler.fill({"items": [], "page": 1}, {}), [])
        self.assertEqual(filler.fill({"items": [
            {"id": 1, "page": 5, "kind": "text", "quote": "EM 0.42"}], "page": 5}, {}), [])
        # A broken window must not crash: not a dict, no items key.
        self.assertEqual(filler.fill({}, {}), [])

    def test_crooked_window_does_not_raise(self):
        filler = RagTableFiller(TEST_VOCAB)
        self.assertEqual(filler.fill(None, {}), [])
        self.assertEqual(filler.fill({"items": [{"kind": "table"}]}, {}), [])

    def test_known_metric_in_prose_is_extracted_with_local_evidence(self):
        item = {"id": 1, "page": 5, "kind": "text",
                "quote": "ConfigA on dataset D1 achieved MRR of 0.88."}
        rows = RagTableFiller(TEST_VOCAB).fill(window([item]), {})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["metric_name"], "MRR")
        self.assertEqual(rows[0]["metric_value"], "0.88")
        self.assertEqual(rows[0]["source_fragment"], item["quote"])

    def test_prose_does_not_borrow_value_from_another_source(self):
        items = [
            {"id": 1, "page": 5, "kind": "text", "quote": "MRR was reported."},
            {"id": 2, "page": 5, "kind": "text", "quote": "The value was 0.88."},
        ]
        self.assertEqual(RagTableFiller(TEST_VOCAB).fill(window(items), {}), [])


class RagTableFillerRunTest(unittest.TestCase):
    def test_run_on_synthetic_pdf_never_raises_and_writes_the_envelope(self):
        filler = RagTableFiller(TEST_VOCAB)
        with TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "rag.sqlite3"
            summary = extract.run("rag", "R100", table_pdf(), filler, db_path=path)
            # The gate decides; the pipeline must not crash either way.
            self.assertIn(summary["verified"] + summary["rejected"],
                          {summary["candidates"]})
            self.assertGreaterEqual(summary["candidates"], 0)
            connection = storage.connect(path)
            try:
                documents = connection.execute(
                    "SELECT COUNT(*) FROM documents").fetchone()[0]
                results = connection.execute(
                    "SELECT COUNT(*) FROM rag_results").fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(documents, 1)  # the envelope always lands
            if summary["candidates"]:
                # Every surviving row is verified, so it is also written.
                self.assertEqual(results, summary["written"])
                self.assertEqual(summary["verified"], summary["written"])


if __name__ == "__main__":
    unittest.main()
