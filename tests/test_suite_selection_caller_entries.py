"""A caller run through the validation caller template: a suite selected per entry file runs only its selected entries.

The fixture repository holds the filled caller template, a validation workflow in the form that setup gives it, and a
suite list with one suite selected per entry file and one selected as a whole. The selection Tool runs as the shared
selection workflow runs it; the caller's `with` values and the validation workflow's conditions and `env` values are
then evaluated for the expression forms they use, and each step that runs executes its command in the fixture.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import string
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

VALIDATION_WORKFLOW = """\
name: Tests
on:
  workflow_call:
    inputs:
      suites:
        type: string
        required: true
      entries:
        type: string
        required: false
      language-steps:
        type: string
        required: false
      shared-digest:
        type: string
        required: true
permissions: {}
jobs:
  tests:
    runs-on: ubuntu-latest
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@0000000000000000000000000000000000000000 # v1.0.0
        with:
          ref: ${{ github.event_name == 'repository_dispatch' && github.event.client_payload.sha || '' }}
          persist-credentials: false
      - name: Prepare the second unit entry
        if: contains(fromJSON(inputs.entries).unit, 'checks/test_two.py')
        run: python tooling/prepare.py
      - name: Run the selected unit entries
        if: fromJSON(inputs.suites).unit == 'run'
        env:
          ENTRIES: ${{ join(fromJSON(inputs.entries).unit, ' ') }}
        run: python -m unittest $ENTRIES -v
      - name: Run the lint suite
        if: fromJSON(inputs.suites).lint == 'run'
        run: python -m unittest checks/test_lint.py -v
"""

SUITE_LIST = {"always": [], "suites": [
    {"name": "unit", "entries": ["checks/test_one.py", "checks/test_two.py"],
     "entry_selection": True, "paths": ["checks/test_one.py", "checks/test_two.py", "src/one.txt", "src/two.txt"],
     "entry_paths": {"checks/test_one.py": ["checks/test_one.py", "src/one.txt"],
                     "checks/test_two.py": ["checks/test_two.py", "src/two.txt"]}},
    {"name": "lint", "entries": ["checks/test_lint.py"],
     "paths": ["checks/test_lint.py", "src/lint.txt"]},
]}

WHOLE_VALIDATION_WORKFLOW = VALIDATION_WORKFLOW.replace("""\
      - name: Prepare the second unit entry
        if: contains(fromJSON(inputs.entries).unit, 'checks/test_two.py')
        run: python tooling/prepare.py
      - name: Run the selected unit entries
        if: fromJSON(inputs.suites).unit == 'run'
        env:
          ENTRIES: ${{ join(fromJSON(inputs.entries).unit, ' ') }}
        run: python -m unittest $ENTRIES -v
""", """\
      - name: Run the unit suite
        if: fromJSON(inputs.suites).unit == 'run'
        run: python -m unittest checks/test_one.py checks/test_two.py -v
""")

WHOLE_SUITE_LIST = {"always": [], "suites": [
    {"name": "unit", "entries": ["checks/test_one.py", "checks/test_two.py"],
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
RUN_GATE = re.compile(r"^fromJSON\(inputs\.([a-z0-9-]+)\)\.([a-z0-9-]+) == 'run'$")
JSON_ENTRY_GATE = re.compile(r"^contains\(fromJSON\(inputs\.([a-z0-9-]+)\)\.([a-z0-9-]+), '([^']+)'\)$")
JOINED_ENTRIES = re.compile(r"^\$\{\{ join\(fromJSON\(inputs\.([a-z0-9-]+)\)\.([a-z0-9-]+), ' '\) \}\}$")


def git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(["git", *GIT_IDENTITY, *arguments], cwd=repository, capture_output=True, text=True, check=True)
    return completed.stdout.strip()


def place(repository: Path, relative: str, text: str) -> None:
    path = repository.joinpath(*relative.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class CallerRun(unittest.TestCase):
    """A fixture caller repository with the filled template, `WORKFLOW` as its validation workflow and `SUITES` as its
    suite list; it holds no tests of its own."""

    SUITES: dict = SUITE_LIST
    WORKFLOW = VALIDATION_WORKFLOW

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
            ".github/workflows/tests.yml": self.WORKFLOW,
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
        self.validation = ci_definition.read_yaml(self.WORKFLOW)

    def change(self, *relatives: str) -> str:
        """Commit a change to each file and return the new commit; it records the commit before it as the base that
        `select` compares with."""
        self.base = git(self.repository, "rev-parse", "HEAD")
        for relative in relatives:
            path = self.repository.joinpath(*relative.split("/"))
            path.write_text(path.read_text(encoding="utf-8") + "# changed\n", encoding="utf-8")
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
        declared = self.validation["on"]["workflow_call"]["inputs"]
        self.assertLessEqual(set(passed), set(declared), "a called workflow rejects an input it does not declare")
        return passed

    def expression(self, text: str, inputs: dict[str, str]) -> object:
        text = text.strip()
        if match := RUN_GATE.match(text):
            return json.loads(inputs[match.group(1)])[match.group(2)] == "run"
        if match := JSON_ENTRY_GATE.match(text):
            return match.group(3) in json.loads(inputs[match.group(1)])[match.group(2)]
        if match := JOINED_ENTRIES.match(text):
            return " ".join(json.loads(inputs[match.group(1)])[match.group(2)])
        self.fail(f"unexpected expression form: {text}")

    def run_validation(self, inputs: dict[str, str]) -> set[str]:
        """Each step of the validation workflow whose condition holds runs its command; the result is what ran."""
        self.markers.mkdir(exist_ok=True)
        for marker in self.markers.iterdir():
            marker.unlink()
        for step in self.validation["jobs"]["tests"]["steps"]:
            if "run" not in step or ("if" in step and not self.expression(step["if"], inputs)):
                continue
            environment = {name: self.expression(value, inputs) for name, value in step.get("env", {}).items()}
            command = shlex.split(string.Template(step["run"]).substitute(environment))
            self.assertEqual("python", command[0])
            completed = subprocess.run([sys.executable, *command[1:]], cwd=self.repository, capture_output=True, text=True,
                                       env={**os.environ, **environment, "MARKERS": str(self.markers),
                                            "PYTHONPYCACHEPREFIX": str(Path(self.repository.parent, "pycache"))})
            self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        return {marker.name for marker in self.markers.iterdir()}


class CallerEntrySelectionTest(CallerRun):
    def test_a_change_one_entry_reads_runs_that_entry_and_not_the_suite_s_other_entry(self) -> None:
        outputs = self.select(self.change("src/one.txt"))
        self.assertEqual({"unit": "run", "lint": "skip"}, json.loads(outputs["suites"]))
        self.assertEqual({"unit": ["checks/test_one.py"]}, json.loads(outputs["entries"]))
        self.assertEqual({"one"}, self.run_validation(self.inputs(outputs)))

    def test_a_change_both_entries_read_runs_both_entries(self) -> None:
        outputs = self.select(self.change("src/one.txt", "src/two.txt"))
        self.assertEqual({"one", "two", "prepare"}, self.run_validation(self.inputs(outputs)))

    def test_a_file_only_an_entry_gated_step_names_runs_that_entry_alone(self) -> None:
        outputs = self.select(self.change("tooling/prepare.py"))
        self.assertEqual({"unit": ["checks/test_two.py"]}, json.loads(outputs["entries"]))
        self.assertEqual({"prepare", "two"}, self.run_validation(self.inputs(outputs)))

    def test_a_suite_selected_as_a_whole_runs_on_run_and_skips_on_skip(self) -> None:
        outputs = self.select(self.change("src/lint.txt"))
        self.assertEqual({"unit": "skip", "lint": "run"}, json.loads(outputs["suites"]))
        self.assertEqual({"lint"}, self.run_validation(self.inputs(outputs)))
        outputs = self.select(self.change("src/two.txt"))
        self.assertEqual({"unit": "run", "lint": "skip"}, json.loads(outputs["suites"]))
        self.assertEqual({"prepare", "two"}, self.run_validation(self.inputs(outputs)))


class CallerWithoutEntrySelectionTest(CallerRun):
    """A suite list that selects no suite per entry file: the selection writes no `entries`, the caller passes it empty,
    and each suite runs or skips as a whole."""

    SUITES = WHOLE_SUITE_LIST
    WORKFLOW = WHOLE_VALIDATION_WORKFLOW

    def test_each_suite_runs_with_every_entry_file_on_run_and_skips_on_skip(self) -> None:
        outputs = self.select(self.change("src/one.txt"))
        self.assertNotIn("entries", outputs)
        self.assertEqual({"suites": outputs["suites"], "entries": "", "language-steps": "", "shared-digest": ""}, self.inputs(outputs))
        self.assertEqual({"unit": "run", "lint": "skip"}, json.loads(outputs["suites"]))
        self.assertEqual({"one", "two"}, self.run_validation(self.inputs(outputs)))
        outputs = self.select(self.change("src/lint.txt"))
        self.assertEqual({"unit": "skip", "lint": "run"}, json.loads(outputs["suites"]))
        self.assertEqual({"lint"}, self.run_validation(self.inputs(outputs)))


if __name__ == "__main__":
    unittest.main()
