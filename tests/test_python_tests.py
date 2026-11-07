"""The command of Python tests on fixture repositories and projects: whether the tests run, how the dependencies are
installed and which test runner runs, with its failures for a missing pytest and for no tests."""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import venv
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPOSITORY_ROOT / "actions" / "python-tests" / "python_tests.py"

sys.path.insert(0, str(SCRIPT.parent))
import python_tests  # noqa: E402

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


def git(root: Path, *arguments: str) -> str:
    result = subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.com", "-c", "commit.gpgsign=false",
                             *arguments], cwd=root, capture_output=True, text=True, check=True)
    return result.stdout.strip()


class Interpreter:
    """A virtual environment without pip that stands for the consumer's Python: no package the test machine holds is
    importable from it, and stand-ins for pip and pytest join it only through ``PYTHONPATH``."""

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
        cls.shared = tempfile.TemporaryDirectory()
        cls.interpreter = Interpreter(Path(cls.shared.name))

    @classmethod
    def tearDownClass(cls) -> None:
        cls.shared.cleanup()

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.interpreter.log.unlink(missing_ok=True)
        shutil.rmtree(self.interpreter.stand_ins, ignore_errors=True)

    def project(self, files: dict[str, str], name: str = "project") -> Path:
        return write(self.root / name, files)


class DecideFixture(unittest.TestCase):
    """A fixture repository with a base commit on ``main`` and a branch ``change``."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "repository"
        self.root.mkdir()
        git(self.root, "init", "-q", "-b", "main")
        write(self.root, {"README.md": "# Fixture\n", "app.py": "print(1)\n", "docs/guide.md": "Guide\n"})
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "base")
        self.base = git(self.root, "rev-parse", "HEAD")
        git(self.root, "checkout", "-q", "-b", "change")

    def commit(self, files: dict[str, str], removed: tuple[str, ...] = (), renamed: tuple[tuple[str, str], ...] = ()) -> str:
        write(self.root, files)
        for path in removed:
            git(self.root, "rm", "-q", path)
        for old, new in renamed:
            git(self.root, "mv", old, new)
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "--allow-empty", "-m", "change")
        return git(self.root, "rev-parse", "HEAD")

    def decide(self, *arguments: str) -> tuple[subprocess.CompletedProcess[str], dict[str, str], str]:
        output = self.root.parent / "github-output"
        summary = self.root.parent / "summary.md"
        output.unlink(missing_ok=True)
        summary.unlink(missing_ok=True)
        result = subprocess.run([sys.executable, str(SCRIPT), "decide", "--root", str(self.root), *arguments, "--github-output", str(output),
                                 "--summary", str(summary)], capture_output=True, text=True, check=False)
        values = dict(line.split("=", 1) for line in output.read_text(encoding="utf-8").splitlines()) if output.exists() else {}
        return result, values, summary.read_text(encoding="utf-8") if summary.exists() else ""

    def lookup(self, document: dict) -> str:
        path = self.root.parent / "push-lookup.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        return str(path)


class DecideOnAPullRequestTests(DecideFixture):
    def pull_request(self, head: str) -> tuple[subprocess.CompletedProcess[str], dict[str, str], str]:
        return self.decide("--base", self.base, "--head", head, "--compare", "merge-base")

    def test_a_pull_request_whose_changed_files_all_end_in_md_in_any_case_skips_the_tests(self) -> None:
        head = self.commit({"README.md": "# Changed\n", "docs/NOTES.MD": "Notes\n", "CHANGELOG.Md": "Log\n"}, removed=("docs/guide.md",))
        result, values, summary = self.pull_request(head)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual({"run": "false"}, values)
        self.assertIn("every changed file ends in `.md`", result.stdout)
        self.assertIn("every changed file ends in `.md`", summary)

    def test_a_pull_request_with_any_other_changed_file_runs_the_tests(self) -> None:
        for name, files in (("python", {"app.py": "print(2)\n"}), ("markdown and python", {"README.md": "# Changed\n", "app.py": "print(2)\n"}),
                            ("workflow", {".github/workflows/python-tests.yml": "name: Python tests\n"}), ("markdown-like name", {"notes.mdx": "x\n"})):
            with self.subTest(change=name):
                git(self.root, "checkout", "-q", "-B", "change", self.base)
                result, values, summary = self.pull_request(self.commit(files))
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertEqual({"run": "true"}, values)
                self.assertIn("is no Markdown file", summary)

    def test_a_renamed_file_counts_under_its_old_path(self) -> None:
        result, values, _ = self.pull_request(self.commit({}, renamed=(("app.py", "app.md"),)))
        self.assertEqual({"run": "true"}, values, result.stdout)
        self.assertIn("`app.py`", result.stdout)

    def test_a_pull_request_compares_from_the_merge_base_so_a_newer_base_branch_adds_nothing(self) -> None:
        head = self.commit({"README.md": "# Changed\n"})
        git(self.root, "checkout", "-q", "main")
        newer_base = self.commit({"app.py": "print(3)\n"})
        _, values, _ = self.decide("--base", newer_base, "--head", head, "--compare", "merge-base")
        self.assertEqual({"run": "false"}, values)

    def test_changed_files_that_cannot_be_determined_or_an_empty_change_run_the_tests(self) -> None:
        head = self.commit({"README.md": "# Changed\n"})
        for name, base, reason in (("empty base", "", "could not be determined"), ("all-zero base", "0" * 40, "could not be determined"),
                                   ("absent base", "1" * 40, "could not be determined"), ("no change", head, "no file changed")):
            with self.subTest(case=name):
                result, values, _ = self.decide("--base", base, "--head", head, "--compare", "direct")
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertEqual({"run": "true"}, values)
                self.assertIn(reason, result.stdout)


class DecideOnAPushTests(DecideFixture):
    def test_a_push_whose_tree_passed_on_its_pull_request_skips_the_tests(self) -> None:
        head = self.commit({"app.py": "print(2)\n"})
        lookup = self.lookup({"case": "identical", "head": head, "landing_base": self.base, "pull_request": 7,
                              "reason": "the tree equals the tested tree"})
        result, values, summary = self.decide("--lookup", lookup)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual({"run": "false"}, values)
        self.assertIn("passed `tests / tests` on pull request #7", result.stdout)
        self.assertIn("Push lookup: `identical`", summary)

    def test_any_other_push_decides_over_the_landing_base_to_the_pushed_commit(self) -> None:
        markdown = self.commit({"README.md": "# Changed\n"})
        for case in ("pull-request", "own", "behind"):
            with self.subTest(case=case, change="markdown"):
                document = {"case": case, "head": markdown, "landing_base": self.base, "tested_base": self.base}
                self.assertEqual({"run": "false"}, self.decide("--lookup", self.lookup(document))[1])
        python = self.commit({"app.py": "print(2)\n"})
        for case in ("pull-request", "own", "behind"):
            with self.subTest(case=case, change="python"):
                document = {"case": case, "head": python, "landing_base": markdown, "tested_base": self.base}
                self.assertEqual({"run": "true"}, self.decide("--lookup", self.lookup(document))[1])

    def test_a_push_without_a_landing_base_runs_the_tests(self) -> None:
        head = self.commit({"README.md": "# Changed\n"})
        result, values, _ = self.decide("--lookup", self.lookup({"case": "own", "head": head, "landing_base": None}))
        self.assertEqual({"run": "true"}, values)
        self.assertIn("could not be determined", result.stdout)

    def test_a_lookup_that_cannot_be_read_fails_and_writes_no_decision(self) -> None:
        for name, content in (("missing", None), ("no case", '{"head": "abc"}'), ("not json", "{")):
            with self.subTest(lookup=name):
                path = self.root.parent / "broken-lookup.json"
                path.unlink(missing_ok=True)
                if content is not None:
                    path.write_text(content, encoding="utf-8")
                result, values, _ = self.decide("--lookup", str(path))
                self.assertEqual(2, result.returncode)
                self.assertIn("push lookup", result.stderr)
                self.assertEqual({}, values)

    def test_the_decision_takes_either_commits_or_the_lookup(self) -> None:
        for arguments in ((), ("--base", "x"), ("--lookup", "x", "--base", "x", "--head", "x")):
            with self.subTest(arguments=arguments), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                python_tests.parse_arguments(["decide", *arguments])


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


class PytestConfigurationTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
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
                            ("conftest.py", {"conftest.py": ""})):
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
        for command in ("decide", "install", "test"):
            self.assertIn(command, output.getvalue())


if __name__ == "__main__":
    unittest.main()
