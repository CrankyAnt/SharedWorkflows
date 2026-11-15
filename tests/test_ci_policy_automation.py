"""Text contract of the automation workflow, of the fixed caller that calls it and of this repository's own caller: every
part that a schema fragment places in `automation.yml` is a job that the plan starts and that states its events as a
job-level condition, so that an event without work for a part starts no runner for it; the plan itself runs only on an
event that some part has work on; and the additions workflow acts only for a branch of the same repository and applies
the additions before any write token exists."""

from __future__ import annotations

import json
import re
import unittest

from tests.ci_policy_support import REPOSITORY_ROOT, ci_definition, flow_items, read_workflow, tracked_files, workflow_triggers

WORKFLOWS = REPOSITORY_ROOT / ".github" / "workflows"
AUTOMATION = WORKFLOWS / "automation.yml"
OWN_CALLER = WORKFLOWS / "automation-caller.yml"
TEMPLATE = REPOSITORY_ROOT / "actions" / "pipeline" / "consumer-kit" / "automation.yml"
FRAGMENTS = "actions/pipeline/schema/v1/parts"
PLAN = "plan"
LEVELS = {"none": 0, "read": 1, "write": 2}
APP = "crankyant-publication-checks[bot]"
REPOSITORY = "example-owner/example-repository"
DECISION = "fromJSON(needs.plan.outputs.plan).parts['{part}'].run"
# What a job-level condition may read so that GitHub decides it without a runner: the event and the repository.
EVENT_VALUE = re.compile(r"github\.(?:event_name|repository|event\.[\w.-]+)")
WRITE_TOKEN_STEPS = ("actions/create-github-app-token",)


def automation_parts() -> list[str]:
    """The parts whose schema fragment names the automation workflow, in the order of their file names."""
    names = []
    for path in sorted(tracked_files(REPOSITORY_ROOT, [FRAGMENTS])):
        fragment = json.loads((REPOSITORY_ROOT / path).read_text(encoding="utf-8"))
        if fragment.get("workflow") == AUTOMATION.name:
            names.append(path.rsplit("/", 1)[1].removesuffix(".json"))
    return names


def needed(job: dict) -> list[str]:
    needs = str(job.get("needs", ""))
    return flow_items(needs[1:-1]) if needs.startswith("[") else [needs] if needs else []


def events_of(part: str, job: dict) -> str | None:
    """The events in the condition of a part's job: what stands in parentheses before the plan's decision for the part."""
    found = re.fullmatch(r"\$\{\{ \((.+)\) && " + re.escape(DECISION.format(part=part)) + r" \}\}", str(job.get("if", "")))
    return found[1] if found else None


def part_job_problems(part: str, job: dict) -> list[str]:
    """Each way the job of a part does not skip without a runner: no need of the plan, or a condition that is not the
    part's events and then the plan's decision for the part."""
    problems = [] if needed(job) == [PLAN] else [f"{part} does not need {PLAN} alone"]
    events = events_of(part, job)
    if events is None:
        return [*problems, f"{part} has no job-level condition of its events and the plan's decision for {part}"]
    if "github.event_name == '" not in events:
        problems.append(f"{part} states no event in its job-level condition")
    left = EVENT_VALUE.sub("", events)
    if re.search(r"\b(?:needs|steps|vars|secrets|env|inputs|matrix|runner|job|github)\b", left):
        problems.append(f"{part} reads more than the event in its job-level condition")
    return problems


def plan_problems(jobs: dict, parts: list[str]) -> list[str]:
    """Each way the plan would start a runner on an event that no part has work on, or miss an event that a part has."""
    events = [events_of(part, jobs.get(part, {})) for part in parts]
    if None in events:
        return ["a part has no events to plan for"]
    expected = "${{ " + " || ".join(f"({event})" for event in events) + " }}"
    return [] if jobs[PLAN].get("if") == expected else ["the plan does not run on exactly the events of the parts"]


def holds(expression: str, github: dict) -> bool:
    """Whether an expression of `github` values, string literals, ==, &&, ||, parentheses and startsWith holds."""
    def value(path: str) -> object:
        current: object = github
        for step in path.split(".")[1:]:
            current = current.get(step) if isinstance(current, dict) else None
        return current

    code = re.sub(r"github(?:\.[\w-]+)+", lambda found: f"value({found[0]!r})", expression)
    code = code.replace("&&", " and ").replace("||", " or ").replace("startsWith(", "starts(")
    assert re.fullmatch(r"[\s\w()'=.,\[\]/-]*", code), expression
    return bool(eval(code, {"__builtins__": {}}, {"value": value, "starts": lambda text, prefix: str(text or "").startswith(prefix)}))


def workflow_run(*, event: str = "pull_request", conclusion: str = "failure", actor: str = "someone", head_branch: str = "feat/change",
                 head_repository: str | None = REPOSITORY) -> dict:
    return {"event_name": "workflow_run", "repository": REPOSITORY, "event": {"workflow_run": {
        "event": event, "conclusion": conclusion, "actor": {"login": actor}, "head_branch": head_branch,
        "head_repository": None if head_repository is None else {"full_name": head_repository}}}}


def dispatch(part: str | None) -> dict:
    return {"event_name": "workflow_dispatch", "repository": REPOSITORY, "event": {"inputs": {"part": part}}}


# Each event that a caller can start the workflow on, with the parts that have work on it.
EVENTS = (
    ("a passed run of a person's pull request", workflow_run(conclusion="success"), []),
    ("a failed run of a person's pull request", workflow_run(), ["additions"]),
    ("a failed run of a pull request from a fork", workflow_run(head_repository="someone-else/example-repository"), []),
    ("a failed run of a pull request from a deleted fork", workflow_run(head_repository=None), []),
    ("a cancelled run of a person's pull request", workflow_run(conclusion="cancelled"), []),
    ("a failed push run", workflow_run(event="push"), []),
    ("a passed run of a Dependabot pull request", workflow_run(conclusion="success", actor="dependabot[bot]", head_branch="dependabot/pip/x"), ["dependabot-auto-merge"]),
    ("a failed run of a Dependabot pull request", workflow_run(actor="dependabot[bot]", head_branch="dependabot/pip/x"), ["additions", "dependabot-auto-merge"]),
    ("a passed run on the App's commit to a Dependabot branch", workflow_run(conclusion="success", actor=APP, head_branch="dependabot/pip/x"), ["dependabot-auto-merge"]),
    ("a passed run on the App's commit to another branch", workflow_run(conclusion="success", actor=APP), []),
    ("a passed push run of Dependabot", workflow_run(event="push", conclusion="success", actor="dependabot[bot]", head_branch="dependabot/pip/x"), []),
    ("a fork's run under a Dependabot branch name", workflow_run(conclusion="success", head_branch="dependabot/pip/x"), []),
    ("the schedule", {"event_name": "schedule", "repository": REPOSITORY, "event": {}}, ["full-recording"]),
    ("a start by hand of the full recording", dispatch("full-recording"), ["full-recording"]),
    ("a start by hand of the additions", dispatch("additions"), ["additions"]),
    ("a start by hand without a part", dispatch(None), []),
    ("an opened issue", {"event_name": "issues", "repository": REPOSITORY, "event": {"action": "opened"}}, ["issue-title"]),
    ("an edited issue", {"event_name": "issues", "repository": REPOSITORY, "event": {"action": "edited"}}, []),
)


class AutomationWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text, self.workflow = read_workflow(AUTOMATION)
        self.jobs = self.workflow["jobs"]
        self.parts = automation_parts()

    def test_it_is_only_called_takes_no_input_and_declares_the_one_secret_it_is_passed_by_name(self) -> None:
        self.assertEqual({"workflow_call"}, workflow_triggers(self.text, str(AUTOMATION)))
        self.assertEqual({"secrets": {"PUBLICATION_CHECKS_APP_PRIVATE_KEY": {
            "description": "The private key of the updater App, with which the additions workflow and the full recording commit and propose",
            "required": "false"}}}, self.workflow["on"]["workflow_call"])
        self.assertEqual("{}", self.workflow["permissions"])
        self.assertNotIn("pull_request_target", self.text)
        self.assertEqual({"secrets.PUBLICATION_CHECKS_APP_PRIVATE_KEY"}, set(re.findall(r"secrets\.\w+", self.text)))

    def test_the_parts_are_those_whose_fragment_names_the_workflow(self) -> None:
        self.assertEqual(["additions", "dependabot-auto-merge", "full-recording", "issue-title"], self.parts)

    def test_the_jobs_are_the_plan_and_per_part_its_job_and_the_jobs_that_follow_it(self) -> None:
        self.assertEqual(PLAN, next(iter(self.jobs)))
        for name, job in self.jobs.items():
            if name == PLAN or name in self.parts:
                continue
            with self.subTest(job=name):
                owners = [part for part in self.parts if name.startswith(part + "-")]
                self.assertEqual(1, len(owners), "a job that is no part belongs to one part by its name")
                self.assertIn(owners[0], needed(job), "it needs its part's job, so that it is skipped with it")
                self.assertNotIn(PLAN, needed(job))
        self.assertEqual(set(self.parts), {name for name in self.jobs if name in self.parts}, "every part has its job")

    def test_only_the_jobs_of_the_full_recording_are_a_reusable_workflow_that_a_job_calls(self) -> None:
        self.assertEqual({"full-recording-jobs": "$/.github/workflows/suite-jobs.yml"}, {name: job["uses"] for name, job in self.jobs.items() if "uses" in job})

    def test_every_part_s_job_states_its_events_and_the_plan_s_decision_as_a_job_level_condition(self) -> None:
        for part in self.parts:
            with self.subTest(part=part):
                self.assertEqual([], part_job_problems(part, self.jobs[part]))

    def test_a_job_that_would_start_a_runner_without_work_is_named(self) -> None:
        events = "github.event_name == 'issues' && github.event.action == 'opened'"
        good = {"needs": "plan", "if": f"${{{{ ({events}) && {DECISION.format(part='invented-part')} }}}}"}
        self.assertEqual([], part_job_problems("invented-part", good))
        condition = "invented-part has no job-level condition of its events and the plan's decision for invented-part"
        for change, problem in (
                ({"if": None}, condition),
                ({"if": f"${{{{ {DECISION.format(part='invented-part')} }}}}"}, condition),
                ({"if": f"${{{{ ({events}) }}}}"}, condition),
                ({"if": f"${{{{ ({events}) && {DECISION.format(part='other-part')} }}}}"}, condition),
                ({"if": f"${{{{ (always()) && {DECISION.format(part='invented-part')} }}}}"}, "invented-part states no event in its job-level condition"),
                ({"if": f"${{{{ ({events} && needs.plan.result == 'success') && {DECISION.format(part='invented-part')} }}}}"},
                 "invented-part reads more than the event in its job-level condition"),
                ({"if": f"${{{{ ({events} && vars.SWITCH == 'on') && {DECISION.format(part='invented-part')} }}}}"},
                 "invented-part reads more than the event in its job-level condition"),
                ({"needs": "[plan, other]"}, "invented-part does not need plan alone")):
            with self.subTest(change=change):
                self.assertEqual([problem], part_job_problems("invented-part", {key: value for key, value in {**good, **change}.items() if value is not None}))

    def test_a_part_s_job_without_its_condition_fails_for_each_part_of_the_workflow(self) -> None:
        for part in self.parts:
            with self.subTest(part=part):
                without = {key: value for key, value in self.jobs[part].items() if key != "if"}
                self.assertTrue(part_job_problems(part, without))
                self.assertTrue(plan_problems({**self.jobs, part: without}, self.parts))

    def test_the_plan_runs_on_exactly_the_events_of_the_parts_with_read_rights_and_a_sparse_checkout(self) -> None:
        plan = self.jobs[PLAN]
        self.assertEqual([], plan_problems(self.jobs, self.parts))
        self.assertEqual(["the plan does not run on exactly the events of the parts"], plan_problems({**self.jobs, PLAN: {**plan, "if": "always()"}}, self.parts))
        self.assertEqual(["the plan does not run on exactly the events of the parts"],
                         plan_problems({**self.jobs, PLAN: {key: value for key, value in plan.items() if key != "if"}}, self.parts))
        self.assertNotIn("needs", plan)
        self.assertEqual({"contents": "read"}, plan["permissions"])
        self.assertEqual("ubuntu-slim", plan["runs-on"])
        self.assertEqual({"plan": "${{ steps.plan.outputs.plan }}"}, plan["outputs"])
        checkout = next(step for step in plan["steps"] if str(step.get("uses", "")).startswith("actions/checkout@"))
        self.assertEqual({"fetch-depth": "1", "persist-credentials": "false", "sparse-checkout": ".github/ci-config.yml", "sparse-checkout-cone-mode": "false"},
                         checkout["with"])
        self.assertNotIn("ref", checkout["with"], "the plan reads the configuration file of the event's own commit, the default branch's on a completed run")
        self.assertIn('plan.py" plan --root . --shared-workflow automation.yml ', plan["steps"][-1]["run"])

    def test_each_event_starts_only_the_parts_that_have_work_on_it_and_no_runner_without_one(self) -> None:
        conditions = {part: events_of(part, self.jobs[part]) for part in self.parts}
        plan = self.jobs[PLAN]["if"].removeprefix("${{ ").removesuffix(" }}")
        for name, github, working in EVENTS:
            with self.subTest(event=name):
                self.assertEqual(working, [part for part in self.parts if holds(conditions[part], github)])
                self.assertEqual(bool(working), holds(plan, github), "the plan starts a runner only when a part has work")

    def test_every_job_states_its_rights_and_no_checkout_keeps_its_credentials(self) -> None:
        for name, job in self.jobs.items():
            with self.subTest(job=name):
                self.assertIsInstance(job.get("permissions"), dict)
                for step in job.get("steps", []):
                    if str(step.get("uses", "")).startswith("actions/checkout@"):
                        self.assertEqual("false", step["with"]["persist-credentials"])
        external = re.findall(r"(?m)^\s*(?:- )?uses: ([^$\s]\S*)", self.text)
        self.assertTrue(external)
        for uses in external:
            self.assertRegex(uses, r"^actions/[\w-]+@[0-9a-f]{40}$", "every action is one of actions/* at a commit")
        self.assertEqual([], re.findall(r"uses: \./", self.text))
        self.assertNotIn("actions/cache", self.text)

    def test_the_rights_of_each_part(self) -> None:
        self.assertEqual({"contents": "write", "pull-requests": "write"}, self.jobs["dependabot-auto-merge"]["permissions"])
        self.assertEqual({"contents": "read", "issues": "write"}, self.jobs["issue-title"]["permissions"])
        self.assertEqual({"actions": "read", "contents": "read", "pull-requests": "read"}, self.jobs["additions"]["permissions"])
        self.assertEqual({"pull-requests": "write"}, self.jobs["additions-comment"]["permissions"])
        for name in ("full-recording", "full-recording-jobs", "full-recording-close"):
            self.assertEqual({"contents": "read"}, self.jobs[name]["permissions"], name)

    def test_each_part_reads_its_values_from_the_plan(self) -> None:
        for part, setting in (("dependabot-auto-merge", "suites"), ("issue-title", "categories"), ("additions", "suites"),
                              ("full-recording", "suites"), ("full-recording", "validation-workflow")):
            with self.subTest(part=part, setting=setting):
                self.assertIn(f"fromJSON(needs.plan.outputs.plan).parts['{part}'].config.{setting} }}}}", json.dumps(self.jobs[part]))
                fragment = json.loads((REPOSITORY_ROOT / FRAGMENTS / f"{part}.json").read_text(encoding="utf-8"))
                self.assertIn(setting, fragment["configuration"]["properties"])
        for path in ("tests/suites.json", ".github/suites.json", "validate.yml"):
            self.assertNotIn(path, self.text, "the workflow names no file of one repository")

    def test_the_issue_title_job_runs_the_program_of_its_own_revision_on_the_event_with_the_configured_category_file(self) -> None:
        steps = self.jobs["issue-title"]["steps"]
        self.assertEqual("$/actions/issue-title", steps[0]["uses"])
        run = steps[-1]
        self.assertEqual('PYTHONPYCACHEPREFIX="$RUNNER_TEMP/repo-pycache" python3 "$RESOURCE_PATH/issue_title.py" --event "$GITHUB_EVENT_PATH" --root . '
                         '--categories "$CATEGORIES" --apply', run["run"])
        self.assertEqual({"GITHUB_TOKEN": "${{ github.token }}", "RESOURCE_PATH": "${{ steps.resources.outputs.path }}",
                          "CATEGORIES": "${{ fromJSON(needs.plan.outputs.plan).parts['issue-title'].config.categories }}"}, run["env"])
        self.assertNotIn("github.event.issue", json.dumps(self.jobs["issue-title"]), "the title and the body reach the program only through the event file")

    def test_the_auto_merge_job_decides_for_the_head_branch_of_the_completed_run_and_checks_out_nothing(self) -> None:
        job = self.jobs["dependabot-auto-merge"]
        self.assertEqual(["$/actions/dependabot-auto-merge"], [step["uses"] for step in job["steps"] if "uses" in step])
        run = job["steps"][-1]
        self.assertTrue(run["run"].endswith('auto_merge.py" --repository "$GITHUB_REPOSITORY" --suites "$SUITES" --summary "$GITHUB_STEP_SUMMARY" '
                                            'workflow-run --head-branch "$HEAD_BRANCH" --disable-auto-merge'), run["run"])
        self.assertEqual("${{ github.event.workflow_run.head_branch }}", run["env"]["HEAD_BRANCH"])
        shared = read_workflow(WORKFLOWS / "dependabot-auto-merge.yml")[1]["jobs"]["merge"]
        self.assertEqual(shared["if"], events_of("dependabot-auto-merge", job), "the events of the merge job of dependabot-auto-merge.yml")

    def test_the_auto_merge_job_disables_a_still_enabled_auto_merge_since_no_pull_request_event_reaches_the_caller(self) -> None:
        self.assertEqual([], auto_merge_disable_problems(self.jobs["dependabot-auto-merge"], TEMPLATE.read_text(encoding="utf-8")))
        job = self.jobs["dependabot-auto-merge"]
        without_option = {**job, "steps": [{**step, "run": str(step.get("run", "")).replace(" --disable-auto-merge", "")} for step in job["steps"]]}
        self.assertEqual(["the job does not ask the program to disable an auto-merge that a pull request still has"],
                         auto_merge_disable_problems(without_option, TEMPLATE.read_text(encoding="utf-8")))
        reading = {**job, "permissions": {"contents": "write", "pull-requests": "read"}}
        self.assertEqual(["the job may not write pull requests, which disabling an auto-merge needs"],
                         auto_merge_disable_problems(reading, TEMPLATE.read_text(encoding="utf-8")))
        with_event = TEMPLATE.read_text(encoding="utf-8").replace("  workflow_dispatch:\n", "  pull_request:\n  workflow_dispatch:\n")
        self.assertEqual([], auto_merge_disable_problems(without_option, with_event), "a caller that a pull request event reaches may leave it to that event")


def auto_merge_disable_problems(job: dict, caller_text: str) -> list[str]:
    """Each way a still enabled auto-merge would survive a commit that Dependabot did not make: the caller starts on no
    pull request event, so the job of the completed run disables it, for which it may write pull requests."""
    if "pull_request" in workflow_triggers(caller_text, "the caller"):
        return []
    problems = []
    if not any(str(step.get("run", "")).endswith(" --disable-auto-merge") for step in job["steps"]):
        problems.append("the job does not ask the program to disable an auto-merge that a pull request still has")
    if (job.get("permissions") or {}).get("pull-requests") != "write":
        problems.append("the job may not write pull requests, which disabling an auto-merge needs")
    return problems


def additions_problems(jobs: dict) -> list[str]:
    """Each way the additions job could act for a branch of another repository, or hold a write token while it applies."""
    job = jobs["additions"]
    problems = []
    events = events_of("additions", job) or ""
    if "github.event.workflow_run.head_repository.full_name == github.repository" not in events:
        problems.append("the job-level condition does not require a branch of the same repository")
    steps = job["steps"]
    find = next((step for step in steps if step.get("id") == "run"), {})
    if 'if [ "$head_repository" != "$GITHUB_REPOSITORY" ]; then' not in str(find.get("run", "")):
        problems.append("the step that reads the run does not refuse a branch of another repository")
    if any(level == "write" for level in (job.get("permissions") or {}).values()):
        problems.append("the workflow token of the job may write")
    names = [str(step.get("id") or step.get("name") or step.get("uses")) for step in steps]
    tokens = [index for index, step in enumerate(steps) if str(step.get("uses", "")).split("@")[0] in WRITE_TOKEN_STEPS]
    apply = names.index("apply") if "apply" in names else None
    if apply is None or not tokens or min(tokens) < apply:
        problems.append("the additions are not applied before the App token exists")
    for index, step in enumerate(steps[:(min(tokens) if tokens else len(steps))]):
        text = json.dumps(step).replace("$GITHUB_STEP_SUMMARY", "")
        if "secrets." in text or "app-token" in text:
            problems.append(f"the step {names[index]} before the App token reads a secret or that token")
    if "secrets." in json.dumps(job.get("env", {})):
        problems.append("the job passes a secret to every step")
    return problems


class AdditionsPropertyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text, workflow = read_workflow(AUTOMATION)
        self.jobs = workflow["jobs"]

    def changed(self, old: str, new: str) -> dict:
        self.assertEqual(1, self.text.count(old), old)
        return ci_definition().read_yaml(self.text.replace(old, new), "a changed automation.yml")["jobs"]

    def test_it_acts_only_for_a_branch_of_the_same_repository_and_applies_before_any_write_token_exists(self) -> None:
        self.assertEqual([], additions_problems(self.jobs))

    def test_a_workflow_without_the_same_repository_condition_fails(self) -> None:
        jobs = self.changed(" && github.event.workflow_run.head_repository.full_name == github.repository)) && fromJSON(needs.plan.outputs.plan).parts['additions'].run",
                            ")) && fromJSON(needs.plan.outputs.plan).parts['additions'].run")
        self.assertEqual(["the job-level condition does not require a branch of the same repository"], additions_problems(jobs))

    def test_a_workflow_whose_read_of_the_run_accepts_a_fork_fails(self) -> None:
        jobs = self.changed('if [ "$head_repository" != "$GITHUB_REPOSITORY" ]; then', 'if [ -z "$head_repository" ]; then')
        self.assertEqual(["the step that reads the run does not refuse a branch of another repository"], additions_problems(jobs))

    def test_a_workflow_that_mints_the_token_before_it_applies_fails(self) -> None:
        steps = list(self.jobs["additions"]["steps"])
        token = next(step for step in steps if step.get("id") == "app-token")
        steps.remove(token)
        steps.insert(next(index for index, step in enumerate(steps) if step.get("id") == "apply"), token)
        self.assertEqual(["the additions are not applied before the App token exists"],
                         additions_problems({**self.jobs, "additions": {**self.jobs["additions"], "steps": steps}}))

    def test_a_workflow_that_gives_the_apply_step_the_key_or_a_write_token_fails(self) -> None:
        jobs = self.changed("          REPORT: ${{ runner.temp }}/apply-additions-report.md\n        run: |\n          set -euo pipefail\n          shopt -s globstar\n",
                            "          REPORT: ${{ runner.temp }}/apply-additions-report.md\n          KEY: ${{ secrets.PUBLICATION_CHECKS_APP_PRIVATE_KEY }}\n"
                            "        run: |\n          set -euo pipefail\n          shopt -s globstar\n")
        self.assertEqual(["the step apply before the App token reads a secret or that token"], additions_problems(jobs))
        writing = {**self.jobs, "additions": {**self.jobs["additions"], "permissions": {"actions": "read", "contents": "write", "pull-requests": "read"}}}
        self.assertEqual(["the workflow token of the job may write"], additions_problems(writing))


def reach_problems(caller_text: str, automation: dict, source: str) -> list[str]:
    """Each way a caller keeps a job of the automation workflow from doing its work: an event, a permission or a secret
    that the caller does not give."""
    caller = ci_definition().read_yaml(caller_text, source)
    triggers = workflow_triggers(caller_text, source)
    conditions = " ".join(str(job.get("if", "")) for job in automation["jobs"].values())
    problems = [f"the caller does not start on {event}" for event in sorted(set(re.findall(r"github\.event_name == '(\w+)'", conditions)) - triggers)]
    (job,) = caller["jobs"].values()
    granted = job.get("permissions") or {}
    for name, called in automation["jobs"].items():
        for scope, level in (called.get("permissions") or {}).items():
            if LEVELS[str(granted.get(scope, "none"))] < LEVELS[str(level)]:
                problems.append(f"the job {name} needs {scope}: {level}, which the caller does not grant")
    declared = set(automation["on"]["workflow_call"].get("secrets", {}))
    passed = set(job.get("secrets") or {}) if isinstance(job.get("secrets"), dict) else set()
    problems += [f"the caller does not pass the secret {secret}" for secret in sorted(declared - passed)]
    problems += [f"the caller passes the secret {secret}, which the workflow does not declare" for secret in sorted(passed - declared)]
    inputs = (caller["on"].get("workflow_dispatch") or {}).get("inputs") or {}
    problems += [f"the caller's start by hand has no input {name}" for name in sorted(set(re.findall(r"github\.event\.inputs\.([\w-]+)", json.dumps(automation))) - set(inputs))]
    if "issues" in triggers and caller["on"]["issues"] != {"types": "[opened]"}:
        problems.append("the caller starts on other issue events than opened")
    return problems


class CallerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.automation = read_workflow(AUTOMATION)[1]
        self.template = TEMPLATE.read_text(encoding="utf-8")
        self.own = OWN_CALLER.read_text(encoding="utf-8")

    def test_the_fixed_caller_gives_every_job_its_event_its_rights_its_secret_and_its_inputs(self) -> None:
        self.assertEqual([], reach_problems(self.template, self.automation, str(TEMPLATE)))

    def test_a_caller_that_withholds_an_event_a_right_a_secret_or_an_input_is_named(self) -> None:
        for old, new, problem in (
                ("  issues:\n    types: [opened]\n", "", "the caller does not start on issues"),
                ("    types: [opened]\n", "    types: [opened, edited]\n", "the caller starts on other issue events than opened"),
                ("      issues: write\n", "      issues: read\n", "the job issue-title needs issues: write, which the caller does not grant"),
                ("      actions: read\n", "", "the job additions needs actions: read, which the caller does not grant"),
                ("    secrets:\n      PUBLICATION_CHECKS_APP_PRIVATE_KEY: ${{ secrets.PUBLICATION_CHECKS_APP_PRIVATE_KEY }}\n", "",
                 "the caller does not pass the secret PUBLICATION_CHECKS_APP_PRIVATE_KEY"),
                ("      run-id:\n        description: For additions, the id of the run whose recording check reported additions\n        required: false\n        type: string\n", "",
                 "the caller's start by hand has no input run-id")):
            with self.subTest(problem=problem):
                self.assertEqual(1, self.template.count(old), old)
                self.assertEqual([problem], reach_problems(self.template.replace(old, new), self.automation, "a changed template"))

    def test_the_start_by_hand_names_the_parts_that_have_work_on_it(self) -> None:
        _, template = read_workflow(TEMPLATE)
        inputs = template["on"]["workflow_dispatch"]["inputs"]
        self.assertEqual(["part", "run-id", "verify-token"], list(inputs))
        self.assertEqual({"required": "true", "type": "choice", "options": "[full-recording, additions]"}, {key: inputs["part"][key] for key in ("required", "type", "options")})
        jobs = self.automation["jobs"]
        named = sorted(part for part in automation_parts() if "github.event_name == 'workflow_dispatch'" in (events_of(part, jobs[part]) or ""))
        self.assertEqual(sorted(flow_items(inputs["part"]["options"][1:-1])), named)
        for part in named:
            self.assertIn(f"github.event.inputs.part == '{part}'", events_of(part, jobs[part]))

    def test_this_repository_s_caller_equals_the_fixed_caller_apart_from_the_local_path_and_its_own_check_workflows(self) -> None:
        expected = self.template.replace("uses: CrankyAnt/SharedWorkflows/.github/workflows/automation.yml@v1", "uses: $/.github/workflows/automation.yml")
        expected = expected.replace("    workflows: [CI]\n", "    workflows: [CI, Repository hygiene, Python tests]\n")
        self.assertEqual(expected, self.own)
        self.assertNotIn("@v1", self.own)
        self.assertEqual([], reach_problems(self.own, self.automation, str(OWN_CALLER)))
        names = {read_workflow(path)[1]["name"] for path in (WORKFLOWS / "ci.yml", WORKFLOWS / "repository-hygiene-caller.yml", WORKFLOWS / "python-tests-caller.yml")}
        self.assertEqual({"CI", "Repository hygiene", "Python tests"}, names, "the workflows whose runs report a check of a pull request or its additions")

    def test_this_repository_switches_every_part_of_the_workflow_on(self) -> None:
        configuration = ci_definition().read_yaml((REPOSITORY_ROOT / ".github" / "ci-config.yml").read_text(encoding="utf-8"), "ci-config.yml")
        self.assertLessEqual(set(automation_parts()), set(configuration["parts"]))
        self.assertEqual({"suites": "tests/suites.json", "validation-workflow": ".github/workflows/ci.yml"}, configuration["parts"]["full-recording"])
        self.assertEqual({"suites": "tests/suites.json"}, configuration["parts"]["additions"])
        self.assertTrue((REPOSITORY_ROOT / configuration["parts"]["issue-title"]["categories"]).is_file())


if __name__ == "__main__":
    unittest.main()
