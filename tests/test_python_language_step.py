"""The shared Python language step's command on fixture Python projects: ruff check with the project's configuration
or ruff's default rules, mypy only where the project configures it, and the optional ruff format check. The pattern
that recognises a shown ruff command is checked on fixed command lines too, since a runner shows only its own spelling
of the executable."""

from __future__ import annotations

import importlib.util
import io
import os
import re
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from tests import temporary_support
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


def shown_ruff_pattern(subcommand: str) -> str:
    """The pattern of a shown ruff command with this subcommand: ruff found on PATH keeps the case of its extension
    as PATHEXT spells it, such as ruff.EXE."""
    return rf"(?m)^\+ (?:.*[\\/])?ruff(?i:\.exe)? {re.escape(subcommand)}(?= |$)"


EXECUTABLES = ("ruff", "ruff.exe", "ruff.EXE")


class ShownRuffCommandTests(unittest.TestCase):
    def test_the_executable_matches_with_or_without_an_extension_of_any_case(self) -> None:
        for executable in (*EXECUTABLES, "ruff.Exe", "/opt/tools/ruff", r"C:\tools\ruff.EXE"):
            with self.subTest(executable=executable):
                self.assertRegex(f"+ {executable} check --no-cache .", shown_ruff_pattern("check"))
                self.assertRegex(f"+ {executable} format --check --no-cache .", shown_ruff_pattern("format --check"))
                self.assertRegex(f"+ {executable} format --check --no-cache .", shown_ruff_pattern("format"))

    def test_another_subcommand_does_not_match(self) -> None:
        for executable in EXECUTABLES:
            with self.subTest(executable=executable):
                self.assertNotRegex(f"+ {executable} format --check --no-cache .", shown_ruff_pattern("check"))
                self.assertNotRegex(f"+ {executable} check --no-cache .", shown_ruff_pattern("format --check"))
                self.assertNotRegex(f"+ {executable} format --no-cache .", shown_ruff_pattern("format --check"))
                self.assertNotRegex(f"+ {executable} checker .", shown_ruff_pattern("check"))
                self.assertNotRegex(f"+ {executable}x check .", shown_ruff_pattern("check"))
                self.assertNotRegex(f"+ {executable}.bat check .", shown_ruff_pattern("check"))

    def test_a_shown_format_command_fails_the_assertion_that_no_format_check_runs(self) -> None:
        for executable in EXECUTABLES:
            with self.subTest(executable=executable), self.assertRaises(AssertionError):
                self.assertNotRegex(f"+ {executable} check --no-cache .\n+ {executable} format --check --no-cache .\n", shown_ruff_pattern("format"))


class FixtureProject(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = temporary_support.Directory()
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
        self.assertRegex(result.stdout, shown_ruff_pattern("check"), "the command is shown")
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
        self.assertNotRegex(result.stdout, shown_ruff_pattern("format"))
        result = step(project, "--format-check")
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertRegex(result.stdout, shown_ruff_pattern("format --check"))
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

    def test_a_root_below_the_repository_runs_ruff_and_mypy_from_there_with_its_own_mypy_configuration(self) -> None:
        repository = self.project("repository", {"outside.py": UNUSED_IMPORT, "backend/mypy.ini": "[mypy]\n", "backend/app.py": TYPE_ERROR})
        directory = tools_directory()
        options = ["--tools", str(directory)] if directory is not None else []

        def from_repository(*arguments: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run([sys.executable, str(STEP), *arguments, *options], cwd=repository, capture_output=True, text=True, check=False)

        default = from_repository()
        self.assertEqual(1, default.returncode, default.stdout + default.stderr)
        self.assertIn("F401", default.stdout, "without --root the step checks the repository root")
        self.assertNotRegex(default.stdout, r"(?m)^\+ .*mypy", "the repository root configures no mypy")
        backend = from_repository("--root", "backend")
        self.assertEqual(1, backend.returncode, backend.stdout + backend.stderr)
        self.assertNotIn("F401", backend.stdout, "ruff checks only the root's directory")
        self.assertRegex(backend.stdout, r"(?m)^\+ .*mypy")
        self.assertIn("Incompatible types in assignment", backend.stdout)

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

    def test_print_cache_key_reflects_whether_mypy_is_configured(self) -> None:
        without_mypy = self.project("no-mypy", {"app.py": CLEAN})
        with_mypy = self.project("with-mypy", {"app.py": CLEAN, "mypy.ini": "[mypy]\n"})
        key_a = subprocess.run([sys.executable, str(STEP), "--root", str(without_mypy), "--print-cache-key", str(PINS)],
                               capture_output=True, text=True, check=False)
        key_b = subprocess.run([sys.executable, str(STEP), "--root", str(with_mypy), "--print-cache-key", str(PINS)],
                               capture_output=True, text=True, check=False)
        self.assertEqual(0, key_a.returncode, key_a.stderr)
        self.assertEqual(0, key_b.returncode, key_b.stderr)
        self.assertTrue(key_a.stdout.strip().startswith("tools-"))
        self.assertNotEqual(key_a.stdout.strip(), key_b.stdout.strip())

    def test_print_digest_matches_recording_digest_for_python_language_step(self) -> None:
        import json

        digest_run = subprocess.run([sys.executable, str(STEP), "--print-digest"], capture_output=True, text=True, check=False)
        self.assertEqual(0, digest_run.returncode, digest_run.stderr)
        step_digest = digest_run.stdout.strip()
        self.assertRegex(step_digest, r"^[0-9a-f]{64}$")
        recording_run = subprocess.run(
            [sys.executable, str(REPOSITORY_ROOT / "actions" / "recording" / "recording.py"), "digest"],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(0, recording_run.returncode, recording_run.stderr)
        (raw,) = re.findall(r"^shared CI digests (\{[^\r\n]+\})$", recording_run.stdout, re.MULTILINE)
        self.assertEqual(json.loads(raw)["language:python"], step_digest)


class MissingToolTests(FixtureProject):
    def test_a_missing_tool_is_a_failure(self) -> None:
        empty = self.root / "empty-tools"
        empty.mkdir()
        result = step(self.project("app", {"app.py": CLEAN}), tools=empty)
        self.assertEqual(2, result.returncode, result.stdout + result.stderr)
        self.assertIn("ruff is missing from", result.stderr)


class MalformedConfigurationTests(FixtureProject):
    def test_a_malformed_type_checker_configuration_fails_with_a_message_instead_of_a_traceback(self) -> None:
        empty = self.root / "empty-tools"
        empty.mkdir()
        for name, filename, content in (
            ("broken-pyproject", "pyproject.toml", "[tool.mypy\nstrict = true\n"),
            ("non-table-tool", "pyproject.toml", 'tool = "not-a-table"\n'),
            ("broken-mypy-ini", "mypy.ini", "not a section header\n"),
            ("broken-dot-mypy-ini", ".mypy.ini", "[mypy]\n[mypy]\n"),
            ("broken-setup-cfg", "setup.cfg", "not a section header\n"),
        ):
            with self.subTest(case=name):
                project = self.project(name, {"app.py": CLEAN, filename: content})
                result = step(project, tools=empty)
                if name == "non-table-tool":
                    # Valid TOML without a [tool.mypy] table: does not crash, reaches tool lookup cleanly.
                    self.assertNotIn("Traceback", result.stderr)
                    continue
                self.assertEqual(2, result.returncode, result.stdout + result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                self.assertIn("error:", result.stderr)
                self.assertIn(filename, result.stderr)


if __name__ == "__main__":
    unittest.main()
