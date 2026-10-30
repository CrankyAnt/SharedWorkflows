"""The shared Python language step's command on fixture Python projects: ruff check with the project's configuration
or ruff's default rules, mypy only where the project configures it, and the optional ruff format check."""

from __future__ import annotations

import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from tests.hygiene_fixtures import REPOSITORY_ROOT, tools_available, tools_directory

STEP = REPOSITORY_ROOT / "actions" / "python-language-step" / "python_language_step.py"
PINS = REPOSITORY_ROOT / "actions" / "repository-hygiene" / "pins.json"
UNUSED_IMPORT = "import os\n"
CLEAN = "print(1)\n"


def write(root: Path, files: dict[str, str]) -> Path:
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
    return root


def step(root: Path, *arguments: str, tools: Path | None = None) -> subprocess.CompletedProcess[str]:
    directory = tools if tools is not None else tools_directory()
    options = ["--tools", str(directory)] if directory is not None else []
    return subprocess.run([sys.executable, str(STEP), "--root", str(root), *options, *arguments], capture_output=True, text=True, check=False)


class FixtureProject(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def project(self, name: str, files: dict[str, str]) -> Path:
        return write(self.root / name, files)


@unittest.skipUnless(tools_available("ruff"), "the pinned ruff is neither in HYGIENE_TOOLS nor on PATH")
class RuffCheckTests(FixtureProject):
    def test_without_a_ruff_configuration_ruff_s_default_rules_apply(self) -> None:
        result = step(self.project("defaults", {"app.py": UNUSED_IMPORT}))
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn("F401", result.stdout)
        self.assertRegex(result.stdout, r"(?m)^\+ .*ruff(?:\.exe)? check", "the command is shown")
        clean = step(self.project("clean", {"app.py": CLEAN}))
        self.assertEqual(0, clean.returncode, clean.stdout + clean.stderr)
        self.assertFalse((self.root / "clean" / ".ruff_cache").exists(), "ruff writes no cache into the checkout")

    def test_the_project_s_ruff_configuration_sets_the_rules(self) -> None:
        for name, files in (("ruff-toml", {"ruff.toml": '[lint]\nselect = ["E9"]\n'}),
                            ("pyproject", {"pyproject.toml": '[tool.ruff.lint]\nselect = ["E9"]\n'})):
            with self.subTest(configuration=name):
                result = step(self.project(name, {**files, "app.py": UNUSED_IMPORT}))
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        broken = step(self.project("syntax", {"ruff.toml": '[lint]\nselect = ["E9"]\n', "app.py": "def broken(:\n"}))
        self.assertEqual(1, broken.returncode, broken.stdout + broken.stderr)

    def test_the_format_check_runs_only_when_asked_for(self) -> None:
        project = self.project("unformatted", {"app.py": "print( 1 )\n"})
        result = step(project)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertNotRegex(result.stdout, r"(?m)^\+ .*ruff(?:\.exe)? format")
        result = step(project, "--format-check")
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r"(?m)^\+ .*ruff(?:\.exe)? format --check")
        self.assertEqual("print( 1 )\n", (project / "app.py").read_text(encoding="utf-8"), "the check changes no file")
        formatted = step(self.project("formatted", {"app.py": "print(1)\n"}), "--format-check")
        self.assertEqual(0, formatted.returncode, formatted.stdout + formatted.stderr)


TYPE_ERROR = 'count: int = "one"\n'


@unittest.skipUnless(tools_available("ruff", "mypy"), "the pinned ruff and mypy are not both in HYGIENE_TOOLS or on PATH")
class MypyTests(FixtureProject):
    def test_mypy_does_not_run_without_a_mypy_configuration(self) -> None:
        result = step(self.project("unconfigured", {"app.py": TYPE_ERROR, "pyproject.toml": '[tool.ruff]\n'}))
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertNotRegex(result.stdout, r"(?m)^\+ .*mypy")

    def test_mypy_runs_where_the_project_configures_it(self) -> None:
        for name, files in (("mypy-ini", {"mypy.ini": "[mypy]\n"}), ("dot-mypy-ini", {".mypy.ini": "[mypy]\n"}),
                            ("pyproject", {"pyproject.toml": "[tool.mypy]\nstrict = false\n"}), ("setup-cfg", {"setup.cfg": "[mypy]\n"}),
                            ("files", {"mypy.ini": "[mypy]\nfiles = src\n", "elsewhere.py": TYPE_ERROR})):
            with self.subTest(configuration=name):
                project = self.project(name, {**files, "src/app.py": TYPE_ERROR})
                result = step(project)
                self.assertEqual(1, result.returncode, result.stdout + result.stderr)
                self.assertRegex(result.stdout, r"(?m)^\+ .*mypy")
                self.assertIn("Incompatible types in assignment", result.stdout)
        result = step(self.project("files-clean", {"mypy.ini": "[mypy]\nfiles = src\n", "src/app.py": CLEAN, "elsewhere.py": TYPE_ERROR}))
        self.assertEqual(0, result.returncode, "a configured files list sets what mypy checks: " + result.stdout + result.stderr)

    def test_every_check_runs_when_one_fails(self) -> None:
        result = step(self.project("both", {"mypy.ini": "[mypy]\n", "app.py": UNUSED_IMPORT + TYPE_ERROR}))
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn("F401", result.stdout)
        self.assertIn("Incompatible types in assignment", result.stdout)


class InstallTests(FixtureProject):
    def installed(self, files: dict[str, str]) -> list[str]:
        spec = importlib.util.spec_from_file_location("python_language_step", STEP)
        module = importlib.util.module_from_spec(spec)
        sys.modules["python_language_step"] = module
        self.addCleanup(sys.modules.pop, "python_language_step", None)
        spec.loader.exec_module(module)
        project = self.project("install", files)
        tools = self.root / "tools"
        with mock.patch.object(module.install_tools, "install", return_value=[]) as install, redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            module.main(["--root", str(project), "--tools", str(tools), "--install", str(PINS)])
        install.assert_called_once()
        pins, platform, destination = install.call_args.args[:3]
        self.assertIn("ruff", pins["python"], "the pins file is the shared one")
        self.assertEqual("windows-x64" if os.name == "nt" else "linux-x64", platform)
        self.assertEqual(tools, destination)
        return install.call_args.kwargs["tools"]

    def test_install_takes_ruff_from_the_shared_pins_and_mypy_only_where_the_project_configures_it(self) -> None:
        self.assertEqual(["ruff"], self.installed({"app.py": CLEAN}))
        self.assertEqual(["ruff", "mypy"], self.installed({"app.py": CLEAN, "mypy.ini": "[mypy]\n"}))


class MissingToolTests(FixtureProject):
    def test_a_missing_tool_is_a_failure(self) -> None:
        empty = self.root / "empty-tools"
        empty.mkdir()
        result = step(self.project("app", {"app.py": CLEAN}), tools=empty)
        self.assertEqual(2, result.returncode, result.stdout + result.stderr)
        self.assertIn("ruff is missing from", result.stderr)


if __name__ == "__main__":
    unittest.main()
