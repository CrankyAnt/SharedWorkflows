"""Policy checks over this repository's periodic full recording: its schedule, its shards and Windows job under the
full-recording mode, and the closing job that joins the results and proposes the list changes through the updater App."""

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

from tests.ci_policy_support import REPOSITORY_ROOT, action_pin_pattern, read_workflow

WORKFLOWS = REPOSITORY_ROOT / ".github" / "workflows"
FULL_RECORDING = WORKFLOWS / "full-recording.yml"
VALIDATE = WORKFLOWS / "validate.yml"
ON_DEFAULT_BRANCH = "github.ref_name == github.event.repository.default_branch"
VERIFY_TOKEN = {"description": "Mint the updater App's token even without a change to propose, to verify its private key",
                "required": "false", "type": "boolean", "default": "false"}


def step_index(steps: list[dict], text: str) -> int:
    return next(index for index, step in enumerate(steps) if text in str(step.get("run", "")))


def named(steps: list[dict], name: str) -> dict:
    return next(step for step in steps if step.get("name") == name)


class FullRecordingWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text, self.workflow = read_workflow(FULL_RECORDING)
        self.jobs = self.workflow["jobs"]

    def test_it_runs_on_a_weekly_schedule_and_by_hand_and_never_cancels_a_run(self) -> None:
        self.assertEqual({"schedule": [{"cron": "17 3 * * 1"}], "workflow_dispatch": {"inputs": {"verify_token": VERIFY_TOKEN}}},
                         self.workflow["on"])
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
                if name == "close":
                    self.assertEqual(f"${{{{ {ON_DEFAULT_BRANCH} && github.event.repository.default_branch || '' }}}}", checkouts[0]["with"]["ref"],
                                     "on the default branch close applies to its tip, elsewhere to the run's own commit")
                else:
                    self.assertNotIn("ref", checkouts[0]["with"], "every recording job records the run's own commit")

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

    def test_each_recording_job_starts_the_full_recording_mode_under_its_own_job_name(self) -> None:
        for job, run, name in (("linux", 'python3 "$RECORDING/recording.py" start --full --job "$JOB"', "shard-${{ matrix.shard.shard }}"),
                               ("windows", 'python "$env:RECORDING\\recording.py" start --full --job $env:JOB', "windows")):
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
        self.assertIn('python3 "$RECORDING/recording.py" apply --suites tests/suites.json "$@" --summary "$GITHUB_STEP_SUMMARY" '
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
            if step.get("id") in ("stopped", "app-token", "verified"):
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

    def test_a_run_by_hand_that_asks_for_the_token_verification_mints_the_token_without_a_change(self) -> None:
        token = next(step for step in self.steps if step.get("id") == "app-token")
        self.assertEqual(f"(steps.changes.outputs.changed == 'true' || inputs.verify_token == true) && {ON_DEFAULT_BRANCH}", token["if"])
        verified = next(step for step in self.steps if step.get("id") == "verified")
        self.assertEqual(self.steps.index(token) + 1, self.steps.index(verified))
        self.assertEqual(f"${{{{ !cancelled() && inputs.verify_token == true && {ON_DEFAULT_BRANCH} }}}}", verified["if"])
        self.assertEqual("${{ steps.app-token.outcome }}", verified["env"]["OUTCOME"])
        self.assertIn('if [ "$OUTCOME" = success ]; then', verified["run"])
        self.assertIn('elif [ "$OUTCOME" = failure ]; then', verified["run"])
        self.assertIn('>> "$GITHUB_STEP_SUMMARY"', verified["run"])
        # Every step that commits, pushes or opens the pull request still needs a change.
        for name in ("Configure the GitHub App identity", "Commit the recorded lists", "Push the review branch", "Create or update the review pull request"):
            self.assertTrue(named(self.steps, name)["if"].startswith("steps.changes.outputs.changed == 'true' && "), name)

    def test_on_a_moved_default_branch_apply_keeps_the_lists_of_the_entry_files_the_change_selects(self) -> None:
        apply = named(self.steps, "Join the results, apply the removals and write the duration estimates")
        selection = next(step for step in self.steps if step.get("id") == "selection")
        self.assertEqual("$/actions/suite-selection", selection["uses"])
        self.assertLess(self.steps.index(selection), self.steps.index(apply))
        self.assertEqual({"RECORDING": "${{ steps.recording.outputs.path }}", "SELECTION": "${{ steps.selection.outputs.path }}",
                          "RECORDED": "${{ github.sha }}", "REPORT": "${{ runner.temp }}/full-recording-report.md"}, apply["env"])
        self.assertIn('if [ "$head" != "$RECORDED" ]; then', apply["run"])
        self.assertIn('python3 "$SELECTION/suite_selection.py" select --root . --suites tests/suites.json --base "$RECORDED" --head "$head" '
                      '--compare direct --workflow .github/workflows/validate.yml --json-output "$RUNNER_TEMP/moved-selection.txt"', apply["run"])
        self.assertIn('set -- --keep-lists "$RUNNER_TEMP/moved-selection.txt"', apply["run"])
        self.assertIn("set -- --keep-all-lists", apply["run"])

    def test_a_failing_app_step_stops_the_run_and_names_the_question_to_the_user(self) -> None:
        stopped = next(step for step in self.steps if step.get("id") == "stopped")
        self.assertEqual("${{ failure() && (steps.app-token.outcome == 'failure' || steps.push.outcome == 'failure' "
                         "|| steps.pull-request.outcome == 'failure') }}", stopped["if"])
        self.assertIn("docs/agents/publication.md", stopped["run"])
        self.assertIn("exit 1", stopped["run"])


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
        self.git(work, "checkout", "-q", "--detach", ref) if ref != "main" else None
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


if __name__ == "__main__":
    unittest.main()
