"""A caller run through the validation caller template: the selection's shards run only the selected entry files.

The fixture repository holds the filled caller template, the filled validation workflow template and a suite list with
one suite selected per entry file and one selected as a whole. The suite selection runs as the shared selection
workflow runs it; the caller's `with` values become the validation workflow's inputs, and each shard of its `shards`
input runs, as the shard job does, the setups whose gate holds for the shard and then each of its entry files with the
command the shard gives for it.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SELECTION_KIT = REPOSITORY_ROOT / "actions" / "suite-selection" / "consumer-kit"
TOOL = REPOSITORY_ROOT / "actions" / "suite-selection"
SELECTION_TOOL = TOOL / "suite_selection.py"
sys.path.insert(0, str(TOOL))

import ci_definition  # noqa: E402
GIT_IDENTITY = ["-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false"]

# The validation workflow template as a consumer fills it: one Linux setup `prepare`, which only the second unit entry
# file states, and the Windows job without a Windows suite in the suite list.
PLACEHOLDERS = {
    "<name>": "Tests", "<linux-runner>": "ubuntu-latest", "<windows-runner>": "windows-latest", "<windows-job>": "windows",
    "<python-version>": "3.13", "<setup>": "prepare", "<setup-command>": "python tooling/prepare.py",
    "<setup-provides>": '--path "$RUNNER_TEMP/setups/prepare"', "<windows-setup>": "dotnet",
    "<windows-setup-command>": "dotnet --info", "<windows-setup-provides>": "--path $env:DOTNET_ROOT",
    "@<pin>": "@0000000000000000000000000000000000000000 # v1.0.0",
}


def filled(template: str) -> str:
    for placeholder, value in PLACEHOLDERS.items():
        template = template.replace(placeholder, value)
    assert re.search(r"<[a-z][a-z-]*>", template) is None, "every placeholder is filled"
    return template


VALIDATION_WORKFLOW = filled((SELECTION_KIT / "validation-workflow.yml").read_text(encoding="utf-8"))
COMMAND = ["python", "-m", "unittest", "-v"]

SUITE_LIST = {"always": [], "suites": [
    {"name": "unit", "entries": ["checks/test_one.py", "checks/test_two.py"], "command": COMMAND,
     "entry_selection": True, "entry_setups": {"checks/test_two.py": ["prepare"]},
     "paths": ["checks/test_one.py", "checks/test_two.py", "src/one.txt", "src/two.txt"],
     "entry_paths": {"checks/test_one.py": ["checks/test_one.py", "src/one.txt"],
                     "checks/test_two.py": ["checks/test_two.py", "src/two.txt"]}},
    {"name": "lint", "entries": ["checks/test_lint.py"], "command": COMMAND,
     "paths": ["checks/test_lint.py", "src/lint.txt"]},
]}

WHOLE_SUITE_LIST = {"always": [], "suites": [
    {"name": "unit", "entries": ["checks/test_one.py", "checks/test_two.py"], "command": COMMAND,
     "paths": ["checks/test_one.py", "checks/test_two.py", "src/one.txt", "src/two.txt"]},
    SUITE_LIST["suites"][1],
]}

MARKING_TEST = """\
import os
import unittest
from pathlib import Path


class Marks(unittest.TestCase):
    def test_marks_that_it_ran(self):
        Path(os.environ["MARKERS"], "{name}").write_text("ran", encoding="utf-8")
"""

MARKING_SCRIPT = """\
import os
from pathlib import Path

Path(os.environ["MARKERS"], "prepare").write_text("ran", encoding="utf-8")
"""

OUTPUT = re.compile(r"^\$\{\{ needs\.select\.outputs\.([a-z0-9-]+) \}\}$")
SETUP_GATE = re.compile(r"^contains\(matrix\.shard\.setups, '([^']+)'\)$")
SETUP_COMMAND = re.compile(r"recording\.py\" exec --setup \S+ -- (.+)$", re.MULTILINE)


def git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(["git", *GIT_IDENTITY, *arguments], cwd=repository, capture_output=True, text=True, check=True)
    return completed.stdout.strip()


def place(repository: Path, relative: str, text: str) -> None:
    path = repository.joinpath(*relative.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class CallerRun(unittest.TestCase):
    """A fixture caller repository with the filled caller template, the filled validation workflow template as
    `tests.yml` and `SUITES` as its suite list; it holds no tests of its own."""

    SUITES: dict = SUITE_LIST

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repository = Path(temporary.name) / "caller"
        self.markers = Path(temporary.name) / "markers"
        self.repository.mkdir()
        template = (SELECTION_KIT / "validation.yml").read_text(encoding="utf-8")
        caller = template.replace("<default-branch>", "main").replace("<validation-workflow>", "tests")
        files = {
            ".github/workflows/validation.yml": caller,
            ".github/workflows/tests.yml": VALIDATION_WORKFLOW,
            ".github/suites.json": json.dumps(self.SUITES, indent=2) + "\n",
            "tooling/prepare.py": MARKING_SCRIPT,
        }
        files.update({f"checks/test_{name}.py": MARKING_TEST.format(name=name) for name in ("one", "two", "lint")})
        files.update({f"src/{name}.txt": "base\n" for name in ("one", "two", "lint")})
        for relative, text in files.items():
            place(self.repository, relative, text)
        git(self.repository, "init", "-q", "-b", "main")
        git(self.repository, "config", "core.autocrlf", "false")
        git(self.repository, "add", "-A")
        git(self.repository, "commit", "-q", "-m", "base")
        self.caller = ci_definition.read_yaml(caller)
        self.validation = ci_definition.read_yaml(VALIDATION_WORKFLOW)

    def change(self, *relatives: str, edit=lambda text: text + "# changed\n") -> str:
        """Commit a change to each file and return the new commit; it records the commit before it as the base that
        `select` compares with."""
        self.base = git(self.repository, "rev-parse", "HEAD")
        for relative in relatives:
            path = self.repository.joinpath(*relative.split("/"))
            path.write_text(edit(path.read_text(encoding="utf-8")), encoding="utf-8")
        git(self.repository, "commit", "-q", "-am", "change")
        return git(self.repository, "rev-parse", "HEAD")

    def select(self, head: str) -> dict[str, str]:
        """The outputs of the shared selection job: `select --json-output` as `suite-selection.yml` runs it, from the
        base that the last `change` recorded to ``head``."""
        outputs = Path(self.repository.parent, "outputs.txt")
        outputs.write_text("", encoding="utf-8")
        environment = {**os.environ, "PYTHONPYCACHEPREFIX": str(Path(self.repository.parent, "pycache"))}
        completed = subprocess.run(
            [sys.executable, str(SELECTION_TOOL), "select", "--root", ".", "--suites", ".github/suites.json",
             "--workflow", "Fixture/caller/.github/workflows/validation.yml@refs/heads/main", "--base", self.base,
             "--head", head, "--compare", "direct", "--json-output", str(outputs)],
            cwd=self.repository, capture_output=True, text=True, env=environment)
        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        return dict(line.split("=", 1) for line in outputs.read_text(encoding="utf-8").splitlines())

    def inputs(self, outputs: dict[str, str]) -> dict[str, str]:
        """The inputs the caller's validation job passes to the validation workflow."""
        passed = {}
        for name, value in self.caller["jobs"]["tests"]["with"].items():
            match = OUTPUT.match(value)
            self.assertIsNotNone(match, f"unexpected caller input form: {value}")
            passed[name] = outputs.get(match.group(1), "")
        self.assertEqual(set(self.validation["on"]["workflow_call"]["inputs"]), set(passed),
                         "a called workflow rejects an input it does not declare and the caller passes every one")
        return passed

    def run_validation(self, inputs: dict[str, str]) -> set[str]:
        """Each shard of the `shards` input runs its gated setups and then its entry files; the result is what ran."""
        self.markers.mkdir(exist_ok=True)
        for marker in self.markers.iterdir():
            marker.unlink()
        self.assertEqual("", inputs["windows"], "the suite list has no Windows suite")
        if inputs["shards"] in ("", "[]"):
            return set()
        environment = {**os.environ, "MARKERS": str(self.markers), "PYTHONPYCACHEPREFIX": str(Path(self.repository.parent, "pycache"))}
        for shard in json.loads(inputs["shards"]):
            for step in self.validation["jobs"]["linux"]["steps"]:
                gate = SETUP_GATE.match(str(step.get("if", "")))
                command = SETUP_COMMAND.search(str(step.get("run", "")))
                if gate and command and gate.group(1) in shard["setups"]:
                    self.execute(shlex.split(command.group(1)), environment)
            for item in shard["entries"]:
                self.execute([*item["command"], item["entry"]], environment)
        return {marker.name for marker in self.markers.iterdir()}

    def execute(self, command: list[str], environment: dict[str, str]) -> None:
        self.assertEqual("python", command[0])
        completed = subprocess.run([sys.executable, *command[1:]], cwd=self.repository, capture_output=True, text=True, env=environment)
        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)


class CallerEntrySelectionTest(CallerRun):
    def test_a_change_one_entry_reads_runs_that_entry_and_not_the_suite_s_other_entry(self) -> None:
        outputs = self.select(self.change("src/one.txt"))
        self.assertEqual({"unit": "run", "lint": "skip"}, json.loads(outputs["suites"]))
        self.assertEqual({"unit": ["checks/test_one.py"]}, json.loads(outputs["entries"]))
        self.assertEqual({"one"}, self.run_validation(self.inputs(outputs)))

    def test_a_change_both_entries_read_runs_both_entries_and_the_setup_that_one_of_them_states(self) -> None:
        outputs = self.select(self.change("src/one.txt", "src/two.txt"))
        self.assertEqual({"one", "two", "prepare"}, self.run_validation(self.inputs(outputs)))

    def test_a_setup_runs_only_in_a_shard_whose_entry_files_state_it(self) -> None:
        outputs = self.select(self.change("src/two.txt"))
        self.assertEqual([["prepare"]], [shard["setups"] for shard in json.loads(outputs["shards"])])
        self.assertEqual({"prepare", "two"}, self.run_validation(self.inputs(outputs)))
        outputs = self.select(self.change("src/one.txt"))
        self.assertEqual([[]], [shard["setups"] for shard in json.loads(outputs["shards"])])
        self.assertEqual({"one"}, self.run_validation(self.inputs(outputs)))

    def test_a_suite_selected_as_a_whole_runs_on_run_and_skips_on_skip(self) -> None:
        outputs = self.select(self.change("src/lint.txt"))
        self.assertEqual({"unit": "skip", "lint": "run"}, json.loads(outputs["suites"]))
        self.assertEqual({"lint"}, self.run_validation(self.inputs(outputs)))
        outputs = self.select(self.change("src/two.txt"))
        self.assertEqual({"unit": "run", "lint": "skip"}, json.loads(outputs["suites"]))
        self.assertEqual({"prepare", "two"}, self.run_validation(self.inputs(outputs)))

    def test_a_change_to_a_step_of_the_validation_workflow_runs_every_suite(self) -> None:
        for before, after in (("persist-credentials: false", "persist-credentials: false\n          fetch-depth: 1"),
                              ("-r -n 1 -P 2", "-r -n 1 -P 3")):
            with self.subTest(change=after):
                self.assertIn(before, VALIDATION_WORKFLOW, "a step of the shard job")
                outputs = self.select(self.change(".github/workflows/tests.yml", edit=lambda text: text.replace(before, after, 1)))
                self.assertEqual({"unit": "run", "lint": "run"}, json.loads(outputs["suites"]))
                self.assertEqual({"unit": ["checks/test_one.py", "checks/test_two.py"]}, json.loads(outputs["entries"]))
                self.assertEqual({"one", "two", "lint", "prepare"}, self.run_validation(self.inputs(outputs)))


class CallerWithoutEntrySelectionTest(CallerRun):
    """A suite list that selects no suite per entry file: each suite runs or skips as a whole."""

    SUITES = WHOLE_SUITE_LIST

    def test_each_suite_runs_with_every_entry_file_on_run_and_skips_on_skip(self) -> None:
        outputs = self.select(self.change("src/one.txt"))
        self.assertNotIn("entries", outputs)
        self.assertEqual({"shards": outputs["shards"], "windows": "", "language-steps": "", "shared-digest": ""}, self.inputs(outputs))
        self.assertEqual({"unit": "run", "lint": "skip"}, json.loads(outputs["suites"]))
        self.assertEqual({"one", "two"}, self.run_validation(self.inputs(outputs)))
        outputs = self.select(self.change("src/lint.txt"))
        self.assertEqual({"unit": "skip", "lint": "run"}, json.loads(outputs["suites"]))
        self.assertEqual({"lint"}, self.run_validation(self.inputs(outputs)))


if __name__ == "__main__":
    unittest.main()
