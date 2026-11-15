"""Policy checks over the full recording, a part of the automation workflow: its two-weekly gate, its shards and Windows
job under the full-recording mode, and the closing job that joins the results and proposes the list changes through the
updater App, as a pull request that merges itself."""

from __future__ import annotations

import json
import re
import unittest

from tests.ci_policy_support import REPOSITORY_ROOT, action_pin_pattern, read_workflow

WORKFLOWS = REPOSITORY_ROOT / ".github" / "workflows"
FULL_RECORDING = WORKFLOWS / "automation.yml"
CALLER = WORKFLOWS / "automation-caller.yml"
PART = "full-recording"
LINUX, WINDOWS, CLOSE = "full-recording-linux", "full-recording-windows", "full-recording-close"
VALIDATE = WORKFLOWS / "validate.yml"
APPLY_STEP = "Join the results, apply the removals and write the duration estimates"
ON_DEFAULT_BRANCH = "github.ref_name == github.event.repository.default_branch"
VERIFY_TOKEN = {"description": "For full-recording, mint the token of the updater App even without a change to propose, to verify its private key",
                "required": "false", "type": "boolean", "default": "false"}
SUITES = "${{ needs.full-recording.outputs.suites }}"
PLANNED = "fromJSON(needs.plan.outputs.plan).parts['full-recording']"


def step_index(steps: list[dict], text: str) -> int:
    return next(index for index, step in enumerate(steps) if text in str(step.get("run", "")))


def named(steps: list[dict], name: str) -> dict:
    return next(step for step in steps if step.get("name") == name)


class FullRecordingWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text, self.workflow = read_workflow(FULL_RECORDING)
        self.jobs = {name: job for name, job in self.workflow["jobs"].items() if name.startswith(PART)}

    def test_it_runs_on_the_caller_s_weekly_schedule_and_by_hand_and_no_closing_job_cancels_another(self) -> None:
        _, caller = read_workflow(CALLER)
        self.assertEqual([{"cron": "17 3 * * 1"}], caller["on"]["schedule"])
        self.assertEqual(VERIFY_TOKEN, caller["on"]["workflow_dispatch"]["inputs"]["verify-token"])
        self.assertEqual(f"${{{{ (github.event_name == 'schedule' || (github.event_name == 'workflow_dispatch' && github.event.inputs.part == 'full-recording')) "
                         f"&& {PLANNED}.run }}}}", self.jobs[PART]["if"])
        self.assertEqual({"group": "${{ github.workflow }}-full-recording-${{ github.ref }}", "cancel-in-progress": "false"}, self.jobs[CLOSE]["concurrency"])

    def test_the_schedule_records_every_two_weeks_and_a_run_by_hand_always(self) -> None:
        plan = self.jobs[PART]
        gate = plan["steps"][0]
        self.assertEqual("week", gate["id"])
        self.assertIn('if [ "$EVENT" != schedule ] || [ $((10#$(date -u +%V) % 2)) -eq 0 ]; then', gate["run"])
        self.assertEqual("${{ github.event_name }}", gate["env"]["EVENT"])
        for step in plan["steps"][1:]:
            self.assertEqual("steps.week.outputs.run == 'true'", step["if"])
        for name in (LINUX, WINDOWS):
            self.assertIn("needs.full-recording.outputs.run == 'true'", self.jobs[name]["if"])
        self.assertEqual("${{ !cancelled() && needs.full-recording.outputs.run == 'true' }}", self.jobs[CLOSE]["if"])

    def test_the_plan_shards_every_entry_file_with_the_full_mode_of_select(self) -> None:
        steps = self.jobs[PART]["steps"]
        select = steps[step_index(steps, "suite_selection.py")]
        self.assertEqual("select", select["id"])
        self.assertEqual("$/actions/suite-selection", next(step for step in steps if step.get("id") == "selection")["uses"])
        self.assertEqual('PYTHONPYCACHEPREFIX="$RUNNER_TEMP/repo-pycache" python3 "$SELECTION/suite_selection.py" '
                         'select --root . --suites "$SUITES" --full --json-output "$GITHUB_OUTPUT" --summary "$GITHUB_STEP_SUMMARY"',
                         select["run"])
        self.assertEqual({"SELECTION": "${{ steps.selection.outputs.path }}", "SUITES": f"${{{{ {PLANNED}.config.suites }}}}"}, select["env"])
        self.assertEqual({"run": "${{ steps.week.outputs.run }}", "suites": f"${{{{ {PLANNED}.config.suites }}}}",
                          "validation-workflow": f"${{{{ {PLANNED}.config.validation-workflow }}}}",
                          "shards": "${{ steps.select.outputs.shards }}", "windows": "${{ steps.select.outputs.windows }}"}, self.jobs[PART]["outputs"])
        linux = self.jobs[LINUX]
        self.assertEqual("needs.full-recording.outputs.run == 'true' && needs.full-recording.outputs.shards != '' && needs.full-recording.outputs.shards != '[]'",
                         linux["if"])
        self.assertEqual({"fail-fast": "false", "matrix": {"shard": "${{ fromJSON(needs.full-recording.outputs.shards) }}"}}, linux["strategy"])
        self.assertEqual("needs.full-recording.outputs.run == 'true' && needs.full-recording.outputs.windows != ''", self.jobs[WINDOWS]["if"])
        self.assertNotIn("strategy", self.jobs[WINDOWS], "one Windows job without shards")

    def test_job_order_and_runner_labels(self) -> None:
        self.assertEqual([PART, LINUX, WINDOWS, CLOSE], list(self.jobs))
        self.assertEqual("plan", self.jobs[PART]["needs"])
        self.assertEqual(PART, self.jobs[LINUX]["needs"])
        self.assertEqual(PART, self.jobs[WINDOWS]["needs"])
        self.assertEqual(f"[{PART}, {LINUX}, {WINDOWS}]", self.jobs[CLOSE]["needs"])
        self.assertEqual({PART: "ubuntu-26.04", LINUX: "ubuntu-26.04", WINDOWS: "windows-2025", CLOSE: "ubuntu-slim"},
                         {name: job["runs-on"] for name, job in self.jobs.items()})

    def test_every_job_holds_contents_read_and_no_checkout_persists_credentials(self) -> None:
        self.assertIn("\npermissions: {}\n\n", self.text)
        for name, job in self.jobs.items():
            with self.subTest(job=name):
                self.assertEqual({"contents": "read"}, job["permissions"])
                checkouts = [step for step in job["steps"] if str(step.get("uses", "")).startswith("actions/checkout@")]
                self.assertEqual(1, len(checkouts))
                self.assertEqual("false", checkouts[0]["with"]["persist-credentials"])
                if name == CLOSE:
                    self.assertEqual(f"${{{{ {ON_DEFAULT_BRANCH} && github.event.repository.default_branch || '' }}}}", checkouts[0]["with"]["ref"],
                                     "on the default branch close applies to its tip, elsewhere to the run's own commit")
                else:
                    self.assertNotIn("ref", checkouts[0]["with"], "every recording job records the run's own commit")

    def test_the_actions_are_pinned_and_the_recording_step_is_used_by_local_path(self) -> None:
        text = self.text[self.text.index(f"\n  {PART}:\n"):]
        external = sorted({uses.split("@")[0] for uses in re.findall(r"(?m)^\s*(?:- )?uses: ([^$\s]\S*)", text)})
        self.assertEqual(["actions/checkout", "actions/create-github-app-token", "actions/download-artifact", "actions/setup-dotnet", "actions/setup-node",
                          "actions/setup-python", "actions/upload-artifact"], external)
        for action in external:
            pinned = len(re.findall(action_pin_pattern(action).replace(r"^[ \t]*- uses: ", r"^[ \t]*(?:- )?uses: "), text))
            self.assertEqual(text.count(f"uses: {action}@"), pinned, f"every use of {action} is pinned to a SHA with its version")
        self.assertEqual([], re.findall(r"uses: \./", self.text))
        self.assertNotIn("actions/cache", self.text)


class FullRecordingJobTests(unittest.TestCase):
    """The shards and the Windows job run the validation's own setups and entry runs, with the recording in the full mode."""

    def setUp(self) -> None:
        _, workflow = read_workflow(FULL_RECORDING)
        _, validate = read_workflow(VALIDATE)
        self.jobs, self.validate = workflow["jobs"], validate["jobs"]

    @staticmethod
    def with_this_repository_s_suite_list(steps: list[dict]) -> list[dict]:
        """The steps with the suite list of the job's environment as this repository's own, which validate.yml names."""
        text = json.dumps(steps).replace('--suites \\"$SUITES\\" --suite', "--suites tests/suites.json --suite").replace("--suites $env:SUITES --suite", "--suites tests/suites.json --suite")
        return json.loads(text)

    def test_each_recording_job_starts_the_full_recording_mode_under_its_own_job_name(self) -> None:
        for job, run, name in ((LINUX, 'python3 "$RECORDING/recording.py" start --full --job "$JOB"', "shard-${{ matrix.shard.shard }}"),
                               (WINDOWS, 'python "$env:RECORDING\\recording.py" start --full --job $env:JOB', "windows")):
            with self.subTest(job=job):
                steps = self.jobs[job]["steps"]
                start = steps[step_index(steps, "recording.py\" start")]
                self.assertEqual("start", start["id"])
                self.assertEqual(run, start["run"])
                self.assertEqual(name, start["env"]["JOB"])

    def test_each_recording_job_runs_the_validation_s_setups_and_entry_run_unchanged(self) -> None:
        def setups_and_entry_run(steps: list[dict]) -> list[dict]:
            """Every step before the check but the checkout and the start of the recording."""
            check = next(index for index, step in enumerate(steps) if step.get("uses") == "$/actions/recording" and "with" in step)
            return [step for step in steps[:check] if step.get("id") != "start" and not str(step.get("uses", "")).startswith("actions/checkout@")]

        for job, own in ((LINUX, "linux"), (WINDOWS, "windows")):
            with self.subTest(job=job):
                ours = setups_and_entry_run(self.jobs[job]["steps"])
                self.assertNotIn("tests/suites.json", json.dumps(ours))
                self.assertEqual(setups_and_entry_run(self.validate[own]["steps"]), self.with_this_repository_s_suite_list(ours))
                self.assertGreater(len(ours), 5)
                self.assertEqual(SUITES, self.jobs[job]["env"]["SUITES"])
        self.assertEqual("${{ toJSON(matrix.shard) }}", self.jobs[LINUX]["env"]["SHARD"])
        self.assertEqual("${{ needs.full-recording.outputs.windows }}", self.jobs[WINDOWS]["env"]["WINDOWS"])

    def test_check_uploads_each_job_s_result_under_a_unique_name_unless_the_run_was_cancelled(self) -> None:
        names = []
        for job in (LINUX, WINDOWS):
            steps = self.jobs[job]["steps"]
            check = next(step for step in steps if step.get("uses") == "$/actions/recording" and "with" in step)
            self.assertLess(step_index(steps, "exec --entry"), steps.index(check))
            self.assertEqual("${{ !cancelled() && steps.start.outcome == 'success' }}", check["if"])
            self.assertEqual({"check": "true", "suites": SUITES}, {key: check["with"][key] for key in ("check", "suites")})
            names.append(check["with"]["artifact-name"])
        self.assertEqual(["full-recording-shard-${{ matrix.shard.shard }}", "full-recording-windows"], names)


class ClosingJobTests(unittest.TestCase):
    def setUp(self) -> None:
        text, workflow = read_workflow(FULL_RECORDING)
        self.text = text[text.index(f"\n  {CLOSE}:\n"):]
        self.job = workflow["jobs"][CLOSE]
        self.steps = self.job["steps"]

    def test_it_joins_every_result_applies_it_and_uploads_the_joined_result(self) -> None:
        download = next(step for step in self.steps if str(step.get("uses", "")).startswith("actions/download-artifact@"))
        self.assertEqual({"pattern": "full-recording-*", "path": "${{ runner.temp }}/full-recording"}, download["with"])
        apply = named(self.steps, APPLY_STEP)
        # One downloaded artifact lands directly in the download folder, several each in a folder named after it.
        self.assertIn("shopt -s globstar\n", apply["run"])
        self.assertEqual({"SUITES": SUITES}, self.job["env"])
        self.assertIn('python3 "$RECORDING/recording.py" apply --suites "$SUITES" "$@" --summary "$GITHUB_STEP_SUMMARY" '
                      '"$RUNNER_TEMP"/full-recording/**/full-recording.json', apply["run"])
        upload = next(step for step in self.steps if str(step.get("uses", "")).startswith("actions/upload-artifact@"))
        self.assertLess(self.steps.index(apply), self.steps.index(upload))
        self.assertEqual("full-recording-joined", upload["with"]["name"])
        self.assertEqual(SUITES + "\n${{ runner.temp }}/full-recording-report.md", upload["with"]["path"])

    def test_only_a_run_on_the_default_branch_with_changes_proposes_and_only_the_suite_list_may_change(self) -> None:
        upload = next(step for step in self.steps if str(step.get("uses", "")).startswith("actions/upload-artifact@"))
        proposing = self.steps[self.steps.index(upload) + 1:]
        self.assertTrue(proposing)
        for step in proposing:
            if step.get("id") in ("stopped", "app-token", "verified"):
                continue
            self.assertEqual(f"steps.changes.outputs.changed == 'true' && {ON_DEFAULT_BRANCH}", step["if"], step.get("name"))
        changes = named(self.steps, "Check the changed files")
        self.assertIn('if [ "$path" != "$SUITES" ]; then echo "Unexpected changed file: $path" >&2; exit 1; fi', changes["run"])
        self.assertIn('git add -- "$SUITES"', named(self.steps, "Commit the recorded lists")["run"])

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
        install = named(self.steps, "Install the pinned Gitleaks")
        self.assertEqual("$/actions/repository-hygiene", next(step for step in self.steps if step.get("id") == "hygiene")["uses"])
        self.assertIn('python3 "$HYGIENE/install_tools.py" --pins "$HYGIENE/pins.json" --platform linux-x64 --tool gitleaks', install["run"])

    def test_the_pull_request_is_asked_to_merge_itself_once_its_required_checks_have_passed(self) -> None:
        pull_request = named(self.steps, "Create or update the review pull request")
        auto_merge = self.steps[self.steps.index(pull_request) + 1]
        self.assertEqual("auto-merge", auto_merge["id"])
        self.assertEqual(pull_request["if"], auto_merge["if"])
        self.assertEqual({"GH_TOKEN": "${{ steps.app-token.outputs.token }}"}, auto_merge["env"])
        self.assertIn('gh pr merge "chore/update-recorded-lists" --repo "$GITHUB_REPOSITORY" --auto --squash --match-head-commit "$(git rev-parse HEAD)"',
                      auto_merge["run"])

    def test_a_run_by_hand_that_asks_for_the_token_verification_mints_the_token_without_a_change(self) -> None:
        token = next(step for step in self.steps if step.get("id") == "app-token")
        self.assertEqual(f"(steps.changes.outputs.changed == 'true' || github.event.inputs.verify-token == 'true') && {ON_DEFAULT_BRANCH}", token["if"])
        verified = next(step for step in self.steps if step.get("id") == "verified")
        self.assertEqual(self.steps.index(token) + 1, self.steps.index(verified))
        self.assertEqual(f"${{{{ !cancelled() && github.event.inputs.verify-token == 'true' && {ON_DEFAULT_BRANCH} }}}}", verified["if"])
        self.assertEqual("${{ steps.app-token.outcome }}", verified["env"]["OUTCOME"])
        self.assertIn('if [ "$OUTCOME" = success ]; then', verified["run"])
        self.assertIn('elif [ "$OUTCOME" = failure ]; then', verified["run"])
        self.assertIn('>> "$GITHUB_STEP_SUMMARY"', verified["run"])
        # Every step that commits, pushes or opens the pull request still needs a change.
        for name in ("Configure the GitHub App identity", "Commit the recorded lists", "Push the review branch", "Create or update the review pull request"):
            self.assertTrue(named(self.steps, name)["if"].startswith("steps.changes.outputs.changed == 'true' && "), name)

    def test_on_a_moved_default_branch_apply_keeps_the_lists_of_the_entry_files_the_change_selects(self) -> None:
        apply = named(self.steps, APPLY_STEP)
        selection = next(step for step in self.steps if step.get("id") == "selection")
        self.assertEqual("$/actions/suite-selection", selection["uses"])
        self.assertLess(self.steps.index(selection), self.steps.index(apply))
        self.assertEqual({"RECORDING": "${{ steps.recording.outputs.path }}", "SELECTION": "${{ steps.selection.outputs.path }}",
                          "RECORDED": "${{ github.sha }}", "VALIDATION_WORKFLOW": "${{ needs.full-recording.outputs.validation-workflow }}",
                          "REPORT": "${{ runner.temp }}/full-recording-report.md"}, apply["env"])
        self.assertIn('if [ "$head" != "$RECORDED" ]; then', apply["run"])
        self.assertIn('python3 "$SELECTION/suite_selection.py" select --root . --suites "$SUITES" --base "$RECORDED" --head "$head" '
                      '--compare direct --workflow "$VALIDATION_WORKFLOW" --json-output "$RUNNER_TEMP/moved-selection.txt"', apply["run"])
        self.assertIn('set -- --keep-lists "$RUNNER_TEMP/moved-selection.txt"', apply["run"])
        self.assertIn("set -- --keep-all-lists", apply["run"])

    def test_a_failing_app_step_stops_the_run_and_names_the_question_to_the_user(self) -> None:
        stopped = next(step for step in self.steps if step.get("id") == "stopped")
        self.assertEqual("${{ failure() && (steps.app-token.outcome == 'failure' || steps.push.outcome == 'failure' "
                         "|| steps.pull-request.outcome == 'failure' || steps.auto-merge.outcome == 'failure') }}", stopped["if"])
        self.assertIn("Any App or repository setting is a question to the user", stopped["run"])
        self.assertIn("exit 1", stopped["run"])


if __name__ == "__main__":
    unittest.main()
