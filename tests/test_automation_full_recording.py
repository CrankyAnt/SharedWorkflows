"""The closing job of the full recording, a part of the automation workflow, as a consumer with its own suite list runs
it: its apply step, run in Bash on the downloaded results, and the request with which it lets the review pull request
merge itself, made to a stand-in `gh`."""

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

from tests.ci_policy_support import read_workflow

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "automation.yml"
CLOSE = "full-recording-close"
SUITES = ".github/suites.json"
REPOSITORY = "owner/consumer"
REVIEW_BRANCH = "chore/update-recorded-lists"
ON_DEFAULT_BRANCH = "github.ref_name == github.event.repository.default_branch"
APPLY_STEP = "Join the results, apply the removals and write the duration estimates"
PULL_REQUEST_STEP = "Create or update the review pull request"
AUTO_MERGE_STEP = "Let the review pull request merge itself once its required checks have passed"
GIT_IDENTITY = ["-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false"]
# A stand-in gh that logs its token and its arguments, prints the pull request that `pr list` finds and ends with $GH_STATUS.
STAND_IN_GH = """#!/usr/bin/env bash
printf '%s|%s\\n' "$GH_TOKEN" "$*" >> "$GH_CALLS"
if [ "$1 $2" = "pr list" ]; then printf '%s\\n' "${GH_OPEN_PULL_REQUEST:-}"; fi
exit "${GH_STATUS:-0}"
"""
requires_bash = unittest.skipUnless(sys.platform != "win32" and shutil.which("bash") and shutil.which("git"), "the close steps run in Bash off Windows")


def named(steps: list[dict], name: str) -> dict:
    return next(step for step in steps if step.get("name") == name)


def clean(suite: str, entry: str, paths: list[str]) -> dict:
    return {"suite": suite, "entry": entry, "job": "shard-1", "exit_code": 0, "skipped": 0, "seconds": 1.0,
            "reads": {"paths": paths, "existence_reads": [], "directory_memberships": []}}


class AutoMergeStepTest(unittest.TestCase):
    def setUp(self) -> None:
        self.steps = read_workflow(WORKFLOW)[1]["jobs"][CLOSE]["steps"]
        self.step = named(self.steps, AUTO_MERGE_STEP)

    def test_it_follows_the_pull_request_with_the_app_s_token_only_when_the_default_branch_s_lists_changed(self) -> None:
        self.assertEqual(self.steps.index(named(self.steps, PULL_REQUEST_STEP)) + 1, self.steps.index(self.step))
        self.assertEqual("auto-merge", self.step["id"])
        self.assertEqual(f"steps.changes.outputs.changed == 'true' && {ON_DEFAULT_BRANCH}", self.step["if"])
        self.assertEqual({"GH_TOKEN": "${{ steps.app-token.outputs.token }}"}, self.step["env"],
                         "a merge that the App's token asks for starts the runs of the merged commit")

    def test_a_failed_request_stops_the_run_with_the_setting_it_needs(self) -> None:
        stopped = next(step for step in self.steps if step.get("id") == "stopped")
        self.assertIn("steps.auto-merge.outcome == 'failure'", stopped["if"])
        self.assertIn("auto-merge needs the repository setting that allows it", stopped["run"])


class CloseSteps(unittest.TestCase):
    """The steps of the closing job, run in Bash as Actions runs them, on a consumer checkout of the recorded commit."""

    def setUp(self) -> None:
        job = read_workflow(WORKFLOW)[1]["jobs"][CLOSE]
        self.steps, self.job_env = job["steps"], job["env"]
        self.base = Path(tempfile.mkdtemp(prefix="automation-full-recording-"))
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.work = self.base / "work"
        suites = {"always": [], "suites": [
            {"name": "unit", "entries": ["checks/test_a.py"], "command": ["python3"], "paths": ["checks/test_a.py", "lib/old.txt"]},
            {"name": "lint", "entries": ["checks/lint.py"], "command": ["python3"], "paths": ["checks/lint.py", "lib/old.txt"]}]}
        for relative, text in {"checks/test_a.py": "", "checks/lint.py": "", "lib/old.txt": "", "lib/data.txt": "",
                               SUITES: json.dumps(suites, indent=2) + "\n"}.items():
            (self.work / relative).parent.mkdir(parents=True, exist_ok=True)
            (self.work / relative).write_text(text, encoding="utf-8")
        for arguments in (("init", "-q", "-b", "main"), ("add", "-A"), ("commit", "-q", "-m", "feat: adopt")):
            subprocess.run(["git", *GIT_IDENTITY, *arguments], cwd=self.work, check=True, capture_output=True)
        self.recorded = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.work, check=True, capture_output=True, text=True).stdout.strip()
        self.runner_temp = self.base / "temp"
        (self.runner_temp / "full-recording").mkdir(parents=True)
        self.calls = self.base / "gh-calls.log"
        (self.base / "bin").mkdir()
        (self.base / "bin" / "gh").write_text(STAND_IN_GH, encoding="utf-8")
        (self.base / "bin" / "gh").chmod(0o755)

    def run_step(self, name: str, **extra: str) -> subprocess.CompletedProcess[str]:
        """The step with the job's and its own `env` resolved; the recorded commit is the checkout."""
        step = named(self.steps, name)
        values = {"steps.recording.outputs.path": str(REPOSITORY_ROOT / "actions" / "recording"),
                  "steps.selection.outputs.path": str(REPOSITORY_ROOT / "actions" / "suite-selection"), "github.sha": self.recorded,
                  "runner.temp": str(self.runner_temp), "needs.full-recording.outputs.suites": SUITES,
                  "needs.full-recording.outputs.validation-workflow": ".github/workflows/validation.yml",
                  "steps.app-token.outputs.token": "app-token", "github.event.repository.default_branch": "main"}
        environment = {**os.environ, "PATH": f"{self.base / 'bin'}{os.pathsep}{os.environ['PATH']}", "GH_CALLS": str(self.calls),
                       "GITHUB_SERVER_URL": "https://github.example", "GITHUB_REPOSITORY": REPOSITORY,
                       "GITHUB_RUN_ID": "9", "GITHUB_STEP_SUMMARY": str(self.base / "summary.md"), "RUNNER_TEMP": str(self.runner_temp),
                       **{key: re.sub(r"\$\{\{\s*([\w.-]+)\s*\}\}", lambda found: values[found[1]], str(value))
                          for key, value in {**self.job_env, **step.get("env", {})}.items()}, **extra}
        environment.pop("GITHUB_TOKEN", None)
        return subprocess.run(["bash", "-e", "-o", "pipefail", "-c", step["run"]], cwd=self.work, env=environment, capture_output=True, text=True)

    def gh_calls(self) -> list[str]:
        return self.calls.read_text(encoding="utf-8").splitlines() if self.calls.exists() else []


@requires_bash
class FullRecordingApplyRunTest(CloseSteps):
    """The apply step with the downloaded results where `actions/download-artifact` places them by `pattern`: in a folder
    named after each artifact when the pattern matches several, and the one artifact's file directly in the download
    folder."""

    def downloaded(self, documents: dict[str, dict]) -> None:
        folder = self.runner_temp / "full-recording"
        for artifact, document in documents.items():
            target = (folder if len(documents) == 1 else folder / artifact) / "full-recording.json"
            target.parent.mkdir(exist_ok=True)
            target.write_text(json.dumps(document), encoding="utf-8")

    def result(self, *entries: dict) -> dict:
        return {"kind": "full-recording", "suite_list": SUITES, "entries": list(entries)}

    def stored(self) -> dict[str, list[str]]:
        suites = json.loads((self.work / SUITES).read_text(encoding="utf-8"))
        return {item["name"]: item["paths"] for item in suites["suites"]}

    def test_the_result_of_one_downloaded_artifact_is_applied_to_the_configured_suite_list(self) -> None:
        self.downloaded({"full-recording-shard-1": self.result(clean("unit", "checks/test_a.py", ["checks/test_a.py", "lib/data.txt"]),
                                                                clean("lint", "checks/lint.py", ["checks/lint.py"]))})
        self.assertEqual([self.runner_temp / "full-recording" / "full-recording.json"], list((self.runner_temp / "full-recording").iterdir()))
        ran = self.run_step(APPLY_STEP)
        self.assertEqual(0, ran.returncode, ran.stdout + ran.stderr)
        self.assertEqual({"unit": ["checks/test_a.py", "lib/data.txt"], "lint": ["checks/lint.py"]}, self.stored())

    def test_the_results_of_several_downloaded_artifacts_are_each_applied(self) -> None:
        self.downloaded({"full-recording-shard-1": self.result(clean("unit", "checks/test_a.py", ["checks/test_a.py", "lib/data.txt"])),
                         "full-recording-windows": self.result(clean("lint", "checks/lint.py", ["checks/lint.py"]))})
        ran = self.run_step(APPLY_STEP)
        self.assertEqual(0, ran.returncode, ran.stdout + ran.stderr)
        self.assertEqual({"unit": ["checks/test_a.py", "lib/data.txt"], "lint": ["checks/lint.py"]}, self.stored())

    def test_a_download_folder_without_a_result_fails_with_the_cannot_read_error(self) -> None:
        ran = self.run_step(APPLY_STEP)
        self.assertNotEqual(0, ran.returncode)
        self.assertIn(f"cannot read {self.runner_temp}/full-recording/**/full-recording.json", ran.stderr)
        self.assertEqual("", subprocess.run(["git", "status", "--porcelain"], cwd=self.work, capture_output=True, text=True, check=True).stdout)

    def test_only_a_change_to_the_configured_suite_list_passes_the_check_of_the_changed_files(self) -> None:
        (self.work / SUITES).write_text("{}\n", encoding="utf-8")
        self.assertEqual(0, self.run_step("Check the changed files").returncode)
        (self.work / "lib/data.txt").write_text("changed\n", encoding="utf-8")
        ran = self.run_step("Check the changed files")
        self.assertEqual(1, ran.returncode)
        self.assertIn("Unexpected changed file: lib/data.txt", ran.stderr)


@requires_bash
class ReviewPullRequestRunTest(CloseSteps):
    """The requests that the closing job makes for its review pull request, as the stand-in gh receives them."""

    def test_a_new_pull_request_is_opened_and_then_asked_to_merge_itself_by_squash_at_the_pushed_commit(self) -> None:
        (self.runner_temp / "full-recording-report.md").write_text("report\n", encoding="utf-8")
        for name in (PULL_REQUEST_STEP, AUTO_MERGE_STEP):
            ran = self.run_step(name)
            self.assertEqual(0, ran.returncode, ran.stdout + ran.stderr)
        title = "chore: update the recorded path lists"
        self.assertEqual([
            f"app-token|pr list --repo {REPOSITORY} --head {REVIEW_BRANCH} --base main --state open --json url --jq .[0].url",
            f"app-token|pr create --repo {REPOSITORY} --base main --head {REVIEW_BRANCH} --title {title} --body-file {self.runner_temp}/full-recording-report.md",
            f"app-token|pr merge {REVIEW_BRANCH} --repo {REPOSITORY} --auto --squash --match-head-commit {self.recorded}"], self.gh_calls())

    def test_an_open_pull_request_is_updated_and_asked_again(self) -> None:
        url = f"https://github.example/{REPOSITORY}/pull/5"
        for name in (PULL_REQUEST_STEP, AUTO_MERGE_STEP):
            ran = self.run_step(name, GH_OPEN_PULL_REQUEST=url)
            self.assertEqual(0, ran.returncode, ran.stdout + ran.stderr)
        calls = self.gh_calls()
        self.assertEqual(3, len(calls))
        self.assertTrue(calls[1].startswith(f"app-token|pr edit {url} --title "), calls[1])
        self.assertEqual(f"app-token|pr merge {REVIEW_BRANCH} --repo {REPOSITORY} --auto --squash --match-head-commit {self.recorded}", calls[2])

    def test_a_refused_request_fails_the_step(self) -> None:
        ran = self.run_step(AUTO_MERGE_STEP, GH_STATUS="1")
        self.assertEqual(1, ran.returncode)
        self.assertEqual(1, len(self.gh_calls()))


if __name__ == "__main__":
    unittest.main()
