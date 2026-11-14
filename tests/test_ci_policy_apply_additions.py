"""This repository's additions workflow: the additions template of the suite-selection consumer kit, run here over
`Validate` and `tests/suites.json`, kept equal to the template apart from the stated substitutions. It holds the
condition under which the rebuild workflow of the ETW consumer acts, equal to the rebuild workflow's, and leaves such a
run to the rebuild; the rebuild workflow's comment job, which evaluates the condition again, holds it equal too. Its
bound also recognises the rebuild workflow's commit, so that the two automatic routes make at most one round per push.
The run tests of both routes are in `tests/test_consumer_kit_apply_additions.py`."""

from __future__ import annotations

import re
import textwrap
import unittest

from tests.ci_policy_support import REPOSITORY_ROOT, ci_definition, read_workflow

TEMPLATE = REPOSITORY_ROOT / "actions" / "suite-selection" / "consumer-kit" / "apply-additions.yml"
WORKFLOWS = REPOSITORY_ROOT / ".github" / "workflows"
OWN_WORKFLOW = WORKFLOWS / "apply-additions.yml"
REBUILD = WORKFLOWS / "rebuild-etw-consumer.yml"
MAINTAINING = REPOSITORY_ROOT / "docs" / "maintaining.md"
ADDITIONS_SUBJECT = "chore: apply the recorded additions of run 4100"
REBUILD_SUBJECT = "chore: rebuild the ETW consumer"
TEMPLATE_BOUND = "bound='^chore: apply the recorded additions of run [0-9]+$'"
OWN_BOUND = "bound='^(chore: apply the recorded additions of run [0-9]+|chore: rebuild the ETW consumer)$'"
GITLEAKS_OPTIONS = "--config .gitleaks.toml --baseline-path .gitleaks-baseline.json"
# The stated substitutions, from this repository's text to the template's, applied to every string of the workflow.
SUBSTITUTIONS = (
    ("[Validate]", "[Validation]"),
    ("$/actions/recording", "CrankyAnt/SharedWorkflows/actions/recording@v1"),
    ("$/actions/repository-hygiene", "CrankyAnt/SharedWorkflows/actions/repository-hygiene@v1"),
    ("tests/suites.json", ".github/suites.json"),
    (GITLEAKS_OPTIONS, "<gitleaks-options>"),
    (OWN_BOUND, TEMPLATE_BOUND),
)
# How "Apply recorded additions" in docs/maintaining.md names each substitution, the pins and the rebuild workflow's
# condition included, in the order of its sentence.
STATED = ("`Validate` for the validation caller", "`tests/suites.json` for `.github/suites.json`",
          "the recording step and repository hygiene by local path", "the pins of `full-recording.yml` for `<pin>`",
          "this repository's Gitleaks options for `<gitleaks-options>`", "the bound with the rebuild subject added",
          "the condition of the rebuild workflow")
# The rebuild workflow's condition, as its plan step holds it, and the additions workflow's early end on a run it acts on.
CONDITION = re.compile(r"(?ms)^[ ]*# Begin: the condition under which the rebuild workflow acts on the run\b.*?^[ ]*# End: the condition[^\n]*\n")
REBUILD_ACTS = """\
if [ "$rebuild" = true ]; then
  echo "The rebuild workflow acts on run $RUN_ID, since $rebuild_reason; its rebuild commit carries the run's additions, so this workflow commits nothing and does not comment on #$pull_request." >> "$GITHUB_STEP_SUMMARY"
  echo "apply=false" >> "$GITHUB_OUTPUT"
  exit 0
fi
"""
# The clause that ends the plan job's `if`, a check that the condition makes again.
DEPENDABOT_CLAUSE = " && startsWith(github.event.workflow_run.head_branch, 'dependabot/')"
PIN = re.compile(r"^([\w.-]+/[\w./-]+)@[0-9a-f]{40}$")


def step(steps: list[dict], key: str) -> dict:
    return next(item for item in steps if key in (item.get("id"), item.get("name")))


def condition(run: str) -> str:
    """The rebuild workflow's condition in a step's script, without its indentation."""
    found = CONDITION.findall(run)
    assert len(found) == 1, f"the script holds the condition {len(found)} times"
    return textwrap.dedent(found[0])


def rebuild_condition(rebuild_text: str) -> str:
    """The condition in the plan step of the rebuild workflow."""
    rebuild = ci_definition().read_yaml(rebuild_text, str(REBUILD))
    return condition(step(rebuild["jobs"]["plan"]["steps"], "pull-request")["run"])


def comment_condition(rebuild_text: str) -> str:
    """The condition in the comment job of the rebuild workflow, which evaluates it again before it comments."""
    rebuild = ci_definition().read_yaml(rebuild_text, str(REBUILD))
    (comment,) = rebuild["jobs"]["comment"]["steps"]
    return condition(comment["run"])


def automatic_filter(own_text: str) -> str:
    """The job filter of the additions workflow's automatic route: its `if` without the start by hand."""
    expression = ci_definition().read_yaml(own_text, str(OWN_WORKFLOW))["jobs"]["apply"]["if"]
    found = re.fullmatch(r"github\.event_name == 'workflow_dispatch' \|\| \((.+)\)", expression)
    assert found, expression
    return found[1]


def plan_filter(rebuild_text: str) -> str:
    """The `if` of the rebuild workflow's plan job, without its Dependabot clause, which the condition checks again."""
    expression = ci_definition().read_yaml(rebuild_text, str(REBUILD))["jobs"]["plan"]["if"]
    return expression.removesuffix(DEPENDABOT_CLAUSE)


def substitutions(rebuild_text: str) -> tuple[tuple[str, str], ...]:
    """The stated substitutions and the last one: the rebuild workflow's condition and the early end after it, at the
    depth of the additions workflow's automatic route, which the template does not hold."""
    return (*SUBSTITUTIONS, (textwrap.indent(rebuild_condition(rebuild_text) + REBUILD_ACTS, "  "), ""))


def as_template(value: object, made: tuple[tuple[str, str], ...], key: str = "") -> object:
    """This repository's workflow document with the substitutions `made` and each commit pin of an action as the
    template's `<pin>`."""
    if isinstance(value, dict):
        return {name: as_template(item, made, name) for name, item in value.items()}
    if isinstance(value, list):
        return [as_template(item, made) for item in value]
    if isinstance(value, str):
        pinned = PIN.fullmatch(value) if key == "uses" else None
        if pinned:
            return f"{pinned[1]}@<pin>"
        for ours, theirs in made:
            value = value.replace(ours, theirs)
    return value


def template_differences(own_text: str, template_text: str, rebuild_text: str | None = None) -> list[str]:
    """Where this repository's workflow differs from the template beyond the stated substitutions, as document paths."""
    read = ci_definition().read_yaml
    differences: list[str] = []

    def compare(ours: object, theirs: object, path: str) -> None:
        if isinstance(ours, dict) and isinstance(theirs, dict):
            for name in [*ours, *(name for name in theirs if name not in ours)]:
                compare(ours.get(name), theirs.get(name), f"{path}.{name}")
            if [name for name in ours if name in theirs] != [name for name in theirs if name in ours]:
                differences.append(f"{path}: the order of the keys")
        elif isinstance(ours, list) and isinstance(theirs, list) and len(ours) == len(theirs):
            for index, (mine, other) in enumerate(zip(ours, theirs)):
                compare(mine, other, f"{path}[{index}]")
        elif ours != theirs:
            differences.append(path)

    made = substitutions(REBUILD.read_text(encoding="utf-8") if rebuild_text is None else rebuild_text)
    compare(as_template(read(own_text, str(OWN_WORKFLOW)), made), read(template_text, str(TEMPLATE)), "")
    return differences


class ApplyAdditionsWorkflowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.text, self.workflow = read_workflow(OWN_WORKFLOW)
        self.template = TEMPLATE.read_text(encoding="utf-8")
        self.rebuild_text = REBUILD.read_text(encoding="utf-8")

    def test_it_equals_the_template_apart_from_the_stated_substitutions(self) -> None:
        self.assertEqual([], template_differences(self.text, self.template))

    def test_a_difference_beyond_the_substitutions_fails_and_names_its_place(self) -> None:
        early_end = '              echo "apply=false" >> "$GITHUB_OUTPUT"\n              exit 0\n            fi\n            # At most one'
        for name, changed, place in (
                ("a changed condition", self.text.replace("steps.changes.outputs.changed == 'true' && github.event_name == 'workflow_run'",
                                                          "steps.changes.outputs.changed == 'true'", 1), ".jobs.apply.steps["),
                ("a changed comment", self.text.replace("applying them leaves the suite list unchanged.", "nothing changed.", 1),
                 ".jobs.comment.steps[0].run"),
                ("another bound", self.text.replace(OWN_BOUND, TEMPLATE_BOUND.replace("[0-9]+", "[0-9]*"), 1), ".jobs.apply.steps[0].run"),
                ("a dropped step", re.sub(r"(?ms)^      - name: Check the changed files\n.*?(?=^      - )", "", self.text, count=1), ".jobs.apply.steps"),
                ("another trigger type", self.text.replace("    types: [completed]\n", "    types: [completed, requested]\n", 1), ".on.workflow_run.types"),
                ("a version tag for a pin", re.sub(r"actions/checkout@[0-9a-f]{40}", "actions/checkout@v7", self.text, count=1), ".jobs.apply.steps[1].uses"),
                ("another suite list", self.text.replace("tests/suites.json", "tests/other.json", 1), ".jobs.apply.steps["),
                ("another early end on a run that the rebuild acts on",
                 self.text.replace("so this workflow commits nothing and does not comment", "so this workflow comments", 1), ".jobs.apply.steps[0].run"),
                ("the condition without its early end", self.text.replace(early_end, "            fi\n            # At most one", 1),
                 ".jobs.apply.steps[0].run")):
            with self.subTest(change=name):
                self.assertNotEqual(self.text, changed, "the mutation applies")
                differences = template_differences(changed, self.template)
                self.assertTrue(differences, "the comparison fails")
                self.assertTrue(any(difference.startswith(place) for difference in differences), differences)

    def test_it_runs_over_validate_and_this_repository_s_suite_list_with_the_shared_steps_by_local_path(self) -> None:
        self.assertEqual(f"[{read_workflow(WORKFLOWS / 'validate.yml')[1]['name']}]", self.workflow["on"]["workflow_run"]["workflows"])
        self.assertRegex(self.text, r"(?m)^  workflow_run: # zizmor: ignore\[dangerous-triggers\] \S")
        steps = self.workflow["jobs"]["apply"]["steps"]
        self.assertEqual("$/actions/recording", step(steps, "recording")["uses"])
        self.assertEqual("$/actions/repository-hygiene", step(steps, "hygiene")["uses"])
        self.assertIn('"$RECORDING/recording.py" apply --suites tests/suites.json', step(steps, "apply")["run"])
        self.assertIn(f"gitleaks\" git {GITLEAKS_OPTIONS} --log-opts=", step(steps, "Scan the commit before pushing")["run"])
        for absent in ("CrankyAnt/SharedWorkflows/", "<pin>", "<gitleaks-options>", ".github/suites.json"):
            with self.subTest(absent=absent):
                self.assertNotIn(absent, self.text)
        self.assertEqual(self.text.count("uses: actions/"), len(re.findall(r"uses: actions/[\w-]+@[0-9a-f]{40} # v\d+(?:\.\d+)*\n", self.text)))

    def test_the_bound_recognises_both_app_subjects_and_nothing_else(self) -> None:
        bound = re.search(r"bound='(.+)'", step(self.workflow["jobs"]["apply"]["steps"], "run")["run"])[1]
        _, rebuild = read_workflow(REBUILD)
        rebuild_commit = step(rebuild["jobs"]["push"]["steps"], "Commit the rebuild as the GitHub App")["run"]
        self.assertIn(f'commit -m "{REBUILD_SUBJECT}"', rebuild_commit)
        for subject in (ADDITIONS_SUBJECT, REBUILD_SUBJECT):
            self.assertRegex(subject, bound)
        for subject in (f"{REBUILD_SUBJECT} (#12)", "fix: rebuild the ETW consumer", f"{ADDITIONS_SUBJECT} (#12)"):
            self.assertNotRegex(subject, bound)

    def test_it_holds_the_rebuild_workflow_s_condition_and_ends_on_a_run_the_rebuild_acts_on_before_the_bound(self) -> None:
        run = step(self.workflow["jobs"]["apply"]["steps"], "run")["run"]
        self.assertEqual(rebuild_condition(self.rebuild_text), condition(run))
        automatic = run[run.index('if [ "$GITHUB_EVENT_NAME" = workflow_run ]; then'):]
        self.assertLess(automatic.index('echo "pull-request=$pull_request"'), automatic.index("# Begin: the condition"))
        self.assertLess(automatic.index(textwrap.indent(REBUILD_ACTS, "  ")), automatic.index("bound='"))

    def test_the_rebuild_plan_job_filters_runs_as_the_additions_job_s_automatic_route_does(self) -> None:
        # The plan job's `if` adds only the Dependabot prefix, which the condition checks again, so the additions workflow
        # leaves no run to the rebuild that the plan job skips.
        self.assertEqual(plan_filter(self.rebuild_text), automatic_filter(self.text))
        run = step(self.workflow["jobs"]["apply"]["steps"], "run")["run"]
        self.assertIn('if [[ "$head_branch" != dependabot/* ]]; then', condition(run))
        for name, changed in (("an actor check", self.rebuild_text.replace(" && startsWith(", " && github.event.workflow_run.actor.login == 'dependabot[bot]' && startsWith(", 1)),
                              ("another prefix", self.rebuild_text.replace("startsWith(github.event.workflow_run.head_branch, 'dependabot/')",
                                                                           "startsWith(github.event.workflow_run.head_branch, 'dependabot/nuget/')", 1))):
            with self.subTest(change=name):
                self.assertNotEqual(self.rebuild_text, changed, "the mutation applies")
                self.assertNotEqual(plan_filter(changed), automatic_filter(self.text))

    def test_the_rebuild_workflow_s_comment_job_holds_the_same_condition(self) -> None:
        self.assertEqual(rebuild_condition(self.rebuild_text), comment_condition(self.rebuild_text))
        # Another branch prefix in the plan job only.
        plan_part, comment_part = self.rebuild_text.split("  comment:\n")
        mutated = plan_part.replace('[[ "$head_branch" != dependabot/* ]]', '[[ "$head_branch" != dependabot/nuget/* ]]') + "  comment:\n" + comment_part
        self.assertNotEqual(self.rebuild_text, mutated, "the mutation applies")
        self.assertNotEqual(rebuild_condition(mutated), comment_condition(mutated), "a plan job that differs from the comment job fails")

    def test_a_condition_that_differs_from_the_rebuild_workflow_s_fails(self) -> None:
        for name, ours, theirs in (
                ("another branch prefix", self.text.replace("!= dependabot/* ]]", "!= dependabot/github_actions/* ]]", 1), self.rebuild_text),
                ("another author", self.text.replace('.user.login == \\"dependabot[bot]\\"', '.user.login == \\"renovate[bot]\\"', 1), self.rebuild_text),
                ("the build folder counted as source", self.text, self.rebuild_text.replace(r" && !/^actions\/recording\/etw-consumer\/build\//", "", 1)),
                ("another rebuild subject", self.text, self.rebuild_text.replace('[ "$subject" = "chore: rebuild the ETW consumer" ]',
                                                                                '[ "$subject" = "chore: rebuild the consumer" ]', 1))):
            with self.subTest(change=name):
                self.assertNotEqual((self.text, self.rebuild_text), (ours, theirs), "the mutation applies")
                run = step(ci_definition().read_yaml(ours, str(OWN_WORKFLOW))["jobs"]["apply"]["steps"], "run")["run"]
                self.assertNotEqual(rebuild_condition(theirs), condition(run))
                self.assertIn(".jobs.apply.steps[0].run", template_differences(ours, self.template, theirs))

    def test_the_maintenance_guide_states_each_substitution(self) -> None:
        section = re.search(r"(?ms)^#### Apply recorded additions\n(.*?)(?=^#{2,4} )", MAINTAINING.read_text(encoding="utf-8"))[1]
        sentence = re.search(r"`ci-policy` fails while the workflow differs from the kit template `apply-additions.yml` "
                             r"beyond these substitutions: ([^\n]+)", section)[1]
        for phrase in STATED:
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, sentence)
        self.assertEqual(sorted(STATED, key=sentence.index), list(STATED))

    def test_the_maintenance_guide_gives_the_manual_route_that_the_comment_names(self) -> None:
        (comment,) = self.workflow["jobs"]["comment"]["steps"]
        command = re.search(r"\\n(gh workflow run (\S+) -f run-id=)%s\\n", comment["run"])
        self.assertEqual(OWN_WORKFLOW.name, command[2])
        section = re.search(r"(?ms)^#### Apply recorded additions\n(.*?)(?=^#{2,4} )", MAINTAINING.read_text(encoding="utf-8"))[1]
        self.assertIn(f"{command[1]}<run-id>", section)


if __name__ == "__main__":
    unittest.main()
