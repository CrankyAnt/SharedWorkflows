"""Text contract of the pipeline and of this repository's caller of it: one verdict that always runs, repository hygiene
outside the configuration, and every other shared part a job that the plan starts, in the pipeline or in the shared
workflow that its fragment names."""

from __future__ import annotations

import json
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
# The steps that only the pipeline's hygiene job holds, each with its reason.
PIPELINE_ONLY = {
    "Load the fixed callers and the schema from this workflow revision": "a caller of repository-hygiene.yml holds a caller of its own and no configuration file",
    "Fixed callers and local workflows": "a caller of repository-hygiene.yml holds a caller of its own and no configuration file",
    "Issues of the local workflow register": "a caller of repository-hygiene.yml holds no configuration file",
    "Generated Dependabot configuration": "a caller of repository-hygiene.yml holds no configuration file",
}
# The jobs of the pipeline that are no shared part.
PLAN, VERDICT = "plan", "verdict"
# The free job, the known setups, and the definition that a setting refers to when it names code of the repository to run.
CUSTOM = "custom"
SETUPS = REPOSITORY_ROOT / "actions" / "pipeline" / "schema" / "v1" / "setups.json"
SCRIPT_REFERENCE = "#/$defs/script"
# The action that installs each tool of a setup, with the input that takes the setup's version.
SETUP_ACTIONS = {"python": ("actions/setup-python", "python-version"), "dotnet": ("actions/setup-dotnet", "dotnet-version")}


def fragments() -> dict[str, dict]:
    """The fragment of each shared part of the current schema version, by the part's name."""
    return {path.rsplit("/", 1)[1].removesuffix(".json"): json.loads((REPOSITORY_ROOT / path).read_text(encoding="utf-8"))
            for path in sorted(tracked_files(REPOSITORY_ROOT, [FRAGMENTS]))}


def pipeline_parts() -> list[str]:
    """The parts whose job is in the pipeline: those whose fragment names no other shared workflow."""
    return [name for name, fragment in fragments().items() if "workflow" not in fragment]


def step_key(step: dict) -> str:
    """What names a step in both hygiene jobs: its name, or the action it uses without its pin."""
    return str(step.get("name") or str(step["uses"]).split("@", 1)[0])


def hygiene_step_problems(shared: list[dict], pipeline: list[dict], left_out: dict[str, str], different: dict[str, str], pipeline_only: dict[str, str] | None = None) -> list[str]:
    """Each way the pipeline's hygiene job is out of step with the hygiene job of the shared workflow: a step without a
    counterpart, a step that differs, or a stated difference that no longer exists."""
    pipeline_only = PIPELINE_ONLY if pipeline_only is None else pipeline_only
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
    problems += [f"{key}: only the pipeline's hygiene job has this step" for key in ours if key not in theirs and key not in pipeline_only]
    problems += [f"{key}: listed as a step of the pipeline alone, but the shared workflow has this step" for key in pipeline_only if key in theirs]
    problems += [f"{key}: listed as a step of the pipeline alone, but the pipeline has no such step" for key in pipeline_only if key not in ours]
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


def refers_to(schema: object, reference: str) -> bool:
    """Whether a schema refers to a definition, at any depth."""
    if isinstance(schema, dict):
        return schema.get("$ref") == reference or any(refers_to(value, reference) for value in schema.values())
    return isinstance(schema, list) and any(refers_to(value, reference) for value in schema)


def read_class_problems(name: str, job: dict, workflow: dict) -> list[str]:
    """Each way a job that runs code of the repository exceeds the limits of the free job: other rights than
    ``contents: read``, a secret or a token, checkout credentials that stay in the checkout, or a cache mode that writes."""
    problems = []
    if job.get("permissions") != {"contents": "read"}:
        problems.append(f"{name} has other rights than contents: read")
    text = json.dumps(job)
    problems += [f"{name} receives a secret or a token: {word}" for word in ("secrets", "github.token", "GITHUB_TOKEN") if word in text]
    for step in job.get("steps", []):
        if str(step.get("uses", "")).startswith("actions/checkout@") and (step.get("with") or {}).get("persist-credentials") != "false":
            problems.append(f"{name} keeps the checkout credentials")
    for holder, owner in ((workflow, "the workflow"), (job, name)):
        if holder.get("cache-mode", "read") not in ("read", "none"):
            problems.append(f"{owner} requests cache-mode {holder['cache-mode']}")
    return problems


class PipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text, self.pipeline = read_workflow(PIPELINE)
        self.jobs = self.pipeline["jobs"]
        self.parts = pipeline_parts()

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
        self.assertEqual({"actions": "read", "contents": "read", "issues": "read", "pull-requests": "read"}, self.jobs[HYGIENE]["permissions"])
        checkouts = re.findall(r"- uses: actions/checkout@[0-9a-f]{40} # v[\d.]+\n        with:\n((?:          .*\n)+)", self.text)
        self.assertEqual(self.text.count("actions/checkout@"), len(checkouts), "every checkout of the pipeline states its options")
        self.assertGreaterEqual(len(checkouts), 2)
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


class FreeJobTests(unittest.TestCase):
    """The free job runs the repository's script after the setups that the plan names, and nothing decides its result but
    that script."""

    def setUp(self) -> None:
        self.job = read_workflow(PIPELINE)[1]["jobs"][CUSTOM]
        self.steps = self.job["steps"]
        self.plan = "fromJSON(needs.plan.outputs.plan).parts['custom']"

    def test_the_job_checks_out_the_repository_and_installs_the_named_setups_in_the_listed_order_before_the_script(self) -> None:
        self.assertEqual("ubuntu-26.04", self.job["runs-on"])
        self.assertTrue(str(self.steps[0]["uses"]).startswith("actions/checkout@"))
        known = json.loads(SETUPS.read_text(encoding="utf-8"))
        self.assertEqual(sorted(SETUP_ACTIONS), sorted({setup["tool"] for setup in known.values()}), "every tool of a known setup has its action here")
        # A list without duplicates names at most every known setup, so that many places install any list in its order.
        expected = [(position, tool) for position in range(len(known)) for tool in SETUP_ACTIONS]
        installs = self.steps[1:-1]
        self.assertEqual(len(expected), len(installs))
        for (position, tool), step in zip(expected, installs):
            with self.subTest(position=position, tool=tool):
                action, version_input = SETUP_ACTIONS[tool]
                self.assertEqual(action, str(step["uses"]).split("@", 1)[0])
                self.assertEqual(f"${{{{ {self.plan}.setups[{position}].tool == '{tool}' }}}}", step["if"])
                self.assertEqual({version_input: f"${{{{ {self.plan}.setups[{position}].version }}}}"}, step["with"], "the setup's version and no cache")
        self.assertEqual(len(SETUP_ACTIONS), len({str(step["uses"]) for step in installs}), "one pin per action")

    def test_the_last_step_runs_the_configured_script_and_its_exit_status_is_the_job_s_result(self) -> None:
        step = self.steps[-1]
        self.assertEqual({"SCRIPT": f"${{{{ {self.plan}.config.script }}}}"}, step["env"])
        self.assertEqual('bash -- "$SCRIPT"', step["run"], "the script's path reaches the shell as a value, never as text of the command")
        self.assertEqual(["env", "name", "run"], sorted(step))
        for holder in (self.job, *self.steps):
            self.assertNotIn("continue-on-error", holder)
        self.assertEqual([], [step for step in self.steps[1:] if "if" in step and "].tool ==" not in step["if"]])


class ReadClassTests(unittest.TestCase):
    """Code of the repository runs only in a job with the limits of the free job. A setting that names such code refers to
    the definition ``script`` of the schema, so the parts that run it are known from their fragments."""

    def setUp(self) -> None:
        self.workflow = read_workflow(PIPELINE)[1]
        self.jobs = self.workflow["jobs"]
        self.script_parts = [name for name in pipeline_parts() if refers_to(fragments()[name], SCRIPT_REFERENCE)]

    def test_the_free_job_has_read_rights_only_no_secret_no_checkout_credentials_and_no_cache_mode_that_writes(self) -> None:
        self.assertEqual([], read_class_problems(CUSTOM, self.jobs[CUSTOM], self.workflow))
        self.assertEqual({"contents": "read"}, self.jobs[CUSTOM]["permissions"])

    def test_every_part_whose_configuration_names_a_script_of_the_repository_has_a_job_with_the_limits_of_the_free_job(self) -> None:
        self.assertIn(CUSTOM, self.script_parts)
        for name in self.script_parts:
            with self.subTest(part=name):
                self.assertEqual([], read_class_problems(name, self.jobs[name], self.workflow))

    def test_only_a_job_of_such_a_part_reads_a_script_from_the_plan(self) -> None:
        readers = [name for name, job in self.jobs.items() if re.search(r"\.config\.[\w.-]*script", json.dumps(job))]
        self.assertEqual(self.script_parts, readers)

    def test_a_job_that_exceeds_a_limit_is_named(self) -> None:
        good = self.jobs[CUSTOM]
        checkout = good["steps"][0]
        for change, problem in (
            ({"permissions": {"contents": "write"}}, "custom has other rights than contents: read"),
            ({"permissions": {"contents": "read", "pull-requests": "read"}}, "custom has other rights than contents: read"),
            ({"permissions": "{}"}, "custom has other rights than contents: read"),
            ({"steps": [*good["steps"], {"run": "deploy", "env": {"KEY": "${{ secrets.DEPLOY_KEY }}"}}]}, "custom receives a secret or a token: secrets"),
            ({"steps": [*good["steps"], {"run": "gh api user", "env": {"GH_TOKEN": "${{ github.token }}"}}]}, "custom receives a secret or a token: github.token"),
            ({"steps": [{**checkout, "with": {"persist-credentials": "true"}}, *good["steps"][1:]]}, "custom keeps the checkout credentials"),
            ({"steps": [{"uses": checkout["uses"]}, *good["steps"][1:]]}, "custom keeps the checkout credentials"),
            ({"cache-mode": "write"}, "custom requests cache-mode write"),
            ({"cache-mode": "write-only"}, "custom requests cache-mode write-only"),
            ({"cache-mode": "${{ github.event_name == 'push' && 'write' || 'read' }}"}, "custom requests cache-mode ${{ github.event_name == 'push' && 'write' || 'read' }}"),
        ):
            with self.subTest(change=change):
                self.assertEqual([problem], read_class_problems(CUSTOM, {**good, **change}, self.workflow))
        self.assertEqual(["the workflow requests cache-mode write"], read_class_problems(CUSTOM, good, {**self.workflow, "cache-mode": "write"}))
        for mode in ("read", "none"):
            self.assertEqual([], read_class_problems(CUSTOM, {**good, "cache-mode": mode}, self.workflow))


def other_workflow_problems(name: str, fragment: dict, workflows: dict[str, dict]) -> list[str]:
    """Each way a part whose fragment names another shared workflow is not a job of that workflow that its plan starts.
    ``workflows`` holds the document of each tracked workflow by its file name."""
    workflow = workflows.get(fragment["workflow"])
    if workflow is None:
        return [f"{name} names the workflow {fragment['workflow']}, which is no tracked workflow"]
    on = workflow.get("on")
    if "workflow_call" not in (on if isinstance(on, dict) else {}):
        return [f"{name} names the workflow {fragment['workflow']}, which is no shared workflow"]
    job = workflow.get("jobs", {}).get(name)
    if not isinstance(job, dict):
        return [f"{fragment['workflow']} has no job {name}"]
    problems = []
    if PLAN not in needed(job):
        problems.append(f"{name} does not need {PLAN}")
    if str(job.get("if", "")) != f"${{{{ fromJSON(needs.plan.outputs.plan).parts['{name}'].run }}}}":
        problems.append(f"{name} does not run on the plan's decision for {name} alone")
    return problems


class OtherWorkflowTests(unittest.TestCase):
    """A part whose fragment names a shared workflow other than the pipeline is a job of that workflow under the part's
    name, started by the decision of that workflow's own plan."""

    def setUp(self) -> None:
        self.elsewhere = {name: fragment for name, fragment in fragments().items() if "workflow" in fragment}
        self.workflows = {path.rsplit("/", 1)[1]: read_workflow(REPOSITORY_ROOT / path)[1] for path in sorted(tracked_files(REPOSITORY_ROOT, [".github/workflows"]))}

    def test_every_part_of_another_workflow_is_a_job_of_that_workflow_that_its_plan_starts(self) -> None:
        self.assertEqual({"github-release": "release.yml"}, {name: fragment["workflow"] for name, fragment in self.elsewhere.items()})
        for name, fragment in self.elsewhere.items():
            with self.subTest(part=name):
                self.assertEqual([], other_workflow_problems(name, fragment, self.workflows))
                self.assertNotEqual("pipeline.yml", fragment["workflow"], "a part of the pipeline names no workflow")
                self.assertNotIn(name, read_workflow(PIPELINE)[1]["jobs"])

    def test_a_part_of_another_workflow_without_its_job_there_is_named(self) -> None:
        good = {"needs": "plan", "if": "${{ fromJSON(needs.plan.outputs.plan).parts['invented-release'].run }}"}
        fragment = {"description": "x", "workflow": "invented.yml", "configuration": {}}
        shared = {"on": {"workflow_call": None}, "jobs": {"plan": {}, "invented-release": good}}
        self.assertEqual([], other_workflow_problems("invented-release", fragment, {"invented.yml": shared}))
        for workflows, problem in (
            ({}, "invented-release names the workflow invented.yml, which is no tracked workflow"),
            ({"invented.yml": {**shared, "on": {"push": None}}}, "invented-release names the workflow invented.yml, which is no shared workflow"),
            ({"invented.yml": {**shared, "jobs": {"plan": {}}}}, "invented.yml has no job invented-release"),
            ({"invented.yml": {**shared, "jobs": {"invented-release": {**good, "needs": "other"}}}}, "invented-release does not need plan"),
            ({"invented.yml": {**shared, "jobs": {"invented-release": {**good, "if": "always()"}}}}, "invented-release does not run on the plan's decision for invented-release alone"),
        ):
            with self.subTest(problem=problem):
                self.assertEqual([problem], other_workflow_problems("invented-release", fragment, workflows))


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

    def test_a_step_of_the_pipeline_alone_is_stated_with_its_reason(self) -> None:
        only = [step_key(step) for step in self.pipeline if step_key(step) not in {step_key(other) for other in self.shared}]
        self.assertEqual(list(PIPELINE_ONLY), only)
        callers = "Fixed callers and local workflows"
        self.assertEqual([f"{callers}: only the pipeline's hygiene job has this step"],
                         hygiene_step_problems(self.shared, self.pipeline, LEFT_OUT, DIFFERENT, {key: reason for key, reason in PIPELINE_ONLY.items() if key != callers}))
        self.assertEqual(["A removed check: listed as a step of the pipeline alone, but the pipeline has no such step"],
                         hygiene_step_problems(self.shared, self.pipeline, LEFT_OUT, DIFFERENT, {**PIPELINE_ONLY, "A removed check": "none"}))
        moved = "Check every pull request commit"
        self.assertEqual([f"{moved}: listed as a step of the pipeline alone, but the shared workflow has this step"],
                         hygiene_step_problems(self.shared, self.pipeline, LEFT_OUT, DIFFERENT, {**PIPELINE_ONLY, moved: "none"}))

    def test_the_consumer_checks_run_whatever_the_earlier_checks_found_and_read_github_with_the_job_s_token(self) -> None:
        checks = [step for step in self.pipeline if "consumer_checks.py" in str(step.get("run", ""))]
        self.assertEqual(["Fixed callers and local workflows", "Issues of the local workflow register", "Generated Dependabot configuration"], [step["name"] for step in checks])
        for step in checks:
            with self.subTest(step=step["name"]):
                self.assertEqual("${{ !cancelled() }}", step["if"])
                self.assertEqual({"RESOURCE_PATH": "${{ steps.pipeline.outputs.path }}", "GITHUB_TOKEN": "${{ github.token }}", "EVENT": "${{ github.event_name }}",
                                  "PR_AUTHOR": "${{ github.event.pull_request.user.login }}"}, step["env"])
                self.assertIn('--event "$EVENT" --author "$PR_AUTHOR" --summary "$GITHUB_STEP_SUMMARY"', step["run"])


class CallerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text, self.caller = read_workflow(CALLER)

    def test_the_caller_runs_on_pull_requests_and_main_pushes_only(self) -> None:
        self.assertIn("\non:\n  pull_request:\n  push:\n    branches: [main]\n\npermissions: {}\n", self.text)

    def test_the_caller_calls_the_pipeline_by_local_path_as_its_one_job_ci_so_that_the_verdict_s_check_is_ci_verdict(self) -> None:
        self.assertEqual(["ci"], list(self.caller["jobs"]))
        job = self.caller["jobs"]["ci"]
        self.assertEqual({"permissions": {"actions": "read", "contents": "read", "issues": "read", "pull-requests": "read"}, "uses": "$/.github/workflows/pipeline.yml"}, job)
        self.assertIn(VERDICT, read_workflow(PIPELINE)[1]["jobs"])
        for text in ("@v1", "run:", "secrets", "with:", "pull_request_target"):
            self.assertNotIn(text, self.text)

    def test_no_other_workflow_of_this_repository_calls_the_pipeline(self) -> None:
        callers = [path for path in sorted(tracked_files(REPOSITORY_ROOT, [".github/workflows"]))
                   if "workflows/pipeline.yml" in (REPOSITORY_ROOT / path).read_text(encoding="utf-8") and path != ".github/workflows/pipeline.yml"]
        self.assertEqual([".github/workflows/ci.yml"], callers)


if __name__ == "__main__":
    unittest.main()
