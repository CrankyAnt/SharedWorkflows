"""Text contract of the pipeline and of this repository's caller of it: one verdict that always runs, repository hygiene
outside the plan, reading only its two exclusions from the configuration file, and every other shared part a job that
the plan starts, in the pipeline or in the shared workflow that its fragment names, with the jobs that follow a part's
job stated here."""

from __future__ import annotations

import json
import re
import sys
import unittest

from tests.ci_policy_support import REPOSITORY_ROOT, ci_definition, condition_holds, expression_value, flow_items, read_workflow, tracked_files

WORKFLOWS = REPOSITORY_ROOT / ".github" / "workflows"
PIPELINE = WORKFLOWS / "pipeline.yml"
CALLER = WORKFLOWS / "ci.yml"
FRAGMENTS = "actions/pipeline/schema/v1/parts"
HYGIENE = "repository-hygiene"
HYGIENE_WORKFLOW = WORKFLOWS / "repository-hygiene.yml"
# The steps of the hygiene job of repository-hygiene.yml that the pipeline's job leaves out, each with its reason.
LEFT_OUT = {
    "Load the push lookup from this workflow revision": "the pipeline loads suite-selection once in plan as Load the scripts of the suite selection from this workflow revision",
    "Restore the default-branch REUSE version marker": "the pipeline compares hygiene:reuse through the shared digests instead of a default-branch REUSE version marker",
    "Store the default-branch REUSE version marker": "the pipeline compares hygiene:reuse through the shared digests instead of a default-branch REUSE version marker",
    "Restore the passed checks of the pull request": "the pipeline has no edited run that reuses passed checks",
    "Record the passed checks for an edit of the pull request": "the pipeline has no edited run that reuses passed checks",
    "Store the passed checks in the pull request's cache": "the pipeline has no edited run that reuses passed checks",
}
# The steps that differ between the two jobs, each with its reason; every other step is equal in both.
DIFFERENT = {
    "actions/checkout": "the pipeline names the checkout step id checkout so light checks in plan wait for it",
    "Load resources from this workflow revision": "the pipeline loads hygiene resources in plan under id hygiene",
    "Restore the default-branch record": "the pipeline restores the default-branch record for pull requests as well as pushes, and skips it on edited",
    "Look up the tested tree of the pushed commit's pull request": "the pipeline runs the push lookup once in plan and exports it as an output",
    "Decide which checks the changed files can affect": "the pipeline has no inputs and no record of an edited run, passes shared-lookup for hygiene check digests, and runs change detection in plan",
    "Decide whether the PR author skips the convention checks": "the pipeline runs light convention checks in plan",
    "Load the convention checks from this workflow revision": "the pipeline loads publication-checks in plan for branch/reference checks and in repository-hygiene for commitlint",
    "actions/setup-node": "the pipeline reads pr-author-skip from needs.plan.outputs",
    "Check the pull request branch": "the pipeline runs the light branch check in plan",
    "Check every pull request commit": "the pipeline skips commitlint on commits on an edited run",
    "Compute the commitlint cache key": "the pipeline reads pr-author-skip from needs.plan.outputs",
    "Restore the cached commitlint toolchain": "the pipeline reads pr-author-skip from needs.plan.outputs",
    "Install the locked commitlint toolchain": "the pipeline reads pr-author-skip from needs.plan.outputs",
    "Check the pull request title": "the pipeline reads the current title through the API, for a re-run after a corrected title",
    "Check the references to repositories of the owner": "the pipeline runs the light reference check in plan",
    "Dependabot coverage of tracked package files": "the pipeline runs coverage in plan and reads coverage-exclude from the configuration file, not from an input",
    "Relative links in tracked Markdown": "the pipeline runs links in plan and reads link-exclude from the configuration file, not from an input",
    "Compute the hygiene tools cache key": "the pipeline reads hygiene change flags from needs.plan.outputs, relies on plan for the dispatch check, and skips tools on edited",
    "Restore the cached hygiene tools": "the pipeline relies on plan for the dispatch check and skips tools on edited",
    "Install actionlint and ShellCheck": "the pipeline reads hygiene-actionlint from needs.plan.outputs, relies on plan for the dispatch check, and skips tools on edited",
    "actionlint with the pinned ShellCheck": "the pipeline reads hygiene-actionlint from needs.plan.outputs",
    "Install zizmor": "the pipeline reads hygiene-zizmor from needs.plan.outputs, relies on plan for the dispatch check, and skips tools on edited",
    "zizmor over workflows and action definitions": "the pipeline has no input zizmor-persona and reads hygiene-zizmor from needs.plan.outputs",
    "Install REUSE": "the pipeline reads hygiene-reuse from needs.plan.outputs, relies on plan for the dispatch check, and skips tools on edited",
    "REUSE licence assignments": "the pipeline reads hygiene-reuse from needs.plan.outputs",
    "Install Gitleaks": "the pipeline relies on plan for the dispatch check and skips tools on edited",
    "Restore the default-branch Gitleaks scan marker": "the pipeline skips Gitleaks on an edited run",
    "Gitleaks over added commits": "the pipeline takes the Gitleaks files by their fixed names, not from inputs, and skips Gitleaks on an edited run",
}
# The steps that only the pipeline's hygiene checks hold, each with its reason.
PIPELINE_ONLY = {
    "Write the push lookup from the plan": "the pipeline reuses the single push lookup run in plan",
    "Consumer checks": "a caller of repository-hygiene.yml holds a caller of its own and no configuration file",
}
# The jobs of the pipeline that are no shared part.
PLAN, VERDICT = "plan", "verdict"
# The part that runs inside plan (no separate job of its own) and the jobs that follow it.
SUITE_SELECTION = "suite-selection"
IN_PLAN = frozenset({SUITE_SELECTION})
FOLLOWING = {SUITE_SELECTION: ["suite-selection-jobs", "suite-selection-results"]}
# The one reusable workflow that a job of the pipeline calls, with that job: the suite jobs, which the full recording of
# the automation workflow runs as well.
CALLED = {"suite-selection-jobs": "$/.github/workflows/suite-jobs.yml"}
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


# A job that needs the plan runs whenever the plan wrote its plan, also when a light hygiene check inside ``plan``
# failed: the status function keeps a failed ``plan`` from skipping it.
PLAN_WRITTEN = "!cancelled() && needs.plan.outputs.plan != ''"


def part_job_problems(name: str, job: dict) -> list[str]:
    """Each way the job of a shared part that the configuration file switches on is not started by the plan alone."""
    problems = []
    if PLAN not in needed(job):
        problems.append(f"{name} does not need {PLAN}")
    condition = str(job.get("if", ""))
    if condition != f"${{{{ {PLAN_WRITTEN} && fromJSON(needs.plan.outputs.plan).parts['{name}'].run }}}}":
        problems.append(f"{name} does not run on the plan's decision for {name} alone, whenever the plan wrote its plan")
    if "concurrency" in job:
        problems.append(f"{name} sets job-level concurrency instead of relying on the caller's workflow concurrency")
    return problems


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

    def test_the_jobs_are_the_plan_per_shared_part_with_a_schema_fragment_its_job_and_the_jobs_that_follow_it_and_the_verdict(self) -> None:
        expected = [PLAN, *[name for part in self.parts for name in ((() if part in IN_PLAN else (part,)) + tuple(FOLLOWING.get(part, ())))], VERDICT]
        self.assertEqual(expected, list(self.jobs))
        self.assertIn(HYGIENE, self.parts)
        self.assertLessEqual(set(FOLLOWING), set(self.parts))

    def test_only_the_suite_jobs_are_a_reusable_workflow_that_a_job_calls(self) -> None:
        self.assertEqual(CALLED, {name: job["uses"] for name, job in self.jobs.items() if "uses" in job})
        self.assertEqual(["suite-selection-jobs"], [name for names in FOLLOWING.values() for name in names if name in CALLED])

    def test_a_job_that_follows_a_part_s_job_needs_that_job_and_starts_only_after_it(self) -> None:
        for part, following in FOLLOWING.items():
            upstream = PLAN if part in IN_PLAN else part
            for name in following:
                with self.subTest(job=name):
                    self.assertIn(upstream, needed(self.jobs[name]))
                    self.assertIn(f"needs.{upstream}.", self.jobs[name]["if"], "the job decides on what the upstream job gave")

    def test_the_verdict_runs_unless_cancelled_and_needs_the_plan_and_every_part(self) -> None:
        verdict = self.jobs[VERDICT]
        self.assertEqual("${{ !cancelled() }}", verdict["if"])
        self.assertTrue(condition_holds(verdict["if"], {}))
        self.assertEqual([name for name in self.jobs if name != VERDICT], needed(verdict))
        self.assertIn("NEEDS: ${{ toJSON(needs) }}", self.text)
        self.assertEqual({"contents": "read"}, verdict["permissions"])

    def test_every_job_of_the_pipeline_has_a_time_limit_from_the_plan(self) -> None:
        plan_timeout = self.jobs[PLAN].get("timeout-minutes")
        self.assertIsNotNone(plan_timeout, "plan must declare timeout-minutes")
        self.assertGreater(int(str(plan_timeout)), 0)
        for name, job in self.jobs.items():
            if name == PLAN:
                continue
            with self.subTest(job=name):
                if name in CALLED:
                    passed = job.get("with", {})
                    self.assertEqual("${{ fromJSON(needs.plan.outputs.plan).timeouts['suite-selection-linux'] }}",
                                     passed.get("linux-timeout-minutes"))
                    self.assertEqual("${{ fromJSON(needs.plan.outputs.plan).timeouts['suite-selection-windows'] }}",
                                     passed.get("windows-timeout-minutes"))
                else:
                    timeout = str(job.get("timeout-minutes", ""))
                    self.assertIn("needs.plan.outputs.plan", timeout, f"{name} must take timeout-minutes from the plan")
                    self.assertIn("timeouts", timeout, f"{name} must read timeouts from the plan")
                    self.assertIn(name, timeout, f"{name} must read its own timeout from the plan")

    def test_the_plan_gives_its_plan_as_its_output_and_runs_one_checkout_one_push_lookup_one_shared_lookup_and_light_hygiene(self) -> None:
        plan = self.jobs[PLAN]
        self.assertEqual("${{ steps.plan.outputs.plan }}", plan["outputs"]["plan"])
        self.assertEqual({"actions": "read", "contents": "read", "issues": "read", "pull-requests": "read"}, plan["permissions"])
        self.assertNotIn("if", plan)
        self.assertNotIn("needs", plan)
        plan_checkouts = [step for step in plan["steps"] if str(step.get("uses", "")).startswith("actions/checkout@")]
        self.assertEqual(1, len(plan_checkouts), "plan checks out the repository once")
        self.assertEqual(1, self.text.count("suite_selection.py\" lookup "), "the push lookup runs once per pipeline run")
        self.assertEqual(1, self.text.count("suite_selection.py\" shared-lookup "), "the shared lookup runs once per pipeline run")
        self.assertEqual(1, self.text.count("hygiene.py\" changes "), "hygiene change detection runs once per pipeline run")
        self.assertEqual(1, self.text.count("suite_selection.py\" select "), "suite selection runs once per pipeline run")
        plan_step_names = {step_key(step) for step in plan["steps"]}
        for light in ("Decide which checks the changed files can affect", "Check the pull request branch",
                      "Check the references to repositories of the owner", "Consumer checks",
                      "Dependabot coverage of tracked package files", "Relative links in tracked Markdown"):
            self.assertIn(light, plan_step_names, f"light hygiene check {light!r} runs in plan")
        ordered_names = [step_key(step) for step in plan["steps"]]
        self.assertLess(ordered_names.index("Take the shared CI digests"), ordered_names.index("Decide which checks the changed files can affect"))
        self.assertLess(ordered_names.index("Look up the shared CI digests from the default-branch record"),
                        ordered_names.index("Decide which checks the changed files can affect"))
        changes_step = next(step for step in plan["steps"] if step.get("id") == "changes")
        self.assertIn('--shared-lookup "$RUNNER_TEMP/shared-lookup.json"', changes_step["run"])

    def test_repository_hygiene_runs_whenever_the_plan_wrote_its_plan_and_runs_only_tool_installing_checks(self) -> None:
        hygiene = self.jobs[HYGIENE]
        self.assertEqual([PLAN], needed(hygiene))
        self.assertEqual(f"${{{{ {PLAN_WRITTEN} && fromJSON(needs.plan.outputs.plan).parts['repository-hygiene'].run }}}}", hygiene["if"])
        self.assertNotIn("concurrency", hygiene)
        hygiene_text = json.dumps(hygiene)
        self.assertNotIn("suite_selection.py", hygiene_text, "repository-hygiene runs neither check-dispatch nor push lookup")
        self.assertNotIn("hygiene.py\" changes", hygiene_text, "repository-hygiene reuses hygiene change detection from plan")
        self.assertNotIn("consumer_checks.py", hygiene_text, "light consumer checks run in plan")

    def test_every_other_part_runs_on_the_plan_s_decision_without_per_part_concurrency(self) -> None:
        for name in self.parts:
            if name not in IN_PLAN:
                with self.subTest(part=name):
                    self.assertEqual([], part_job_problems(name, self.jobs[name]))

    def test_a_part_job_that_does_not_follow_the_plan_is_named(self) -> None:
        good = {"needs": "plan", "if": f"${{{{ {PLAN_WRITTEN} && fromJSON(needs.plan.outputs.plan).parts['invented-test'].run }}}}"}
        self.assertEqual([], part_job_problems("invented-test", good))
        alone = "invented-test does not run on the plan's decision for invented-test alone, whenever the plan wrote its plan"
        for change, problem in (({"needs": "repository-hygiene"}, "invented-test does not need plan"),
                                ({"if": "always()"}, alone),
                                ({"if": f"${{{{ {PLAN_WRITTEN} && fromJSON(needs.plan.outputs.plan).parts['other-test'].run }}}}"}, alone),
                                ({"if": "${{ fromJSON(needs.plan.outputs.plan).parts['invented-test'].run }}"}, alone),
                                ({"concurrency": {"group": "fixed", "cancel-in-progress": "true"}},
                                 "invented-test sets job-level concurrency instead of relying on the caller's workflow concurrency")):
            with self.subTest(change=change):
                self.assertEqual([problem], part_job_problems("invented-test", {**good, **change}))

    def test_a_failed_light_check_in_plan_skips_no_job_that_needs_the_plan_once_the_plan_is_written(self) -> None:
        # The run of a failed link check: plan ended failure after it wrote its plan with every part planned.
        context = {
            "github": {"event_name": "pull_request"},
            "needs": {"plan": {"result": "failure", "outputs": {
                "plan": "written", "selection": "success", "shards": "[1]", "windows": "",
                "units": '{"a":{}}', "reused": "", "light-links": "failure",
            }}},
        }
        self.assertEqual("${{ steps.select.outcome }}", self.jobs[PLAN]["outputs"].get("selection"))
        for name, job in self.jobs.items():
            if PLAN not in needed(job) or name == VERDICT:
                continue
            with self.subTest(job=name):
                condition = str(job["if"])
                self.assertTrue(condition.startswith(f"${{{{ {PLAN_WRITTEN} && "), f"{name} runs whenever the plan wrote its plan")
                rest = condition[len(f"${{{{ {PLAN_WRITTEN} && "):-2]
                if "fromJSON" not in rest:
                    self.assertTrue(expression_value(rest, context), f"{name} runs after a failed light check")
                without_plan = {"needs": {"plan": {"result": "failure", "outputs": {"plan": ""}}}}
                self.assertFalse(expression_value("needs.plan.outputs.plan != ''", without_plan), "no job runs without a written plan")
        self.assertEqual("${{ !cancelled() }}", self.jobs[VERDICT]["if"], "the verdict judges the jobs after a failed light check")

    def test_light_checks_in_plan_do_not_depend_on_the_plan_step_succeeding(self) -> None:
        by_id = {step.get("id"): step for step in self.jobs[PLAN]["steps"] if step.get("id")}
        expected_loaders = {
            "changes": ("hygiene", "checkout"),
            "author-decision": (),
            "conventions": (),
            "branch": ("conventions",),
            "references": ("conventions", "checkout"),
            "consumer-checks": ("resources", "checkout"),
            "coverage": ("resources", "hygiene", "checkout"),
            "links": ("resources", "hygiene", "checkout"),
        }
        for step_id, loaders in expected_loaders.items():
            with self.subTest(step=step_id):
                condition = str(by_id[step_id].get("if", ""))
                self.assertIn("!cancelled()", condition)
                self.assertNotIn("steps.plan.outcome", condition, f"{step_id} must still run when an earlier plan step fails")
                for loader in loaders:
                    self.assertIn(f"steps.{loader}.outcome == 'success'", condition, f"{step_id} must wait for its input loader {loader}")
        by_name = {step.get("name"): step for step in self.jobs[PLAN]["steps"] if step.get("name")}
        for name in ("Upload the pipeline plan", "Record the tested state of the pull request", "Upload the tested state of the pull request"):
            with self.subTest(step=name):
                self.assertIn("steps.plan.outcome == 'success'", str(by_name[name].get("if", "")))

    def test_suite_selection_results_downloads_entry_times_and_checks_out_only_when_additions_were_reported(self) -> None:
        steps = self.jobs["suite-selection-results"]["steps"]
        by_name = {step.get("id") or step.get("name") or step.get("uses"): step for step in steps}
        self.assertEqual("recording-additions-*", by_name["Download the additions of the run"]["with"]["pattern"])
        entry_times = by_name["Download the entry times of the run"]
        self.assertEqual("steps.additions.outputs.found == 'true'", entry_times["if"])
        self.assertEqual("recording-entry-times-*", entry_times["with"]["pattern"])
        self.assertEqual("${{ runner.temp }}/suite-results/additions/entry-times", entry_times["with"]["path"])
        checkout = next(step for step in steps if str(step.get("uses", "")).startswith("actions/checkout@"))
        self.assertEqual("steps.additions.outputs.found == 'true'", checkout["if"])

    def test_the_plan_gives_the_outcome_of_each_light_check_to_the_verdict(self) -> None:
        outputs = self.jobs[PLAN]["outputs"]
        steps = {step.get("id"): step_key(step) for step in self.jobs[PLAN]["steps"] if step.get("id")}
        for output, step_id, step_name in (("light-branch", "branch", "Check the pull request branch"),
                                           ("light-references", "references", "Check the references to repositories of the owner"),
                                           ("light-consumer-checks", "consumer-checks", "Consumer checks"),
                                           ("light-coverage", "coverage", "Dependabot coverage of tracked package files"),
                                           ("light-links", "links", "Relative links in tracked Markdown")):
            with self.subTest(output=output):
                self.assertEqual(f"${{{{ steps.{step_id}.outcome }}}}", outputs[output])
                self.assertEqual(step_name, steps[step_id])

    def test_repository_hygiene_has_read_rights_only_and_keeps_no_checkout_credentials(self) -> None:
        self.assertEqual({"contents": "read", "pull-requests": "read"}, self.jobs[HYGIENE]["permissions"])
        checkouts = re.findall(r"- uses: actions/checkout@[0-9a-f]{40} # v[\d.]+\n        with:\n((?:          .*\n)+)", self.text)
        self.assertEqual(self.text.count("actions/checkout@"), len(checkouts), "every checkout of the pipeline states its options")
        self.assertGreaterEqual(len(checkouts), 2)
        for options in checkouts:
            self.assertIn("persist-credentials: false", options)

    def test_repository_hygiene_takes_the_gitleaks_files_by_their_fixed_names_and_offers_no_other_choice(self) -> None:
        self.assertIn('if [ -f .gitleaks.toml ]; then config=".gitleaks.toml"; fi', self.text)
        self.assertIn('if [ -f .gitleaks-baseline.json ]; then baseline=".gitleaks-baseline.json"; fi', self.text)
        self.assertIn('hygiene.py" gitleaks "$@" --tools "$HYGIENE_TOOLS" --config "$config" --baseline "$baseline"', self.text)
        self.assertIn('hygiene.py" zizmor --tools "$HYGIENE_TOOLS" --persona ""', self.text)

    def test_the_coverage_and_link_checks_take_their_exclusions_from_the_configuration_file_and_a_change_to_one_starts_its_check(self) -> None:
        steps = {step.get("id"): step for step in self.jobs[PLAN]["steps"]}
        for check, setting, command in (("coverage", "coverage-exclude", 'hygiene.py" coverage --exclude-from -'),
                                        ("links", "link-exclude", 'hygiene.py" links --exclude-from - --summary "$GITHUB_STEP_SUMMARY"')):
            with self.subTest(check=check):
                step = steps[check]
                self.assertEqual("${{ steps.resources.outputs.path }}", step["env"]["PIPELINE_PATH"], "the pipeline's own revision reads the setting")
                self.assertIn("set -euo pipefail", step["run"])
                self.assertRegex(step["run"], rf'"\$PIPELINE_PATH/plan\.py" setting --part repository-hygiene --setting {setting} \|\n'
                                              rf'\s+PYTHONPYCACHEPREFIX="\$RUNNER_TEMP/repo-pycache" python3 "\$RESOURCE_PATH/{re.escape(command)}\n?\Z')
                self.assertNotIn("inputs.", step["run"] + json.dumps(step["env"]))
        changes = steps["changes"]["run"]
        self.assertIn('--packages-definition ".github/ci-config.yml#value:parts.repository-hygiene.coverage-exclude"', changes)
        self.assertIn('--markdown-definition ".github/ci-config.yml#value:parts.repository-hygiene.link-exclude"', changes)

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

    def test_the_job_runs_on_the_runner_that_the_configuration_names_and_on_ubuntu_without_one(self) -> None:
        self.assertEqual(f"${{{{ {self.plan}.config['runner-label'] || 'ubuntu-26.04' }}}}", self.job["runs-on"])

    def test_the_last_step_runs_the_configured_script_with_bash_on_every_runner_and_its_exit_status_is_the_job_s_result(self) -> None:
        step = self.steps[-1]
        self.assertEqual({"SCRIPT": f"${{{{ {self.plan}.config.script }}}}"}, step["env"])
        self.assertEqual('bash -- "$SCRIPT"', step["run"], "the script's path reaches the shell as a value, never as text of the command")
        self.assertEqual("bash", step["shell"], "on a Windows runner the default shell is pwsh, where $SCRIPT is no environment variable")
        self.assertEqual(["env", "name", "run", "shell"], sorted(step))
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
    decision = f"fromJSON(needs.plan.outputs.plan).parts['{name}'].run }}}}"
    condition = str(job.get("if", ""))
    # The plan's decision alone, or behind the events of the job in parentheses, as a job of the automation workflow holds it.
    if condition != "${{ " + decision and not (condition.startswith("${{ (") and condition.endswith(") && " + decision)):
        problems.append(f"{name} does not run on the plan's decision for {name} alone")
    return problems


class OtherWorkflowTests(unittest.TestCase):
    """A part whose fragment names a shared workflow other than the pipeline is a job of that workflow under the part's
    name, started by the decision of that workflow's own plan."""

    def setUp(self) -> None:
        self.elsewhere = {name: fragment for name, fragment in fragments().items() if "workflow" in fragment}
        self.workflows = {path.rsplit("/", 1)[1]: read_workflow(REPOSITORY_ROOT / path)[1] for path in sorted(tracked_files(REPOSITORY_ROOT, [".github/workflows"]))}

    def test_every_part_of_another_workflow_is_a_job_of_that_workflow_that_its_plan_starts(self) -> None:
        self.assertEqual({"additions": "automation.yml", "dependabot-auto-merge": "automation.yml", "full-recording": "automation.yml",
                          "github-release": "release.yml", "issue-title": "automation.yml"}, {name: fragment["workflow"] for name, fragment in self.elsewhere.items()})
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
        behind_events = {**good, "if": "${{ (github.event_name == 'schedule') && fromJSON(needs.plan.outputs.plan).parts['invented-release'].run }}"}
        self.assertEqual([], other_workflow_problems("invented-release", fragment, {"invented.yml": {**shared, "jobs": {"plan": {}, "invented-release": behind_events}}}))
        for workflows, problem in (
            ({}, "invented-release names the workflow invented.yml, which is no tracked workflow"),
            ({"invented.yml": {**shared, "on": {"push": None}}}, "invented-release names the workflow invented.yml, which is no shared workflow"),
            ({"invented.yml": {**shared, "jobs": {"plan": {}}}}, "invented.yml has no job invented-release"),
            ({"invented.yml": {**shared, "jobs": {"invented-release": {**good, "needs": "other"}}}}, "invented-release does not need plan"),
            ({"invented.yml": {**shared, "jobs": {"invented-release": {**good, "if": "always()"}}}}, "invented-release does not run on the plan's decision for invented-release alone"),
            ({"invented.yml": {**shared, "jobs": {"invented-release": {**good, "if": "${{ (github.event_name == 'push') || fromJSON(needs.plan.outputs.plan).parts['invented-release'].run }}"}}}},
             "invented-release does not run on the plan's decision for invented-release alone"),
        ):
            with self.subTest(problem=problem):
                self.assertEqual([problem], other_workflow_problems("invented-release", fragment, workflows))


class HygieneStepTests(unittest.TestCase):
    """The pipeline's hygiene checks (light checks in ``plan``, tool-installing checks in ``repository-hygiene``) run
    the floor of repository-hygiene.yml: each step there has its counterpart here."""

    def setUp(self) -> None:
        self.shared = read_workflow(HYGIENE_WORKFLOW)[1]["jobs"]["hygiene"]["steps"]
        jobs = read_workflow(PIPELINE)[1]["jobs"]
        self.plan_steps = jobs[PLAN]["steps"]
        self.hygiene_steps = jobs[HYGIENE]["steps"]
        theirs_keys = {step_key(step) for step in self.shared}
        # Combine the hygiene steps from plan and repository-hygiene in shared order, deduplicating shared keys.
        seen: set[str] = set()
        combined: list[dict] = []
        for step in [*self.plan_steps, *self.hygiene_steps]:
            key = step_key(step)
            if key in seen:
                continue
            if key in theirs_keys or key in PIPELINE_ONLY:
                seen.add(key)
                combined.append(step)
        self.pipeline = combined

    def test_every_step_of_the_shared_hygiene_job_has_its_counterpart_in_the_pipeline_apart_from_the_stated_differences(self) -> None:
        theirs = {step_key(step): step for step in self.shared}
        ours = {step_key(step): step for step in self.pipeline}
        for key in theirs:
            if key in LEFT_OUT:
                self.assertNotIn(key, ours)
            else:
                self.assertIn(key, ours, f"{key} missing from pipeline")
                if key not in DIFFERENT:
                    self.assertEqual(theirs[key], ours[key], f"{key} differs")

    def test_a_step_that_only_the_shared_hygiene_job_holds_is_named(self) -> None:
        added = [*self.shared, {"name": "A new check of the floor", "run": "check"}]
        self.assertIn("A new check of the floor: the pipeline's hygiene job has no such step", hygiene_step_problems(added, self.pipeline, LEFT_OUT, DIFFERENT))
        references = "Check the references to repositories of the owner"
        without = [step for step in self.pipeline if step.get("name") != references]
        self.assertIn(f"{references}: the pipeline's hygiene job has no such step", hygiene_step_problems(self.shared, without, LEFT_OUT, DIFFERENT))

    def test_a_step_that_changed_in_one_job_only_is_named(self) -> None:
        name = "Store the default-branch Gitleaks scan marker"
        changed = [{**step, "run": "another command"} if step.get("name") == name else step for step in self.shared]
        self.assertIn(f"{name}: the step differs between the two jobs", hygiene_step_problems(changed, self.pipeline, LEFT_OUT, DIFFERENT))

    def test_a_stated_difference_that_no_longer_exists_is_named(self) -> None:
        self.assertIn("Store the default-branch Gitleaks scan marker: the step is equal in both jobs, but it is listed as different",
                      hygiene_step_problems(self.shared, self.pipeline, LEFT_OUT, {**DIFFERENT, "Store the default-branch Gitleaks scan marker": "none"}))
        self.assertIn("A removed step: listed as a difference, but the shared workflow has no such step",
                      hygiene_step_problems(self.shared, self.pipeline, {**LEFT_OUT, "A removed step": "none"}, DIFFERENT))
        self.assertIn("An extra step: only the pipeline's hygiene job has this step",
                      hygiene_step_problems(self.shared, [*self.pipeline, {"name": "An extra step", "run": "extra"}], LEFT_OUT, DIFFERENT))

    def test_a_step_of_the_pipeline_alone_is_stated_with_its_reason(self) -> None:
        only = [step_key(step) for step in self.pipeline if step_key(step) not in {step_key(other) for other in self.shared}]
        self.assertEqual(sorted(PIPELINE_ONLY), sorted(only))
        callers = "Consumer checks"
        self.assertIn(f"{callers}: only the pipeline's hygiene job has this step",
                      hygiene_step_problems(self.shared, self.pipeline, LEFT_OUT, DIFFERENT, {key: reason for key, reason in PIPELINE_ONLY.items() if key != callers}))
        self.assertIn("A removed check: listed as a step of the pipeline alone, but the pipeline has no such step",
                      hygiene_step_problems(self.shared, self.pipeline, LEFT_OUT, DIFFERENT, {**PIPELINE_ONLY, "A removed check": "none"}))
        moved = "Check every pull request commit"
        self.assertIn(f"{moved}: listed as a step of the pipeline alone, but the shared workflow has this step",
                      hygiene_step_problems(self.shared, self.pipeline, LEFT_OUT, DIFFERENT, {**PIPELINE_ONLY, moved: "none"}))

    def test_the_consumer_checks_run_in_plan_and_read_github_with_the_job_s_token(self) -> None:
        checks = [step for step in self.plan_steps if "consumer_checks.py" in str(step.get("run", ""))]
        self.assertEqual(["Consumer checks"], [step["name"] for step in checks])
        step = checks[0]
        self.assertEqual(
            "${{ !cancelled() && steps.resources.outcome == 'success' && steps.checkout.outcome == 'success' && github.event_name == 'pull_request' && github.event.action != 'edited' && github.event.pull_request.user.login != 'dependabot[bot]' }}",
            step["if"],
        )
        self.assertEqual({"RESOURCE_PATH": "${{ steps.resources.outputs.path }}", "GITHUB_TOKEN": "${{ github.token }}", "EVENT": "${{ github.event_name }}",
                          "PR_AUTHOR": "${{ github.event.pull_request.user.login }}"}, step["env"])
        self.assertIn('consumer_checks.py" all --event "$EVENT" --author "$PR_AUTHOR" --summary "$GITHUB_STEP_SUMMARY"', step["run"])

    def test_each_hygiene_tool_install_gates_only_its_own_check_so_other_checks_still_run(self) -> None:
        by_id = {step.get("id"): step for step in self.hygiene_steps if step.get("id")}
        for check in ("actionlint", "zizmor", "reuse", "gitleaks"):
            install_id = f"install-{check}"
            self.assertIn(install_id, by_id)
            self.assertIn("!cancelled()", str(by_id[install_id].get("if", "")))
            self.assertIn(f"steps.{install_id}.outcome == 'success'", str(by_id[check].get("if", "")))
            for other in ("actionlint", "zizmor", "reuse", "gitleaks"):
                if other != check:
                    self.assertNotIn(f"steps.{install_id}.", str(by_id[other].get("if", "")))


DISPATCHED = "${{ github.event_name == 'repository_dispatch' && github.event.client_payload.sha || '' }}"
DISPATCH_CHECK = "Check that the dispatched commit is on the default branch"


def dispatch_problems(jobs: dict) -> list[str]:
    """Each way a dependabot-merged run would not be a push of the dispatched commit behind the dispatch check: a checkout
    of another commit, or a plan that does not wait for the check."""
    problems = []
    for name, job in jobs.items():
        for step in job.get("steps", []):
            if str(step.get("uses", "")).startswith("actions/checkout@") and (step.get("with") or {}).get("ref") != DISPATCHED:
                problems.append(f"{name} does not check out the dispatched commit")
    steps = jobs[PLAN]["steps"]
    names = [str(step.get("name") or step.get("id") or step.get("uses")) for step in steps]
    checkout = next((index for index, step in enumerate(steps) if str(step.get("uses", "")).startswith("actions/checkout@")), None)
    if DISPATCH_CHECK not in names or checkout is None or names.index(DISPATCH_CHECK) != checkout + 1:
        problems.append(f"{PLAN} does not check the dispatched commit right after its checkout")
    else:
        check = steps[names.index(DISPATCH_CHECK)]
        if check.get("if") != "github.event_name == 'repository_dispatch'" or "check-dispatch --root . --sha" not in str(check.get("run")):
            problems.append(f"{PLAN} does not run the dispatch check on a dispatched commit")
        for guarded in ("initial-plan", "select", "plan"):
            later = next(step for step in steps if step.get("id") == guarded)
            if steps.index(later) < names.index(DISPATCH_CHECK):
                problems.append(f"{PLAN} runs {guarded} before the dispatch check")
    return problems


class DispatchTests(unittest.TestCase):
    """A dependabot-merged run: the pipeline treats it as a push of the dispatched commit, behind the dispatch check."""

    def setUp(self) -> None:
        self.text, pipeline = read_workflow(PIPELINE)
        self.jobs = pipeline["jobs"]

    def test_every_checkout_takes_the_dispatched_commit_and_the_plan_waits_for_the_dispatch_check(self) -> None:
        self.assertEqual([], dispatch_problems(self.jobs))

    def test_a_pipeline_that_drops_a_part_of_the_route_is_named(self) -> None:
        def changed(old: str, new: str, count: int = 1) -> dict:
            self.assertGreaterEqual(self.text.count(old), count, old)
            return ci_definition().read_yaml(self.text.replace(old, new, count), "a changed pipeline.yml")["jobs"]

        ref = f"          ref: {DISPATCHED}\n"
        self.assertEqual(["plan does not check out the dispatched commit"], dispatch_problems(changed(ref, "", 1)))
        self.assertEqual(8, self.text.count(ref), "every checkout of the pipeline")
        check = self.text[self.text.index(f"      - name: {DISPATCH_CHECK}\n"):]
        check = check[:check.index("\n      - ", 1) + 1]
        self.assertEqual(1, self.text.count(check), "the dispatch check runs once in plan")
        self.assertEqual(["plan does not check the dispatched commit right after its checkout"], dispatch_problems(changed(check, "", 1)))

    def test_the_plan_takes_the_dispatched_commit_as_its_head_and_that_commit_s_first_parent_as_its_base(self) -> None:
        plan = next(step for step in self.jobs[PLAN]["steps"] if step.get("id") == "plan")
        self.assertEqual("${{ github.event_name == 'pull_request' && github.event.pull_request.head.sha || (github.event_name == 'repository_dispatch' "
                         "&& github.event.client_payload.sha) || github.sha }}", plan["env"]["HEAD"])
        self.assertIn('if [ "$EVENT" = repository_dispatch ]; then BASE="$(git rev-parse --verify --quiet "$HEAD^" || true)"; fi\n', plan["run"])
        self.assertLess(plan["run"].index('BASE="$(git rev-parse'), plan["run"].index("plan.py"))

    def test_the_checks_of_the_hygiene_job_decide_on_a_dispatch_as_on_a_push(self) -> None:
        changes = next(step for step in self.jobs[PLAN]["steps"] if step.get("id") == "changes")
        self.assertIn('push|repository_dispatch) set -- --lookup "$RUNNER_TEMP/push-lookup.json" ;;', changes["run"])


# The shared workflows whose run blocks reach this repository's files only through a part folder that a `$/` step loaded.
CONSUMER_PATH_WORKFLOWS = ("pipeline.yml", "repository-hygiene.yml", "python-tests.yml")
# The one stated exception: the definition options of `hygiene.py changes` name a definition by its path and selector,
# which the program looks up in the checkout's history; they name no file of this repository to read.
DEFINITION_OPTIONS = ("--definition", "--packages-definition", "--markdown-definition")
LOADED_PATH = re.compile(r"^\$\{\{\s*steps\.([\w-]+)\.outputs\.path\s*\}\}$")
HEREDOC = re.compile(r"<<-?\s*'?(\w+)'?\n.*?\n\s*\1(?:\n|$)", re.DOTALL)
SHELL_WORD = re.compile(r"""(?:[^\s"';|&()<>]|"[^"]*"|'[^']*')+""")
VARIABLE_PREFIX = re.compile(r"^\$\{?(\w+)\}?/(.*)$")
EXPRESSION_PREFIX = re.compile(r"^\$\{\{\s*(.+?)\s*\}\}/(.*)$")


def repository_paths() -> tuple[set[str], set[str]]:
    """The tracked paths of the shared parts and shared workflows, and the names of the files that lie directly in a part
    folder, which a block could give a program bare, through the working directory."""
    tracked = tracked_files(REPOSITORY_ROOT, ["actions", ".github/workflows"])
    return tracked, {path.split("/")[2] for path in tracked if path.startswith("actions/") and path.count("/") == 2}


def names_repository_path(text: str, tracked: set[str], part_files: set[str]) -> bool:
    path = text.split("#", 1)[0].removeprefix("./").rstrip("/")
    return bool(path) and (path in part_files or any(item == path or item.startswith(path + "/") for item in tracked))


def run_block_words(run: str) -> list[str]:
    """The shell words of a run block without quotes, an assignment or an option with `=` as its value alone, and without
    the bodies of here-documents, which are another program's text."""
    words = []
    for word in SHELL_WORD.findall(HEREDOC.sub("\n", run)):
        word = word.replace('"', "").replace("'", "")
        if "=" in word and not word.startswith("$"):
            word = word.split("=", 1)[1]
        words.append(word)
    return words


def consumer_path_problems(name: str, workflow: dict, tracked: set[str], part_files: set[str]) -> list[str]:
    """Each path of this repository that a run block of ``workflow`` gives a program, the program's own path included,
    and that does not start with a variable whose value a `$/` step set; a consumer's working directory holds none."""
    problems = []
    for job_name, job in workflow.get("jobs", {}).items():
        steps = job.get("steps", [])
        loaded_steps = {step["id"] for step in steps if str(step.get("uses", "")).startswith("$/") and "id" in step}
        for step in steps:
            if "run" not in step:
                continue
            where = f"{name}: job {job_name}, step {step.get('name') or step.get('id')}"
            environment = {**job.get("env", {}), **step.get("env", {})}
            loaded = {key for key, value in environment.items() if (found := LOADED_PATH.match(str(value))) and found[1] in loaded_steps}
            for key, value in environment.items():
                value = str(value)
                found = EXPRESSION_PREFIX.match(value)
                if found:
                    source = LOADED_PATH.match(f"${{{{ {found[1]} }}}}")
                    if source and source[1] in loaded_steps:
                        continue
                    rest = found[2]
                elif value.startswith("${{"):
                    continue
                else:
                    rest = value
                if names_repository_path(rest, tracked, part_files):
                    problems.append(f"{where}: the variable {key} gives the path {value} of this repository, which does not start with a part folder that a $/ step loaded")
            words = run_block_words(str(step["run"]))
            defines = 'hygiene.py" changes' in str(step["run"]) or "hygiene.py changes" in str(step["run"])
            for index, word in enumerate(words):
                if defines and index and words[index - 1] in DEFINITION_OPTIONS:
                    continue
                prefixed = VARIABLE_PREFIX.match(word)
                if prefixed and prefixed[1] in loaded:
                    continue
                rest = prefixed[2] if prefixed else word
                if not word.startswith("$") or prefixed:
                    if names_repository_path(rest, tracked, part_files):
                        problems.append(f"{where}: {word} is a path of this repository that does not start with a variable set from a $/ step")
    return problems


class ConsumerPathTests(unittest.TestCase):
    """A consumer's checkout holds no file of this repository: a run block reaches one only through a part folder that a
    `$/` step loaded, whose path a variable of the step carries."""

    def setUp(self) -> None:
        self.tracked, self.part_files = repository_paths()

    def problems(self, name: str, workflow: dict) -> list[str]:
        return consumer_path_problems(name, workflow, self.tracked, self.part_files)

    def test_every_run_block_gives_a_program_a_path_of_this_repository_only_through_a_loaded_part_folder(self) -> None:
        for name in CONSUMER_PATH_WORKFLOWS:
            with self.subTest(workflow=name):
                self.assertEqual([], self.problems(name, read_workflow(WORKFLOWS / name)[1]))

    def test_a_path_of_this_repository_given_through_the_working_directory_is_named_with_its_workflow(self) -> None:
        install = '--pins "$RESOURCE_PATH/pins.json"'
        for replacement, path in (("--pins actions/repository-hygiene/pins.json", "actions/repository-hygiene/pins.json"),
                                  ('--pins "$GITHUB_WORKSPACE/actions/repository-hygiene/pins.json"', "$GITHUB_WORKSPACE/actions/repository-hygiene/pins.json"),
                                  ("--pins pins.json", "pins.json")):
            with self.subTest(replacement=replacement):
                text = PIPELINE.read_text(encoding="utf-8")
                self.assertIn(install, text)
                workflow = ci_definition().read_yaml(text.replace(install, replacement, 1), str(PIPELINE))
                problems = self.problems("pipeline.yml", workflow)
                self.assertEqual(1, len(problems), problems)
                self.assertTrue(problems[0].startswith("pipeline.yml: job repository-hygiene, step Compute the hygiene tools cache key"), problems[0])
                self.assertIn(f"{path} is a path of this repository", problems[0])

    def test_a_program_s_own_path_and_a_variable_that_no_loaded_part_folder_set_are_named(self) -> None:
        text, workflow = read_workflow(WORKFLOWS / "python-tests.yml")
        program = '"$RESOURCE_PATH/python_tests.py"'
        self.assertIn(program, text)
        problems = self.problems("python-tests.yml", ci_definition().read_yaml(text.replace(program, "actions/python-tests/python_tests.py", 1), "python-tests.yml"))
        self.assertEqual(1, len(problems), problems)
        self.assertIn("actions/python-tests/python_tests.py is a path of this repository", problems[0])
        step = next(step for step in workflow["jobs"]["tests"]["steps"] if program in str(step.get("run", "")))
        step["env"] = {**step["env"], "RESOURCE_PATH": "${{ github.workspace }}"}
        self.assertTrue(any("$RESOURCE_PATH/python_tests.py" in problem for problem in self.problems("python-tests.yml", workflow)))

    def test_the_definitions_of_the_change_decision_are_the_one_stated_exception(self) -> None:
        text = PIPELINE.read_text(encoding="utf-8")
        definition = '--definition "actions/repository-hygiene/action.yml#step:resources"'
        self.assertIn(definition, text)
        self.assertEqual([], self.problems("pipeline.yml", ci_definition().read_yaml(text, str(PIPELINE))))
        problems = self.problems("pipeline.yml", ci_definition().read_yaml(text.replace(definition, f"{definition} --ecosystems actions/repository-hygiene/package-ecosystems.json"), str(PIPELINE)))
        self.assertEqual(["pipeline.yml: job plan, step Decide which checks the changed files can affect: actions/repository-hygiene/package-ecosystems.json "
                          "is a path of this repository that does not start with a variable set from a $/ step"], problems)


class CallerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text, self.caller = read_workflow(CALLER)

    def test_the_caller_runs_on_pull_requests_main_pushes_and_the_dispatch_after_a_dependabot_merge_only(self) -> None:
        self.assertIn("\non:\n  pull_request:\n    types: [opened, synchronize, reopened, edited]\n  push:\n    branches: [main]\n  repository_dispatch:\n    types: [dependabot-merged]\n\npermissions: {}\n", self.text)

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


def load_module(name: str, relative: str):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, REPOSITORY_ROOT / relative)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # a dataclass reads its module from sys.modules while the module loads
    spec.loader.exec_module(module)
    return module


class PartDigestCoverageTests(unittest.TestCase):
    """Every shared folder that a part's job loads with ``uses: $/actions/<folder>`` is covered by a digest that the plan
    names for that part, so that moving a part's code to a new folder without updating the digest map fails."""

    def setUp(self) -> None:
        self.plan = load_module("ci_policy_plan", "actions/pipeline/plan.py")
        self.promote = load_module("ci_policy_promote_v1", "actions/repository-hygiene/promote_v1.py")
        self.jobs = read_workflow(PIPELINE)[1]["jobs"]

    def uncovered(self, jobs: dict) -> dict[str, set[str]]:
        """Per part, the folders its job loads that no digest the plan names for it covers."""
        found = {}
        for part, digests in self.plan.PART_DIGESTS.items():
            covered: set[str] = set()
            for digest in digests:
                kind, _, name = digest.partition(":")
                if kind == "part":
                    covered.update(self.promote.PART_DIGEST_FOLDERS[name])
                elif kind == "language":
                    covered.add(self.promote.LANGUAGE_STEP_FOLDERS[name])
                else:
                    self.fail(f"{part} names the digest {digest}, which covers no folder")
            loaded = {step["uses"].removeprefix("$/") for step in jobs[part].get("steps", [])
                      if isinstance(step, dict) and str(step.get("uses", "")).startswith("$/actions/")}
            if loaded - covered:
                found[part] = loaded - covered
        return found

    def test_every_folder_a_part_job_loads_is_covered_by_a_digest_of_that_part(self) -> None:
        self.assertLessEqual(set(self.plan.PART_DIGESTS), set(self.jobs))
        self.assertEqual({}, self.uncovered(self.jobs))

    def test_a_part_s_code_moved_to_a_new_folder_without_its_digest_fails_the_check(self) -> None:
        jobs = json.loads(json.dumps(self.jobs))
        for step in jobs["dotnet-test"]["steps"]:
            if step.get("uses") == "$/actions/dotnet-test":
                step["uses"] = "$/actions/dotnet-tests"
        self.assertEqual({"dotnet-test": {"actions/dotnet-tests"}}, self.uncovered(jobs))


if __name__ == "__main__":
    unittest.main()
