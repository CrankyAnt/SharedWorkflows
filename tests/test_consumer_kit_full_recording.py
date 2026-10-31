"""The full-recording template: the repository's own scheduled workflow that records every entry file in the
full-recording mode, with its own setups, shards and closing job, and proposes the list changes through the updater App."""

from __future__ import annotations

import re
import unittest
from pathlib import Path
from tests.ci_policy_support import read_workflow

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SELECTION_KIT = REPOSITORY_ROOT / "actions" / "suite-selection" / "consumer-kit"
OWN_FULL_RECORDING = REPOSITORY_ROOT / ".github" / "workflows" / "full-recording.yml"
RECORDING = "CrankyAnt/SharedWorkflows/actions/recording@v1"
ON_DEFAULT_BRANCH = "github.ref_name == github.event.repository.default_branch"
# The actions each job of the template references with `@<pin>`, in their order; setup replaces each `@<pin>` at install.
PIN_PLACEHOLDER_ACTIONS = {
    "plan": ["actions/checkout"],
    "linux": ["actions/checkout", "actions/setup-python"],
    "windows": ["actions/checkout"],
    "close": ["actions/checkout", "actions/download-artifact", "actions/upload-artifact", "actions/create-github-app-token"],
}


def named(steps: list[dict], name: str) -> dict:
    return next(step for step in steps if step.get("name") == name)


def with_pin_placeholder(step: dict) -> dict:
    """This repository's own step with its action's commit pin replaced by the template's `<pin>`."""
    uses = re.fullmatch(r"([\w.-]+/[\w./-]+)@[0-9a-f]{40}", str(step.get("uses", "")))
    return step | {"uses": f"{uses.group(1)}@<pin>"} if uses else step


class FullRecordingTemplateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.text, self.workflow = read_workflow(SELECTION_KIT / "full-recording.yml")
        self.jobs = self.workflow["jobs"]
        _, self.own = read_workflow(OWN_FULL_RECORDING)

    def test_it_has_the_schedule_trigger_concurrency_and_jobs_of_this_repository_s_full_recording(self) -> None:
        for key in ("name", "on", "permissions", "concurrency"):
            with self.subTest(key=key):
                self.assertEqual(self.own[key], self.workflow[key])
        self.assertEqual(["plan", "linux", "windows", "close"], list(self.jobs))
        self.assertEqual(self.own["jobs"]["plan"]["steps"][0], self.jobs["plan"]["steps"][0])
        for name in ("linux", "windows", "close"):
            with self.subTest(job=name):
                self.assertEqual(self.own["jobs"][name]["if"], self.jobs[name]["if"])
                self.assertEqual(self.own["jobs"][name]["needs"], self.jobs[name]["needs"])

    def test_each_action_reference_names_its_action_with_the_pin_placeholder(self) -> None:
        for name, actions in PIN_PLACEHOLDER_ACTIONS.items():
            with self.subTest(job=name):
                references = [step["uses"] for step in self.jobs[name]["steps"]
                              if "uses" in step and not step["uses"].startswith("CrankyAnt/SharedWorkflows/")]
                self.assertEqual([f"{action}@<pin>" for action in actions], references)
        self.assertEqual([], re.findall(r"@[0-9a-f]{40}", self.text), "the template carries no commit pin")
        self.assertEqual([], re.findall(r"(?m)^ +(?:- )?uses: \S+ +#.*$", self.text), "the template carries no version comment")

    def test_every_job_reads_the_contents_only(self) -> None:
        for name, job in self.jobs.items():
            with self.subTest(job=name):
                self.assertEqual({"contents": "read"}, job["permissions"])
        self.assertEqual("ubuntu-26.04", self.jobs["plan"]["runs-on"], "the plan job, which runs select --full, runs on ubuntu-26.04")
        self.assertEqual("<linux-runner>", self.jobs["linux"]["runs-on"])
        self.assertEqual("<windows-runner>", self.jobs["windows"]["runs-on"])

    def test_the_plan_shards_every_entry_file_of_the_repository_s_suite_list_with_the_shared_select(self) -> None:
        plan = self.jobs["plan"]
        self.assertEqual(self.own["jobs"]["plan"]["outputs"], plan["outputs"])
        selection = next(step for step in plan["steps"] if step.get("id") == "selection")
        self.assertEqual("CrankyAnt/SharedWorkflows/actions/suite-selection@v1", selection["uses"])
        select = next(step for step in plan["steps"] if step.get("id") == "select")
        self.assertEqual("${{ steps.selection.outputs.path }}", select["env"]["SELECTION"])
        self.assertEqual('PYTHONPYCACHEPREFIX="$RUNNER_TEMP/repo-pycache" python3 "$SELECTION/suite_selection.py" '
                         'select --root . --suites .github/suites.json --full --json-output "$GITHUB_OUTPUT" --summary "$GITHUB_STEP_SUMMARY"',
                         select["run"])

    def test_each_shard_and_the_windows_job_run_their_setups_and_entry_files_in_the_full_recording_mode(self) -> None:
        for name, setup_if in (("linux", "contains(matrix.shard.setups, '<setup>')"), ("windows", "contains(fromJSON(env.SETUPS), '<setup>')")):
            with self.subTest(job=name):
                job, own = self.jobs[name], self.own["jobs"][name]
                steps = job["steps"]
                self.assertEqual(own.get("strategy"), job.get("strategy"))
                self.assertEqual(own.get("env"), job.get("env"))
                self.assertEqual({"id": "recording", "uses": RECORDING}, steps[1])
                self.assertEqual(named(own["steps"], "Start the full recording"), named(steps, "Start the full recording"))
                entries = own["steps"][-2]
                self.assertEqual(entries["run"].replace("tests/suites.json", ".github/suites.json"), named(steps, entries["name"])["run"])
                self.assertEqual({"RECORDING": "${{ steps.recording.outputs.path }}"}, named(steps, entries["name"])["env"])
                self.assertEqual(entries["name"], steps[-2]["name"])
                if name == "linux":
                    # The Python setup with the lookup report, as this repository's own shard has it, at the repository's version.
                    for label in ("python", "Provide the set-up Python with the lookup report to the entry files that state it"):
                        ours = with_pin_placeholder(next(step for step in own["steps"] if label in (step.get("id"), step.get("name"))))
                        theirs = next(step for step in steps if label in (step.get("id"), step.get("name")))
                        self.assertEqual(ours | ({"with": ours["with"] | {"python-version": "<python-version>"}} if "with" in ours else {}), theirs)
                setup = named(steps, "Run the setup <setup> under the recording")
                self.assertEqual(setup_if, setup["if"])
                self.assertIn("exec --setup <setup> -- <setup-command>", setup["run"])
                self.assertIn("provide --setup <setup> <setup-provides>", setup["run"])
                check = steps[-1]
                self.assertEqual(RECORDING, check["uses"])
                self.assertEqual(own["steps"][-1]["if"], check["if"])
                self.assertEqual(own["steps"][-1]["with"] | {"suites": ".github/suites.json"}, check["with"])

    def test_the_closing_job_applies_the_joined_results_to_the_suite_list_only(self) -> None:
        steps = self.jobs["close"]["steps"]
        join = named(steps, "Join the results, apply the removals and write the duration estimates")
        self.assertIn('recording.py" apply --suites .github/suites.json --summary "$GITHUB_STEP_SUMMARY" '
                      '"$RUNNER_TEMP"/full-recording/*/full-recording.json', join["run"])
        self.assertIn(".github/suites.json) ;;", named(steps, "Check the changed files")["run"])
        self.assertIn("git add .github/suites.json", named(steps, "Commit the recorded lists")["run"])
        self.assertNotIn("tests/suites.json", self.text)

    def test_the_updater_app_makes_the_pull_request_after_a_credential_scan(self) -> None:
        own_steps = self.own["jobs"]["close"]["steps"]
        steps = self.jobs["close"]["steps"]
        token = next(step for step in steps if step.get("id") == "app-token")
        own_token = with_pin_placeholder(next(step for step in own_steps if step.get("id") == "app-token"))
        self.assertEqual(own_token, token)
        for name in ("Report the token verification", "Configure the GitHub App identity", "Push the review branch",
                     "Create or update the review pull request", "Name the App setting this run would need"):
            with self.subTest(step=name):
                self.assertEqual(named(own_steps, name), named(steps, name))
        scan = named(steps, "Scan the commit before pushing")
        self.assertIn('"$RUNNER_TEMP/hygiene-tools/gitleaks" git <gitleaks-options> --log-opts="HEAD --not --remotes=origin" '
                      '--redact --no-banner --no-color', scan["run"])
        install = named(steps, "Install the pinned Gitleaks")
        self.assertIn('python3 "$HYGIENE/install_tools.py" --pins "$HYGIENE/pins.json" --platform linux-x64 --tool gitleaks', install["run"])
        for step in steps[steps.index(named(steps, "Check the changed files")):]:
            if step.get("id") != "stopped":
                with self.subTest(step=step.get("name") or step.get("uses")):
                    self.assertIn(ON_DEFAULT_BRANCH, step["if"])


if __name__ == "__main__":
    unittest.main()
