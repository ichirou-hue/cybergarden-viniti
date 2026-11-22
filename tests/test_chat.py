"""Chat context, citation validation and per-user history."""

from tempfile import TemporaryDirectory
from unittest import TestCase

from viniti import chat


def envelope(*, pages=3, sources=None, title="Solar cell study", publication_id="D001"):
    return {
        "pages": pages,
        "publication": {
            "publication_id": publication_id,
            "title": title,
            "authors": ["A. Author"],
            "year": "2021",
            "DOI": "10.1000/test",
        },
        "text_sources": sources if sources is not None else [
            {"source_id": 1, "page": 1, "kind": "text",
             "fragment": "The PCE reached 21.5 % under standard illumination."},
            {"source_id": 2, "page": 2, "kind": "table",
             "fragment": "Device DV1 showed a fill factor of 0.81 in the champion cell."},
        ],
        "results": [],
        "issues": [],
        "warnings": [],
    }


class ContextTest(TestCase):
    def test_context_numbers_only_real_sources(self):
        documents = [{"document_id": "rag:d001", "envelope": envelope()}]
        sources, text, truncated = chat.build_context(documents)
        self.assertEqual([source["ref"] for source in sources], ["S1", "S2"])
        self.assertIn("[S1] стр. 2", text)
        self.assertIn("[S2] стр. 1", text)
        self.assertFalse(truncated)

    def test_context_skips_tiny_fragments_and_unknown_pages(self):
        documents = [{"document_id": "rag:d001", "envelope": envelope(sources=[
            {"source_id": 1, "page": 0, "kind": "text", "fragment": "x" * 100},
            {"source_id": 2, "page": 1, "kind": "text", "fragment": "short"},
            {"source_id": 3, "page": 1, "kind": "text",
             "fragment": "A long enough sentence that can be cited."},
        ])}]
        sources, _, _ = chat.build_context(documents)
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["source_id"], 3)

    def test_selection_history_key_is_order_independent(self):
        first = chat.context_key("selection", ["rag:b", "rag:a"])
        second = chat.context_key("selection", ["rag:a", "rag:b"])
        self.assertEqual(first, second)
        self.assertNotEqual(first, chat.context_key("selection", ["rag:a"]))


class AskTest(TestCase):
    def test_malformed_model_reference_does_not_crash(self):
        result = chat.ask("Какой результат?", "document",
                          [{"document_id": "rag:d001", "envelope": envelope()}],
                          complete_fn=lambda *a, **k: {
                              "answer": "Ответ", "citations": [{"ref": [], "quote": "x"}],
                              "not_found": False})
        self.assertTrue(result["unverified"])
        self.assertEqual(result["citations"], [])

    def test_recognized_source_is_available_with_explicit_provenance(self):
        data = envelope(sources=[])
        data["recognized_sources"] = [{"source_id": 2, "page": 1,
            "fragment": "The measured signal-to-noise ratio was 24."}]
        result = chat.ask("Какой результат?", "document",
                          [{"document_id": "parse:0123456789abcdef", "envelope": data}],
                          complete_fn=lambda *a, **k: {"answer": "24", "not_found": False,
                              "citations": [{"ref": "S1", "quote": "ratio was 24"}]})
        citation = result["citations"][0]
        self.assertEqual(citation["provenance"], "recognized")
        self.assertEqual(citation["pdf_url"], "/api/parse/0123456789abcdef/pdf#page=1")
        self.assertIsNone(citation["anchor_url"])

    def test_valid_citation_is_attached_to_page_and_document(self):
        documents = [{"document_id": "rag:d001", "envelope": envelope()}]

        def fake(messages, **options):
            return {"answer": "КПД составил 21.5 %.", "not_found": False,
                    "citations": [{"ref": "S2", "quote": "PCE reached 21.5 %"}],
                    "contradictions": []}

        result = chat.ask("Какой КПД?", "document", documents, complete_fn=fake)
        self.assertEqual(result["citations"][0]["page"], 1)
        self.assertEqual(result["citations"][0]["document_id"], "rag:d001")
        self.assertFalse(result["unverified"])

    def test_invented_citation_is_dropped_and_answer_is_marked(self):
        documents = [{"document_id": "rag:d001", "envelope": envelope()}]

        def fake(messages, **options):
            return {"answer": "КПД 30 %.", "not_found": False,
                    "citations": [{"ref": "S1", "quote": "PCE reached 30 %"}],
                    "contradictions": []}

        result = chat.ask("Какой КПД?", "document", documents, complete_fn=fake)
        self.assertEqual(result["citations"], [])
        self.assertTrue(result["unverified"])

    def test_missing_answer_is_reported(self):
        documents = [{"document_id": "rag:d001", "envelope": envelope()}]

        def fake(messages, **options):
            return {"answer": "В статье нет данных о сроке службы.", "not_found": True,
                    "citations": [], "contradictions": []}

        result = chat.ask("Какой срок службы?", "document", documents, complete_fn=fake)
        self.assertTrue(result["not_found"])
        self.assertFalse(result["unverified"])

    def test_document_without_sources_does_not_call_the_model(self):
        documents = [{"document_id": "rag:d001", "envelope": envelope(sources=[])}]
        called = []

        def fake(messages, **options):
            called.append(True)
            return {}

        result = chat.ask("О чём статья?", "document", documents, complete_fn=fake)
        self.assertEqual(called, [])
        self.assertTrue(result["not_found"])
        self.assertEqual(result["citations"], [])

    def test_general_mode_has_no_documents_and_no_citations(self):
        def fake(messages, **options):
            return {"answer": "Общие знания модели, не вывод по статье: эффект известен.",
                    "not_found": False, "citations": [{"ref": "S1", "quote": "x"}],
                    "contradictions": []}

        result = chat.ask("Что такое эффект?", "general", [], complete_fn=fake)
        self.assertTrue(result["general_knowledge"])
        self.assertEqual(result["citations"], [])
        self.assertEqual(result["context"]["label"], "Общий вопрос без документов")

    def test_contradiction_requires_two_known_sources(self):
        documents = [{"document_id": "rag:d001", "envelope": envelope()}]

        def fake(messages, **options):
            return {"answer": "Есть расхождение.", "not_found": False,
                    "citations": [{"ref": "S2", "quote": "PCE reached 21.5 %"},
                                  {"ref": "S1", "quote": "fill factor of 0.81"}],
                    "contradictions": [
                        {"topic": "КПД", "refs": ["S1", "S9"], "explanation": "разные значения"},
                        {"topic": "Фактор заполнения", "refs": ["S1", "S2"], "explanation": "разные условия"},
                    ]}

        result = chat.ask("Сравни данные", "document", documents, complete_fn=fake)
        self.assertEqual(len(result["contradictions"]), 1)
        self.assertEqual(result["contradictions"][0]["topic"], "Фактор заполнения")


class StoreTest(TestCase):
    def test_history_is_isolated_by_user_and_context(self):
        with TemporaryDirectory() as folder:
            store = chat.ChatStore(f"{folder}/chat.sqlite3")
            store.append("u1", "document", "doc:rag:d001", "user", "Вопрос")
            store.append("u1", "document", "doc:rag:d001", "assistant", "Ответ")
            store.append("u2", "document", "doc:rag:d001", "user", "Чужой вопрос")
            store.append("u1", "general", "general", "user", "Общий вопрос")
            history = store.history("u1", "document", "doc:rag:d001")
            self.assertEqual([item["content"] for item in history], ["Вопрос", "Ответ"])
            store.clear("u1", "document", "doc:rag:d001")
            self.assertEqual(store.history("u1", "document", "doc:rag:d001"), [])
            self.assertEqual(len(store.history("u2", "document", "doc:rag:d001")), 1)
