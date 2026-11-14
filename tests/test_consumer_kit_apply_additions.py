"""The additions template of the suite-selection consumer kit: a workflow that applies all additions artifacts of a
validation run whose recording check reported additions. Started by `workflow_run` when a pull-request run of the
validation caller failed, it commits the changed suite list as the updater App onto that pull request's branch, or
comments on the pull request with the reason and the manual route; started by hand with a run id, it proposes the
changed suite list as a pull request onto the reporting pull request's branch.

The structure tests read the template; the run tests run its shell steps with Bash against a fixture repository, a
local remote and a stand-in `gh` that answers the GitHub API from fixture documents and filters them with `jq`, as
`gh api --jq` does. A test that runs actionlint and zizmor over the installed template needs the pinned tools.

The last run tests run the same steps of Shared Workflows' own additions workflow, which `ci-policy` keeps equal to the
template, beside the rebuild workflow of its ETW consumer: the bound of each recognises the App's commits, and when both
act on the same failed run at most one of them pushes."""

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

from tests.ci_policy_support import expression_value, read_workflow
from tests.hygiene_fixtures import fixture_repository, tools_available
from tests.hygiene_process import hygiene
from tests.recording_fixtures import GIT_IDENTITY

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SELECTION_KIT = REPOSITORY_ROOT / "actions" / "suite-selection" / "consumer-kit"
TEMPLATE = SELECTION_KIT / "apply-additions.yml"
KIT_FULL_RECORDING = SELECTION_KIT / "full-recording.yml"
VALIDATION_CALLER = SELECTION_KIT / "validation.yml"
PIN_SOURCE = REPOSITORY_ROOT / ".github" / "workflows" / "full-recording.yml"
RECORDING = REPOSITORY_ROOT / "actions" / "recording"
REPOSITORY = "owner/consumer"
RUN_ID = "4242"
HEAD_BRANCH = "feat/adopt-suite-selection"
PROPOSAL = f"chore/apply-recorded-additions-{RUN_ID}"
SUBJECT = f"chore: apply the recorded additions of run {RUN_ID}"
MANUAL_ROUTE = f"gh workflow run apply-additions.yml -f run-id={RUN_ID}"
APP_STEPS = ("app-token", "Configure the GitHub App identity", "Commit the applied additions", "hygiene", "Install the pinned Gitleaks",
             "Scan the commit before pushing")
DISPATCH_STEPS = ("Push the proposal branch", "Create or update the proposal pull request")
AUTOMATIC_STEPS = ("Push the applied additions to the pull request's branch",)
# This repository's own additions workflow, kept equal to the template, and the rebuild workflow of its ETW consumer.
OWN_WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "apply-additions.yml"
REBUILD = REPOSITORY_ROOT / ".github" / "workflows" / "rebuild-etw-consumer.yml"
ADDITIONS_SUBJECT = "chore: apply the recorded additions of run 4100"
REBUILD_SUBJECT = "chore: rebuild the ETW consumer"

requires_bash_and_jq = unittest.skipUnless(sys.platform != "win32" and shutil.which("bash") and shutil.which("jq"),
                                           "the run tests run the template's Bash steps with jq off Windows")

# A stand-in gh: `gh api [--paginate] [--method M] <path> [-f k=v ...] [--jq <filter>]` answers from
# $GH_FIXTURES/<path with / as _>.json, filtered with jq -r like gh's own --jq, or fails like a 404; `gh pr list` prints
# $GH_FIXTURES/pr-list.txt; `gh pr comment` keeps its body file as $GH_FIXTURES/comment-<n>.md; every call is logged.
STAND_IN_GH = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "$GH_FIXTURES/calls.log"
if [ "$1" = api ]; then
  shift
  while [ "${1:-}" = --paginate ] || [ "${1:-}" = --method ]; do
    [ "$1" = --method ] && shift
    shift
  done
  path="$1"; shift
  while [ "${1:-}" = -f ]; do shift 2; done
  file="$GH_FIXTURES/${path//\//_}.json"
  if [ ! -f "$file" ]; then echo "gh: Not Found (HTTP 404)" >&2; exit 1; fi
  if [ "${1:-}" = --jq ]; then jq -r "$2" "$file"; else cat "$file"; fi
elif [ "$1 $2" = "pr list" ]; then
  cat "$GH_FIXTURES/pr-list.txt" 2>/dev/null || true
elif [ "$1 $2" = "pr comment" ]; then
  while [ "$#" -gt 0 ] && [ "$1" != --body-file ]; do shift; done
  cp "$2" "$GH_FIXTURES/comment-$(ls "$GH_FIXTURES" | grep -c '^comment-').md"
fi
"""


def step(steps: list[dict], key: str) -> dict:
    return next(item for item in steps if key in (item.get("id"), item.get("name")))


def position(steps: list[dict], key: str) -> int:
    return steps.index(step(steps, key))


def suite_list(lint_paths: list[str] | None = None) -> dict:
    return {"always": [], "suites": [
        {"name": "unit", "entries": ["checks/test_a.py"], "command": ["python3", "-m", "unittest", "-v"], "paths": ["checks/test_a.py"]},
        {"name": "lint", "entries": ["checks/lint.py"], "command": ["python3"], "paths": lint_paths or ["checks/lint.py"]},
    ]}


def additions(suite: str, entry: str, paths: list[str]) -> dict:
    return {"kind": "additions", "suite_list": ".github/suites.json", "suites": [
        {"suite": suite, "create": False, "recorded_entries": [entry],
         "reads": {"paths": paths, "existence_reads": [], "directory_memberships": [], "lookup_directories": [], "module_lookups": {}}}]}


def workflow_run_event(*, event: str = "pull_request", conclusion: str = "failure", head_repository: str | None = REPOSITORY) -> dict:
    return {"event_name": "workflow_run", "repository": REPOSITORY, "event": {"workflow_run": {
        "id": int(RUN_ID), "event": event, "conclusion": conclusion, "head_branch": HEAD_BRANCH,
        "head_repository": None if head_repository is None else {"full_name": head_repository}}}}


class ApplyAdditionsTemplateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.text, self.workflow = read_workflow(TEMPLATE)
        self.job = self.workflow["jobs"]["apply"]
        self.steps = self.job["steps"]

    def test_it_starts_by_hand_with_the_run_id_and_when_a_run_of_the_validation_caller_completes(self) -> None:
        self.assertEqual("Apply recorded additions", self.workflow["name"])
        _, caller = read_workflow(VALIDATION_CALLER)
        self.assertEqual({"workflow_dispatch": {"inputs": {"run-id": {
            "description": "The id of the validation run whose recording check reported additions", "required": "true", "type": "string"}}},
            "workflow_run": {"workflows": f"[{caller['name']}]", "types": "[completed]"}}, self.workflow["on"])
        self.assertRegex(self.text, r"(?m)^  workflow_run: # zizmor: ignore\[dangerous-triggers\] \S")
        self.assertEqual(["apply", "comment"], list(self.workflow["jobs"]))

    def test_runs_for_one_head_branch_share_one_concurrency_group_without_cancel_and_a_start_by_hand_keeps_its_group(self) -> None:
        self.assertNotIn("concurrency", self.workflow)
        self.assertEqual({"group": "${{ github.workflow }}-${{ inputs.run-id || format('branch-{0}', github.event.workflow_run.head_branch) }}",
                          "cancel-in-progress": "false"}, self.job["concurrency"])
        self.assertNotIn("concurrency", self.workflow["jobs"]["comment"])

    def test_the_job_acts_only_on_a_failed_pull_request_run_from_a_branch_of_the_repository_or_a_start_by_hand(self) -> None:
        condition = self.job["if"]
        self.assertTrue(expression_value(condition, {"github": workflow_run_event()}))
        self.assertTrue(expression_value(condition, {"github": {"event_name": "workflow_dispatch", "repository": REPOSITORY, "event": {}}}))
        for name, context in (("a fork", workflow_run_event(head_repository="someone/consumer")),
                              ("a deleted fork", workflow_run_event(head_repository=None)),
                              ("a run that passed", workflow_run_event(conclusion="success")),
                              ("a cancelled run", workflow_run_event(conclusion="cancelled")),
                              ("a push run", workflow_run_event(event="push")),
                              ("a dispatched run", workflow_run_event(event="repository_dispatch"))):
            with self.subTest(run=name):
                self.assertFalse(expression_value(condition, {"github": context}))

    def test_the_workflow_token_only_reads_and_only_the_app_token_writes_the_branch(self) -> None:
        self.assertIn("\npermissions: {}\n", self.text)
        self.assertEqual({"actions": "read", "contents": "read", "pull-requests": "read"}, self.job["permissions"])
        token = step(self.steps, "app-token")
        self.assertEqual({"client-id": "${{ vars.PUBLICATION_CHECKS_APP_CLIENT_ID }}", "private-key": "${{ secrets.PUBLICATION_CHECKS_APP_PRIVATE_KEY }}",
                          "permission-contents": "write", "permission-pull-requests": "write"}, token["with"])
        for name in DISPATCH_STEPS + AUTOMATIC_STEPS + ("Scan the commit before pushing",):
            with self.subTest(step=name):
                self.assertEqual("${{ steps.app-token.outputs.token }}", step(self.steps, name)["env"]["GH_TOKEN"])
        readers = [item for item in self.steps if "${{ github.token }}" in json.dumps(item)]
        self.assertEqual(["run", "additions"], [item["id"] for item in readers], "the workflow token only reads the run and its artifacts")
        self.assertEqual("false", step(self.steps, "checkout")["with"]["persist-credentials"])

    def test_only_the_comment_job_writes_with_the_workflow_token_and_it_checks_out_nothing(self) -> None:
        comment = self.workflow["jobs"]["comment"]
        self.assertEqual({"pull-requests": "write"}, comment["permissions"])
        self.assertEqual("apply", comment["needs"])
        self.assertEqual("always() && needs.apply.outputs.reason != ''", comment["if"])
        self.assertEqual([], [item for item in comment["steps"] if "uses" in item])
        self.assertEqual({"pull-request": "${{ steps.run.outputs.pull-request }}", "reason": "${{ steps.reason.outputs.reason }}"}, self.job["outputs"])

    def test_the_apply_step_runs_the_recording_step_at_v1_before_the_app_token_with_no_token_in_its_environment(self) -> None:
        apply = step(self.steps, "apply")
        self.assertEqual("Apply every additions artifact of the run", apply["name"])
        self.assertLess(position(self.steps, "recording"), position(self.steps, "apply"))
        self.assertEqual("CrankyAnt/SharedWorkflows/actions/recording@v1", step(self.steps, "recording")["uses"])
        self.assertIn('"$RECORDING/recording.py" apply --suites .github/suites.json', apply["run"])
        self.assertLess(position(self.steps, "apply"), position(self.steps, "app-token"))
        self.assertEqual({"RECORDING", "PULL_REQUEST", "HEAD_BRANCH", "REPORT"}, set(apply["env"]))
        for key in ("token", "secrets", "GH_", "GITHUB_TOKEN"):
            with self.subTest(key=key):
                self.assertNotIn(key, json.dumps(apply).replace("$GITHUB_STEP_SUMMARY", ""))
                self.assertNotIn(key, json.dumps(self.job["env"]))

    def test_each_action_reference_carries_the_pin_placeholder_and_the_shared_steps_v1(self) -> None:
        references = [item["uses"] for item in self.steps if "uses" in item]
        self.assertEqual(["actions/checkout@<pin>", "actions/download-artifact@<pin>", "CrankyAnt/SharedWorkflows/actions/recording@v1",
                          "actions/create-github-app-token@<pin>", "CrankyAnt/SharedWorkflows/actions/repository-hygiene@v1"], references)
        self.assertEqual([], re.findall(r"@[0-9a-f]{40}", self.text))

    def test_it_checks_out_the_run_s_ref_downloads_every_additions_artifact_and_applies_each_file_at_any_depth(self) -> None:
        self.assertEqual({"ref": "${{ steps.run.outputs.ref }}", "fetch-depth": "1", "persist-credentials": "false"},
                         step(self.steps, "checkout")["with"])
        self.assertEqual({"pattern": "recording-additions-*", "path": "${{ runner.temp }}/additions", "run-id": "${{ env.RUN_ID }}",
                          "github-token": "${{ github.token }}"}, step(self.steps, "additions")["with"])
        # One matching artifact lands directly in the download folder, several each in a folder named after it.
        apply = step(self.steps, "apply")["run"]
        self.assertIn("shopt -s globstar\n", apply)
        self.assertIn('"$RUNNER_TEMP"/additions/**/additions.json', apply)

    def test_the_steps_up_to_the_change_check_run_only_when_the_run_has_additions_to_apply(self) -> None:
        for key in ("checkout", "additions", "recording", "apply", "changes"):
            with self.subTest(step=key):
                self.assertEqual("steps.run.outputs.apply == 'true'", step(self.steps, key)["if"])

    def test_every_step_after_the_change_check_runs_only_when_the_suite_list_changed_and_the_pushes_by_event(self) -> None:
        for key in APP_STEPS + ("Check the changed files",):
            with self.subTest(step=key):
                self.assertEqual("steps.changes.outputs.changed == 'true'", step(self.steps, key)["if"])
        for key in DISPATCH_STEPS:
            with self.subTest(step=key):
                self.assertEqual("steps.changes.outputs.changed == 'true' && github.event_name == 'workflow_dispatch'", step(self.steps, key)["if"])
        for key in AUTOMATIC_STEPS:
            with self.subTest(step=key):
                self.assertEqual("steps.changes.outputs.changed == 'true' && github.event_name == 'workflow_run'", step(self.steps, key)["if"])

    def test_the_bound_recognises_the_subject_that_the_app_commits(self) -> None:
        bound = re.search(r"bound='(.+)'", step(self.steps, "run")["run"])[1]
        subject = self.job["env"]["SUBJECT"].replace("${{ inputs.run-id || github.event.workflow_run.id }}", "18234567890")
        self.assertRegex(subject, bound)
        self.assertIn('git commit -m "$SUBJECT" -m "[dependabot skip]"', step(self.steps, "Commit the applied additions")["run"])

    def test_a_failed_app_step_names_the_app_setting_and_the_reason_step_runs_last_on_the_automatic_route_only(self) -> None:
        stopped = step(self.steps, "stopped")
        for key in ("app-token", "push-branch", "push", "pull-request"):
            with self.subTest(step=key):
                self.assertIn(f"steps.{key}.outcome == 'failure'", stopped["if"])
        self.assertEqual("reason", self.steps[-1]["id"])
        self.assertEqual("always() && github.event_name == 'workflow_run'", self.steps[-1]["if"])

    def test_the_gitleaks_scan_and_the_app_identity_follow_the_full_recording(self) -> None:
        _, full = read_workflow(KIT_FULL_RECORDING)
        close = full["jobs"]["close"]["steps"]
        for name in ("Configure the GitHub App identity", "Install the pinned Gitleaks", "Scan the commit before pushing", "hygiene"):
            with self.subTest(step=name):
                ours, theirs = dict(step(self.steps, name)), dict(step(close, name))
                ours.pop("if"), theirs.pop("if")
                self.assertEqual(theirs, ours)


def installed_template() -> str:
    """The template as a consumer installs it: each `@<pin>` filled from the pin source, and the Gitleaks options."""
    pins = dict(re.findall(r"uses: ([\w./-]+)@([0-9a-f]{40} # v[\w.]+)", PIN_SOURCE.read_text(encoding="utf-8")))
    text = TEMPLATE.read_text(encoding="utf-8")
    text = re.sub(r"uses: ([\w./-]+)@<pin>", lambda found: f"uses: {found[1]}@{pins[found[1]]}", text)
    return text.replace("<gitleaks-options>", "--config .gitleaks.toml")


@unittest.skipUnless(tools_available("actionlint", "shellcheck", "zizmor"), "the pinned tools are neither in HYGIENE_TOOLS nor on PATH")
class InstalledTemplateLintTest(unittest.TestCase):
    def test_the_installed_template_passes_actionlint_and_zizmor(self) -> None:
        text = installed_template()
        self.assertEqual([], re.findall(r"<pin>|<gitleaks-options>", text), "every placeholder is filled")
        with tempfile.TemporaryDirectory() as temporary:
            repository = fixture_repository(Path(temporary) / "consumer", {".github/workflows/apply-additions.yml": text})
            for check in ("actionlint", "zizmor"):
                with self.subTest(check=check):
                    result = hygiene(check, repository)
                    self.assertEqual(0, result.returncode, result.stdout + result.stderr)


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def git(repository: Path, *arguments: str) -> str:
    return subprocess.run(["git", *GIT_IDENTITY, "-C", str(repository), *arguments], capture_output=True, text=True, check=True).stdout


@requires_bash_and_jq
class ApplyAdditionsSteps(unittest.TestCase):
    """Runs the shell steps of `WORKFLOW`, the template or a workflow kept equal to it, in order as the job runs them,
    with the step outputs passed on."""

    WORKFLOW = TEMPLATE

    def setUp(self) -> None:
        _, workflow = read_workflow(self.WORKFLOW)
        self.steps = workflow["jobs"]["apply"]["steps"]
        self.comment_steps = workflow["jobs"]["comment"]["steps"]
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
        self.head_sha = "0" * 40

    # --- the GitHub side ------------------------------------------------------------------------------------------

    def api(self, path: str, document: object) -> None:
        write(self.fixtures / (path.replace("/", "_") + ".json"), json.dumps(document))

    def given_run(self, *, event: str = "pull_request", head_repository: str | None = REPOSITORY, pull_requests: list[dict] | None = None,
                  artifacts: tuple[str, ...] = ("recording-additions-shard-1",), expired: bool = False, message: str = "feat: adopt\n\nbody") -> None:
        self.api(f"repos/{REPOSITORY}/actions/runs/{RUN_ID}", {
            "id": int(RUN_ID), "event": event, "head_branch": HEAD_BRANCH, "head_sha": self.head_sha, "head_commit": {"message": message},
            "head_repository": None if head_repository is None else {"full_name": head_repository},
            "pull_requests": [{"number": 7}] if pull_requests is None else pull_requests})
        self.api(f"repos/{REPOSITORY}/actions/runs/{RUN_ID}/artifacts",
                 {"total_count": len(artifacts), "artifacts": [{"name": name, "expired": expired} for name in artifacts]})

    def given_open_pull_requests(self, *heads: str) -> None:
        self.api(f"repos/{REPOSITORY}/pulls", [{"number": 7 + index, "head": {"ref": HEAD_BRANCH, "sha": sha}} for index, sha in enumerate(heads)])

    def calls(self) -> list[str]:
        log = self.fixtures / "calls.log"
        return log.read_text(encoding="utf-8").splitlines() if log.exists() else []

    def comments(self) -> list[str]:
        return [path.read_text(encoding="utf-8") for path in sorted(self.fixtures.glob("comment-*.md"))]

    # --- running a step -------------------------------------------------------------------------------------------

    def run_step(self, key: str, cwd: Path, *, event: str = "workflow_dispatch", run_id: str = RUN_ID, outputs: dict[str, dict[str, str]] | None = None,
                 extra: dict | None = None, steps: list[dict] | None = None) -> tuple[subprocess.CompletedProcess[str], dict[str, str]]:
        """Run one step's script as Actions would, with its `env` resolved from the event, the run id and earlier step outputs."""
        item = step(steps or self.steps, key)
        output_file = self.base / f"output-{len(list(self.base.glob('output-*')))}"
        output_file.touch()
        automatic = event == "workflow_run"
        context = {
            "inputs": {"run-id": "" if automatic else run_id},
            "github": {"token": "workflow-token", "event_name": event, "repository": REPOSITORY, "server_url": "https://github.example", "run_id": "9",
                       "event": {"workflow_run": {"id": run_id, "html_url": f"https://github.example/{REPOSITORY}/actions/runs/{run_id}"}} if automatic else {}},
            "runner": {"temp": str(self.runner_temp)},
            "steps": {"app-token": {"outputs": {"token": "app-token", "app-slug": "updater"}}, "recording": {"outputs": {"path": str(RECORDING)}},
                      "hygiene": {"outputs": {"path": "unused"}},
                      **{step_id: {"outputs": values} for step_id, values in (outputs or {}).items()}},
            **(extra or {}),
        }

        def resolve(text: str) -> str:
            def value(found: re.Match[str]) -> str:
                result = expression_value(found[1], context)
                return "" if result is None else str(result)
            return re.sub(r"\$\{\{\s*(.+?)\s*\}\}", value, text)

        job_env = self.job_env if steps is None else {}
        environment = {**os.environ, "PATH": f"{self.base / 'bin'}{os.pathsep}{os.environ['PATH']}", "GH_FIXTURES": str(self.fixtures),
                       "GITHUB_REPOSITORY": REPOSITORY, "GITHUB_REPOSITORY_OWNER": REPOSITORY.split("/")[0], "GITHUB_EVENT_NAME": event,
                       "GITHUB_SERVER_URL": "https://github.example", "GITHUB_RUN_ID": "9",
                       "GITHUB_OUTPUT": str(output_file), "GITHUB_STEP_SUMMARY": str(self.summary), "RUNNER_TEMP": str(self.runner_temp),
                       **{name: resolve(str(value)) for name, value in {**job_env, **item.get("env", {})}.items()}}
        environment.pop("GITHUB_TOKEN", None)
        ran = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", item["run"]], cwd=cwd, env=environment, capture_output=True, text=True)
        produced = dict(line.split("=", 1) for line in output_file.read_text(encoding="utf-8").splitlines() if "=" in line)
        return ran, produced

    def run_find_step(self, run_id: str = RUN_ID, event: str = "workflow_dispatch") -> tuple[subprocess.CompletedProcess[str], dict[str, str]]:
        return self.run_step("run", self.base, run_id=run_id, event=event)

    def reason(self, **outcomes: str) -> str:
        values = {"BOUND": "", "APPLIED": "", "CHANGED": "", "JOB_STATUS": "success", **outcomes}
        context = {"steps": {"run": {"outputs": {"bound": values["BOUND"]}}, "apply": {"outcome": values["APPLIED"]},
                             "changes": {"outputs": {"changed": values["CHANGED"]}}},
                   "job": {"status": values["JOB_STATUS"]}}
        ran, produced = self.run_step("reason", self.base, event="workflow_run", extra=context)
        self.assertEqual(0, ran.returncode, ran.stdout + ran.stderr)
        return produced["reason"]

    def comment(self, reason: str) -> subprocess.CompletedProcess[str]:
        ran, _ = self.run_step("Comment on the pull request with the reason and the manual route", self.base, event="workflow_run",
                               steps=self.comment_steps, extra={"needs": {"apply": {"outputs": {"pull-request": "7", "reason": reason}}}})
        return ran


@requires_bash_and_jq
class ApplyAdditionsRunTest(ApplyAdditionsSteps):
    """The template's shell steps."""

    # --- the refusals of a start by hand -------------------------------------------------------------------------

    def test_a_pull_request_run_with_additions_names_its_head_branch_and_pull_request(self) -> None:
        self.given_run(artifacts=["recording-additions-shard-1", "suite-results-linux", "recording-additions-windows"])
        ran, outputs = self.run_find_step()
        self.assertEqual(0, ran.returncode, ran.stdout + ran.stderr)
        self.assertEqual({"apply": "true", "ref": HEAD_BRANCH, "head-branch": HEAD_BRANCH, "head-sha": self.head_sha, "pull-request": "7"}, outputs)

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
        self.assertEqual({"apply": "true", "ref": HEAD_BRANCH, "head-branch": HEAD_BRANCH, "head-sha": self.head_sha, "pull-request": ""}, outputs)

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

    # --- the automatic route: what it acts on ---------------------------------------------------------------------

    def test_a_failed_run_with_additions_whose_head_is_an_open_pull_request_s_head_applies_at_that_head(self) -> None:
        self.given_run()
        self.given_open_pull_requests(self.head_sha)
        ran, outputs = self.run_find_step(event="workflow_run")
        self.assertEqual(0, ran.returncode, ran.stdout + ran.stderr)
        self.assertEqual({"apply": "true", "ref": self.head_sha, "head-branch": HEAD_BRANCH, "head-sha": self.head_sha, "pull-request": "7"}, outputs)

    def test_a_run_without_additions_artifacts_ends_without_a_commit_and_without_a_comment(self) -> None:
        for artifacts, expired in (([], False), (["suite-results-linux"], False), (["recording-additions-shard-1"], True)):
            with self.subTest(artifacts=artifacts, expired=expired):
                self.given_run(artifacts=artifacts, expired=expired)
                self.given_open_pull_requests(self.head_sha)
                ran, outputs = self.run_find_step(event="workflow_run")
                self.assertEqual(0, ran.returncode, ran.stdout + ran.stderr)
                self.assertEqual({"apply": "false"}, outputs)
                self.assertEqual("", self.reason())
        self.assertIn("uploaded no recording-additions-* artifact", self.summary.read_text(encoding="utf-8"))

    def test_a_moved_head_or_a_closed_pull_request_ends_without_a_commit_and_without_a_comment(self) -> None:
        for name, heads in (("moved", ("1" * 40,)), ("closed", ())):
            with self.subTest(case=name):
                self.given_run()
                self.given_open_pull_requests(*heads)
                ran, outputs = self.run_find_step(event="workflow_run")
                self.assertEqual(0, ran.returncode, ran.stdout + ran.stderr)
                self.assertEqual({"apply": "false"}, outputs)
                self.assertEqual("", self.reason())
        self.assertIn(f"No open pull request has the head {self.head_sha} of {HEAD_BRANCH}", self.summary.read_text(encoding="utf-8"))

    def test_at_the_bound_it_applies_nothing_and_comments_once_with_the_reason_and_the_manual_route(self) -> None:
        self.given_run(message="chore: apply the recorded additions of run 4100\n\n[dependabot skip]")
        self.given_open_pull_requests(self.head_sha)
        ran, outputs = self.run_find_step(event="workflow_run")
        self.assertEqual(0, ran.returncode, ran.stdout + ran.stderr)
        self.assertEqual({"pull-request": "7", "apply": "false", "bound": "true"}, outputs)
        self.assertEqual("bound", self.reason(BOUND="true"))
        ran = self.comment("bound")
        self.assertEqual(0, ran.returncode, ran.stdout + ran.stderr)
        (body,) = self.comments()
        self.assertIn(f"[run {RUN_ID}](https://github.example/{REPOSITORY}/actions/runs/{RUN_ID})", body)
        self.assertIn("already carries additions that the App applied, and the workflow applies one round per push", body)
        self.assertIn(MANUAL_ROUTE, body)
        self.assertEqual([f"pr comment 7 --repo {REPOSITORY} --body-file {self.runner_temp}/apply-additions-comment.md"],
                         [call for call in self.calls() if call.startswith("pr ")])

    def test_a_subject_that_only_resembles_the_app_s_applies_as_usual(self) -> None:
        for message in ("chore: apply the recorded additions of run 4100 (#12)", "feat: apply the recorded additions of run 4100",
                        "chore: apply the recorded additions of run x"):
            with self.subTest(message=message):
                self.given_run(message=message)
                self.given_open_pull_requests(self.head_sha)
                _, outputs = self.run_find_step(event="workflow_run")
                self.assertEqual("true", outputs.get("apply"))

    def test_the_comment_names_each_reason_and_refuses_an_unknown_one(self) -> None:
        for reason, text in (("unchanged", "applying them leaves the suite list unchanged."),
                             ("failed", "a step after applying them failed; [the workflow run](https://github.example/owner/consumer/actions/runs/9) names it.")):
            with self.subTest(reason=reason):
                ran = self.comment(reason)
                self.assertEqual(0, ran.returncode, ran.stdout + ran.stderr)
                self.assertIn(text, self.comments()[-1])
                self.assertIn(MANUAL_ROUTE, self.comments()[-1])
        ran = self.comment("other")
        self.assertEqual(1, ran.returncode)
        self.assertEqual(2, len(self.comments()))

    def test_the_reason_follows_the_outcomes_of_the_job(self) -> None:
        self.assertEqual("bound", self.reason(BOUND="true"))
        self.assertEqual("unchanged", self.reason(APPLIED="success", CHANGED="false"))
        self.assertEqual("failed", self.reason(APPLIED="success", CHANGED="true", JOB_STATUS="failure"))
        self.assertEqual("", self.reason(APPLIED="success", CHANGED="true"), "a committed change gets no comment")
        self.assertEqual("", self.reason(APPLIED="failure", JOB_STATUS="failure"), "a failed apply gets no comment")
        self.assertEqual("", self.reason(APPLIED="success", CHANGED="true", JOB_STATUS="cancelled"))
        self.assertEqual("", self.reason())

    # --- applying, committing and proposing -----------------------------------------------------------------------

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
        self.head_sha = git(work, "rev-parse", "HEAD").strip()
        return remote, work

    def downloaded(self, documents: dict[str, dict]) -> None:
        """The run's additions artifacts as `actions/download-artifact` places them by `pattern`: in a folder named after
        each artifact when the pattern matches several, and the one artifact's file directly in the download folder."""
        folder = self.runner_temp / "additions"
        folder.mkdir(exist_ok=True)
        for artifact, document in documents.items():
            write((folder if len(documents) == 1 else folder / artifact) / "additions.json", json.dumps(document))

    def run_outputs(self) -> dict[str, dict[str, str]]:
        return {"run": {"head-branch": HEAD_BRANCH, "head-sha": self.head_sha, "pull-request": "7"}}

    def run_apply_step(self, work: Path, event: str = "workflow_dispatch") -> subprocess.CompletedProcess[str]:
        ran, _ = self.run_step("apply", work, event=event, outputs=self.run_outputs())
        return ran

    def apply(self, work: Path, event: str = "workflow_dispatch") -> dict[str, str]:
        ran = self.run_apply_step(work, event)
        self.assertEqual(0, ran.returncode, ran.stdout + ran.stderr)
        ran, changes = self.run_step("changes", work, event=event)
        self.assertEqual(0, ran.returncode, ran.stdout + ran.stderr)
        return changes

    def test_the_additions_of_every_job_are_applied_and_proposed_onto_the_head_branch_changing_only_the_suite_list(self) -> None:
        remote, work = self.consumer(suite_list())
        self.downloaded({"recording-additions-shard-1": additions("unit", "checks/test_a.py", ["lib/data.txt"]),
                         "recording-additions-shard-2": additions("lint", "checks/lint.py", ["lib/rules.toml"]),
                         "recording-additions-windows": additions("unit", "checks/test_a.py", ["lib/rules.toml"])})
        self.assertEqual({"changed": "true"}, self.apply(work))
        ran, _ = self.run_step("Check the changed files", work)
        self.assertEqual(0, ran.returncode, ran.stderr)
        for key in ("Commit the applied additions", "Push the proposal branch"):
            ran, _ = self.run_step(key, work, outputs=self.run_outputs())
            self.assertEqual(0, ran.returncode, f"{key}: {ran.stdout}{ran.stderr}")
        changed = subprocess.run(["git", "-C", str(remote), "diff", "--name-only", HEAD_BRANCH, PROPOSAL], capture_output=True, text=True, check=True)
        self.assertEqual(".github/suites.json\n", changed.stdout)
        proposed = json.loads(git(remote, "show", f"{PROPOSAL}:.github/suites.json"))
        self.assertEqual({"unit": ["checks/test_a.py", "lib/data.txt", "lib/rules.toml"], "lint": ["checks/lint.py", "lib/rules.toml"]},
                         {item["name"]: item["paths"] for item in proposed["suites"]})
        self.assertEqual(SUBJECT, git(remote, "log", "-1", "--format=%B", PROPOSAL).strip(), "the proposal's commit keeps its one-line message")

        ran, _ = self.run_step("Create or update the proposal pull request", work, outputs=self.run_outputs())
        self.assertEqual(0, ran.returncode, ran.stdout + ran.stderr)
        create = [call for call in self.calls() if call.startswith("pr create")]
        self.assertEqual([f"pr create --repo {REPOSITORY} --base {HEAD_BRANCH} --head {PROPOSAL} "
                          f"--title {SUBJECT} --body-file {self.runner_temp}/apply-additions-report.md"], create)
        report = (self.runner_temp / "apply-additions-report.md").read_text(encoding="utf-8")
        self.assertIn(f"https://github.example/{REPOSITORY}/actions/runs/{RUN_ID}", report)
        self.assertIn(f"for the pull request #7 on {HEAD_BRANCH}.", report)

    def test_the_additions_of_a_run_with_one_artifact_are_applied_and_proposed_onto_the_head_branch(self) -> None:
        remote, work = self.consumer(suite_list())
        self.downloaded({"recording-additions-shard-1": additions("unit", "checks/test_a.py", ["lib/data.txt"])})
        self.assertEqual([self.runner_temp / "additions" / "additions.json"], list((self.runner_temp / "additions").iterdir()))
        self.assertEqual({"changed": "true"}, self.apply(work))
        for key in ("Check the changed files", "Commit the applied additions", "Push the proposal branch",
                    "Create or update the proposal pull request"):
            ran, _ = self.run_step(key, work, outputs=self.run_outputs())
            self.assertEqual(0, ran.returncode, f"{key}: {ran.stdout}{ran.stderr}")
        self.assertEqual(1, len([call for call in self.calls() if call.startswith(f"pr create --repo {REPOSITORY} --base {HEAD_BRANCH} --head {PROPOSAL} ")]))
        proposed = json.loads(git(remote, "show", f"{PROPOSAL}:.github/suites.json"))
        self.assertEqual({"unit": ["checks/test_a.py", "lib/data.txt"], "lint": ["checks/lint.py"]},
                         {item["name"]: item["paths"] for item in proposed["suites"]})

    def test_the_automatic_route_commits_the_applied_suite_list_as_the_app_onto_the_branch_and_opens_no_pull_request(self) -> None:
        remote, work = self.consumer(suite_list())
        before = self.head_sha
        self.given_open_pull_requests(before)
        self.downloaded({"recording-additions-shard-1": additions("unit", "checks/test_a.py", ["lib/data.txt"]),
                         "recording-additions-windows": additions("lint", "checks/lint.py", ["lib/rules.toml"])})
        self.assertEqual({"changed": "true"}, self.apply(work, "workflow_run"))
        for key in ("Check the changed files", "Commit the applied additions", "Push the applied additions to the pull request's branch"):
            ran, _ = self.run_step(key, work, event="workflow_run", outputs=self.run_outputs())
            self.assertEqual(0, ran.returncode, f"{key}: {ran.stdout}{ran.stderr}")
        self.assertEqual(before, git(remote, "rev-parse", f"{HEAD_BRANCH}^").strip(), "the App commit's parent is the run's head")
        self.assertEqual(f"{SUBJECT}\n\n[dependabot skip]", git(remote, "log", "-1", "--format=%B", HEAD_BRANCH).strip())
        self.assertEqual("updater[bot]\n", git(remote, "log", "-1", "--format=%an", HEAD_BRANCH))
        self.assertEqual(".github/suites.json\n", git(remote, "diff", "--name-only", before, HEAD_BRANCH))
        self.assertEqual({"unit": ["checks/test_a.py", "lib/data.txt"], "lint": ["checks/lint.py", "lib/rules.toml"]},
                         {item["name"]: item["paths"] for item in json.loads(git(remote, "show", f"{HEAD_BRANCH}:.github/suites.json"))["suites"]})
        self.assertEqual(f"{HEAD_BRANCH}\n", git(remote, "branch", "--format=%(refname:short)"), "no proposal branch")
        self.assertEqual([], [call for call in self.calls() if call.startswith("pr ")], "no pull request and no comment")
        self.assertEqual("", self.reason(APPLIED="success", CHANGED="true"))
        self.assertIn(f"Committed the applied additions of run {RUN_ID} onto {HEAD_BRANCH}", self.summary.read_text(encoding="utf-8"))

    def test_the_push_carries_a_lease_on_the_run_s_head_and_pushes_nothing_onto_a_moved_branch_or_for_a_closed_pull_request(self) -> None:
        for case in ("moved", "closed"):
            with self.subTest(case=case):
                shutil.rmtree(self.base / "remote.git", ignore_errors=True)
                for folder in ("seed", "work"):
                    shutil.rmtree(self.base / folder, ignore_errors=True)
                remote, work = self.consumer(suite_list())
                before = self.head_sha
                self.given_open_pull_requests(before)
                self.downloaded({"recording-additions-shard-1": additions("unit", "checks/test_a.py", ["lib/data.txt"])})
                self.apply(work, "workflow_run")
                ran, _ = self.run_step("Commit the applied additions", work, event="workflow_run", outputs=self.run_outputs())
                self.assertEqual(0, ran.returncode, ran.stderr)
                if case == "moved":
                    # A later push while the job ran; the API may still list the pull request with the run's head.
                    seed = self.base / "seed"
                    write(seed / "lib" / "data.txt", "moved\n")
                    subprocess.run(["git", *GIT_IDENTITY, "-C", str(seed), "commit", "-q", "-am", "feat: move on"], check=True)
                    subprocess.run(["git", "-C", str(seed), "push", "-q", str(remote), HEAD_BRANCH], check=True)
                else:
                    self.given_open_pull_requests()
                tip = git(remote, "rev-parse", HEAD_BRANCH)
                ran, _ = self.run_step("Push the applied additions to the pull request's branch", work, event="workflow_run", outputs=self.run_outputs())
                self.assertEqual(0, ran.returncode, ran.stdout + ran.stderr)
                self.assertEqual(tip, git(remote, "rev-parse", HEAD_BRANCH), "nothing is pushed")
                self.assertEqual("", self.reason(APPLIED="success", CHANGED="true"), "and nothing is commented")
                shutil.rmtree(self.runner_temp / "additions")
        summary = self.summary.read_text(encoding="utf-8")
        self.assertIn(f"The branch {HEAD_BRANCH} moved from ", summary)
        self.assertIn("any more; nothing is pushed.", summary)
        step_text = step(self.steps, "push-branch")["run"]
        self.assertIn('--force-with-lease="refs/heads/$HEAD_BRANCH:$HEAD_SHA" origin "HEAD:refs/heads/$HEAD_BRANCH"', step_text)

    def test_a_push_that_fails_on_an_unmoved_branch_fails_the_step_so_that_the_job_comments(self) -> None:
        _, work = self.consumer(suite_list())
        self.given_open_pull_requests(self.head_sha)
        subprocess.run(["git", "-C", str(work), "remote", "set-url", "--push", "origin", str(self.base / "missing.git")], check=True)
        self.downloaded({"recording-additions-shard-1": additions("unit", "checks/test_a.py", ["lib/data.txt"])})
        self.apply(work, "workflow_run")
        self.run_step("Commit the applied additions", work, event="workflow_run", outputs=self.run_outputs())
        ran, _ = self.run_step("Push the applied additions to the pull request's branch", work, event="workflow_run", outputs=self.run_outputs())
        self.assertNotEqual(0, ran.returncode)
        self.assertEqual("failed", self.reason(APPLIED="success", CHANGED="true", JOB_STATUS="failure"))

    def test_a_change_to_any_file_beside_the_suite_list_fails_the_check_before_the_token(self) -> None:
        _, work = self.consumer(suite_list())
        self.downloaded({"recording-additions-shard-1": additions("unit", "checks/test_a.py", ["lib/data.txt"])})
        self.apply(work, "workflow_run")
        write(work / "checks" / "lint.py", "changed\n")
        ran, _ = self.run_step("Check the changed files", work, event="workflow_run")
        self.assertEqual(1, ran.returncode)
        self.assertIn("Unexpected changed file: checks/lint.py", ran.stderr)

    def test_a_download_folder_without_an_additions_file_fails_with_the_cannot_read_error(self) -> None:
        _, work = self.consumer(suite_list())
        for layout in ([], ["recording-additions-shard-1/other.json"]):
            with self.subTest(layout=layout):
                shutil.rmtree(self.runner_temp / "additions", ignore_errors=True)
                (self.runner_temp / "additions").mkdir()
                for relative in layout:
                    write(self.runner_temp / "additions" / relative, "{}")
                ran = self.run_apply_step(work)
                self.assertNotEqual(0, ran.returncode)
                self.assertIn(f"cannot read {self.runner_temp}/additions/**/additions.json", ran.stderr)
                self.assertEqual("", git(work, "status", "--porcelain"))

    def test_an_additions_file_that_is_no_valid_additions_document_fails_the_apply_step(self) -> None:
        _, work = self.consumer(suite_list())
        document = additions("unit", "checks/test_a.py", ["lib/data.txt"])
        document["suites"][0]["reads"]["paths"] = "lib/data.txt"
        self.downloaded({"recording-additions-shard-1": document})
        ran = self.run_apply_step(work, "workflow_run")
        self.assertNotEqual(0, ran.returncode)
        self.assertIn("is no valid additions document: suites[0].reads: paths is no list of strings", ran.stderr)
        self.assertEqual("", git(work, "status", "--porcelain"))
        self.assertEqual("", self.reason(APPLIED="failure", JOB_STATUS="failure"), "a failed apply gets no comment")

    def test_an_open_proposal_is_updated_instead_of_opening_a_second(self) -> None:
        _, work = self.consumer(suite_list())
        write(self.runner_temp / "apply-additions-report.md", "report\n")
        write(self.fixtures / "pr-list.txt", "https://github.example/owner/consumer/pull/8\n")
        ran, _ = self.run_step("Create or update the proposal pull request", work, outputs=self.run_outputs())
        self.assertEqual(0, ran.returncode, ran.stdout + ran.stderr)
        self.assertEqual([], [call for call in self.calls() if call.startswith("pr create")])
        self.assertEqual(1, len([call for call in self.calls() if call.startswith("pr edit https://github.example/owner/consumer/pull/8")]))

    def test_additions_that_change_nothing_open_no_pull_request_and_the_summary_says_so(self) -> None:
        _, work = self.consumer(suite_list(["checks/lint.py", "lib/rules.toml"]))
        self.downloaded({"recording-additions-shard-1": additions("lint", "checks/lint.py", ["lib/rules.toml"])})
        self.assertEqual({"changed": "false"}, self.apply(work))
        self.assertIn(f"Applying the additions of run {RUN_ID} changes nothing in the suite list; no pull request opens.",
                      self.summary.read_text(encoding="utf-8"))
        self.assertEqual("", git(work, "status", "--porcelain"))

    def test_on_the_automatic_route_additions_that_change_nothing_commit_nothing_and_comment_once(self) -> None:
        _, work = self.consumer(suite_list(["checks/lint.py", "lib/rules.toml"]))
        self.downloaded({"recording-additions-shard-1": additions("lint", "checks/lint.py", ["lib/rules.toml"])})
        self.assertEqual({"changed": "false"}, self.apply(work, "workflow_run"))
        self.assertIn(f"Applying the additions of run {RUN_ID} changes nothing in the suite list; nothing is committed.",
                      self.summary.read_text(encoding="utf-8"))
        self.assertEqual("unchanged", self.reason(APPLIED="success", CHANGED="false"))
        self.assertEqual(0, self.comment("unchanged").returncode)
        self.assertEqual(1, len(self.comments()))
        self.assertIn(MANUAL_ROUTE, self.comments()[0])


@requires_bash_and_jq
class ApplyAdditionsBoundRunTest(ApplyAdditionsSteps):
    """The bound of this repository's additions workflow, run with Bash."""

    WORKFLOW = OWN_WORKFLOW

    def test_at_either_app_subject_it_applies_nothing_and_comments_with_the_reason_and_the_manual_route(self) -> None:
        for message in (f"{ADDITIONS_SUBJECT}\n\n[dependabot skip]", f"{REBUILD_SUBJECT}\n\nRebuilt from the source of {'0' * 40}."):
            with self.subTest(message=message.splitlines()[0]):
                for comment in self.fixtures.glob("comment-*.md"):
                    comment.unlink()
                self.given_run(message=message)
                self.given_open_pull_requests(self.head_sha)
                ran, outputs = self.run_find_step(event="workflow_run")
                self.assertEqual(0, ran.returncode, ran.stdout + ran.stderr)
                self.assertEqual({"pull-request": "7", "apply": "false", "bound": "true"}, outputs)
                self.assertEqual("bound", self.reason(BOUND="true"))
                ran = self.comment("bound")
                self.assertEqual(0, ran.returncode, ran.stdout + ran.stderr)
                (body,) = self.comments()
                self.assertIn(f"[run {RUN_ID}](https://github.example/{REPOSITORY}/actions/runs/{RUN_ID})", body)
                self.assertIn(MANUAL_ROUTE, body)
        self.assertIn(f'carries the subject "{REBUILD_SUBJECT}"', self.summary.read_text(encoding="utf-8"))

    def test_a_subject_that_only_resembles_one_of_the_app_s_applies_as_usual(self) -> None:
        for message in (f"{REBUILD_SUBJECT} (#12)", "fix: rebuild the ETW consumer", f"chore: {REBUILD_SUBJECT}"):
            with self.subTest(message=message):
                self.given_run(message=message)
                self.given_open_pull_requests(self.head_sha)
                _, outputs = self.run_find_step(event="workflow_run")
                self.assertEqual("true", outputs.get("apply"))


@requires_bash_and_jq
class RebuildBoundRunTest(ApplyAdditionsSteps):
    """The plan step of the rebuild workflow, run with Bash, commits nothing for a run on its own rebuild commit."""

    WORKFLOW = OWN_WORKFLOW

    def run_plan(self, message: str) -> tuple[subprocess.CompletedProcess[str], dict[str, str]]:
        _, rebuild = read_workflow(REBUILD)
        context = {"github": {"token": "workflow-token", "repository": REPOSITORY, "repository_owner": REPOSITORY.split("/")[0], "event": {
            "workflow_run": {"head_branch": "dependabot/nuget/trace-event", "head_sha": self.head_sha, "head_commit": {"message": message}}}}}
        return self.run_step("pull-request", self.base, event="workflow_run", steps=rebuild["jobs"]["plan"]["steps"], extra=context)

    def given_dependabot_pull_request(self, files: list[str]) -> None:
        self.api(f"repos/{REPOSITORY}/pulls", [{"number": 9, "user": {"login": "dependabot[bot]"}, "head": {"sha": self.head_sha}}])
        self.api(f"repos/{REPOSITORY}/pulls/9/files", [{"filename": name} for name in files])

    def test_a_run_on_the_rebuild_commit_rebuilds_nothing_and_reads_no_pull_request(self) -> None:
        self.given_dependabot_pull_request(["actions/recording/etw-consumer/packages.lock.json"])
        ran, outputs = self.run_plan(f"{REBUILD_SUBJECT}\n\nRebuilt from the source of {'1' * 40}, with the recording additions of x.")
        self.assertEqual(0, ran.returncode, ran.stdout + ran.stderr)
        self.assertEqual({"rebuild": "false"}, outputs)
        self.assertEqual([], self.calls(), "no API call")
        self.assertIn(f'carries the subject "{REBUILD_SUBJECT}" of the workflow\'s own rebuild', self.summary.read_text(encoding="utf-8"))

    def test_a_run_on_any_other_head_commit_rebuilds_when_the_pull_request_changes_the_source(self) -> None:
        self.given_dependabot_pull_request(["actions/recording/etw-consumer/packages.lock.json"])
        for message in (f"{ADDITIONS_SUBJECT}\n\n[dependabot skip]", "build(deps): bump TraceEvent", f"{REBUILD_SUBJECT} (#12)"):
            with self.subTest(message=message.splitlines()[0]):
                ran, outputs = self.run_plan(message)
                self.assertEqual(0, ran.returncode, ran.stdout + ran.stderr)
                self.assertEqual({"rebuild": "true"}, outputs)


@requires_bash_and_jq
class BothRoutesOnOneFailedRunTest(ApplyAdditionsSteps):
    """The additions workflow and the rebuild workflow both act on the same failed run of a Dependabot pull request and
    both find the branch at the run's head before either pushes: the first push lands, and the other pushes nothing."""

    WORKFLOW = OWN_WORKFLOW

    def setUp(self) -> None:
        super().setUp()
        self.remote = self.base / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(self.remote)], check=True)
        seed = self.base / "seed"
        write(seed / "tests" / "suites.json", json.dumps({"always": [], "suites": []}, indent=2) + "\n")
        write(seed / "actions" / "recording" / "etw-consumer" / "build" / "EtwConsumer.dll", "old build\n")
        for arguments in (("init", "-q", "-b", HEAD_BRANCH), ("add", "-A"), ("commit", "-q", "-m", "build(deps): bump TraceEvent"),
                          ("push", "-q", str(self.remote), HEAD_BRANCH)):
            git(seed, *arguments)
        self.head_sha = git(seed, "rev-parse", "HEAD").strip()
        self.given_open_pull_requests(self.head_sha)
        _, rebuild = read_workflow(REBUILD)
        self.rebuild_steps = rebuild["jobs"]["push"]["steps"]

    def clone(self, path: Path) -> Path:
        subprocess.run(["git", "clone", "-q", "--depth", "1", "--branch", HEAD_BRANCH, f"file://{self.remote}", str(path)], check=True, cwd=self.base)
        for key, value in (("user.name", "updater[bot]"), ("user.email", "1+updater[bot]@users.noreply.github.com"), ("commit.gpgsign", "false")):
            git(path, "config", key, value)
        return path

    def additions_commit(self) -> Path:
        """The additions workflow's checkout with its committed suite list, before its push."""
        work = self.clone(self.base / "additions")
        write(work / "tests" / "suites.json", json.dumps({"always": [], "suites": [], "module_lookups": {}}, indent=2) + "\n")
        ran, _ = self.run_step("Commit the applied additions", work, event="workflow_run", outputs=self.run_outputs())
        self.assertEqual(0, ran.returncode, ran.stdout + ran.stderr)
        return work

    def rebuild_commit(self) -> Path:
        """The rebuild workflow's push job with its committed rebuild and the branch fetched by its scan step."""
        root = self.base / "rebuild"
        work = self.clone(root / "pull-request")
        write(work / "actions" / "recording" / "etw-consumer" / "build" / "EtwConsumer.dll", "rebuild\n")
        git(work, "commit", "-q", "-am", REBUILD_SUBJECT)
        git(work, "fetch", "-q", "--depth=1", "origin", f"refs/heads/{HEAD_BRANCH}:refs/remotes/origin/{HEAD_BRANCH}")
        return root

    def run_outputs(self) -> dict[str, dict[str, str]]:
        return {"run": {"head-branch": HEAD_BRANCH, "head-sha": self.head_sha, "pull-request": "7"}}

    def push_additions(self, work: Path) -> subprocess.CompletedProcess[str]:
        ran, _ = self.run_step("push-branch", work, event="workflow_run", outputs=self.run_outputs())
        return ran

    def push_rebuild(self, root: Path) -> subprocess.CompletedProcess[str]:
        context = {"github": {"event": {"workflow_run": {"head_branch": HEAD_BRANCH, "head_sha": self.head_sha}}}}
        ran, _ = self.run_step("Push the rebuild to the pull request's branch", root, event="workflow_run", steps=self.rebuild_steps, extra=context)
        return ran

    def tip(self) -> str:
        return git(self.remote, "rev-parse", HEAD_BRANCH).strip()

    def test_when_the_additions_push_lands_first_the_rebuild_pushes_nothing(self) -> None:
        additions, rebuild = self.additions_commit(), self.rebuild_commit()
        ran = self.push_additions(additions)
        self.assertEqual(0, ran.returncode, ran.stdout + ran.stderr)
        landed = self.tip()
        self.assertEqual(git(additions, "rev-parse", "HEAD").strip(), landed)
        ran = self.push_rebuild(rebuild)
        self.assertEqual(0, ran.returncode, ran.stdout + ran.stderr)
        self.assertEqual(landed, self.tip(), "the rebuild pushes nothing")
        self.assertIn(f"The branch {HEAD_BRANCH} moved from {self.head_sha} to {landed}; nothing is pushed", self.summary.read_text(encoding="utf-8"))

    def test_when_the_rebuild_push_lands_first_the_additions_workflow_pushes_nothing(self) -> None:
        additions, rebuild = self.additions_commit(), self.rebuild_commit()
        ran = self.push_rebuild(rebuild)
        self.assertEqual(0, ran.returncode, ran.stdout + ran.stderr)
        landed = self.tip()
        self.assertEqual(git(rebuild / "pull-request", "rev-parse", "HEAD").strip(), landed)
        ran = self.push_additions(additions)
        self.assertEqual(0, ran.returncode, ran.stdout + ran.stderr)
        self.assertEqual(landed, self.tip(), "the additions workflow pushes nothing")
        self.assertIn(f"The branch {HEAD_BRANCH} moved from {self.head_sha} to {landed}; nothing is pushed", self.summary.read_text(encoding="utf-8"))

    def test_a_rebuild_push_that_fails_on_an_unmoved_branch_fails_the_step(self) -> None:
        rebuild = self.rebuild_commit()
        git(rebuild / "pull-request", "remote", "set-url", "--push", "origin", str(self.base / "missing.git"))
        ran = self.push_rebuild(rebuild)
        self.assertNotEqual(0, ran.returncode)
        self.assertEqual(self.head_sha, self.tip())


if __name__ == "__main__":
    unittest.main()
