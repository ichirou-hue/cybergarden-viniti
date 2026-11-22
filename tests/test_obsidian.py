"""Obsidian export: stable notes, confirmed links and no invented entities."""

import io
import zipfile
from pathlib import PurePosixPath
from unittest import TestCase

from viniti.obsidian import export_zip


def rag_envelope(publication_id="D001", title="Solar cell study", dataset="Dataset X"):
    return {
        "pages": 4,
        "track": "rag",
        "publication": {
            "publication_id": publication_id,
            "title": title,
            "authors": ["A. Author"],
            "year": "2021",
            "DOI": "10.1000/test",
        },
        "counts": {"results": 1, "issues": 1},
        "entities": {
            "configurations": [{"id": "C01", "label": "Configuration A", "detail": "one junction"}],
            "datasets": [{"id": "D01", "label": dataset}],
            "tasks": [{"id": "T01", "label": "unspecified"}],
            "experiments": [{"id": "E01", "label": "Table 2 experiment"}],
        },
        "results": [{
            "metric_name": "F1", "metric_value": "0.81", "metric_unit": "",
            "config_id": "C01", "dataset_id": "D01", "task_id": "T01", "experiment_id": "E01",
        }],
        "issues": [{
            "issue_id": "C1", "check_name": "duplicate", "description": "Same value twice",
            "source": {"page": 2},
        }],
        "text_sources": [
            {"source_id": 1, "page": 2, "kind": "table", "fragment": "F1 was 0.81 in the champion cell."},
        ],
        "warnings": [],
    }


def names(data):
    # Keep the archive open for the caller: closing it inside the helper made
    # every read() fail with "ZIP archive that was already closed".
    archive = zipfile.ZipFile(io.BytesIO(data))
    return archive.namelist(), archive


class ObsidianExportTest(TestCase):
    def test_article_note_has_frontmatter_and_confirmed_links(self):
        data = export_zip([{"document_id": "rag:d001", "envelope": rag_envelope()}])
        files, archive = names(data)
        self.assertIn("README.md", files)
        article = next(name for name in files if name.startswith("Статьи/"))
        text = archive.read(article).decode()
        self.assertIn("type: \"article\"", text)
        self.assertIn("authors: [\"A. Author\"]", text)
        dataset_note = PurePosixPath(next(name for name in files if name.startswith("Сущности/Наборы данных/"))).stem
        author_note = PurePosixPath(next(name for name in files if name.startswith("Сущности/Авторы/"))).stem
        config_note = PurePosixPath(next(name for name in files if name.startswith("Сущности/Конфигурации/"))).stem
        self.assertIn(f"[[{dataset_note}|Dataset X]]", text)
        self.assertIn(f"[[{author_note}|A. Author]]", text)
        self.assertIn(f"[[{config_note}\\|C01]]", text)
        self.assertIn("F1 was 0.81", text)
        self.assertIn("стр. 2", text)
        self.assertIn("C1", text)

    def test_entity_note_has_backlink(self):
        data = export_zip([{"document_id": "rag:d001", "envelope": rag_envelope()}])
        files, archive = names(data)
        entity = next(name for name in files if name.startswith("Сущности/Наборы данных/"))
        text = archive.read(entity).decode()
        self.assertIn("type: \"entity\"", text)
        article_note = PurePosixPath(next(name for name in files if name.startswith("Статьи/"))).stem
        self.assertIn(f"[[{article_note}]]", text)
        self.assertIn("aliases:", text)

    def test_same_dataset_in_two_articles_gets_one_note(self):
        data = export_zip([
            {"document_id": "rag:d001", "envelope": rag_envelope()},
            {"document_id": "rag:d002", "envelope": rag_envelope("D002", "Second study", "Dataset X")},
        ])
        files, archive = names(data)
        datasets = [name for name in files if name.startswith("Сущности/Наборы данных/")]
        self.assertEqual(len(datasets), 1)
        text = archive.read(datasets[0]).decode()
        article_notes = [PurePosixPath(name).stem for name in files if name.startswith("Статьи/")]
        self.assertEqual(len(article_notes), 2)
        for article_note in article_notes:
            self.assertIn(f"[[{article_note}]]", text)

    def test_solar_device_ids_do_not_become_shared_notes(self):
        envelope = {
            "pages": 2,
            "track": "solar",
            "publication": {"publication_id": "D010", "title": "Solar device", "authors": []},
            "entities": {},
            "results": [{"metric_name": "PCE", "metric_value": "21", "device_id": "DV1"}],
            "issues": [],
            "text_sources": [{"source_id": 1, "page": 1, "kind": "text",
                              "fragment": "The device DV1 reached 21 %."}],
            "warnings": [],
        }
        data = export_zip([{"document_id": "solar:d010", "envelope": envelope}])
        files, archive = names(data)
        device_notes = [name for name in files if name.startswith("Сущности/Устройства/")]
        self.assertEqual(len(device_notes), 1)
        article = next(name for name in files if name.startswith("Статьи/"))
        device_note = PurePosixPath(device_notes[0]).stem
        self.assertIn(f"[[{device_note}\\|DV1]]", archive.read(article).decode())
        self.assertIn("Атрибуты сущности не предоставлены", archive.read(device_notes[0]).decode())
        second = {**envelope, "publication": {"publication_id": "D011", "title": "Second device"}}
        both_files, _ = names(export_zip([
            {"document_id": "solar:d010", "envelope": envelope},
            {"document_id": "solar:d011", "envelope": second},
        ]))
        self.assertEqual(len([name for name in both_files if name.startswith("Сущности/Устройства/")]), 2)

    def test_empty_selection_is_rejected(self):
        with self.assertRaises(ValueError):
            export_zip([])


class ObsidianExportRegressionTest(TestCase):
    def test_numeric_zero_and_integer_year_are_preserved(self):
        envelope = rag_envelope()
        envelope["publication"]["year"] = 2021
        envelope["results"][0]["metric_value"] = 0
        files, archive = names(export_zip([{"document_id": "rag:d001", "envelope": envelope}]))
        article = next(name for name in files if name.startswith("Статьи/"))
        text = archive.read(article).decode()
        self.assertIn('year: "2021"', text)
        self.assertIn("| F1 | 0 |", text)

    def test_filenames_are_stable_across_selection_and_order(self):
        first = {"document_id": "rag:d001", "envelope": rag_envelope()}
        second = {"document_id": "rag:d002", "envelope": rag_envelope("D002", "Second study")}
        first_files, _ = names(export_zip([first]))
        second_files, _ = names(export_zip([second]))
        together, _ = names(export_zip([first, second]))
        reversed_files, _ = names(export_zip([second, first]))
        self.assertEqual(set(together), set(reversed_files))
        self.assertTrue(set(first_files).issubset(set(together)))
        self.assertTrue(set(second_files).issubset(set(together)))

    def test_shared_dataset_links_use_each_documents_local_id(self):
        first = {"document_id": "rag:d001", "envelope": rag_envelope()}
        second_envelope = rag_envelope("D002", "Second study")
        second_envelope["entities"]["datasets"][0]["id"] = "OTHER_DATASET_ID"
        second_envelope["results"][0]["dataset_id"] = "OTHER_DATASET_ID"
        files, archive = names(export_zip([first, {"document_id": "rag:d002", "envelope": second_envelope}]))
        article = next(name for name in files if name.startswith("Статьи/D002"))
        dataset_note = PurePosixPath(next(name for name in files if name.startswith("Сущности/Наборы данных/"))).stem
        self.assertIn(f"[[{dataset_note}\\|OTHER_DATASET_ID]]", archive.read(article).decode())

    def test_source_text_cannot_create_paths_wikilinks_or_html(self):
        envelope = rag_envelope("../../outside/[[evil]]", "[[evil]] <img src=x onerror=alert(1)>")
        envelope["results"][0]["metric_name"] = "F1 | [[evil]]"
        envelope["text_sources"][0]["fragment"] = "[[evil]] <script>alert(1)</script>"
        envelope["entities"]["datasets"][0]["label"] = "../[[evil]]|alias"
        files, archive = names(export_zip([{"document_id": "../untrusted", "envelope": envelope}]))
        for filename in files:
            path = PurePosixPath(filename)
            self.assertFalse(path.is_absolute())
            self.assertNotIn("..", path.parts)
            self.assertNotIn("[", filename)
            self.assertNotIn("]", filename)
            self.assertNotIn("|", filename)
        article = next(name for name in files if name.startswith("Статьи/"))
        body = archive.read(article).decode().split("---", 2)[-1]
        self.assertNotIn("[[evil]]", body)
        # Backslash escaping keeps Markdown from rendering raw HTML; the
        # escaped forms may still contain the literal "<img"/"<script" text.
        self.assertNotRegex(body, r"(?<!\\)<script>")
        self.assertNotRegex(body, r"(?<!\\)<img")
        self.assertIn("\\[\\[evil\\]\\]", body)
        self.assertIn("\\<img", body)
