"""Детерминированные тесты модуля 2: словарь, числа, сетка, цитаты."""

from __future__ import annotations

import unittest

from rag.extract import (
    clean_variant,
    metric_value_text,
    quote_from_page,
    raw_unit_text,
    resolve_generator,
)
from rag.grid import _row_label, find_tables
from rag.pdf import Line, Page, Word, _page_lines
from rag.vocab import (
    canonical_dataset,
    looks_like_generator,
    normalize_generator,
    number_value,
    parse_metric,
)


class NumberTests(unittest.TestCase):
    def test_plain_and_leading_dot(self) -> None:
        self.assertAlmostEqual(number_value("0.204"), 0.204)
        self.assertAlmostEqual(number_value(".204"), 0.204)

    def test_percent_and_units(self) -> None:
        self.assertAlmostEqual(number_value("20.4%"), 20.4)
        self.assertAlmostEqual(number_value("12ms"), 12.0)
        self.assertAlmostEqual(number_value("3.5s"), 3.5)

    def test_thousands_separator(self) -> None:
        self.assertAlmostEqual(number_value("1,234"), 1234.0)
        self.assertAlmostEqual(number_value("1,234.5"), 1234.5)
        self.assertAlmostEqual(number_value("0,204"), 0.204)

    def test_not_a_number(self) -> None:
        self.assertIsNone(number_value("—"))
        self.assertIsNone(number_value("n/a"))

    def test_metric_value_text(self) -> None:
        self.assertEqual(metric_value_text(".204"), "0.204")
        self.assertEqual(metric_value_text("20.4%"), "20.4")
        self.assertEqual(metric_value_text("1,234"), "1234")
        self.assertEqual(raw_unit_text("20.4%"), "%")
        self.assertEqual(raw_unit_text("12ms"), "ms")
        self.assertEqual(raw_unit_text("0.204"), "")


class MetricTests(unittest.TestCase):
    def test_basic_metrics(self) -> None:
        self.assertEqual(parse_metric("EM").name, "EM")
        self.assertEqual(parse_metric("F1").name, "F1")
        self.assertEqual(parse_metric("Accuracy (%)").name, "Accuracy")
        self.assertEqual(parse_metric("Accuracy (%)").unit_hint, "%")

    def test_at_k(self) -> None:
        metric = parse_metric("Recall@10")
        self.assertIsNotNone(metric)
        assert metric is not None
        self.assertEqual(metric.name, "Recall")
        self.assertEqual(metric.k, "10")

    def test_rouge_and_bleu_underscore(self) -> None:
        self.assertEqual(parse_metric("ROUGE_1").name, "ROUGE-1")
        self.assertEqual(parse_metric("ROUGE_2").name, "ROUGE-2")
        self.assertEqual(parse_metric("ROUGE_L").name, "ROUGE-L")
        self.assertEqual(parse_metric("BLEU_1").name, "BLEU-1")
        self.assertEqual(parse_metric("BLEU_4").name, "BLEU-4")
        self.assertEqual(parse_metric("BLEU-4").name, "BLEU-4")

    def test_unknown_metric(self) -> None:
        self.assertIsNone(parse_metric("Some custom thing"))


class DatasetTests(unittest.TestCase):
    def test_aliases(self) -> None:
        self.assertEqual(canonical_dataset("Natural Questions"), "Natural Questions")
        self.assertEqual(canonical_dataset("NQ"), "Natural Questions")
        self.assertEqual(canonical_dataset("HotpotQA"), "HotpotQA")

    def test_case_sensitive_collision(self) -> None:
        # QuALITY (набор) не должен схлопываться с обычным словом Quality.
        self.assertNotEqual(canonical_dataset("Quality"), canonical_dataset("QuaLITY"))

    def test_unknown(self) -> None:
        self.assertIsNone(canonical_dataset("Some Unseen Benchmark"))


class GeneratorTests(unittest.TestCase):
    def test_looks_like_generator(self) -> None:
        self.assertTrue(looks_like_generator("Llama3.3-70B"))
        self.assertTrue(looks_like_generator("GPT-4"))
        self.assertFalse(looks_like_generator("Retrieval"))

    def test_normalize_strips_size_parentheses(self) -> None:
        self.assertEqual(normalize_generator("FLAN-T5-XL (3B)"), "FLAN-T5-XL 3B")

    def test_resolve_generator_prefers_long_identifier(self) -> None:
        self.assertEqual(
            resolve_generator("GPT-3.5", "gpt-3.5-turbo-instruct"),
            "gpt-3.5-turbo-instruct",
        )
        self.assertEqual(
            resolve_generator("FLAN-T5-XL 3B", "gpt-3.5-turbo-instruct"),
            "FLAN-T5-XL 3B",
        )
        self.assertEqual(resolve_generator("", "gpt-4"), "gpt-4")


class VariantTests(unittest.TestCase):
    def test_clean_variant(self) -> None:
        self.assertEqual(clean_variant("Simple Self-RAG"), "Self-RAG")
        self.assertEqual(clean_variant("Adaptive Self-RAG"), "Self-RAG")
        self.assertEqual(clean_variant("Ours (Ours)"), "Ours")
        self.assertEqual(clean_variant("Single-step Approach"), "Single-step Approach")


class QuoteTests(unittest.TestCase):
    def test_exact_substring(self) -> None:
        page = "Results: RA-CoT 0.204 and Self-RAG 0.312."
        quote = quote_from_page(page, "RA-CoT", "0.204", "fallback")
        self.assertIn(quote, page)
        self.assertIn("0.204", quote)

    def test_hyphenated_line_break(self) -> None:
        page = "Prompt- B variant gives 12.5% gain."
        quote = quote_from_page(page, "Prompt- B", "12.5%", "fallback")
        self.assertIn("12.5%", quote)

    def test_fallback_when_label_missing(self) -> None:
        page = "Nothing relevant here."
        self.assertEqual(quote_from_page(page, "RA-CoT", "0.204", "fallback"), "fallback")


def _line(text: str, y: float, words: list[tuple[str, float]]) -> Line:
    items = tuple(
        Word(text=word, x0=x, y0=y, x1=x + 0.55 * len(word) * 6, y1=y + 10)
        for word, x in words
    )
    return Line(
        text=text,
        x0=min(item.x0 for item in items),
        y0=y,
        x1=max(item.x1 for item in items),
        y1=y + 10,
        words=items,
    )


class GridTests(unittest.TestCase):
    def test_simple_table(self) -> None:
        caption = _line("Table 1: Results", 10, [("Table", 50), ("1:", 90), ("Results", 110)])
        header = _line("Methods EM F1", 30, [("Methods", 50), ("EM", 150), ("F1", 220)])
        row1 = _line(
            "RA-CoT 0.204 0.330",
            50,
            [("RA-CoT", 50), ("0.204", 150), ("0.330", 220)],
        )
        row2 = _line(
            "Self-RAG 0.312 0.429",
            65,
            [("Self-RAG", 50), ("0.312", 150), ("0.429", 220)],
        )
        page = Page(
            number=1,
            text="Table 1: Results\nMethods EM F1\nRA-CoT 0.204 0.330\nSelf-RAG 0.312 0.429\n",
            lines=(caption, header, row1, row2),
        )
        tables = find_tables(page)
        self.assertEqual(len(tables), 1)
        table = tables[0]
        self.assertEqual(table.caption, "Table 1")
        self.assertEqual([column.metric.name for column in table.columns], ["EM", "F1"])
        self.assertEqual([row.label for row in table.rows], ["RA-CoT", "Self-RAG"])
        self.assertAlmostEqual(table.rows[0].values[0][1], 0.204)

    def test_row_label_strips_marks(self) -> None:
        numbers = [Word(text="0.5", x0=150, y0=0, x1=170, y1=10)]
        line = Line(
            text="✓ RA-CoT 0.5",
            x0=50,
            y0=0,
            x1=170,
            y1=10,
            words=(
                Word(text="✓", x0=50, y0=0, x1=60, y1=10),
                Word(text="RA-CoT", x0=65, y0=0, x1=110, y1=10),
                numbers[0],
            ),
        )
        self.assertEqual(_row_label(line, numbers), "RA-CoT")


class PdfColumnTests(unittest.TestCase):
    @staticmethod
    def page(rows):
        class FakePage:
            def get_text(self, mode):
                return [(x, y, x + len(text) * 4, y + 10, text, 0, index, word)
                        for index, (y, tokens) in enumerate(rows)
                        for word, (x, text) in enumerate(tokens)]
        return FakePage()

    def test_two_column_prose_is_not_joined(self):
        rows = [(20 + i * 15, [(40, "Left"), (65, "column"), (100, "text"),
                              (320, "Right"), (350, "column"), (385, "text")])
                for i in range(3)]
        lines = _page_lines(self.page(rows))
        self.assertEqual(len(lines), 6)
        self.assertEqual([line.column for line in lines], [1, 2, 1, 2, 1, 2])
        self.assertTrue(all(line.text in {"Left column text", "Right column text"} for line in lines))
        self.assertTrue(all(line.x1 < 200 or line.x0 > 300 for line in lines))

    def test_established_gutter_splits_short_prose_but_keeps_numeric_rows(self):
        rows = [(20 + i * 15, [(40, "Left"), (65, "column"), (100, "text"),
                              (320, "Right"), (350, "column"), (385, "text")])
                for i in range(3)]
        rows.extend([(70, [(40, "Short"), (320, "Separate")]),
                     (85, [(40, "Method"), (100, "0.204"), (320, "0.330")])])
        lines = _page_lines(self.page(rows))
        self.assertEqual([line.text for line in lines[-3:]], ["Short", "Separate", "Method 0.204 0.330"])

    def test_table_gaps_do_not_establish_prose_columns(self):
        rows = [(20 + i * 15, [(40, "Method"), (100, "0.204"), (320, "0.330")])
                for i in range(4)]
        lines = _page_lines(self.page(rows))
        self.assertEqual(len(lines), 4)
        self.assertTrue(all(line.text == "Method 0.204 0.330" for line in lines))
        self.assertTrue(all(line.column == 0 for line in lines))


if __name__ == "__main__":
    unittest.main()
