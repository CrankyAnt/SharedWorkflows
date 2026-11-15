"""Text contract of the release workflow and of this repository's caller of it: nothing but the plan runs unless the plan
decides to publish, no job restores a cache, no secret reaches the workflow, the job that runs code of the repository
has the limits of the free job, and the job that publishes runs no code of the repository."""

from __future__ import annotations

import json
import re
import unittest

from tests.ci_policy_support import REPOSITORY_ROOT, ci_definition, read_workflow, tracked_files
from tests.test_ci_policy_pipeline import SETUP_ACTIONS, needed, read_class_problems

WORKFLOWS = REPOSITORY_ROOT / ".github" / "workflows"
RELEASE = WORKFLOWS / "release.yml"
PIPELINE = WORKFLOWS / "pipeline.yml"
CALLER = WORKFLOWS / "release-caller.yml"
TEMPLATE = REPOSITORY_ROOT / "actions" / "pipeline" / "consumer-kit" / "release.yml"
PART = "github-release"
PLAN, FILES = "plan", "github-release-files"
PART_PLAN = f"fromJSON(needs.plan.outputs.plan).parts['{PART}']"
RUNS = f"${{{{ {PART_PLAN}.run }}}}"
PUBLISHING_RIGHTS = {"attestations": "write", "contents": "write", "id-token": "write"}
LEVELS = {"none": 0, "read": 1, "write": 2}


def steps_of(job: dict) -> list[dict]:
    return job.get("steps") or []


def cache_problems(workflow: dict) -> list[str]:
    """Each way a job of the workflow restores or saves a cache: a cache action, a cache input of another action, or a
    cache mode."""
    problems = [f"the workflow requests cache-mode {workflow['cache-mode']}"] if "cache-mode" in workflow else []
    for name, job in workflow.get("jobs", {}).items():
        if "cache-mode" in job:
            problems.append(f"{name} requests cache-mode {job['cache-mode']}")
        for step in steps_of(job):
            action = str(step.get("uses", "")).split("@", 1)[0]
            if action == "actions/cache" or action.startswith("actions/cache/"):
                problems.append(f"{name} uses {action}")
            problems += [f"{name} sets the input {key} of {action}" for key in (step.get("with") or {}) if "cache" in key.lower()]
    return problems


def secret_problems(text: str, workflow: dict) -> list[str]:
    """Each way a secret reaches the workflow other than by name, or a token reaches a job other than the publishing one:
    the workflow declares no secret, so it reads none, and only the last step of the job that publishes holds the run's
    token."""
    problems = []
    if "secrets" in text:
        problems.append("the workflow names secrets, and it declares none that a caller passes by name")
    for name, job in workflow.get("jobs", {}).items():
        for index, step in enumerate(steps_of(job)):
            holds = any(word in json.dumps(step) for word in ("github.token", "GITHUB_TOKEN"))
            if holds and not (name == PART and index == len(steps_of(job)) - 1):
                problems.append(f"{name}: the step {step.get('name') or step.get('uses')} holds the run's token")
    return problems


def reach_problems(caller: dict, release: dict) -> list[str]:
    """Each permission of a job of the release workflow that the caller's one job does not grant."""
    jobs = list(caller.get("jobs", {}).values())
    granted = (jobs[0].get("permissions") or {}) if len(jobs) == 1 else {}
    return [f"the job {name} needs {scope}: {level}, which the caller does not grant" for name, job in release["jobs"].items()
            for scope, level in (job.get("permissions") or {}).items() if LEVELS[str(granted.get(scope, "none"))] < LEVELS[str(level)]]


class WorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text, self.workflow = read_workflow(RELEASE)
        self.jobs = self.workflow["jobs"]

    def test_the_workflow_is_only_called_and_takes_no_input_and_no_secret(self) -> None:
        self.assertIn("\non:\n  workflow_call:\n\npermissions: {}\n", self.text)
        for word in ("inputs", "secrets", "pull_request_target"):
            self.assertNotIn(word, self.text)

    def test_the_jobs_are_the_plan_the_job_that_collects_the_files_and_the_job_that_publishes(self) -> None:
        self.assertEqual([PLAN, FILES, PART], list(self.jobs))
        self.assertEqual([], [name for name, job in self.jobs.items() if "uses" in job], "no job calls a reusable workflow")

    def test_without_a_decision_to_publish_only_the_plan_runs(self) -> None:
        self.assertNotIn("if", self.jobs[PLAN])
        self.assertNotIn("needs", self.jobs[PLAN])
        self.assertEqual({"plan": "${{ steps.plan.outputs.plan }}"}, self.jobs[PLAN]["outputs"])
        self.assertEqual(([PLAN], RUNS), (needed(self.jobs[FILES]), self.jobs[FILES]["if"]))
        self.assertEqual(([PLAN, FILES], RUNS), (needed(self.jobs[PART]), self.jobs[PART]["if"]),
                         "the publishing job runs on the plan's decision alone, so a failed or skipped earlier job skips it")
        for name in (FILES, PART):
            self.assertNotRegex(self.jobs[name]["if"], r"always\(\)|cancelled\(\)|failure\(\)")

    def test_the_plan_reads_the_tag_of_the_run_and_the_default_branch_from_a_checkout_with_the_full_history(self) -> None:
        plan = self.jobs[PLAN]
        self.assertEqual({"contents": "read"}, plan["permissions"])
        checkout = plan["steps"][1]
        self.assertTrue(str(checkout["uses"]).startswith("actions/checkout@"))
        self.assertEqual({"fetch-depth": "0", "persist-credentials": "false"}, checkout["with"])
        step = plan["steps"][-1]
        self.assertEqual({"RESOURCE_PATH": "${{ steps.resources.outputs.path }}", "REF": "${{ github.ref }}",
                          "DEFAULT_BRANCH": "${{ github.event.repository.default_branch || 'main' }}"}, step["env"])
        self.assertIn('release.py" plan --root . --ref "$REF" --default-branch "$DEFAULT_BRANCH" --github-output "$GITHUB_OUTPUT" --summary "$GITHUB_STEP_SUMMARY"', step["run"])
        self.assertNotIn("continue-on-error", json.dumps(plan), "a tag without a valid signature fails the run")

    def test_the_program_comes_from_the_workflow_s_own_revision(self) -> None:
        loaded = [step["uses"] for job in self.jobs.values() for step in steps_of(job) if str(step.get("uses", "")).startswith("$/")]
        self.assertEqual(["$/actions/pipeline", "$/actions/pipeline"], loaded)
        self.assertEqual([], [name for name in (FILES,) if any(str(step.get("uses", "")).startswith("$/") for step in steps_of(self.jobs[name]))])


class CacheAndSecretTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text, self.workflow = read_workflow(RELEASE)

    def test_no_job_restores_or_saves_a_cache(self) -> None:
        self.assertEqual([], cache_problems(self.workflow))
        self.assertNotIn("cache", self.text.lower().replace("pycache", ""), "apart from Python's bytecode directory, the workflow names no cache")

    def test_a_job_that_restores_a_cache_is_named(self) -> None:
        jobs = self.workflow["jobs"]
        for change, problem in (
            ({"uses": "actions/cache@" + "a" * 40, "with": {"path": "x", "key": "y"}}, f"{FILES} uses actions/cache"),
            ({"uses": "actions/cache/restore@" + "a" * 40}, f"{FILES} uses actions/cache/restore"),
            ({"uses": "actions/setup-python@" + "a" * 40, "with": {"python-version": "3.14", "cache": "pip"}}, f"{FILES} sets the input cache of actions/setup-python"),
            ({"uses": "actions/setup-dotnet@" + "a" * 40, "with": {"cache-dependency-path": "x"}}, f"{FILES} sets the input cache-dependency-path of actions/setup-dotnet"),
        ):
            with self.subTest(problem=problem):
                changed = {**self.workflow, "jobs": {**jobs, FILES: {**jobs[FILES], "steps": [*jobs[FILES]["steps"], change]}}}
                self.assertEqual([problem], cache_problems(changed))
        self.assertEqual([f"{PART} requests cache-mode write"], cache_problems({**self.workflow, "jobs": {**jobs, PART: {**jobs[PART], "cache-mode": "write"}}}))
        self.assertEqual(["the workflow requests cache-mode read"], cache_problems({**self.workflow, "cache-mode": "read"}))

    def test_the_workflow_reads_no_secret_and_only_the_publishing_step_holds_the_run_s_token(self) -> None:
        self.assertEqual([], secret_problems(self.text, self.workflow))
        step = self.workflow["jobs"][PART]["steps"][-1]
        self.assertEqual("${{ github.token }}", step["env"]["GITHUB_TOKEN"])
        self.assertEqual(1, self.text.count("github.token"))

    def test_a_secret_or_a_token_outside_the_publishing_step_is_named(self) -> None:
        jobs = self.workflow["jobs"]
        self.assertEqual(["the workflow names secrets, and it declares none that a caller passes by name"],
                         secret_problems(self.text.replace("  workflow_call:\n", "  workflow_call:\n    secrets:\n      RELEASE_KEY:\n        required: false\n"), self.workflow))
        self.assertEqual(["the workflow names secrets, and it declares none that a caller passes by name"],
                         secret_problems(self.text + "          KEY: ${{ secrets.RELEASE_KEY }}\n", self.workflow))
        for name in (PLAN, FILES):
            with self.subTest(job=name):
                leaking = {"name": "A step with the token", "run": "gh api user", "env": {"GH_TOKEN": "${{ github.token }}"}}
                changed = {**self.workflow, "jobs": {**jobs, name: {**jobs[name], "steps": [*jobs[name]["steps"], leaking]}}}
                self.assertEqual([f"{name}: the step A step with the token holds the run's token"], secret_problems(self.text, changed))
        first = {**jobs[PART]["steps"][1], "with": {"github-token": "${{ github.token }}"}}
        changed = {**self.workflow, "jobs": {**jobs, PART: {**jobs[PART], "steps": [jobs[PART]["steps"][0], first, *jobs[PART]["steps"][2:]]}}}
        self.assertEqual([f"{PART}: the step actions/download-artifact@9000827ccba6bdab643e8b6fd33ac0654aef8333 holds the run's token"], secret_problems(self.text, changed))


class FilesJobTests(unittest.TestCase):
    """The job that collects the files runs the repository's script, when the part names one, with the limits of the free
    job, at the commit that the verified tag names."""

    def setUp(self) -> None:
        _, self.workflow = read_workflow(RELEASE)
        self.job = self.workflow["jobs"][FILES]
        self.steps = self.job["steps"]

    def test_the_job_has_the_limits_of_the_free_job(self) -> None:
        self.assertEqual([], read_class_problems(FILES, self.job, self.workflow))
        self.assertEqual({"contents": "read"}, self.job["permissions"])

    def test_the_job_runs_on_the_runner_of_the_plan_and_checks_out_the_commit_of_the_verified_tag_without_credentials(self) -> None:
        self.assertEqual(f"${{{{ {PART_PLAN}.runner }}}}", self.job["runs-on"])
        checkout = self.steps[0]
        self.assertTrue(str(checkout["uses"]).startswith("actions/checkout@"))
        self.assertEqual({"ref": f"${{{{ {PART_PLAN}.commit }}}}", "persist-credentials": "false"}, checkout["with"])

    def test_the_job_installs_the_named_setups_as_the_free_job_does(self) -> None:
        free = read_workflow(PIPELINE)[1]["jobs"]["custom"]["steps"][1:-1]
        expected = json.loads(json.dumps(free).replace("parts['custom']", f"parts['{PART}']"))
        self.assertEqual(expected, self.steps[1:1 + len(free)])
        self.assertEqual(sorted(action for action, _ in SETUP_ACTIONS.values()), sorted({str(step["uses"]).split("@", 1)[0] for step in expected}))

    def test_the_script_runs_only_when_the_part_names_one_and_its_path_reaches_the_shell_as_a_value(self) -> None:
        script = self.steps[-3]
        self.assertEqual({"name": "Run the script of the repository that writes the files", "if": f"${{{{ {PART_PLAN}.script != '' }}}}", "shell": "bash",
                          "env": {"SCRIPT": f"${{{{ {PART_PLAN}.script }}}}"}, "run": 'bash -- "$SCRIPT"'}, script)
        readers = [name for name, job in self.workflow["jobs"].items() if re.search(r"parts\['github-release'\]\.(?:config\.)?script", json.dumps(job))]
        self.assertEqual([FILES], readers, "only the job with the limits of the free job reads the script")
        for holder in (self.job, *self.steps):
            self.assertNotIn("continue-on-error", holder)

    def test_the_named_files_leave_the_job_as_one_artifact_that_must_hold_them(self) -> None:
        collect, upload = self.steps[-2], self.steps[-1]
        self.assertEqual({"FILES": f"${{{{ {PART_PLAN}.files }}}}", "COLLECTED": "${{ runner.temp }}/release-files"}, collect["env"])
        self.assertEqual("bash", collect["shell"])
        self.assertTrue(str(upload["uses"]).startswith("actions/upload-artifact@"))
        self.assertEqual({"name": "release-files", "path": "${{ runner.temp }}/release-files/", "if-no-files-found": "error", "retention-days": "1"}, upload["with"])


class PublishingJobTests(unittest.TestCase):
    """The job that publishes holds the writing rights and runs no code of the repository: it takes the files as an
    artifact, attests them and publishes them with the program of the workflow's own revision."""

    def setUp(self) -> None:
        self.text, self.workflow = read_workflow(RELEASE)
        self.job = self.workflow["jobs"][PART]
        self.steps = self.job["steps"]

    def test_only_the_publishing_job_holds_a_writing_right(self) -> None:
        self.assertEqual(PUBLISHING_RIGHTS, self.job["permissions"])
        for name in (PLAN, FILES):
            self.assertEqual({"contents": "read"}, self.workflow["jobs"][name]["permissions"])

    def test_the_job_runs_no_code_of_the_repository(self) -> None:
        self.assertEqual(["Load the release program from this workflow revision", "actions/download-artifact", "Attest the build provenance of the files",
                          "Publish the release with the files"], [step.get("name") or str(step["uses"]).split("@", 1)[0] for step in self.steps])
        self.assertEqual("ubuntu-26.04", self.job["runs-on"])
        self.assertEqual([], [step for step in self.steps if str(step.get("uses", "")).startswith("actions/checkout@")])
        self.assertEqual(1, len([step for step in self.steps if "run" in step]))
        self.assertNotIn(".script", json.dumps(self.job))
        self.assertNotIn(".setups", json.dumps(self.job))

    def test_the_files_are_attested_in_a_public_repository_before_they_are_published(self) -> None:
        download, attest, publish = self.steps[1:]
        self.assertEqual({"name": "release-files", "path": "${{ runner.temp }}/release-files"}, download["with"])
        self.assertTrue(str(attest["uses"]).startswith("actions/attest-build-provenance@"))
        self.assertEqual("${{ !github.event.repository.private }}", attest["if"])
        self.assertEqual({"subject-path": "${{ runner.temp }}/release-files/*"}, attest["with"])
        self.assertNotIn("continue-on-error", json.dumps(self.job), "a failed attestation publishes nothing")
        self.assertNotIn("if", publish)

    def test_the_publishing_step_gets_the_plan_and_the_collected_files(self) -> None:
        publish = self.steps[-1]
        self.assertEqual({"RESOURCE_PATH": "${{ steps.resources.outputs.path }}", "GITHUB_TOKEN": "${{ github.token }}", "PLAN": "${{ needs.plan.outputs.plan }}"}, publish["env"])
        self.assertIn("printf '%s' \"$PLAN\" > \"$RUNNER_TEMP/release-plan.json\"", publish["run"])
        self.assertIn('release.py" publish --plan "$RUNNER_TEMP/release-plan.json" --files "$RUNNER_TEMP/release-files" --summary "$GITHUB_STEP_SUMMARY"', publish["run"])

    def test_two_runs_for_one_tag_publish_one_after_the_other(self) -> None:
        self.assertEqual({"group": "github-release-${{ github.ref }}", "cancel-in-progress": "false"}, self.job["concurrency"])


class CallerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text, self.caller = read_workflow(CALLER)
        self.template = TEMPLATE.read_text(encoding="utf-8")
        self.release = read_workflow(RELEASE)[1]

    def test_this_repository_s_caller_differs_from_the_template_only_in_the_local_path(self) -> None:
        self.assertEqual(self.text, self.template.replace("CrankyAnt/SharedWorkflows/.github/workflows/release.yml@v1", "$/.github/workflows/release.yml"))
        for word in ("@v1", "run:", "secrets", "with:", "pull_request_target"):
            self.assertNotIn(word, self.text)

    def test_the_template_and_this_repository_s_caller_let_every_job_of_the_release_workflow_start(self) -> None:
        for label, text in (("template", self.template), ("caller", self.text)):
            with self.subTest(version=label):
                caller = ci_definition().read_yaml(text, label)
                self.assertEqual([], reach_problems(caller, self.release))
                self.assertEqual(PUBLISHING_RIGHTS, caller["jobs"]["release"]["permissions"], "the caller grants what the release workflow needs and nothing else")

    def test_a_caller_that_keeps_a_job_from_starting_is_named(self) -> None:
        older = ci_definition().read_yaml(self.template.replace("      id-token: write\n", ""), "an invented older version")
        self.assertEqual([f"the job {PART} needs id-token: write, which the caller does not grant"], reach_problems(older, self.release))
        reading = ci_definition().read_yaml(self.template.replace("      contents: write\n", "      contents: read\n"), "an invented older version")
        self.assertEqual([f"the job {PART} needs contents: write, which the caller does not grant"], reach_problems(reading, self.release))

    def test_no_other_workflow_of_this_repository_calls_the_release_workflow(self) -> None:
        callers = [path for path in sorted(tracked_files(REPOSITORY_ROOT, [".github/workflows"]))
                   if "workflows/release.yml" in (REPOSITORY_ROOT / path).read_text(encoding="utf-8")]
        self.assertEqual([".github/workflows/release-caller.yml"], callers)


if __name__ == "__main__":
    unittest.main()
