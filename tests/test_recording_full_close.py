"""The closing job of the full recording run as Actions runs its steps, with this repository's recording and selection
scripts, on a fixture repository whose default branch moved from the recorded commit during the run."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.ci_policy_support import REPOSITORY_ROOT, read_workflow

FULL_RECORDING = REPOSITORY_ROOT / ".github" / "workflows" / "full-recording.yml"
APPLY_STEP = "Join the results, apply the removals and write the duration estimates"
RECORDING_PART = REPOSITORY_ROOT / "actions" / "recording"
SELECTION_PART = REPOSITORY_ROOT / "actions" / "suite-selection"
REVIEW_BRANCH = "chore/update-recorded-lists"
GIT_IDENTITY = ["-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false"]
# The suite list of the recorded commit A: tests/one.py and tests/two.py in a suite selected per entry file, tests/w.py in
# a suite selected as a whole. Each stores one read that the recording no longer sees.
SUITES = {"always": [], "suites": [
    {"name": "per-entry", "entries": ["tests/one.py", "tests/two.py"], "command": ["python3"], "entry_selection": True,
     "paths": ["data/one.txt", "data/two.txt", "tests/one.py", "tests/two.py"],
     "entry_paths": {"tests/one.py": ["data/one.txt", "tests/one.py"], "tests/two.py": ["data/two.txt", "tests/two.py"]}},
    {"name": "whole", "entries": ["tests/w.py"], "command": ["python3"], "paths": ["data/w.txt", "tests/w.py"]}]}
FILES = {"tests/one.py": "", "tests/two.py": "", "tests/w.py": "", "data/one.txt": "1\n", "data/two.txt": "2\n", "data/w.txt": "w\n",
         "data/new.txt": "new\n",
         # The validation caller whose CI definition the selection reads; it runs no step that names a path.
         ".github/workflows/validate.yml": "name: Validate\non: push\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - run: python3 -m unittest\n"}


def clean(suite: str, entry: str, paths: list[str]) -> dict:
    return {"suite": suite, "entry": entry, "job": "shard-1", "exit_code": 0, "skipped": 0, "seconds": 1.0,
            "reads": {"paths": paths, "existence_reads": [], "directory_memberships": []}}


# The recording of A: every entry file ran clean and read only itself, tests/one.py also data/one.txt.
RESULTS = {"kind": "full-recording", "suite_list": "tests/suites.json", "entries": [
    clean("per-entry", "tests/one.py", ["data/one.txt", "tests/one.py"]), clean("per-entry", "tests/two.py", ["tests/two.py"]),
    clean("whole", "tests/w.py", ["tests/w.py"])]}


@unittest.skipUnless(sys.platform != "win32" and shutil.which("bash") and shutil.which("git"), "the close steps run in Bash off Windows")
class ClosingRunTests(unittest.TestCase):
    """The steps of close, run as Actions runs them, on a repository whose default branch moved from the recorded commit A
    to B during the run: B changes data/one.txt, which tests/one.py reads, and adds data/new.txt to its stored list."""

    def setUp(self) -> None:
        _, workflow = read_workflow(FULL_RECORDING)
        self.steps = workflow["jobs"]["close"]["steps"]
        self.base = Path(tempfile.mkdtemp(prefix="full-recording-close-"))
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.origin = self.base / "origin.git"
        self.git(self.base, "init", "-q", "--bare", "-b", "main", str(self.origin))
        seed = self.base / "seed"
        self.git(self.base, "init", "-q", "-b", "main", str(seed))
        self.git(seed, "remote", "add", "origin", str(self.origin))
        for relative, text in {**FILES, "tests/suites.json": json.dumps(SUITES, indent=2) + "\n"}.items():
            (seed / relative).parent.mkdir(parents=True, exist_ok=True)
            (seed / relative).write_text(text, encoding="utf-8")
        self.git(seed, "add", "-A")
        self.git(seed, "commit", "-q", "-m", "A")
        self.a = self.git(seed, "rev-parse", "HEAD")
        moved = json.loads(json.dumps(SUITES))
        moved["suites"][0]["entry_paths"]["tests/one.py"].insert(0, "data/new.txt")
        moved["suites"][0]["paths"].insert(0, "data/new.txt")
        (seed / "tests/suites.json").write_text(json.dumps(moved, indent=2) + "\n", encoding="utf-8")
        (seed / "data/one.txt").write_text("changed\n", encoding="utf-8")
        self.git(seed, "commit", "-q", "-am", "B")
        self.b = self.git(seed, "rev-parse", "HEAD")
        self.git(seed, "push", "-q", "origin", "main")
        self.runner_temp = self.base / "temp"
        (self.runner_temp / "full-recording" / "full-recording-shard-1").mkdir(parents=True)
        (self.runner_temp / "full-recording" / "full-recording-shard-1" / "full-recording.json").write_text(json.dumps(RESULTS), encoding="utf-8")
        self.summary = self.base / "summary.md"

    def git(self, cwd: Path, *arguments: str) -> str:
        return subprocess.run(["git", *GIT_IDENTITY, *arguments], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()

    def checkout(self, ref: str) -> Path:
        """The checkout of close: the default branch's tip for ``main``, the recorded commit for a commit."""
        work = self.base / "work"
        self.git(self.base, "clone", "-q", str(self.origin), str(work))
        if ref != "main":
            self.git(work, "checkout", "-q", "--detach", ref)
        self.git(work, "config", "user.name", "Fixture")
        self.git(work, "config", "user.email", "fixture@example.invalid")
        return work

    def run_step(self, name: str, work: Path, selection: Path = SELECTION_PART) -> subprocess.CompletedProcess[str]:
        item = named(self.steps, name)
        values = {"steps.recording.outputs.path": str(RECORDING_PART), "steps.selection.outputs.path": str(selection),
                  "github.sha": self.a, "runner.temp": str(self.runner_temp), "steps.app-token.outputs.token": "app-token"}
        environment = {**os.environ, "GITHUB_SERVER_URL": "https://github.example", "GITHUB_REPOSITORY": "owner/repository",
                       "GITHUB_RUN_ID": "9", "GITHUB_STEP_SUMMARY": str(self.summary), "GITHUB_OUTPUT": str(self.base / "output"),
                       "RUNNER_TEMP": str(self.runner_temp),
                       **{key: re.sub(r"\$\{\{\s*([\w.-]+)\s*\}\}", lambda found: values[found[1]], str(value))
                          for key, value in item.get("env", {}).items()}}
        ran = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", item["run"]], cwd=work, env=environment, capture_output=True, text=True)
        self.assertEqual(0, ran.returncode, ran.stdout + ran.stderr)
        return ran

    def stored(self, work: Path) -> dict:
        suites = json.loads((work / "tests/suites.json").read_text(encoding="utf-8"))
        return {"per-entry": suites["suites"][0]["entry_paths"], "whole": suites["suites"][1]["paths"]}

    def report(self) -> str:
        return (self.runner_temp / "full-recording-report.md").read_text(encoding="utf-8")

    def test_close_commits_onto_the_moved_tip_and_keeps_the_reads_of_the_entry_files_the_change_selects(self) -> None:
        work = self.checkout("main")
        self.run_step(APPLY_STEP, work)
        stored = self.stored(work)
        # tests/one.py is selected by the change A..B: it keeps data/new.txt, which B added, beside its recorded reads.
        self.assertEqual(["data/new.txt", "data/one.txt", "tests/one.py"], stored["per-entry"]["tests/one.py"])
        # tests/two.py and tests/w.py are not selected and ran clean: their recorded reads replace the stored ones.
        self.assertEqual(["tests/two.py"], stored["per-entry"]["tests/two.py"])
        self.assertEqual(["tests/w.py"], stored["whole"])
        report = self.report()
        self.assertIn(f"The default branch moved from {self.a} to {self.b} during the run; the lists apply to {self.b}.", report)
        self.assertIn("- per-entry: kept the list of tests/one.py: the change since the recorded commit selects it", report)
        self.assertNotIn("tests/two.py", report)
        for name in ("Commit the recorded lists", "Push the review branch"):
            self.run_step(name, work)
        pushed = self.git(work, "ls-remote", "--heads", "origin", f"refs/heads/{REVIEW_BRANCH}").split()[0]
        self.git(work, "fetch", "-q", "origin", REVIEW_BRANCH)
        self.assertEqual(self.b, self.git(work, "rev-parse", f"{pushed}^"), "the review commit's parent is the moved tip B")
        self.assertEqual(["tests/suites.json"], self.git(work, "diff", "--name-only", self.b, pushed).splitlines())

    def test_a_failed_selection_removes_no_stored_read_of_the_moved_tip(self) -> None:
        work = self.checkout("main")
        before = self.stored(work)
        self.run_step(APPLY_STEP, work, selection=self.base / "missing")
        after = self.stored(work)
        for entry, reads in before["per-entry"].items():
            self.assertEqual([], [read for read in reads if read not in after["per-entry"][entry]], entry)
        self.assertEqual([], [read for read in before["whole"] if read not in after["whole"]])
        self.assertIn("The change could not be selected, so every entry file keeps its stored reads.", self.report())
        self.assertIn("- whole: kept the list of tests/w.py: the change since the recorded commit could not be selected", self.report())

    def test_without_a_move_close_writes_the_suite_list_and_report_of_apply_alone(self) -> None:
        # The recorded commit is the checkout, on the default branch when it did not move and on any other branch: the
        # step writes what apply wrote before the change, the header and the report lines of apply without options.
        work = self.checkout(self.a)
        self.run_step(APPLY_STEP, work)
        written, report = (work / "tests/suites.json").read_text(encoding="utf-8"), self.report()
        reference = self.base / "reference"
        self.git(self.base, "clone", "-q", str(self.origin), str(reference))
        self.git(reference, "checkout", "-q", "--detach", self.a)
        applied = subprocess.run([sys.executable, str(RECORDING_PART / "recording.py"), "apply", "--suites", "tests/suites.json",
                                  str(self.runner_temp / "full-recording" / "full-recording-shard-1" / "full-recording.json")],
                                 cwd=reference, capture_output=True, text=True, check=True)
        self.assertEqual((reference / "tests/suites.json").read_text(encoding="utf-8"), written)
        self.assertEqual("The full recording of run https://github.example/owner/repository/actions/runs/9.\n\n"
                         + "".join(f"- {line}\n" for line in applied.stdout.splitlines()), report)
        self.assertEqual(self.a, self.git(work, "rev-parse", "HEAD"))


def named(steps: list[dict], name: str) -> dict:
    return next(step for step in steps if step.get("name") == name)


if __name__ == "__main__":
    unittest.main()
