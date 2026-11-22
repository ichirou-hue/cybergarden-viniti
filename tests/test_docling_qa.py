"""Independent, offline QA: injected converters only; never load model weights."""

from copy import deepcopy
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PIL import Image

from viniti.atomic_output import publish_directory
from viniti.docling_source import make_converter, normalize_document, page_anchor, prepare_pdf, table_grid
from viniti.extraction_core import export_result, load_profile, run
from tests.test_docling_source import fixture, table_data
from tests.test_solar import ROOT, synthetic_pdf


def converter(raw, image=None):
    if image is None:
        image = Image.new("RGB", (8, 8), "white")
    document = SimpleNamespace(
        export_to_dict=lambda: raw,
        pictures=[SimpleNamespace(get_image=lambda doc: image)],
    )
    return Mock(convert=Mock(return_value=SimpleNamespace(status="success", document=document)))


class DoclingOfflineQATest(unittest.TestCase):
    def test_bottomleft_full_page_and_fractional_bounds(self):
        item = fixture()["pictures"][0]
        item["prov"][0]["bbox"].update(l=0, b=0, r=600, t=800)
        self.assertEqual(page_anchor(item, fixture()["pages"])["bbox"], [0, 0, 600, 800])
        item["prov"][0]["bbox"].update(l=0.25, r=599.75)
        self.assertEqual(page_anchor(item, fixture()["pages"])["bbox"][0], 0.25)

    def test_nonfinite_boolean_degenerate_and_multiblock_bounds_rejected(self):
        for change in (
            {"l": True}, {"l": float("-inf")}, {"t": 801},
            {"r": 20}, {"b": 450}, {"b": -1},
        ):
            item = deepcopy(fixture()["pictures"][0])
            item["prov"][0]["bbox"].update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                page_anchor(item, fixture()["pages"])
        item["prov"] *= 2
        with self.assertRaises(ValueError):
            page_anchor(item, fixture()["pages"])

    def test_table_spans_may_not_overlap_other_cell_origins(self):
        data = table_data([["spanned heading", "conflicting heading"]])
        data["table_cells"][0]["end_col_offset_idx"] = 2
        with self.assertRaises(ValueError):
            table_grid(data)

    def test_non_dictionary_provenance_is_a_clean_validation_error(self):
        raw = fixture()
        raw["texts"][0]["prov"] = [None]
        try:
            document, _ = normalize_document(raw)
        except ValueError:
            return
        self.assertTrue(document["warnings"])
        self.assertFalse(any(s["quote"] == raw["texts"][0]["text"] for s in document["sources"]))

    def test_non_dictionary_page_is_a_clean_validation_error(self):
        raw = fixture()
        raw["pages"]["1"] = None
        try:
            document, _ = normalize_document(raw)
        except ValueError:
            return
        self.assertFalse(document["sources"])
        self.assertFalse(document["figures"])
        self.assertTrue(document["warnings"])

    def test_nonstring_caption_reference_is_ignored_or_cleanly_rejected(self):
        raw = fixture()
        raw["pictures"][0]["captions"] = [{"$ref": None}]
        try:
            document, _ = normalize_document(raw)
        except ValueError:
            return
        self.assertEqual(document["figures"][0]["caption"], "")

    def test_converter_page_numbers_must_match_actual_pdf_not_only_count(self):
        raw = fixture()
        raw["pages"] = {"2": raw["pages"]["1"]}
        raw["pages"]["2"]["page_no"] = 2
        for collection in ("texts", "tables", "pictures"):
            for item in raw[collection]:
                item["prov"][0]["page_no"] = 2
        with self.assertRaises(ValueError):
            prepare_pdf("single.pdf", synthetic_pdf(), converter=converter(raw))

    def test_figure_only_document_exports_explicit_partial_without_fake_sources(self):
        raw = fixture()
        raw["texts"], raw["tables"] = [], []
        raw["pictures"][0]["captions"] = []
        document, assets = prepare_pdf("figure.pdf", synthetic_pdf(), charts=True, converter=converter(raw))
        result = run(document, load_profile("solar"), mode="scripts")
        self.assertEqual(result["sources"], [])
        self.assertEqual(result["records"], [])
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["coverage"]["status"], "partial")
        with TemporaryDirectory() as tmp:
            out = Path(tmp) / "result"
            export_result(result, out, assets=assets)
            with Image.open(out / "assets/figures/figure-0001.png") as image:
                self.assertEqual(image.format, "PNG")

    def test_completely_empty_document_is_rejected(self):
        raw = fixture()
        raw.update(texts=[], tables=[], pictures=[])
        with self.assertRaises(ValueError):
            prepare_pdf("empty.pdf", synthetic_pdf(), converter=converter(raw))

    def test_failed_png_encoding_does_not_publish_or_mutate_input(self):
        raw = fixture()
        snapshot = deepcopy(raw)
        broken_image = Mock()
        broken_image.save.side_effect = OSError("disk full")
        with self.assertRaises(OSError):
            prepare_pdf("image.pdf", synthetic_pdf(), converter=converter(raw, broken_image))
        self.assertEqual(raw, snapshot)

    def test_repeated_export_preserves_original_artifacts(self):
        document, assets = prepare_pdf("test.pdf", synthetic_pdf(), converter=converter(fixture()))
        result = run(document, load_profile("solar"), mode="scripts")
        with TemporaryDirectory() as tmp:
            out = Path(tmp) / "result"
            export_result(result, out, assets=assets)
            snapshot = {p.relative_to(out): p.read_bytes() for p in out.rglob("*") if p.is_file()}
            with self.assertRaises(ValueError):
                export_result(result, out, assets={"assets/figures/figure-0001.png": b"changed"})
            self.assertEqual(snapshot, {p.relative_to(out): p.read_bytes() for p in out.rglob("*") if p.is_file()})

    def test_export_must_not_replace_concurrently_created_output_directory(self):
        document, assets = prepare_pdf("test.pdf", synthetic_pdf(), converter=converter(fixture()))
        result = run(document, load_profile("solar"), mode="scripts")
        with TemporaryDirectory() as tmp:
            out = Path(tmp) / "result"

            def race(source, target):
                Path(target).mkdir()
                return publish_directory(source, target)

            with patch("viniti.extraction_core.publish_directory", race), self.assertRaises((ValueError, OSError)):
                export_result(result, out, assets=assets)
            self.assertEqual(list(out.iterdir()), [])

    def test_cli_invalid_parser_and_ocr_values_rejected_without_output(self):
        for flags in (["--parser", "unknown"], ["--ocr", "unknown"], ["--charts"]):
            with self.subTest(flags=flags), TemporaryDirectory() as tmp:
                out = Path(tmp) / "result"
                execution = subprocess.run(
                    [sys.executable, "-m", "viniti.extract_document", "unused.pdf", "-o", str(out), *flags],
                    capture_output=True, text=True, timeout=30,
                )
                self.assertNotEqual(execution.returncode, 0)
                self.assertNotIn("Traceback", execution.stderr)
                self.assertFalse(out.exists())

    def test_missing_docling_dependency_has_actionable_clean_error(self):
        real_import = __import__

        def no_docling(name, *args, **kwargs):
            if name == "docling" or name.startswith("docling."):
                raise ImportError("missing optional dependency")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=no_docling):
            with self.assertRaisesRegex(ValueError, "requirements-docling.txt"):
                make_converter(ocr="off")

    def test_actual_core_serialization_without_converter_or_downloads(self):
        try:
            from docling_core.types.doc import (
                BoundingBox, DocItemLabel, DoclingDocument, ImageRef, ProvenanceItem,
                Size, TableCell, TableData,
            )
            from docling_core.types.doc.document import PictureMeta, TabularChartMetaField
        except ImportError:
            self.skipTest("optional docling_core not installed")
        doc = DoclingDocument(name="offline-fixture")
        doc.add_page(page_no=1, size=Size(width=600, height=800))
        prov = ProvenanceItem(
            page_no=1, charspan=(0, 10),
            bbox=BoundingBox(l=10, t=100, r=200, b=200, coord_origin="TOPLEFT"),
        )
        caption = doc.add_text(label=DocItemLabel.CAPTION, text="Figure 1", prov=prov)
        picture = doc.add_picture(
            image=ImageRef.from_pil(Image.new("RGB", (8, 8)), dpi=72), caption=caption, prov=prov,
        )
        chart_data = TableData(num_rows=1, num_cols=1, table_cells=[
            TableCell(text="91.5", start_row_offset_idx=0, end_row_offset_idx=1,
                      start_col_offset_idx=0, end_col_offset_idx=1),
        ])
        picture.meta = PictureMeta(tabular_chart=TabularChartMetaField(chart_data=chart_data))
        doc.add_table(data=chart_data, prov=prov)
        parsed, assets = normalize_document(doc.export_to_dict(), charts=True)
        self.assertEqual(parsed["figures"][0]["caption"], "Figure 1")
        self.assertEqual(parsed["visual_candidates"][0]["verification"], "visual_unverified")
        self.assertIn(b"91.5", assets["assets/charts/chart-0001.csv"])
        self.assertEqual(next(s for s in parsed["sources"] if s["kind"] == "table")["bbox_scope"], "table")

    def test_overlap_rejected_regardless_of_cell_order_or_span_direction(self):
        for reverse in (False, True):
            for direction in ("row", "col"):
                data = table_data([["A", "B"], ["C", "D"]])
                data["table_cells"][0][f"end_{direction}_offset_idx"] = 2
                if reverse:
                    data["table_cells"].reverse()
                with self.subTest(reverse=reverse, direction=direction), self.assertRaises(ValueError):
                    table_grid(data)

    def test_each_asset_reference_is_required_individually_before_creating_parent(self):
        document, assets = prepare_pdf("test.pdf", synthetic_pdf(), charts=True, converter=converter(fixture()))
        result = run(document, load_profile("solar"), mode="scripts")
        for missing in assets:
            with self.subTest(missing=missing), TemporaryDirectory() as tmp:
                out = Path(tmp) / "new-parent" / "result"
                supplied = {name: content for name, content in assets.items() if name != missing}
                with self.assertRaisesRegex(ValueError, "Не переданы все файлы"):
                    export_result(result, out, assets=supplied)
                self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_empty_chart_keeps_figure_and_warns_without_claiming_numeric_extraction(self):
        for values in ([], [["axis", "series"]], [["", ""]]):
            raw = fixture()
            raw["pictures"][0]["meta"]["tabular_chart"]["chart_data"] = table_data(values)
            document, assets = prepare_pdf("test.pdf", synthetic_pdf(), charts=True, converter=converter(raw))
            for mode in ("auto", "model"):
                with self.subTest(values=values, mode=mode):
                    result = run(document, load_profile("custom-example"), mode=mode,
                                 model_client=lambda _: {"equipment": []})
                    self.assertEqual(len(result["parser_output"]["figures"]), 1)
                    self.assertEqual(result["parser_output"]["visual_candidates"], [])
                    self.assertIn("визуальных кандидатов 0", result["coverage"]["reason"])
                    self.assertEqual(result["coverage"]["status"], "partial")
                    self.assertTrue(any("числовые" in warning for warning in result["warnings"]))
                    self.assertFalse(any(name.startswith("assets/charts/") for name in assets))

    def test_native_publication_preserves_existing_targets_including_dangling_symlink(self):
        from viniti.atomic_output import publish_directory

        for kind in ("empty-directory", "nonempty-directory", "file", "dangling-symlink"):
            with self.subTest(kind=kind), TemporaryDirectory() as tmp:
                source, target = Path(tmp) / "source", Path(tmp) / "target"
                source.mkdir()
                (source / "new.txt").write_text("new")
                if kind.endswith("directory"):
                    target.mkdir()
                    if kind == "nonempty-directory":
                        (target / "original.txt").write_text("original")
                elif kind == "file":
                    target.write_text("original")
                else:
                    target.symlink_to(Path(tmp) / "missing", target_is_directory=True)
                with self.assertRaises(OSError):
                    publish_directory(source, target)
                self.assertEqual((source / "new.txt").read_text(), "new")
                if kind == "dangling-symlink":
                    self.assertTrue(target.is_symlink())
                elif kind == "file":
                    self.assertEqual(target.read_text(), "original")
                else:
                    self.assertFalse((target / "new.txt").exists())

    def test_native_publication_success_moves_whole_directory(self):
        from viniti.atomic_output import publish_directory

        with TemporaryDirectory() as tmp:
            source, target = Path(tmp) / "source", Path(tmp) / "target"
            source.mkdir()
            (source / "sentinel").write_bytes(b"complete")
            publish_directory(source, target)
            self.assertFalse(source.exists())
            self.assertEqual((target / "sentinel").read_bytes(), b"complete")


if __name__ == "__main__":
    unittest.main()
