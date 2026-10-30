"""Policy checks over this repository's periodic full recording: its schedule, its shards and Windows job under the
full-recording mode, and the closing job that joins the results and proposes the list changes through the updater App."""

from __future__ import annotations

import re
import unittest

from tests.ci_policy_support import REPOSITORY_ROOT, action_pin_pattern, read_workflow

WORKFLOWS = REPOSITORY_ROOT / ".github" / "workflows"
FULL_RECORDING = WORKFLOWS / "full-recording.yml"
VALIDATE = WORKFLOWS / "validate.yml"
ON_DEFAULT_BRANCH = "github.ref_name == github.event.repository.default_branch"


def step_index(steps: list[dict], text: str) -> int:
    return next(index for index, step in enumerate(steps) if text in str(step.get("run", "")))


def named(steps: list[dict], name: str) -> dict:
    return next(step for step in steps if step.get("name") == name)


class FullRecordingWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text, self.workflow = read_workflow(FULL_RECORDING)
        self.jobs = self.workflow["jobs"]

    def test_it_runs_on_a_weekly_schedule_and_by_hand_and_never_cancels_a_run(self) -> None:
        self.assertEqual({"schedule": [{"cron": "17 3 * * 1"}], "workflow_dispatch": None}, self.workflow["on"])
        self.assertEqual({"group": "${{ github.workflow }}-${{ github.ref }}", "cancel-in-progress": "false"}, self.workflow["concurrency"])

    def test_the_schedule_records_every_two_weeks_and_a_run_by_hand_always(self) -> None:
        plan = self.jobs["plan"]
        gate = plan["steps"][0]
        self.assertEqual("week", gate["id"])
        self.assertIn('if [ "$EVENT" != schedule ] || [ $((10#$(date -u +%V) % 2)) -eq 0 ]; then', gate["run"])
        self.assertEqual("${{ github.event_name }}", gate["env"]["EVENT"])
        for step in plan["steps"][1:]:
            self.assertEqual("steps.week.outputs.run == 'true'", step["if"])
        for name in ("linux", "windows"):
            self.assertIn("needs.plan.outputs.run == 'true'", self.jobs[name]["if"])
        self.assertEqual("${{ !cancelled() && needs.plan.outputs.run == 'true' }}", self.jobs["close"]["if"])

    def test_the_plan_shards_every_entry_file_with_the_full_mode_of_select(self) -> None:
        select = self.jobs["plan"]["steps"][step_index(self.jobs["plan"]["steps"], "suite_selection.py")]
        self.assertEqual("select", select["id"])
        self.assertEqual('PYTHONPYCACHEPREFIX="$RUNNER_TEMP/repo-pycache" python3 actions/suite-selection/suite_selection.py '
                         'select --root . --suites tests/suites.json --full --json-output "$GITHUB_OUTPUT" --summary "$GITHUB_STEP_SUMMARY"',
                         select["run"])
        self.assertEqual({"run": "${{ steps.week.outputs.run }}", "shards": "${{ steps.select.outputs.shards }}",
                          "windows": "${{ steps.select.outputs.windows }}"}, self.jobs["plan"]["outputs"])
        linux = self.jobs["linux"]
        self.assertEqual("needs.plan.outputs.run == 'true' && needs.plan.outputs.shards != '' && needs.plan.outputs.shards != '[]'", linux["if"])
        self.assertEqual({"fail-fast": "false", "matrix": {"shard": "${{ fromJSON(needs.plan.outputs.shards) }}"}}, linux["strategy"])
        self.assertEqual("needs.plan.outputs.run == 'true' && needs.plan.outputs.windows != ''", self.jobs["windows"]["if"])
        self.assertNotIn("strategy", self.jobs["windows"], "one Windows job without shards")

    def test_job_order_and_runner_labels(self) -> None:
        self.assertEqual(["plan", "linux", "windows", "close"], list(self.jobs))
        self.assertEqual("plan", self.jobs["linux"]["needs"])
        self.assertEqual("plan", self.jobs["windows"]["needs"])
        self.assertEqual("[plan, linux, windows]", self.jobs["close"]["needs"])
        self.assertEqual({"plan": "ubuntu-26.04", "linux": "ubuntu-26.04", "windows": "windows-2025", "close": "ubuntu-slim"},
                         {name: job["runs-on"] for name, job in self.jobs.items()})

    def test_every_job_holds_contents_read_and_no_checkout_persists_credentials(self) -> None:
        self.assertIn("\npermissions: {}\n\n", self.text)
        for name, job in self.jobs.items():
            with self.subTest(job=name):
                self.assertEqual({"contents": "read"}, job["permissions"])
                checkouts = [step for step in job["steps"] if str(step.get("uses", "")).startswith("actions/checkout@")]
                self.assertEqual(1, len(checkouts))
                self.assertEqual("false", checkouts[0]["with"]["persist-credentials"])
                self.assertNotIn("ref", checkouts[0]["with"], "every job records and proposes from the run's own commit")

    def test_the_actions_are_pinned_and_the_recording_step_is_used_by_local_path(self) -> None:
        external = sorted({uses.split("@")[0] for uses in re.findall(r"(?m)^\s*(?:- )?uses: ([^$\s]\S*)", self.text)})
        self.assertEqual(["actions/checkout", "actions/create-github-app-token", "actions/download-artifact", "actions/setup-dotnet", "actions/setup-node",
                          "actions/setup-python", "actions/upload-artifact"], external)
        for action in external:
            pinned = len(re.findall(action_pin_pattern(action).replace(r"^[ \t]*- uses: ", r"^[ \t]*(?:- )?uses: "), self.text))
            self.assertEqual(self.text.count(f"uses: {action}@"), pinned, f"every use of {action} is pinned to a SHA with its version")
        self.assertEqual([], re.findall(r"uses: \./", self.text))
        self.assertNotIn("actions/cache", self.text)


class FullRecordingJobTests(unittest.TestCase):
    """The shards and the Windows job run the validation's own setups and entry runs, with the recording in the full mode."""

    def setUp(self) -> None:
        _, workflow = read_workflow(FULL_RECORDING)
        _, validate = read_workflow(VALIDATE)
        self.jobs, self.validate = workflow["jobs"], validate["jobs"]

    def test_each_recording_job_starts_the_full_recording_mode(self) -> None:
        for job, run in (("linux", 'python3 "$RECORDING/recording.py" start --full'),
                         ("windows", 'python "$env:RECORDING\\recording.py" start --full')):
            with self.subTest(job=job):
                steps = self.jobs[job]["steps"]
                start = steps[step_index(steps, "recording.py\" start")]
                self.assertEqual("start", start["id"])
                self.assertEqual(run, start["run"])

    def test_each_recording_job_runs_the_validation_s_setups_and_entry_run_unchanged(self) -> None:
        def setups_and_entry_run(steps: list[dict]) -> list[dict]:
            """Every step before the check but the checkout and the start of the recording."""
            check = next(index for index, step in enumerate(steps) if step.get("uses") == "$/actions/recording" and "with" in step)
            return [step for step in steps[:check] if step.get("id") != "start" and not str(step.get("uses", "")).startswith("actions/checkout@")]

        for job in ("linux", "windows"):
            with self.subTest(job=job):
                ours = setups_and_entry_run(self.jobs[job]["steps"])
                self.assertEqual(setups_and_entry_run(self.validate[job]["steps"]), ours)
                self.assertGreater(len(ours), 5)
        self.assertEqual("${{ toJSON(matrix.shard) }}", self.jobs["linux"]["env"]["SHARD"])
        self.assertEqual("${{ needs.plan.outputs.windows }}", self.jobs["windows"]["env"]["WINDOWS"])

    def test_check_uploads_each_job_s_result_under_a_unique_name_unless_the_run_was_cancelled(self) -> None:
        names = []
        for job in ("linux", "windows"):
            steps = self.jobs[job]["steps"]
            check = next(step for step in steps if step.get("uses") == "$/actions/recording" and "with" in step)
            self.assertLess(step_index(steps, "exec --entry"), steps.index(check))
            self.assertEqual("${{ !cancelled() && steps.start.outcome == 'success' }}", check["if"])
            self.assertEqual({"check": "true", "suites": "tests/suites.json"}, {key: check["with"][key] for key in ("check", "suites")})
            names.append(check["with"]["artifact-name"])
        self.assertEqual(["full-recording-shard-${{ matrix.shard.shard }}", "full-recording-windows"], names)


class ClosingJobTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text, workflow = read_workflow(FULL_RECORDING)
        self.steps = workflow["jobs"]["close"]["steps"]

    def test_it_joins_every_result_applies_it_and_uploads_the_joined_result(self) -> None:
        download = next(step for step in self.steps if str(step.get("uses", "")).startswith("actions/download-artifact@"))
        self.assertEqual({"pattern": "full-recording-*", "path": "${{ runner.temp }}/full-recording"}, download["with"])
        apply = named(self.steps, "Join the results, apply the removals and write the duration estimates")
        self.assertIn('python3 "$RECORDING/recording.py" apply --suites tests/suites.json --summary "$GITHUB_STEP_SUMMARY" '
                      '"$RUNNER_TEMP"/full-recording/*/full-recording.json', apply["run"])
        upload = next(step for step in self.steps if str(step.get("uses", "")).startswith("actions/upload-artifact@"))
        self.assertLess(self.steps.index(apply), self.steps.index(upload))
        self.assertEqual("full-recording-joined", upload["with"]["name"])
        self.assertEqual("tests/suites.json\n${{ runner.temp }}/full-recording-report.md", upload["with"]["path"])

    def test_only_a_run_on_the_default_branch_with_changes_proposes_and_only_tests_suites_json_may_change(self) -> None:
        upload = next(step for step in self.steps if str(step.get("uses", "")).startswith("actions/upload-artifact@"))
        proposing = self.steps[self.steps.index(upload) + 1:]
        self.assertTrue(proposing)
        for step in proposing:
            if step.get("id") == "stopped":
                continue
            self.assertEqual(f"steps.changes.outputs.changed == 'true' && {ON_DEFAULT_BRANCH}", step["if"], step.get("name"))
        changes = named(self.steps, "Check the changed files")
        self.assertIn("tests/suites.json) ;;", changes["run"])
        self.assertIn('echo "Unexpected changed file: $path" >&2; exit 1', changes["run"])

    def test_the_pull_request_is_made_by_the_updater_app_with_contents_and_pull_requests_write(self) -> None:
        token = next(step for step in self.steps if str(step.get("uses", "")).startswith("actions/create-github-app-token@"))
        self.assertEqual({"client-id": "${{ vars.PUBLICATION_CHECKS_APP_CLIENT_ID }}",
                          "private-key": "${{ secrets.PUBLICATION_CHECKS_APP_PRIVATE_KEY }}",
                          "permission-contents": "write", "permission-pull-requests": "write"}, token["with"])
        self.assertEqual("app-token", token["id"])
        push = named(self.steps, "Push the review branch")
        pull_request = named(self.steps, "Create or update the review pull request")
        for step in (push, pull_request):
            self.assertEqual("${{ steps.app-token.outputs.token }}", step["env"]["GH_TOKEN"])
        self.assertIn('branch="chore/update-recorded-lists"', push["run"])
        self.assertIn('--force-with-lease="refs/heads/$branch:$remote_tip"', push["run"])
        self.assertIn('gh pr create --repo "$GITHUB_REPOSITORY" --base "$DEFAULT_BRANCH" --head "$branch"', pull_request["run"])
        self.assertIn('--body-file "$RUNNER_TEMP/full-recording-report.md"', pull_request["run"])
        self.assertNotIn("github.token", self.text, "no step pushes or opens the pull request with the workflow token")
        scan = named(self.steps, "Scan the commit before pushing")
        self.assertLess(self.steps.index(scan), self.steps.index(push))

    def test_a_failing_app_step_stops_the_run_and_names_the_question_to_the_user(self) -> None:
        stopped = next(step for step in self.steps if step.get("id") == "stopped")
        self.assertEqual("${{ failure() && (steps.app-token.outcome == 'failure' || steps.push.outcome == 'failure' "
                         "|| steps.pull-request.outcome == 'failure') }}", stopped["if"])
        self.assertIn("docs/agents/publication.md", stopped["run"])
        self.assertIn("exit 1", stopped["run"])


if __name__ == "__main__":
    unittest.main()
