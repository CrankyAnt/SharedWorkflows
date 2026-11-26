"""The command of Python tests on fixture projects: how the dependencies are installed and which test runner runs, with
its failures for a missing pytest and for no tests."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import unittest
import venv
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPOSITORY_ROOT / "actions" / "python-tests" / "python_tests.py"

sys.path.insert(0, str(SCRIPT.parent))
import python_tests  # noqa: E402
from tests import temporary_support


# A stand-in for a module that ``python -m <module>`` runs: it records its arguments and directory, then exits with the
# code that the environment names for it.
STAND_IN = """import json, os, sys
with open(os.environ["STAND_IN_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps({"module": "%(module)s", "argv": sys.argv[1:], "cwd": os.getcwd()}) + "\\n")
sys.exit(int(os.environ.get("STAND_IN_EXIT_%(variable)s", "0")))
"""
PASSING_TEST = "import unittest\n\n\nclass Passing(unittest.TestCase):\n    def test_passes(self):\n        self.assertTrue(True)\n"
FAILING_TEST = "import unittest\n\n\nclass Failing(unittest.TestCase):\n    def test_fails(self):\n        self.assertTrue(False)\n"


def write(root: Path, files: dict[str, str]) -> Path:
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
    return root


class Interpreter:
    """A virtual environment without pip that stands for the consumer's Python: no package the test machine holds is
    importable from it, and stand-ins for pip and pytest join it only through ``PYTHONPATH``."""

    @temporary_support.measured("python-tests-interpreter")
    def __init__(self, directory: Path) -> None:
        venv.EnvBuilder(with_pip=False, symlinks=os.name != "nt").create(directory / "venv")
        self.python = directory / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        self.stand_ins = directory / "stand-ins"
        self.log = directory / "stand-in.log"

    def stand_in(self, module: str) -> None:
        write(self.stand_ins, {f"{module}/__init__.py": "", f"{module}/__main__.py": STAND_IN % {"module": module, "variable": module.upper()}})

    def calls(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]

    def run(self, *arguments: str, exits: dict[str, int] | None = None) -> subprocess.CompletedProcess[str]:
        environment = {key: value for key, value in os.environ.items() if not key.startswith(("PYTHON", "STAND_IN_"))}
        environment.update({"PYTHONPATH": str(self.stand_ins), "STAND_IN_LOG": str(self.log),
                            **{f"STAND_IN_EXIT_{module.upper()}": str(code) for module, code in (exits or {}).items()}})
        return subprocess.run([str(self.python), str(SCRIPT), *arguments], capture_output=True, text=True, env=environment, check=False)


class Fixture(unittest.TestCase):
    interpreter: Interpreter

    @classmethod
    def setUpClass(cls) -> None:
        cls.shared = temporary_support.Directory()
        cls.interpreter = Interpreter(Path(cls.shared.name))

    @classmethod
    def tearDownClass(cls) -> None:
        cls.shared.cleanup()

    def setUp(self) -> None:
        temporary = temporary_support.Directory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.interpreter.log.unlink(missing_ok=True)
        temporary_support.remove_tree(self.interpreter.stand_ins)

    def project(self, files: dict[str, str], name: str = "project") -> Path:
        return write(self.root / name, files)


class InstallTests(Fixture):
    def test_an_extra_is_installed_from_the_working_directory(self) -> None:
        self.interpreter.stand_in("pip")
        project = self.project({"pyproject.toml": "[project]\nname = 'fixture'\n"})
        result = self.interpreter.run("install", "--root", str(project), "--install", "dev")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        (call,) = self.interpreter.calls()
        self.assertEqual("pip", call["module"])
        self.assertEqual(["install", "--disable-pip-version-check", ".[dev]"], call["argv"])
        self.assertEqual(project.resolve(), Path(call["cwd"]).resolve())
        self.assertIn("+ ", result.stdout, "the command is shown")

    def test_a_value_ending_in_txt_is_a_requirements_file_of_the_working_directory(self) -> None:
        self.interpreter.stand_in("pip")
        project = self.project({"requirements-dev.txt": "pytest\n"})
        result = self.interpreter.run("install", "--root", str(project), "--install", "requirements-dev.txt")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        (call,) = self.interpreter.calls()
        self.assertEqual(["install", "--disable-pip-version-check", "-r", "requirements-dev.txt"], call["argv"])
        self.assertEqual(project.resolve(), Path(call["cwd"]).resolve())

    def test_without_an_install_value_nothing_is_installed(self) -> None:
        self.interpreter.stand_in("pip")
        result = self.interpreter.run("install", "--root", str(self.project({"app.py": ""})), "--install", "")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual([], self.interpreter.calls())
        self.assertIn("nothing is installed", result.stdout)

    def test_a_failed_installation_fails_with_pip_s_exit_code(self) -> None:
        self.interpreter.stand_in("pip")
        result = self.interpreter.run("install", "--root", str(self.project({"app.py": ""})), "--install", "dev", exits={"pip": 1})
        self.assertEqual(1, result.returncode)

    def test_an_extra_name_outside_the_extra_name_rules_fails_before_pip_runs(self) -> None:
        self.interpreter.stand_in("pip")
        for value in ("dev] pytest", "-e", "../other"):
            with self.subTest(value=value):
                result = self.interpreter.run("install", "--root", str(self.project({"app.py": ""})), f"--install={value}")
                self.assertEqual(2, result.returncode)
                self.assertIn("no extra name", result.stderr)
        self.assertEqual([], self.interpreter.calls())

    def test_the_install_command_of_each_value(self) -> None:
        python = Path("python")
        self.assertIsNone(python_tests.install_command("", python))
        self.assertEqual([str(python), "-m", "pip", "install", "--disable-pip-version-check", "-r", "backend/requirements-dev.txt"],
                         python_tests.install_command("backend/requirements-dev.txt", python))
        self.assertEqual([str(python), "-m", "pip", "install", "--disable-pip-version-check", ".[test.extras_1]"],
                         python_tests.install_command("test.extras_1", python))

    def test_cache_key_changes_with_lock_or_dependency_file_contents_and_install_value(self) -> None:
        project = self.project({"pyproject.toml": "[project]\nname = 'a'\ndependencies = ['x==1']\n", "requirements-dev.txt": "pytest==8.0\n"})
        first = self.interpreter.run("cache-key", "--root", str(project), "--install", "dev")
        self.assertEqual(0, first.returncode, first.stderr)
        self.assertTrue(first.stdout.strip().startswith("pip-py"))
        same = self.interpreter.run("cache-key", "--root", str(project), "--install", "dev")
        self.assertEqual(first.stdout.strip(), same.stdout.strip())
        other_install = self.interpreter.run("cache-key", "--root", str(project), "--install", "requirements-dev.txt")
        self.assertNotEqual(first.stdout.strip(), other_install.stdout.strip())
        (project / "pyproject.toml").write_text("[project]\nname = 'a'\ndependencies = ['x==2']\n", encoding="utf-8")
        changed = self.interpreter.run("cache-key", "--root", str(project), "--install", "dev")
        self.assertNotEqual(first.stdout.strip(), changed.stdout.strip())


class PytestConfigurationTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = temporary_support.Directory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def configures(self, files: dict[str, str]) -> bool:
        for path in self.root.iterdir():
            path.unlink()
        return python_tests.pytest_configured(write(self.root, files))

    def test_each_place_that_configures_pytest(self) -> None:
        for name, files in (("pyproject", {"pyproject.toml": "[project]\nname = 'x'\n\n[tool.pytest.ini_options]\naddopts = '-q'\n"}),
                            ("pytest.ini", {"pytest.ini": ""}), ("tox.ini", {"tox.ini": "[tox]\nenvlist = py\n\n[pytest]\naddopts = -q\n"}),
                            ("setup.cfg", {"setup.cfg": "[metadata]\nname = x\n\n[tool:pytest]\naddopts = -q\n"}),
                            ("conftest.py", {"conftest.py": ""}),
                            ("native pyproject table", {"pyproject.toml": "[project]\nname = 'x'\n\n[tool.pytest]\ntestpaths = ['tests']\n"}),
                            ("pytest.toml", {"pytest.toml": "[pytest]\n"}), (".pytest.toml", {".pytest.toml": ""}), (".pytest.ini", {".pytest.ini": ""})):
            with self.subTest(configuration=name):
                self.assertTrue(self.configures(files))

    def test_files_that_do_not_configure_pytest(self) -> None:
        for name, files in (("nothing", {}), ("pyproject without the table", {"pyproject.toml": "[tool.ruff]\nline-length = 100\n"}),
                            ("tox.ini without the section", {"tox.ini": "[tox]\nenvlist = py\n"}),
                            ("setup.cfg with pytest's tox section", {"setup.cfg": "[pytest]\naddopts = -q\n"}),
                            ("conftest.py below the root", {"tests/conftest.py": ""})):
            with self.subTest(configuration=name):
                for path in sorted(self.root.rglob("*"), reverse=True):
                    path.rmdir() if path.is_dir() else path.unlink()
                self.assertFalse(python_tests.pytest_configured(write(self.root, files)))

    def test_a_pyproject_that_is_no_toml_fails(self) -> None:
        with self.assertRaises(python_tests.ProjectError):
            self.configures({"pyproject.toml": "[tool.pytest.ini_options\n"})


class PytestRunTests(Fixture):
    PYPROJECT = {"pyproject.toml": "[project]\nname = 'fixture'\n\n[tool.pytest.ini_options]\ntestpaths = ['tests']\n"}

    def test_a_project_that_configures_pytest_runs_pytest_in_the_working_directory(self) -> None:
        self.interpreter.stand_in("pytest")
        project = self.project(self.PYPROJECT)
        result = self.interpreter.run("test", "--root", str(project))
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        (call,) = self.interpreter.calls()
        self.assertEqual(("pytest", []), (call["module"], call["argv"]))
        self.assertEqual(project.resolve(), Path(call["cwd"]).resolve())

    def test_failing_tests_fail_the_command(self) -> None:
        self.interpreter.stand_in("pytest")
        result = self.interpreter.run("test", "--root", str(self.project(self.PYPROJECT)), exits={"pytest": 1})
        self.assertEqual(1, result.returncode)

    def test_no_collected_tests_fail_with_a_message_naming_the_root(self) -> None:
        self.interpreter.stand_in("pytest")
        project = self.project(self.PYPROJECT, name="backend")
        result = self.interpreter.run("test", "--root", str(project), exits={"pytest": 5})
        self.assertEqual(1, result.returncode)
        self.assertIn(f"pytest found no tests in {project}", result.stderr)

    def test_a_project_that_configures_pytest_without_pytest_installed_fails_with_a_message_that_says_so(self) -> None:
        project = self.project({"conftest.py": ""})
        result = self.interpreter.run("test", "--root", str(project))
        self.assertEqual(1, result.returncode)
        self.assertIn("pytest is not installed", result.stderr)
        self.assertIn(str(project), result.stderr)
        self.assertEqual([], self.interpreter.calls())


class UnittestRunTests(Fixture):
    def test_a_project_without_pytest_configuration_runs_unittest_discover_in_the_working_directory(self) -> None:
        self.interpreter.stand_in("pytest")
        project = self.project({"tests/__init__.py": "", "tests/test_passing.py": PASSING_TEST})
        result = self.interpreter.run("test", "--root", str(project))
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("-m unittest discover", result.stdout)
        self.assertIn("Ran 1 test", result.stdout)
        self.assertEqual([], self.interpreter.calls(), "pytest does not run for a project that does not configure it")

    def test_failing_tests_fail_the_command(self) -> None:
        result = self.interpreter.run("test", "--root", str(self.project({"test_failing.py": FAILING_TEST})))
        self.assertEqual(1, result.returncode)
        self.assertIn("FAILED", result.stdout)

    def test_no_discovered_tests_fail_with_a_message_naming_the_root(self) -> None:
        project = self.project({"app.py": "print(1)\n"}, name="probe")
        result = self.interpreter.run("test", "--root", str(project))
        self.assertEqual(1, result.returncode)
        self.assertIn(f"unittest found no tests in {project}", result.stderr)

    def test_a_run_of_no_tests_counts_by_its_exit_code_or_its_report_line(self) -> None:
        self.assertTrue(python_tests.no_tests("unittest", 5, False))
        self.assertTrue(python_tests.no_tests("unittest", 0, True), "an interpreter before 3.12 ends a run of no tests with 0")
        self.assertFalse(python_tests.no_tests("unittest", 0, False))
        self.assertTrue(python_tests.no_tests("pytest", 5, False))
        self.assertFalse(python_tests.no_tests("pytest", 1, False))
        self.assertTrue(python_tests.NO_TESTS_LINE.match("Ran 0 tests in 0.000s\n"))
        self.assertFalse(python_tests.NO_TESTS_LINE.match("Ran 10 tests in 0.000s\n"))


class CommandLineTests(unittest.TestCase):
    def test_each_subcommand_names_its_root(self) -> None:
        for command in ("install", "test"):
            with self.subTest(command=command), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                python_tests.parse_arguments([command])

    def test_the_help_names_the_three_subcommands(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(SystemExit):
            python_tests.parse_arguments(["--help"])
        for command in ("install", "cache-key", "test"):
            self.assertIn(command, output.getvalue())
        self.assertNotIn("decide", output.getvalue())


if __name__ == "__main__":
    unittest.main()
