"""The Windows launcher must validate the package layout, not removed root scripts.

``run_server.ps1`` used to look for ``server.py``, ``passport.py`` and ``db.py``
beside itself. Those modules moved to ``src/viniti``, so the launcher reported a
missing repository even in a correct checkout. These tests guard the package
layout check and the ``python -m viniti`` entrypoint, then run the preflight
against an explicit temporary database.
"""
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
LAUNCHER = ROOT / "run_server.ps1"
BATCH = ROOT / "run_server.bat"
POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")


def run_launcher(script, *arguments, env=None, timeout=180):
    return subprocess.run(
        [
            POWERSHELL,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            *arguments,
        ],
        cwd=str(ROOT),
        capture_output=True,
        timeout=timeout,
        env=env,
    )


class LauncherSourceTest(unittest.TestCase):
    def test_launcher_validates_the_package_layout(self):
        source = LAUNCHER.read_text(encoding="utf-8")
        self.assertIn("pyproject.toml", source)
        self.assertIn(r"src\viniti\cli.py", source)
        self.assertIn("не найден корень репозитория", source)

    def test_launcher_uses_the_package_entrypoint(self):
        source = LAUNCHER.read_text(encoding="utf-8")
        for command in (
            "-m viniti passport --check",
            "-m viniti db --init",
            "-m viniti db --stats",
            "-m viniti server --port",
        ):
            with self.subTest(command=command):
                self.assertIn(command, source)

    def test_launcher_invokes_no_removed_root_scripts(self):
        source = LAUNCHER.read_text(encoding="utf-8")
        for stale in ("server.py", "passport.py", "db.py"):
            with self.subTest(stale=stale):
                self.assertNotIn(stale, source)

    def test_launcher_reports_an_editable_install_instead_of_installing(self):
        source = LAUNCHER.read_text(encoding="utf-8")
        self.assertIn("не видит установленный пакет viniti", source)
        self.assertIn("-m pip install -e .", source)
        self.assertNotIn("-m pip install -r requirements.txt", source)
        # The install command is printed for the user, never executed by the launcher.
        self.assertNotIn("& $python -m pip", source)

    def test_launcher_has_no_machine_specific_interpreter_path(self):
        source = LAUNCHER.read_text(encoding="utf-8")
        self.assertNotIn("C:\\Users\\", source)
        self.assertNotIn("Desktop", source)

    def test_batch_comments_describe_the_package_commands(self):
        source = BATCH.read_text(encoding="ascii")
        self.assertIn("python -m viniti passport --check", source)
        self.assertIn("python -m viniti db --init --stats", source)
        self.assertIn("python -m viniti server", source)
        for stale in ("server.py", "passport.py", "db.py"):
            with self.subTest(stale=stale):
                self.assertNotIn(stale, source)

    def test_launchers_are_not_git_ignored(self):
        ignored = subprocess.run(
            ["git", "check-ignore", "run_server.bat", "run_server.ps1"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
        )
        self.assertEqual(ignored.stdout.strip(), "",
                         "tracked launchers must not be ignored: %s" % ignored.stdout)


@unittest.skipUnless(os.name == "nt" and POWERSHELL,
                     "the launcher is a Windows PowerShell script")
class LauncherPreflightTest(unittest.TestCase):
    def launcher_environment(self):
        env = dict(os.environ)
        # The launcher only checks that the chosen interpreter can import the
        # package. CI installs it editable; adding src/ keeps the test focused
        # on launcher logic instead of the pip state of the test interpreter.
        env["PYTHONPATH"] = os.pathsep.join(
            part for part in (str(ROOT / "src"), env.get("PYTHONPATH", "")) if part)
        return env

    def test_check_uses_an_explicit_database_and_does_not_start_the_server(self):
        with tempfile.TemporaryDirectory(prefix="viniti-launcher-") as name:
            database = pathlib.Path(name) / "preflight.sqlite3"
            completed = run_launcher(
                LAUNCHER,
                "-Check",
                "-NoPause",
                "-Python",
                sys.executable,
                "-Data",
                str(database),
                env=self.launcher_environment(),
            )
            output = completed.stdout.decode("utf-8", "replace")
            errors = completed.stderr.decode("utf-8", "replace")

            self.assertEqual(completed.returncode, 0, output + errors)
            self.assertIn("passport.json совпадает", output)
            self.assertIn("Проверки пройдены", output)
            self.assertNotIn("== python -m viniti server", output)
            self.assertTrue(database.is_file(), "the explicit database was not created")
            self.assertGreater(database.stat().st_size, 0)

    def test_viniti_python_environment_variable_selects_the_interpreter(self):
        with tempfile.TemporaryDirectory(prefix="viniti-launcher-") as name:
            database = pathlib.Path(name) / "env.sqlite3"
            environment = self.launcher_environment()
            environment["VINITI_PYTHON"] = sys.executable
            completed = run_launcher(
                LAUNCHER,
                "-Check",
                "-NoPause",
                "-Data",
                str(database),
                env=environment,
            )
            output = completed.stdout.decode("utf-8", "replace")
            errors = completed.stderr.decode("utf-8", "replace")

            self.assertEqual(completed.returncode, 0, output + errors)
            expected = os.path.normcase(os.path.abspath(sys.executable))
            self.assertIn(expected, os.path.normcase(output))

    def test_a_directory_without_the_package_layout_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="viniti-launcher-") as name:
            copied = pathlib.Path(name) / "run_server.ps1"
            shutil.copy2(LAUNCHER, copied)
            completed = run_launcher(copied, "-Check", "-NoPause")
            output = completed.stdout.decode("utf-8", "replace")
            errors = completed.stderr.decode("utf-8", "replace")

            self.assertEqual(completed.returncode, 2, output + errors)
            self.assertIn("не найден корень репозитория", output)
            self.assertIn(r"src\viniti\cli.py", output)
            self.assertNotIn("server.py", output)


if __name__ == "__main__":
    unittest.main()
