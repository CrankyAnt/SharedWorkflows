"""The script of the part dotnet-test, started as a process with a stand-in for the ``dotnet`` command: it builds the
configured project and then runs its tests, the exit codes of the two commands decide its own, and a run that executed
no test fails."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PART_FOLDER = REPOSITORY_ROOT / "actions" / "dotnet-test"
SCRIPT = PART_FOLDER / "dotnet_test.py"
PROJECT = "tests/Shop.Tests/Shop.Tests.csproj"

# The stand-in appends its working directory and its arguments to the log and ends with the exit code that the
# environment names for its first argument: STAND_IN_BUILD for ``dotnet build``, STAND_IN_TEST for ``dotnet test``.
# ``dotnet test`` writes one TRX report per number in STAND_IN_EXECUTED, the executed tests of one test assembly, into
# the directory after ``--results-directory``; STAND_IN_REPORT replaces the text of each report.
STAND_IN = """\
import os, sys
arguments = sys.argv[1:]
shown = ["<results>" if index and arguments[index - 1] == "--results-directory" else item for index, item in enumerate(arguments)]
with open(os.environ["STAND_IN_LOG"], "a", encoding="utf-8") as log:
    log.write(os.getcwd() + "|" + " ".join(shown) + "|" + os.environ.get("DOTNET_NOLOGO", "") + os.environ.get("DOTNET_CLI_TELEMETRY_OPTOUT", "") + "\\n")
print("stand-in output of dotnet " + arguments[0])
if arguments[0] == "test" and "--results-directory" in arguments:
    results = arguments[arguments.index("--results-directory") + 1]
    for number, executed in enumerate(os.environ.get("STAND_IN_EXECUTED", "1").split()):
        report = ('<?xml version="1.0" encoding="utf-8"?><TestRun xmlns="http://microsoft.com/schemas/VisualStudio/TeamTest/2010">'
                  '<ResultSummary outcome="Completed"><Counters total="%s" executed="%s" passed="%s" failed="0" /></ResultSummary></TestRun>' % (executed, executed, executed))
        os.makedirs(os.path.join(results, "nested"), exist_ok=True)
        with open(os.path.join(results, "nested", "report-%d.trx" % number), "w", encoding="utf-8") as stream:
            stream.write(os.environ.get("STAND_IN_REPORT", report))
sys.exit(int(os.environ.get("STAND_IN_" + arguments[0].upper(), "0")))
"""
BUILD = f"build {PROJECT}|11"
TEST = f"test {PROJECT} --no-build --logger trx --results-directory <results>|11"


class DotnetTestFixture(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name).resolve()
        self.repository = self.directory / "consumer"
        (self.repository / PROJECT).parent.mkdir(parents=True)
        (self.repository / PROJECT).write_text("<Project />\n", encoding="utf-8")
        self.tools = self.directory / "tools"
        self.tools.mkdir()
        self.temporary = self.directory / "runner-temp"
        self.temporary.mkdir()
        self.log = self.directory / "dotnet.log"
        (self.tools / "stand_in.py").write_text(STAND_IN, encoding="utf-8")
        if os.name == "nt":
            (self.tools / "dotnet.cmd").write_text(f'@"{sys.executable}" "%~dp0stand_in.py" %*\n', encoding="utf-8")
        else:
            command = self.tools / "dotnet"
            command.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{self.tools}/stand_in.py" "$@"\n', encoding="utf-8")
            command.chmod(command.stat().st_mode | stat.S_IXUSR)

    def run_script(self, *arguments: str, path: str | None = None, executed: str | None = None, report: str | None = None,
                   **exit_codes: int) -> subprocess.CompletedProcess[str]:
        environment = {**os.environ, "PATH": str(self.tools) + os.pathsep + os.environ["PATH"] if path is None else path, "STAND_IN_LOG": str(self.log),
                       "RUNNER_TEMP": str(self.temporary)}
        for name in ("DOTNET_NOLOGO", "DOTNET_CLI_TELEMETRY_OPTOUT", "STAND_IN_EXECUTED", "STAND_IN_REPORT"):
            environment.pop(name, None)
        if executed is not None:
            environment["STAND_IN_EXECUTED"] = executed
        if report is not None:
            environment["STAND_IN_REPORT"] = report
        environment.update({f"STAND_IN_{name.upper()}": str(code) for name, code in exit_codes.items()})
        return subprocess.run([sys.executable, str(SCRIPT), "--root", str(self.repository), *arguments], capture_output=True, text=True,
                              encoding="utf-8", env=environment, check=False)

    def commands(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []


class DotnetTestTests(DotnetTestFixture):
    def test_it_builds_the_project_and_then_runs_its_tests_from_the_repository_root(self) -> None:
        completed = self.run_script("--project", PROJECT)
        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        self.assertEqual([f"{self.repository}|{BUILD}", f"{self.repository}|{TEST}"], self.commands(), "each command runs without the logo and without telemetry")
        self.assertIn(f"+ dotnet build {PROJECT}", completed.stdout)
        self.assertIn(f"+ dotnet test {PROJECT} --no-build --logger trx --results-directory ", completed.stdout)
        self.assertIn("stand-in output of dotnet test", completed.stdout, "the output of the commands reaches the job's log")
        self.assertIn("1 test ran", completed.stdout)

    def test_the_test_reports_go_to_the_runner_s_temporary_directory_and_are_removed_afterwards(self) -> None:
        completed = self.run_script("--project", PROJECT)
        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        self.assertIn(f"--results-directory {self.temporary}", completed.stdout)
        self.assertEqual([], list(self.temporary.iterdir()))
        self.assertEqual([], [path for path in self.repository.rglob("*") if path.suffix == ".trx"], "the checkout holds no report")

    def test_a_failing_test_fails_the_script(self) -> None:
        completed = self.run_script("--project", PROJECT, test=1)
        self.assertEqual(1, completed.returncode)
        self.assertEqual(2, len(self.commands()))
        self.assertIn(f"::error title=dotnet-test::the tests of {PROJECT} failed", completed.stderr)

    def test_a_failed_build_fails_the_script_and_runs_no_test(self) -> None:
        completed = self.run_script("--project", PROJECT, build=1)
        self.assertEqual(1, completed.returncode)
        self.assertEqual([f"{self.repository}|{BUILD}"], self.commands())
        self.assertIn(f"::error title=dotnet-test::the build of {PROJECT} failed", completed.stderr)

    def test_a_project_that_does_not_exist_fails_before_any_command_runs(self) -> None:
        for project in ("tests/Missing.Tests/Missing.Tests.csproj", "tests/Shop.Tests"):
            with self.subTest(project=project):
                completed = self.run_script("--project", project)
                self.assertEqual(2, completed.returncode)
                self.assertEqual([], self.commands())
                self.assertIn(f"::error title=dotnet-test::the project {project} is no file of the repository", completed.stderr)

    def test_a_project_outside_the_repository_fails_before_any_command_runs(self) -> None:
        (self.directory / "Outside.csproj").write_text("<Project />\n", encoding="utf-8")
        completed = self.run_script("--project", "../Outside.csproj")
        self.assertEqual(2, completed.returncode)
        self.assertEqual([], self.commands())

    def test_a_job_without_dotnet_fails_with_a_message_that_says_so(self) -> None:
        completed = self.run_script("--project", PROJECT, path=str(self.directory / "empty"))
        self.assertEqual(2, completed.returncode)
        self.assertIn("::error title=dotnet-test::dotnet is not on PATH", completed.stderr)


class NoTestRanTests(DotnetTestFixture):
    """A passed ``dotnet test`` that executed no test fails: a project without tests, or one that is no test project."""

    def assert_no_test_ran(self, completed: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(1, completed.returncode, completed.stdout + completed.stderr)
        self.assertEqual(2, len(self.commands()), "the tests were started")
        self.assertIn(f"::error title=dotnet-test::no test ran in {PROJECT}", completed.stderr)
        self.assertIn("is a test project", completed.stderr)

    def test_a_run_that_writes_no_report_fails(self) -> None:
        self.assert_no_test_ran(self.run_script("--project", PROJECT, executed=""))

    def test_a_run_whose_reports_count_no_executed_test_fails(self) -> None:
        for executed in ("0", "0 0"):
            with self.subTest(executed=executed):
                self.log.unlink(missing_ok=True)
                self.assert_no_test_ran(self.run_script("--project", PROJECT, executed=executed))

    def test_the_executed_tests_of_every_report_count(self) -> None:
        completed = self.run_script("--project", PROJECT, executed="0 3 2")
        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        self.assertIn("5 tests ran", completed.stdout)

    def test_a_report_that_cannot_be_read_counts_no_test(self) -> None:
        for report in ("no XML", "<TestRun><ResultSummary /></TestRun>", '<TestRun><ResultSummary><Counters executed="many" /></ResultSummary></TestRun>'):
            with self.subTest(report=report):
                self.log.unlink(missing_ok=True)
                self.assert_no_test_ran(self.run_script("--project", PROJECT, report=report))

    def test_failing_tests_are_reported_as_failing_and_not_as_no_test(self) -> None:
        completed = self.run_script("--project", PROJECT, executed="", test=1)
        self.assertEqual(1, completed.returncode)
        self.assertIn(f"the tests of {PROJECT} failed", completed.stderr)
        self.assertNotIn("no test ran", completed.stderr)


if __name__ == "__main__":
    unittest.main()
