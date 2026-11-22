"""Layout invariants: one repository root, and every shared path derives from it.

These tests guard the layout itself, not a module's behaviour. They fail when a
module starts rebuilding ``data/``, ``static/``, ``profiles/`` or the corpus from
its own ``__file__`` again: that path silently moves once the module is placed
into a package, which is exactly the failure this stage removes.
"""
import ast
import importlib.util
import os
import pathlib
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from viniti import paths

ROOT = pathlib.Path(__file__).resolve().parents[1]

SHARED_PATHS = (
    ("DATA_DIR", paths.DATA_DIR),
    ("STATIC_DIR", paths.STATIC_DIR),
    ("PROFILES_DIR", paths.PROFILES_DIR),
    ("PASSPORT", paths.PASSPORT),
    ("VOCAB", paths.VOCAB),
)


def load_tool(relative_path, name):
    """Import tools/<file>.py by path: tools/ is not a package."""
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RepositoryRootTest(unittest.TestCase):
    def test_repo_root_is_the_repository_root(self):
        self.assertEqual(paths.REPO_ROOT, ROOT)
        self.assertTrue((paths.REPO_ROOT / "AGENTS.md").is_file())
        self.assertTrue((paths.REPO_ROOT / "passport.json").is_file())

    def test_paths_module_is_a_leaf(self):
        # The leaf rule keeps ``batch --dry-run`` working without pdfplumber.
        source = (paths.REPO_ROOT / "src" / "viniti" / "paths.py").read_text(encoding="utf-8")
        imported = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[0])
        self.assertEqual(imported, {"pathlib"})

    def test_shared_locations_are_absolute_and_inside_the_root(self):
        for label, path in SHARED_PATHS:
            with self.subTest(path=label):
                self.assertIsInstance(path, pathlib.Path)
                self.assertTrue(path.is_absolute(), "%s is not absolute" % label)
                self.assertNotIn("\\", path.as_posix(),
                                 "%s carries a backslash: %s" % (label, path.as_posix()))
                self.assertEqual(path.relative_to(ROOT), path.relative_to(paths.REPO_ROOT))

    def test_parse_module_and_schema_are_where_the_server_expects_them(self):
        parse_module = paths.REPO_ROOT / "src" / "viniti" / "parse_document.py"
        self.assertTrue(parse_module.is_file())
        self.assertEqual(paths.PASSPORT.name, "passport.json")
        self.assertTrue(paths.PASSPORT.is_file())

    def test_static_and_profiles_are_the_directories_that_are_served(self):
        self.assertTrue(paths.STATIC_DIR.is_dir())
        self.assertTrue((paths.STATIC_DIR / "index.html").is_file())
        self.assertTrue(paths.PROFILES_DIR.is_dir())
        self.assertTrue((paths.PROFILES_DIR / "solar.json").is_file())

    def test_corpus_roots_are_keyed_by_area_and_stay_under_the_root(self):
        self.assertEqual(sorted(paths.CORPUS_ROOTS), ["rag", "solar"])
        for area, path in paths.CORPUS_ROOTS.items():
            with self.subTest(area=area):
                self.assertIsInstance(path, pathlib.Path)
                self.assertTrue(path.is_absolute())
                path.relative_to(paths.REPO_ROOT)


class ConsumerPathTest(unittest.TestCase):
    """Modules that used to read beside themselves now read through paths.py."""

    def test_llm_provider_reads_dotenv_from_the_repo_root(self):
        from viniti import llm_provider
        with tempfile.TemporaryDirectory(prefix="viniti-layout-") as name:
            root = pathlib.Path(name)
            (root / ".env").write_bytes(b"OPENAI_MODEL=layout/model\n")
            with mock.patch.object(paths, "REPO_ROOT", root), \
                    mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("OPENAI_MODEL", None)
                llm_provider._load_dotenv()
                self.assertEqual(os.environ.get("OPENAI_MODEL"), "layout/model")

    def test_mock_api_serves_the_static_dir_from_paths(self):
        mock_api = load_tool("tools/mock_api.py", "layout_mock_api")
        self.assertEqual(mock_api.STATIC.resolve(), paths.STATIC_DIR.resolve())
        self.assertEqual(mock_api.ROOT.resolve(), paths.REPO_ROOT.resolve())

    def test_catalog_parse_passes_the_builtin_profile_from_paths(self):
        from viniti import server

        class InlineThread:
            """Run the target at once instead of spawning a real thread."""

            def __init__(self, target=None, args=(), **kwargs):
                self.target, self.args = target, args

            def start(self):
                self.target(*self.args)

        handler = server.Handler.__new__(server.Handler)
        with tempfile.TemporaryDirectory(prefix="viniti-layout-") as name:
            handler.server = SimpleNamespace(parse_root=pathlib.Path(name))
            with mock.patch.object(server.Handler, "run_parse") as runner, \
                    mock.patch.object(server.threading, "Thread", InlineThread):
                handler.start_catalog_parse("solar:example", "D001.pdf", b"%PDF-1.4")
        self.assertEqual(runner.call_args.args[1], paths.PROFILES_DIR / "solar.json")


if __name__ == "__main__":
    unittest.main()
