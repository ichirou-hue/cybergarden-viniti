from copy import deepcopy
from io import BytesIO
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PIL import Image

from docling_source import make_converter, normalize_document, page_anchor, prepare_pdf, table_grid
from extraction_core import export_result, load_profile, run
from tests.test_solar import ROOT, synthetic_pdf


def table_data(values):
    return {
        "num_rows": len(values), "num_cols": len(values[0]) if values else 0,
        "table_cells": [
            {"text": value, "start_row_offset_idx": r, "end_row_offset_idx": r + 1,
             "start_col_offset_idx": c, "end_col_offset_idx": c + 1}
            for r, row in enumerate(values) for c, value in enumerate(row)
        ],
    }


def fixture():
    def prov(left, bottom, right, top):
        return [{"page_no": 1, "bbox": {"l": left, "b": bottom, "r": right, "t": top,
                                      "coord_origin": "BOTTOMLEFT"}, "charspan": [0, 10]}]
    return {
        "schema_name": "DoclingDocument",
        "pages": {"1": {"page_no": 1, "size": {"width": 600, "height": 800}}},
        "texts": [
            {"text": "Experimental perovskite devices.", "label": "text", "prov": prov(20, 700, 500, 720)},
            {"text": "Figure 1. Stability curves.", "label": "caption", "prov": prov(20, 150, 500, 170)},
        ],
        "tables": [{"prov": prov(20, 500, 500, 600), "data": table_data([
            ["Device", "PCE (%)"], ["A", "20"],
        ])}],
        "pictures": [{
            "prov": prov(20, 200, 500, 450), "captions": [{"$ref": "#/texts/1"}],
            "meta": {"tabular_chart": {
                "title": "Stability", "chart_data": table_data([["time", "PCE"], ["100", "91.5"]]),
            }},
        }],
    }


class DoclingAdapterTest(unittest.TestCase):
    def test_converter_enables_builtin_formula_and_script_recovery(self):
        options = SimpleNamespace(script_position_options=SimpleNamespace(enabled=False))
        modules = {name: ModuleType(name) for name in (
            "docling.datamodel.base_models", "docling.datamodel.pipeline_options", "docling.document_converter",
        )}
        modules["docling.datamodel.base_models"].InputFormat = SimpleNamespace(PDF="pdf")
        pipeline = modules["docling.datamodel.pipeline_options"]
        pipeline.PdfPipelineOptions = lambda: options
        pipeline.TesseractCliOcrOptions = Mock()
        converter = modules["docling.document_converter"]
        converter.PdfFormatOption = lambda **kwargs: SimpleNamespace(**kwargs)
        converter.DocumentConverter = Mock()
        with patch.dict(sys.modules, modules):
            make_converter(ocr="off")
        self.assertTrue(options.do_formula_enrichment)
        self.assertTrue(options.script_position_options.enabled)
        configured = converter.DocumentConverter.call_args.kwargs["format_options"]["pdf"].pipeline_options
        self.assertIs(configured, options)

    def test_body_reference_order_preserves_columns_and_interleaved_tables(self):
        raw = fixture()
        first = deepcopy(raw["texts"][0])
        first["text"] = "Left column bottom"
        first["prov"][0]["bbox"].update(l=20, r=250, b=100, t=120)
        second = deepcopy(raw["texts"][0])
        second["text"] = "Right column top"
        second["prov"][0]["bbox"].update(l=300, r=550, b=700, t=720)
        raw["texts"] = [first, second]
        raw["groups"] = [{"label": "section", "children": [{"$ref": "#/texts/0"}, {"$ref": "#/tables/0"}]}]
        raw["body"] = {"children": [{"$ref": "#/groups/0"}, {"$ref": "#/texts/1"}, {"$ref": "#/pictures/0"}]}
        document, _ = normalize_document(raw)
        self.assertEqual([s["quote"] for s in document["sources"]],
                         ["Left column bottom", "Device | PCE (%)", "A | 20", "Right column top"])
        self.assertEqual([s["reading_order"] for s in document["sources"]], [1, 2, 3, 4])

    def test_docling_inline_script_runs_reconstruct_parent_without_duplicate_indices(self):
        raw = fixture()
        parent = raw["texts"][0]
        parent.update(text="", children=[{"$ref": "#/groups/0"}])
        runs = []
        for text, script in (("SrCl", "baseline"), ("2", "sub"), ("-doped CsPbI", "baseline"),
                             ("2", "sub"), ("Br", "baseline")):
            item = deepcopy(parent)
            item.pop("children")
            item.update(text=text, formatting={"script": script})
            raw["texts"].append(item)
            runs.append({"$ref": f"#/texts/{len(raw['texts']) - 1}"})
        raw["groups"] = [{"label": "inline", "children": runs}]
        raw["body"] = {"children": [{"$ref": "#/texts/0"}, {"$ref": "#/texts/1"}]}
        document, _ = normalize_document(raw)
        source = next(s for s in document["sources"] if s["docling_ref"] == "#/texts/0")
        self.assertEqual(source["quote"], "SrCl2-doped CsPbI2Br")
        self.assertEqual(source["display_text"], "SrCl_{2}-doped CsPbI_{2}Br")
        self.assertEqual(len(source["inline_runs"]), 5)
        self.assertFalse(any(s["quote"] == "2" for s in document["sources"]))

    def test_duplicate_inline_groups_do_not_double_the_parent_text(self):
        # Docling payloads in the wild list the same inline group twice, or the
        # same runs in two groups; that must not print "SrCl2SrCl2".
        raw = fixture()
        parent = raw["texts"][0]
        parent.update(text="", children=[{"$ref": "#/groups/0"}, {"$ref": "#/groups/1"}])
        runs = []
        for text, script in (("SrCl", "baseline"), ("2", "sub")):
            item = deepcopy(parent)
            item.pop("children")
            item.update(text=text, formatting={"script": script})
            raw["texts"].append(item)
            runs.append({"$ref": f"#/texts/{len(raw['texts']) - 1}"})
        raw["groups"] = [{"label": "inline", "children": runs}, {"label": "inline", "children": runs}]
        raw["body"] = {"children": [{"$ref": "#/texts/0"}, {"$ref": "#/texts/1"}]}
        document, _ = normalize_document(raw)
        source = next(s for s in document["sources"] if s["docling_ref"] == "#/texts/0")
        self.assertEqual(source["quote"], "SrCl2")
        self.assertEqual(source["display_text"], "SrCl_{2}")
        self.assertEqual(len(source["inline_runs"]), 2)

    def test_formula_label_keeps_enriched_latex_as_unverified_parser_text(self):
        raw = fixture()
        raw["texts"][0].update(label="formula", text=r"E=\frac{hc}{\lambda}")
        document, _ = normalize_document(raw)
        source = next(s for s in document["sources"] if s["kind"] == "equation")
        self.assertEqual(source["latex"], r"E=\frac{hc}{\lambda}")
        self.assertEqual(source["verification"], "parser_text_only")
        self.assertTrue(source["requires_review"])

    def test_cyclic_body_reference_rejected(self):
        raw = fixture()
        raw["groups"] = [{"label": "section", "children": [{"$ref": "#/groups/0"}]}]
        raw["body"] = {"children": [{"$ref": "#/groups/0"}]}
        with self.assertRaisesRegex(ValueError, "Цикл"):
            normalize_document(raw)

    def test_coordinate_origin_and_table_scope(self):
        document, assets = normalize_document(fixture(), charts=True)
        text = next(source for source in document["sources"] if source["kind"] == "text")
        self.assertEqual(text["bbox"], [20, 80, 500, 100])
        table = next(source for source in document["sources"] if source["kind"] == "table")
        self.assertEqual(table["bbox_scope"], "table")
        self.assertEqual(table["cells"][0]["bbox_scope"], "table")
        self.assertIn("assets/tables/table-0001.csv", assets)

    def test_chart_values_do_not_become_literal_text(self):
        document, assets = normalize_document(fixture(), charts=True)
        self.assertFalse(any("91.5" in row["quote"] for row in document["sources"]))
        chart = document["visual_candidates"][0]
        self.assertEqual(chart["status"], "requires_review")
        self.assertEqual(chart["verification"], "visual_unverified")
        self.assertEqual(chart["image"], "assets/figures/figure-0001.png")
        self.assertIn(b"91.5", assets[chart["csv"]])

    def test_chart_disabled_never_exports_legacy_chart_metadata_as_verified(self):
        document, assets = normalize_document(fixture(), charts=False)
        self.assertEqual(document["visual_candidates"], [])
        self.assertFalse(any(path.startswith("assets/charts/") for path in assets))

    def test_unrecognized_chart_payload_gives_warning(self):
        raw = fixture()
        raw["pictures"][0]["meta"]["tabular_chart"] = {"chart_data": {"something": []}}
        document, _ = normalize_document(raw, charts=True)
        self.assertFalse(document["visual_candidates"])
        self.assertTrue(document["warnings"])

    def test_empty_chart_is_not_a_numeric_candidate(self):
        for values in ([], [["Time", "PCE"]], [["", ""]]):
            with self.subTest(values=values):
                raw = fixture()
                raw["pictures"][0]["meta"]["tabular_chart"]["chart_data"] = table_data(values)
                document, assets = normalize_document(raw, charts=True)
                self.assertEqual(document["visual_candidates"], [])
                self.assertTrue(document["warnings"])
                self.assertTrue(document["figures"])
                self.assertFalse(any(path.startswith("assets/charts/") for path in assets))

    def test_unknown_or_out_of_page_bounds_rejected(self):
        pages = fixture()["pages"]
        item = fixture()["texts"][0]
        for mutate in (
            lambda p: p.update(page_no=2),
            lambda p: p["bbox"].update(l=-1),
            lambda p: p["bbox"].update(r=float("nan")),
            lambda p: p["bbox"].update(coord_origin="unknown"),
        ):
            with self.subTest(mutate=mutate):
                bad = deepcopy(item)
                mutate(bad["prov"][0])
                with self.assertRaises(ValueError):
                    page_anchor(bad, pages)

    def test_top_left_coordinates_remain_top_left(self):
        item = {"prov": [{"page_no": 1, "bbox": {
            "l": 10, "t": 20, "r": 100, "b": 200, "coord_origin": "TOPLEFT",
        }}]}
        self.assertEqual(page_anchor(item, fixture()["pages"])["bbox"], [10, 20, 100, 200])

    def test_missing_provenance_is_not_invented(self):
        raw = fixture()
        raw["texts"][0]["prov"] = []
        document, _ = normalize_document(raw)
        self.assertFalse(any(row["quote"] == "Experimental perovskite devices." for row in document["sources"]))
        self.assertTrue(document["warnings"])

    def test_large_and_invalid_table_grids_rejected(self):
        for data in (
            {"num_rows": 100000, "num_cols": 100000, "table_cells": []},
            {"num_rows": True, "num_cols": 1, "table_cells": []},
            {"num_rows": 1, "num_cols": 1, "table_cells": [{"text": "20", "start_row_offset_idx": -1}]},
        ):
            with self.subTest(data=data), self.assertRaises(ValueError):
                table_grid(data)

    def test_spanned_cell_not_duplicated_into_fake_values(self):
        data = table_data([["Header", ""], ["20", "30"]])
        data["table_cells"] = [cell for cell in data["table_cells"] if cell["text"]]
        data["table_cells"][0]["end_col_offset_idx"] = 2
        self.assertEqual(table_grid(data)[0], ["Header", ""])

    def converter(self, raw=None, *, status="success", image=True):
        picture = SimpleNamespace(get_image=lambda doc: Image.new("RGB", (20, 20), "white") if image else None)
        document = SimpleNamespace(export_to_dict=lambda: fixture() if raw is None else raw, pictures=[picture])
        return Mock(convert=Mock(return_value=SimpleNamespace(status=status, document=document)))

    def test_mock_conversion_and_atomic_artifact_export(self):
        document, assets = prepare_pdf("D001.pdf", synthetic_pdf(), charts=True, converter=self.converter())
        result = run(document, load_profile("solar"), mode="scripts")
        with TemporaryDirectory() as tmp:
            out = Path(tmp) / "result"
            export_result(result, out, assets=assets)
            self.assertTrue((out / "parser.json").exists())
            self.assertTrue((out / "assets/figures/figure-0001.png").exists())
            self.assertTrue((out / "assets/charts/chart-0001.csv").exists())
            self.assertFalse((out / "results.csv").exists())
            parser = json.loads((out / "parser.json").read_text())
            self.assertEqual(parser["visual_candidates"][0]["status"], "requires_review")

    def test_model_coverage_does_not_deny_requested_ocr_or_visual_candidates(self):
        document, _ = prepare_pdf("D001.pdf", synthetic_pdf(), charts=True, converter=self.converter())
        result = run(document, load_profile("custom-example"), mode="model",
                     model_client=lambda _: {"equipment": []})
        self.assertIn("визуальных кандидатов 1", result["coverage"]["reason"])
        self.assertNotIn("OCR/графики не обработаны", result["coverage"]["reason"])
        self.assertEqual(result["coverage"]["status"], "partial")

    def test_missing_assets_cannot_publish_dangling_image_or_table_links(self):
        document, assets = prepare_pdf("D001.pdf", synthetic_pdf(), charts=True, converter=self.converter())
        result = run(document, load_profile("solar"), mode="scripts")
        for supplied in (None, {name: value for name, value in assets.items() if "figures" not in name}):
            with self.subTest(supplied=supplied), TemporaryDirectory() as tmp:
                out = Path(tmp) / "result"
                with self.assertRaises(ValueError):
                    export_result(result, out, assets=supplied)
                self.assertFalse(out.exists())

    def test_partial_conversion_never_published(self):
        with self.assertRaises(ValueError):
            prepare_pdf("D001.pdf", synthetic_pdf(), converter=self.converter(status="partial_success"))

    def test_no_image_means_no_unbacked_asset_reference(self):
        with self.assertRaises(ValueError):
            prepare_pdf("D001.pdf", synthetic_pdf(), converter=self.converter(image=False))

    def test_model_or_download_failure_clean_no_provider_details(self):
        converter = Mock(convert=Mock(side_effect=RuntimeError("secret-url-token")))
        with self.assertRaises(ValueError) as error:
            prepare_pdf("D001.pdf", synthetic_pdf(), converter=converter)
        self.assertNotIn("secret", str(error.exception))

    def test_assets_reject_traversal_and_keep_output_absent(self):
        document, assets = prepare_pdf("D001.pdf", synthetic_pdf(), converter=self.converter())
        result = run(document, load_profile("solar"), mode="scripts")
        for path in ("../outside.png", "/absolute.png", "assets/figures/../../result.json",
                     "assets/figures\\bad.png", "result.json"):
            with self.subTest(path=path), TemporaryDirectory() as tmp:
                out = Path(tmp) / "result"
                with self.assertRaisesRegex(ValueError, "путь|артефакт"):
                    export_result(result, out, assets=assets | {path: b"bad"})
                self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_asset_write_failure_removes_entire_staging_bundle(self):
        document, assets = prepare_pdf("D001.pdf", synthetic_pdf(), converter=self.converter())
        result = run(document, load_profile("solar"), mode="scripts")
        with TemporaryDirectory() as tmp:
            out = Path(tmp) / "result"
            with patch.object(Path, "write_bytes", side_effect=OSError("disk full")), self.assertRaises(OSError):
                export_result(result, out, assets=assets)
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_malformed_caption_references_are_warnings_not_crashes(self):
        for captions in (None, {"$ref": "#/texts/1"}, [None], ["#/texts/1"],
                         [{"$ref": "#/texts/99"}], [{"$ref": "#/tables/0"}]):
            with self.subTest(captions=captions):
                raw = fixture()
                raw["pictures"][0]["captions"] = captions
                document, _ = normalize_document(raw)
                self.assertEqual(document["figures"][0]["caption"], "")
                self.assertTrue(any("Изображение 1" in warning for warning in document["warnings"]))

    def test_valid_caption_reference_still_resolves(self):
        document, _ = normalize_document(fixture())
        self.assertEqual(document["figures"][0]["caption"], "Figure 1. Stability curves.")

    def test_missing_or_short_picture_list_is_a_clean_value_error(self):
        raw = fixture()
        for pictures in (None, []):
            with self.subTest(pictures=pictures):
                document = SimpleNamespace(export_to_dict=lambda: raw)
                if pictures is not None:
                    document.pictures = pictures
                converter = Mock(convert=Mock(return_value=SimpleNamespace(status="success", document=document)))
                with self.assertRaises(ValueError):
                    prepare_pdf("D001.pdf", synthetic_pdf(), converter=converter)

    def test_picture_crop_failures_are_sanitized_value_errors(self):
        for failure in (RuntimeError("secret-url-token"), IndexError("bad index"),
                        TypeError("bad payload"), KeyError("secret")):
            with self.subTest(failure=type(failure).__name__):
                picture = SimpleNamespace(get_image=Mock(side_effect=failure))
                document = SimpleNamespace(export_to_dict=fixture, pictures=[picture])
                converter = Mock(convert=Mock(return_value=SimpleNamespace(status="success", document=document)))
                with self.assertRaises(ValueError) as caught:
                    prepare_pdf("D001.pdf", synthetic_pdf(), converter=converter)
                self.assertNotIn("secret", str(caught.exception))

    def test_png_encode_failure_with_unexpected_error_is_sanitized(self):
        image = Mock()
        image.save.side_effect = RuntimeError("secret-url-token")
        picture = SimpleNamespace(get_image=lambda doc: image)
        document = SimpleNamespace(export_to_dict=fixture, pictures=[picture])
        converter = Mock(convert=Mock(return_value=SimpleNamespace(status="success", document=document)))
        with self.assertRaises(ValueError) as caught:
            prepare_pdf("D001.pdf", synthetic_pdf(), converter=converter)
        self.assertNotIn("secret", str(caught.exception))

    def test_charts_require_docling_parser(self):
        with TemporaryDirectory() as tmp:
            out = Path(tmp) / "result"
            execution = subprocess.run([
                sys.executable, "-X", "utf8", str(ROOT / "extract_document.py"), "unused.pdf", "--charts", "-o", str(out),
            ], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
            self.assertEqual(execution.returncode, 1)
            self.assertNotIn("Traceback", execution.stderr)
            self.assertFalse(out.exists())

    def test_optional_dependency_import_not_needed_for_default(self):
        execution = subprocess.run([
            sys.executable, "-X", "utf8", "-c", "import extract_document, sys; assert 'docling' not in sys.modules",
        ], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, cwd=ROOT)
        self.assertEqual(execution.returncode, 0, execution.stderr)


if __name__ == "__main__":
    unittest.main()
