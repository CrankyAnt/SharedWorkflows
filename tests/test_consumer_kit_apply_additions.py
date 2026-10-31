"""The additions template of the suite-selection consumer kit: a workflow started by hand with the id of a validation
run whose recording check reported additions, which applies all of that run's additions artifacts and proposes the
changed suite list through the updater App as a pull request onto the reporting pull request's branch.

The structure tests read the template; the run tests run its shell steps with Bash against a fixture repository, a
local remote and a stand-in `gh` that answers the GitHub API from fixture documents and filters them with `jq`, as
`gh api --jq` does."""

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
from tests.recording_fixtures import GIT_IDENTITY

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SELECTION_KIT = REPOSITORY_ROOT / "actions" / "suite-selection" / "consumer-kit"
TEMPLATE = SELECTION_KIT / "apply-additions.yml"
KIT_FULL_RECORDING = SELECTION_KIT / "full-recording.yml"
RECORDING = REPOSITORY_ROOT / "actions" / "recording"
REPOSITORY = "owner/consumer"
RUN_ID = "4242"
HEAD_BRANCH = "feat/adopt-suite-selection"
PROPOSAL = f"chore/apply-recorded-additions-{RUN_ID}"
APP_STEPS = ("app-token", "Configure the GitHub App identity", "Commit the applied additions", "hygiene", "Install the pinned Gitleaks",
             "Scan the commit before pushing", "Push the proposal branch", "Create or update the proposal pull request")

requires_bash_and_jq = unittest.skipUnless(sys.platform != "win32" and shutil.which("bash") and shutil.which("jq"),
                                           "the run tests run the template's Bash steps with jq off Windows")

# A stand-in gh: `gh api [--paginate] <path> [--jq <filter>]` answers from $GH_FIXTURES/<path with / as _>.json, filtered
# with jq -r like gh's own --jq, or fails like a 404; `gh pr list` prints $GH_FIXTURES/pr-list.txt; every call is logged.
STAND_IN_GH = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "$GH_FIXTURES/calls.log"
if [ "$1" = api ]; then
  shift
  [ "$1" = --paginate ] && shift
  path="$1"; shift
  file="$GH_FIXTURES/${path//\//_}.json"
  if [ ! -f "$file" ]; then echo "gh: Not Found (HTTP 404)" >&2; exit 1; fi
  if [ "${1:-}" = --jq ]; then jq -r "$2" "$file"; else cat "$file"; fi
elif [ "$1 $2" = "pr list" ]; then
  cat "$GH_FIXTURES/pr-list.txt" 2>/dev/null || true
fi
"""


def step(steps: list[dict], key: str) -> dict:
    return next(item for item in steps if key in (item.get("id"), item.get("name")))


def suite_list(lint_paths: list[str] | None = None) -> dict:
    return {"always": [], "suites": [
        {"name": "unit", "entries": ["checks/test_a.py"], "command": ["python3", "-m", "unittest", "-v"], "paths": ["checks/test_a.py"]},
        {"name": "lint", "entries": ["checks/lint.py"], "command": ["python3"], "paths": lint_paths or ["checks/lint.py"]},
    ]}


def additions(suite: str, entry: str, paths: list[str]) -> dict:
    return {"kind": "additions", "suite_list": ".github/suites.json", "suites": [
        {"suite": suite, "create": False, "recorded_entries": [entry],
         "reads": {"paths": paths, "existence_reads": [], "directory_memberships": [], "lookup_directories": [], "module_lookups": {}}}]}


class ApplyAdditionsTemplateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.text, self.workflow = read_workflow(TEMPLATE)
        self.steps = self.workflow["jobs"]["apply"]["steps"]

    def test_it_starts_by_hand_with_the_run_id_and_one_job(self) -> None:
        self.assertEqual("Apply recorded additions", self.workflow["name"])
        self.assertEqual({"workflow_dispatch": {"inputs": {"run-id": {
            "description": "The id of the validation run whose recording check reported additions", "required": "true", "type": "string"}}}},
            self.workflow["on"])
        self.assertEqual(["apply"], list(self.workflow["jobs"]))
        self.assertEqual({"group": "${{ github.workflow }}-${{ inputs.run-id }}", "cancel-in-progress": "false"}, self.workflow["concurrency"])

    def test_the_workflow_token_only_reads_and_only_the_app_token_writes(self) -> None:
        self.assertIn("\npermissions: {}\n", self.text)
        self.assertEqual({"actions": "read", "contents": "read"}, self.workflow["jobs"]["apply"]["permissions"])
        token = step(self.steps, "app-token")
        self.assertEqual({"client-id": "${{ vars.PUBLICATION_CHECKS_APP_CLIENT_ID }}", "private-key": "${{ secrets.PUBLICATION_CHECKS_APP_PRIVATE_KEY }}",
                          "permission-contents": "write", "permission-pull-requests": "write"}, token["with"])
        for name in ("Push the proposal branch", "Create or update the proposal pull request", "Scan the commit before pushing"):
            with self.subTest(step=name):
                self.assertEqual("${{ steps.app-token.outputs.token }}", step(self.steps, name)["env"]["GH_TOKEN"])
        readers = [item for item in self.steps if "${{ github.token }}" in json.dumps(item)]
        self.assertEqual(["run", "additions"], [item["id"] for item in readers], "the workflow token only reads the run and its artifacts")
        self.assertEqual("false", step(self.steps, "checkout")["with"]["persist-credentials"])

    def test_each_action_reference_carries_the_pin_placeholder_and_the_shared_steps_v1(self) -> None:
        references = [item["uses"] for item in self.steps if "uses" in item]
        self.assertEqual(["actions/checkout@<pin>", "actions/download-artifact@<pin>", "CrankyAnt/SharedWorkflows/actions/recording@v1",
                          "actions/create-github-app-token@<pin>", "CrankyAnt/SharedWorkflows/actions/repository-hygiene@v1"], references)
        self.assertEqual([], re.findall(r"@[0-9a-f]{40}", self.text))

    def test_it_checks_out_the_head_branch_and_downloads_every_additions_artifact_of_the_run_into_its_own_directory(self) -> None:
        self.assertEqual({"ref": "${{ steps.run.outputs.head-branch }}", "fetch-depth": "1", "persist-credentials": "false"},
                         step(self.steps, "checkout")["with"])
        self.assertEqual({"pattern": "recording-additions-*", "path": "${{ runner.temp }}/additions", "run-id": "${{ inputs.run-id }}",
                          "github-token": "${{ github.token }}"}, step(self.steps, "additions")["with"])
        self.assertIn('"$RUNNER_TEMP"/additions/*/additions.json', step(self.steps, "Apply every additions artifact of the run")["run"])

    def test_every_step_after_the_change_check_runs_only_when_the_suite_list_changed(self) -> None:
        for key in APP_STEPS + ("Check the changed files",):
            with self.subTest(step=key):
                self.assertEqual("steps.changes.outputs.changed == 'true'", step(self.steps, key)["if"])

    def test_the_gitleaks_scan_and_the_app_identity_follow_the_full_recording(self) -> None:
        _, full = read_workflow(KIT_FULL_RECORDING)
        close = full["jobs"]["close"]["steps"]
        for name in ("Configure the GitHub App identity", "Install the pinned Gitleaks", "Scan the commit before pushing", "hygiene"):
            with self.subTest(step=name):
                ours, theirs = dict(step(self.steps, name)), dict(step(close, name))
                ours.pop("if"), theirs.pop("if")
                self.assertEqual(theirs, ours)


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@requires_bash_and_jq
class ApplyAdditionsRunTest(unittest.TestCase):
    """The template's shell steps, run in order as the job runs them, with the step outputs passed on."""

    def setUp(self) -> None:
        _, workflow = read_workflow(TEMPLATE)
        self.steps = workflow["jobs"]["apply"]["steps"]
        self.job_env = workflow["jobs"]["apply"]["env"]
        self.base = Path(tempfile.mkdtemp(prefix="apply-additions-"))
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.fixtures = self.base / "gh"
        self.fixtures.mkdir()
        write(self.base / "bin" / "gh", STAND_IN_GH).chmod(0o755)
        self.runner_temp = self.base / "runner-temp"
        self.runner_temp.mkdir()
        self.summary = self.base / "summary.md"
        self.summary.touch()

    # --- the GitHub side ------------------------------------------------------------------------------------------

    def api(self, path: str, document: dict) -> None:
        write(self.fixtures / (path.replace("/", "_") + ".json"), json.dumps(document))

    def given_run(self, *, event: str = "pull_request", head_repository: str | None = REPOSITORY, pull_requests: list[dict] | None = None,
                  artifacts: tuple[str, ...] = ("recording-additions-shard-1",), expired: bool = False) -> None:
        self.api(f"repos/{REPOSITORY}/actions/runs/{RUN_ID}", {
            "id": int(RUN_ID), "event": event, "head_branch": HEAD_BRANCH,
            "head_repository": None if head_repository is None else {"full_name": head_repository},
            "pull_requests": [{"number": 7}] if pull_requests is None else pull_requests})
        self.api(f"repos/{REPOSITORY}/actions/runs/{RUN_ID}/artifacts",
                 {"total_count": len(artifacts), "artifacts": [{"name": name, "expired": expired} for name in artifacts]})

    def calls(self) -> list[str]:
        log = self.fixtures / "calls.log"
        return log.read_text(encoding="utf-8").splitlines() if log.exists() else []

    # --- running a step -------------------------------------------------------------------------------------------

    def run_step(self, key: str, cwd: Path, *, run_id: str = RUN_ID, outputs: dict[str, dict[str, str]] | None = None
                 ) -> tuple[subprocess.CompletedProcess[str], dict[str, str]]:
        """Run one step's script as Actions would, with its `env` resolved from the run id and earlier step outputs."""
        item = step(self.steps, key)
        output_file = self.base / f"output-{len(list(self.base.glob('output-*')))}"
        output_file.touch()
        values = {"inputs.run-id": run_id, "github.token": "workflow-token", "steps.app-token.outputs.token": "app-token",
                  "steps.app-token.outputs.app-slug": "updater", "runner.temp": str(self.runner_temp),
                  "steps.recording.outputs.path": str(RECORDING), "steps.hygiene.outputs.path": "unused"}
        for step_id, step_outputs in (outputs or {}).items():
            values.update({f"steps.{step_id}.outputs.{name}": value for name, value in step_outputs.items()})

        def resolve(text: str) -> str:
            return re.sub(r"\$\{\{\s*([\w.-]+)\s*\}\}", lambda found: values[found[1]], text)

        environment = {**os.environ, "PATH": f"{self.base / 'bin'}{os.pathsep}{os.environ['PATH']}", "GH_FIXTURES": str(self.fixtures),
                       "GITHUB_REPOSITORY": REPOSITORY, "GITHUB_SERVER_URL": "https://github.example", "GITHUB_RUN_ID": "9",
                       "GITHUB_OUTPUT": str(output_file), "GITHUB_STEP_SUMMARY": str(self.summary), "RUNNER_TEMP": str(self.runner_temp),
                       **{name: resolve(str(value)) for name, value in {**self.job_env, **item.get("env", {})}.items()}}
        environment.pop("GITHUB_TOKEN", None)
        ran = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", item["run"]], cwd=cwd, env=environment, capture_output=True, text=True)
        produced = dict(line.split("=", 1) for line in output_file.read_text(encoding="utf-8").splitlines() if "=" in line)
        return ran, produced

    def run_find_step(self, run_id: str = RUN_ID) -> tuple[subprocess.CompletedProcess[str], dict[str, str]]:
        return self.run_step("run", self.base, run_id=run_id)

    # --- the refusals ---------------------------------------------------------------------------------------------

    def test_a_pull_request_run_with_additions_names_its_head_branch_and_pull_request(self) -> None:
        self.given_run(artifacts=["recording-additions-shard-1", "suite-results-linux", "recording-additions-windows"])
        ran, outputs = self.run_find_step()
        self.assertEqual(0, ran.returncode, ran.stdout + ran.stderr)
        self.assertEqual({"head-branch": HEAD_BRANCH, "pull-request": "7"}, outputs)

    def test_a_run_without_an_additions_artifact_fails_and_names_the_reason(self) -> None:
        for artifacts, expired in (([], False), (["suite-results-linux"], False), (["recording-additions-shard-1"], True)):
            with self.subTest(artifacts=artifacts, expired=expired):
                self.given_run(artifacts=artifacts, expired=expired)
                ran, outputs = self.run_find_step()
                self.assertEqual(1, ran.returncode)
                self.assertIn(f"::error::Run {RUN_ID} has no recording-additions-* artifact", ran.stdout)
                self.assertEqual({}, outputs)

    def test_a_run_of_no_pull_request_fails_and_names_the_reason(self) -> None:
        for event in ("push", "repository_dispatch"):
            with self.subTest(event=event):
                self.given_run(event=event, pull_requests=[])
                ran, outputs = self.run_find_step()
                self.assertEqual(1, ran.returncode)
                self.assertIn(f"::error::Run {RUN_ID} ran on the event {event} and belongs to no pull request", ran.stdout)
                self.assertEqual({}, outputs)

    def test_an_older_run_of_a_pull_request_whose_head_moved_on_still_proposes_onto_its_branch(self) -> None:
        # GitHub lists a run's pull requests only while their head matches the run; the branch is the run's own.
        self.given_run(pull_requests=[])
        ran, outputs = self.run_find_step()
        self.assertEqual(0, ran.returncode, ran.stdout + ran.stderr)
        self.assertEqual({"head-branch": HEAD_BRANCH, "pull-request": ""}, outputs)

    def test_a_run_of_a_pull_request_from_a_fork_fails_and_names_the_reason(self) -> None:
        for head_repository, shown in (("someone/consumer", "someone/consumer"), (None, "a deleted repository")):
            with self.subTest(head_repository=head_repository):
                self.given_run(head_repository=head_repository, pull_requests=[])
                ran, outputs = self.run_find_step()
                self.assertEqual(1, ran.returncode)
                self.assertIn(f"::error::Run {RUN_ID} comes from a pull request whose head branch lies in the fork {shown}", ran.stdout)
                self.assertEqual({}, outputs)

    def test_an_unknown_run_or_an_id_that_is_no_number_fails_before_any_write(self) -> None:
        ran, _ = self.run_find_step()
        self.assertEqual(1, ran.returncode)
        self.assertIn(f"::error::Run {RUN_ID} of {REPOSITORY} could not be read", ran.stdout)
        ran, _ = self.run_find_step("1; echo injected")
        self.assertEqual(1, ran.returncode)
        self.assertIn("::error::The run id must be a number", ran.stdout)
        self.assertNotIn("injected", ran.stdout.replace("1; echo injected", ""))
        self.assertEqual(1, len(self.calls()), "the id that is no number reaches no API call")

    # --- applying and proposing -----------------------------------------------------------------------------------

    def consumer(self, suites: dict) -> tuple[Path, Path]:
        """A consumer's remote with the reporting pull request's head branch, and a checkout of that branch."""
        remote, work = self.base / "remote.git", self.base / "work"
        subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
        seed = self.base / "seed"
        for relative in ("checks/test_a.py", "checks/lint.py", "lib/data.txt", "lib/rules.toml"):
            write(seed / relative, "")
        write(seed / ".github" / "suites.json", json.dumps(suites, indent=2) + "\n")
        for arguments in (("init", "-q", "-b", HEAD_BRANCH), ("add", "-A"), ("commit", "-q", "-m", "feat: adopt"),
                          ("push", "-q", str(remote), HEAD_BRANCH)):
            subprocess.run(["git", *GIT_IDENTITY, "-C", str(seed), *arguments], check=True)
        # From the fixture's own directory: git also probes a file:// address as a relative path, which must not resolve
        # into this repository, where the recording would store it.
        subprocess.run(["git", "clone", "-q", "--depth", "1", "--branch", HEAD_BRANCH, f"file://{remote}", str(work)], check=True,
                       cwd=self.base)
        # The real job gets its identity from the App step; the fixture gives the checkout one so that the commit step runs.
        for key, value in (("user.name", "updater[bot]"), ("user.email", "1+updater[bot]@users.noreply.github.com"), ("commit.gpgsign", "false")):
            subprocess.run(["git", "-C", str(work), "config", key, value], check=True)
        return remote, work

    def downloaded(self, artifact: str, document: dict) -> None:
        write(self.runner_temp / "additions" / artifact / "additions.json", json.dumps(document))

    def apply(self, work: Path) -> dict[str, str]:
        ran, _ = self.run_step("Apply every additions artifact of the run", work, outputs={"run": {"pull-request": "7", "head-branch": HEAD_BRANCH}})
        self.assertEqual(0, ran.returncode, ran.stdout + ran.stderr)
        ran, changes = self.run_step("changes", work)
        self.assertEqual(0, ran.returncode, ran.stdout + ran.stderr)
        return changes

    def test_the_additions_of_every_job_are_applied_and_proposed_onto_the_head_branch_changing_only_the_suite_list(self) -> None:
        remote, work = self.consumer(suite_list())
        self.downloaded("recording-additions-shard-1", additions("unit", "checks/test_a.py", ["lib/data.txt"]))
        self.downloaded("recording-additions-shard-2", additions("lint", "checks/lint.py", ["lib/rules.toml"]))
        self.downloaded("recording-additions-windows", additions("unit", "checks/test_a.py", ["lib/rules.toml"]))
        self.assertEqual({"changed": "true"}, self.apply(work))
        ran, _ = self.run_step("Check the changed files", work)
        self.assertEqual(0, ran.returncode, ran.stderr)
        for key in ("Commit the applied additions", "Push the proposal branch"):
            ran, _ = self.run_step(key, work, outputs={"run": {"head-branch": HEAD_BRANCH, "pull-request": "7"}})
            self.assertEqual(0, ran.returncode, f"{key}: {ran.stdout}{ran.stderr}")
        changed = subprocess.run(["git", "-C", str(remote), "diff", "--name-only", HEAD_BRANCH, PROPOSAL], capture_output=True, text=True, check=True)
        self.assertEqual(".github/suites.json\n", changed.stdout)
        proposed = json.loads(subprocess.run(["git", "-C", str(remote), "show", f"{PROPOSAL}:.github/suites.json"],
                                             capture_output=True, text=True, check=True).stdout)
        self.assertEqual({"unit": ["checks/test_a.py", "lib/data.txt", "lib/rules.toml"], "lint": ["checks/lint.py", "lib/rules.toml"]},
                         {item["name"]: item["paths"] for item in proposed["suites"]})
        subject = subprocess.run(["git", "-C", str(remote), "log", "-1", "--format=%s", PROPOSAL], capture_output=True, text=True, check=True)
        self.assertEqual(f"chore: apply the recorded additions of run {RUN_ID}\n", subject.stdout)

        ran, _ = self.run_step("Create or update the proposal pull request", work, outputs={"run": {"head-branch": HEAD_BRANCH, "pull-request": "7"}})
        self.assertEqual(0, ran.returncode, ran.stdout + ran.stderr)
        create = [call for call in self.calls() if call.startswith("pr create")]
        self.assertEqual([f"pr create --repo {REPOSITORY} --base {HEAD_BRANCH} --head {PROPOSAL} "
                          f"--title chore: apply the recorded additions of run {RUN_ID} --body-file {self.runner_temp}/apply-additions-report.md"], create)
        report = (self.runner_temp / "apply-additions-report.md").read_text(encoding="utf-8")
        self.assertIn(f"https://github.example/{REPOSITORY}/actions/runs/{RUN_ID}", report)
        self.assertIn(f"for the pull request #7 on {HEAD_BRANCH}.", report)

    def test_an_open_proposal_is_updated_instead_of_opening_a_second(self) -> None:
        _, work = self.consumer(suite_list())
        write(self.runner_temp / "apply-additions-report.md", "report\n")
        write(self.fixtures / "pr-list.txt", "https://github.example/owner/consumer/pull/8\n")
        ran, _ = self.run_step("Create or update the proposal pull request", work, outputs={"run": {"head-branch": HEAD_BRANCH, "pull-request": "7"}})
        self.assertEqual(0, ran.returncode, ran.stdout + ran.stderr)
        self.assertEqual([], [call for call in self.calls() if call.startswith("pr create")])
        self.assertEqual(1, len([call for call in self.calls() if call.startswith("pr edit https://github.example/owner/consumer/pull/8")]))

    def test_additions_that_change_nothing_open_no_pull_request_and_the_summary_says_so(self) -> None:
        _, work = self.consumer(suite_list(["checks/lint.py", "lib/rules.toml"]))
        self.downloaded("recording-additions-shard-1", additions("lint", "checks/lint.py", ["lib/rules.toml"]))
        self.assertEqual({"changed": "false"}, self.apply(work))
        self.assertIn(f"Applying the additions of run {RUN_ID} changes nothing in the suite list; no pull request opens.",
                      self.summary.read_text(encoding="utf-8"))
        self.assertEqual("", subprocess.run(["git", "-C", str(work), "status", "--porcelain"], capture_output=True, text=True, check=True).stdout)


if __name__ == "__main__":
    unittest.main()
