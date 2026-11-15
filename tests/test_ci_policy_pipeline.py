"""Text contract of the pipeline and of this repository's caller of it: one verdict that always runs, repository hygiene
outside the configuration, and every other shared part a job that the plan starts."""

from __future__ import annotations

import re
import unittest

from tests.ci_policy_support import REPOSITORY_ROOT, condition_holds, flow_items, read_workflow, tracked_files

WORKFLOWS = REPOSITORY_ROOT / ".github" / "workflows"
PIPELINE = WORKFLOWS / "pipeline.yml"
CALLER = WORKFLOWS / "ci.yml"
FRAGMENTS = "actions/pipeline/schema/v1/parts"
HYGIENE = "repository-hygiene"
HYGIENE_WORKFLOW = WORKFLOWS / "repository-hygiene.yml"
# The steps of the hygiene job of repository-hygiene.yml that the pipeline's job leaves out, each with its reason.
LEFT_OUT = {
    "Check that the dispatched commit is on the default branch": "the pipeline has no dependabot-merged run",
    "Restore the passed checks of the pull request": "the pipeline has no edited run that reuses passed checks",
    "Record the passed checks for an edit of the pull request": "the pipeline has no edited run that reuses passed checks",
    "Store the passed checks in the pull request's cache": "the pipeline has no edited run that reuses passed checks",
}
# The steps that differ between the two jobs, each with its reason; every other step is equal in both.
DIFFERENT = {
    "actions/checkout": "the pipeline has no dependabot-merged run, whose commit the other job checks out",
    "Look up the tested tree of the pushed commit's pull request": "the pipeline has no dependabot-merged run",
    "Decide which checks the changed files can affect": "the pipeline has no inputs, no dependabot-merged run and no record of an edited run",
    "Check the pull request title": "the pipeline reads the current title through the API, for a re-run after a corrected title",
    "Dependabot coverage of tracked package files": "the pipeline has no input coverage-exclude",
    "Relative links in tracked Markdown": "the pipeline has no input link-exclude",
    "Install the pinned tools and verify their checksums": "the pipeline has no dependabot-merged run, whose dispatch check the other job waits for",
    "zizmor over workflows and action definitions": "the pipeline has no input zizmor-persona",
    "Gitleaks over all reachable history": "the pipeline takes the Gitleaks files by their fixed names, not from inputs",
}
# The jobs of the pipeline that are no shared part.
PLAN, VERDICT = "plan", "verdict"


def step_key(step: dict) -> str:
    """What names a step in both hygiene jobs: its name, or the action it uses without its pin."""
    return str(step.get("name") or str(step["uses"]).split("@", 1)[0])


def hygiene_step_problems(shared: list[dict], pipeline: list[dict], left_out: dict[str, str], different: dict[str, str]) -> list[str]:
    """Each way the pipeline's hygiene job is out of step with the hygiene job of the shared workflow: a step without a
    counterpart, a step that differs, or a stated difference that no longer exists."""
    ours = {step_key(step): step for step in pipeline}
    theirs = {step_key(step): step for step in shared}
    problems = []
    for key, step in theirs.items():
        if key in left_out:
            if key in ours:
                problems.append(f"{key}: the pipeline has this step, but it is listed as left out")
        elif key not in ours:
            problems.append(f"{key}: the pipeline's hygiene job has no such step")
        elif key in different:
            if ours[key] == step:
                problems.append(f"{key}: the step is equal in both jobs, but it is listed as different")
        elif ours[key] != step:
            problems.append(f"{key}: the step differs between the two jobs")
    problems += [f"{key}: listed as a difference, but the shared workflow has no such step" for key in [*left_out, *different] if key not in theirs]
    problems += [f"{key}: only the pipeline's hygiene job has this step" for key in ours if key not in theirs]
    order = [key for key in theirs if key in ours]
    if order != [key for key in ours if key in theirs]:
        problems.append("the steps that both jobs hold are in a different order")
    return problems


def needed(job: dict) -> list[str]:
    needs = str(job.get("needs", ""))
    return flow_items(needs[1:-1]) if needs.startswith("[") else [needs] if needs else []


def part_job_problems(name: str, job: dict) -> list[str]:
    """Each way the job of a shared part that the configuration file switches on is not started by the plan alone."""
    problems = []
    if PLAN not in needed(job):
        problems.append(f"{name} does not need {PLAN}")
    condition = str(job.get("if", ""))
    if condition != f"${{{{ fromJSON(needs.plan.outputs.plan).parts['{name}'].run }}}}":
        problems.append(f"{name} does not run on the plan's decision for {name} alone")
    problems += concurrency_problems(name, job)
    return problems


def concurrency_problems(name: str, job: dict) -> list[str]:
    concurrency = job.get("concurrency")
    text = " ".join(str(value) for value in concurrency.values()) if isinstance(concurrency, dict) else ""
    if set(concurrency or {}) != {"group", "cancel-in-progress"} or text.count(f"needs.plan.outputs.concurrency || '{{}}')['{name}']") != 2:
        return [f"{name} does not take its concurrency group and cancel flag from the plan"]
    return []


class PipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text, self.pipeline = read_workflow(PIPELINE)
        self.jobs = self.pipeline["jobs"]
        fragments = tracked_files(REPOSITORY_ROOT, [FRAGMENTS])
        self.parts = sorted(path.rsplit("/", 1)[1].removesuffix(".json") for path in fragments)

    def test_the_pipeline_is_only_called_and_takes_no_input_and_no_secret(self) -> None:
        self.assertIn("\non:\n  workflow_call:\n\npermissions: {}\n", self.text)
        self.assertNotIn("inputs", self.text)
        self.assertNotIn("secrets", self.text)
        self.assertNotIn("pull_request_target", self.text)

    def test_the_jobs_are_the_plan_one_job_per_shared_part_with_a_schema_fragment_and_the_verdict(self) -> None:
        self.assertEqual([PLAN, *self.parts, VERDICT], list(self.jobs))
        self.assertIn(HYGIENE, self.parts)

    def test_no_job_calls_a_reusable_workflow(self) -> None:
        self.assertEqual([], [name for name, job in self.jobs.items() if "uses" in job])

    def test_the_verdict_always_runs_and_needs_the_plan_and_every_part(self) -> None:
        verdict = self.jobs[VERDICT]
        self.assertEqual("always()", verdict["if"])
        self.assertEqual([name for name in self.jobs if name != VERDICT], needed(verdict))
        self.assertIn("NEEDS: ${{ toJSON(needs) }}", self.text)
        self.assertEqual({"contents": "read"}, verdict["permissions"])

    def test_the_plan_gives_its_plan_and_the_concurrency_of_the_jobs_as_outputs(self) -> None:
        plan = self.jobs[PLAN]
        self.assertEqual({"plan": "${{ steps.plan.outputs.plan }}", "concurrency": "${{ steps.plan.outputs.concurrency }}"}, plan["outputs"])
        self.assertEqual({"contents": "read"}, plan["permissions"])
        self.assertNotIn("if", plan)
        self.assertNotIn("needs", plan)

    def test_repository_hygiene_runs_whatever_the_plan_found(self) -> None:
        hygiene = self.jobs[HYGIENE]
        self.assertEqual([PLAN], needed(hygiene))
        self.assertEqual("${{ !cancelled() }}", hygiene["if"])
        self.assertTrue(condition_holds(hygiene["if"], {}))
        start = self.text.index(f"\n  {HYGIENE}:\n")
        section = self.text[start:self.text.index(f"\n  {VERDICT}:\n")]
        self.assertNotIn("outputs.plan", section, "repository hygiene reads nothing of the configuration file")
        self.assertNotIn("ci-config", section)
        self.assertEqual([], concurrency_problems(HYGIENE, hygiene))
        self.assertIn("|| format('repository-hygiene-run-{0}', github.run_id)", hygiene["concurrency"]["group"],
                      "after a plan without outputs, repository hygiene still has a concurrency group")

    def test_every_other_part_runs_on_the_plan_s_decision_and_takes_its_concurrency_from_the_plan(self) -> None:
        for name in self.parts:
            if name != HYGIENE:
                with self.subTest(part=name):
                    self.assertEqual([], part_job_problems(name, self.jobs[name]))

    def test_a_part_job_that_does_not_follow_the_plan_is_named(self) -> None:
        good = {"needs": "plan", "if": "${{ fromJSON(needs.plan.outputs.plan).parts['invented-test'].run }}",
                "concurrency": {"group": "${{ fromJSON(needs.plan.outputs.concurrency || '{}')['invented-test'].group }}",
                                "cancel-in-progress": "${{ fromJSON(needs.plan.outputs.concurrency || '{}')['invented-test']['cancel-in-progress'] == true }}"}}
        self.assertEqual([], part_job_problems("invented-test", good))
        for change, problem in (({"needs": "repository-hygiene"}, "invented-test does not need plan"),
                                ({"if": "always()"}, "invented-test does not run on the plan's decision for invented-test alone"),
                                ({"if": "${{ fromJSON(needs.plan.outputs.plan).parts['other-test'].run }}"}, "invented-test does not run on the plan's decision for invented-test alone"),
                                ({"concurrency": {"group": "fixed", "cancel-in-progress": "true"}}, "invented-test does not take its concurrency group and cancel flag from the plan")):
            with self.subTest(change=change):
                self.assertEqual([problem], part_job_problems("invented-test", {**good, **change}))

    def test_repository_hygiene_has_read_rights_only_and_keeps_no_checkout_credentials(self) -> None:
        self.assertEqual({"actions": "read", "contents": "read", "pull-requests": "read"}, self.jobs[HYGIENE]["permissions"])
        checkouts = re.findall(r"- uses: actions/checkout@[0-9a-f]{40} # v[\d.]+\n        with:\n((?:          .*\n)+)", self.text)
        self.assertEqual(2, len(checkouts))
        for options in checkouts:
            self.assertIn("persist-credentials: false", options)

    def test_repository_hygiene_takes_the_gitleaks_files_by_their_fixed_names_and_offers_no_other_choice(self) -> None:
        self.assertIn('if [ -f .gitleaks.toml ]; then config=".gitleaks.toml"; fi', self.text)
        self.assertIn('if [ -f .gitleaks-baseline.json ]; then baseline=".gitleaks-baseline.json"; fi', self.text)
        self.assertIn('hygiene.py" gitleaks --tools "$HYGIENE_TOOLS" --config "$config" --baseline "$baseline"', self.text)
        self.assertIn('hygiene.py" zizmor --tools "$HYGIENE_TOOLS" --persona ""', self.text)
        for option in ("--exclude", "exclude-from", "EXCLUDE"):
            self.assertNotIn(option, self.text)

    def test_the_title_check_reads_the_current_title_and_not_the_one_of_the_event(self) -> None:
        self.assertNotIn("github.event.pull_request.title", self.text)
        self.assertIn('title="$(gh api "repos/$GITHUB_REPOSITORY/pulls/$PR_NUMBER" --jq .title)"', self.text)
        step = next(step for step in self.jobs[HYGIENE]["steps"] if step.get("id") == "title")
        self.assertIn("set -euo pipefail", step["run"], "a title that cannot be read fails the check")
        self.assertEqual("${{ github.token }}", step["env"]["GITHUB_TOKEN"])


class HygieneStepTests(unittest.TestCase):
    """The pipeline's hygiene job runs the floor of repository-hygiene.yml: each step there has its counterpart here."""

    def setUp(self) -> None:
        self.shared = read_workflow(HYGIENE_WORKFLOW)[1]["jobs"]["hygiene"]["steps"]
        self.pipeline = read_workflow(PIPELINE)[1]["jobs"][HYGIENE]["steps"]

    def test_every_step_of_the_shared_hygiene_job_has_its_counterpart_in_the_pipeline_apart_from_the_stated_differences(self) -> None:
        self.assertEqual([], hygiene_step_problems(self.shared, self.pipeline, LEFT_OUT, DIFFERENT))

    def test_a_step_that_only_the_shared_hygiene_job_holds_is_named(self) -> None:
        added = [*self.shared, {"name": "A new check of the floor", "run": "check"}]
        self.assertEqual(["A new check of the floor: the pipeline's hygiene job has no such step"], hygiene_step_problems(added, self.pipeline, LEFT_OUT, DIFFERENT))
        references = "Check the references to repositories of the owner"
        without = [step for step in self.pipeline if step.get("name") != references]
        self.assertEqual([f"{references}: the pipeline's hygiene job has no such step"], hygiene_step_problems(self.shared, without, LEFT_OUT, DIFFERENT))

    def test_a_step_that_changed_in_one_job_only_is_named(self) -> None:
        name = "Check every pull request commit"
        changed = [{**step, "run": "another command"} if step.get("name") == name else step for step in self.shared]
        self.assertEqual([f"{name}: the step differs between the two jobs"], hygiene_step_problems(changed, self.pipeline, LEFT_OUT, DIFFERENT))
        decision = "Decide whether the PR author skips the convention checks"
        changed = [{**step, "run": step["run"].replace('"repository reference"', '"another check"')} if step.get("name") == decision else step for step in self.shared]
        self.assertEqual([f"{decision}: the step differs between the two jobs"], hygiene_step_problems(changed, self.pipeline, LEFT_OUT, DIFFERENT))

    def test_a_stated_difference_that_no_longer_exists_is_named(self) -> None:
        self.assertEqual(["Check every pull request commit: the step is equal in both jobs, but it is listed as different"],
                         hygiene_step_problems(self.shared, self.pipeline, LEFT_OUT, {**DIFFERENT, "Check every pull request commit": "none"}))
        self.assertEqual(["A removed step: listed as a difference, but the shared workflow has no such step"],
                         hygiene_step_problems(self.shared, self.pipeline, {**LEFT_OUT, "A removed step": "none"}, DIFFERENT))
        self.assertEqual(["An extra step: only the pipeline's hygiene job has this step"],
                         hygiene_step_problems(self.shared, [*self.pipeline, {"name": "An extra step", "run": "extra"}], LEFT_OUT, DIFFERENT))


class CallerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text, self.caller = read_workflow(CALLER)

    def test_the_caller_runs_on_pull_requests_and_main_pushes_only(self) -> None:
        self.assertIn("\non:\n  pull_request:\n  push:\n    branches: [main]\n\npermissions: {}\n", self.text)

    def test_the_caller_calls_the_pipeline_by_local_path_as_its_one_job_ci_so_that_the_verdict_s_check_is_ci_verdict(self) -> None:
        self.assertEqual(["ci"], list(self.caller["jobs"]))
        job = self.caller["jobs"]["ci"]
        self.assertEqual({"permissions": {"actions": "read", "contents": "read", "pull-requests": "read"}, "uses": "$/.github/workflows/pipeline.yml"}, job)
        self.assertIn(VERDICT, read_workflow(PIPELINE)[1]["jobs"])
        for text in ("@v1", "run:", "secrets", "with:", "pull_request_target"):
            self.assertNotIn(text, self.text)

    def test_no_other_workflow_of_this_repository_calls_the_pipeline(self) -> None:
        callers = [path for path in sorted(tracked_files(REPOSITORY_ROOT, [".github/workflows"]))
                   if "workflows/pipeline.yml" in (REPOSITORY_ROOT / path).read_text(encoding="utf-8") and path != ".github/workflows/pipeline.yml"]
        self.assertEqual([".github/workflows/ci.yml"], callers)


if __name__ == "__main__":
    unittest.main()
