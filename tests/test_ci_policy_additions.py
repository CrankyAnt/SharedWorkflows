"""The additions job of the automation workflow beside the rebuild workflow of this repository's ETW consumer. The job
holds the condition under which the rebuild workflow acts, equal to the rebuild workflow's, and leaves such a run to the
rebuild; the rebuild workflow's comment job, which evaluates the condition again, holds it equal too. Its bound also
recognises the rebuild workflow's commit, so that the two automatic routes make at most one round per push. The run
tests of both routes are in `tests/test_automation_additions.py`."""

from __future__ import annotations

import re
import textwrap
import unittest

from tests.ci_policy_support import REPOSITORY_ROOT, ci_definition, read_workflow

WORKFLOWS = REPOSITORY_ROOT / ".github" / "workflows"
AUTOMATION = WORKFLOWS / "automation.yml"
OWN_CALLER = WORKFLOWS / "automation-caller.yml"
REBUILD = WORKFLOWS / "rebuild-etw-consumer.yml"
MAINTAINING = REPOSITORY_ROOT / "docs" / "maintaining.md"
JOB, COMMENT_JOB = "additions", "additions-comment"
ADDITIONS_SUBJECT = "chore: apply the recorded additions of run 4100"
REBUILD_SUBJECT = "chore: rebuild the ETW consumer"
BOUND = "bound='^(chore: apply the recorded additions of run [0-9]+|chore: rebuild the ETW consumer)$'"
# The rebuild workflow's condition, as its plan step holds it, and the additions job's early end on a run it acts on.
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
# The start by hand and the plan's decision around the automatic route in the additions job's `if`.
BY_HAND = "(github.event_name == 'workflow_dispatch' && github.event.inputs.part == 'additions')"
DECISION = "fromJSON(needs.plan.outputs.plan).parts['additions'].run"


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


def additions_run(text: str) -> str:
    """The script of the additions job's step that reads the run."""
    return step(ci_definition().read_yaml(text, str(AUTOMATION))["jobs"][JOB]["steps"], "run")["run"]


def automatic_filter(text: str) -> str:
    """The job filter of the additions job's automatic route: its `if` without the start by hand, the event's name, which
    the rebuild workflow's only trigger settles, and the plan's decision."""
    expression = ci_definition().read_yaml(text, str(AUTOMATION))["jobs"][JOB]["if"]
    found = re.fullmatch(r"\$\{\{ \(" + re.escape(BY_HAND) + r" \|\| \(github\.event_name == 'workflow_run' && (.+)\)\) && " + re.escape(DECISION) + r" \}\}", expression)
    assert found, expression
    return found[1]


def plan_filter(rebuild_text: str) -> str:
    """The `if` of the rebuild workflow's plan job, without its Dependabot clause, which the condition checks again."""
    expression = ci_definition().read_yaml(rebuild_text, str(REBUILD))["jobs"]["plan"]["if"]
    return expression.removesuffix(DEPENDABOT_CLAUSE)


class AdditionsBesideTheRebuildTest(unittest.TestCase):
    def setUp(self) -> None:
        self.text, self.workflow = read_workflow(AUTOMATION)
        self.rebuild_text = REBUILD.read_text(encoding="utf-8")
        self.steps = self.workflow["jobs"][JOB]["steps"]

    def test_the_bound_recognises_both_app_subjects_and_nothing_else(self) -> None:
        run = step(self.steps, "run")["run"]
        self.assertIn(BOUND, run)
        bound = re.search(r"bound='(.+)'", run)[1]
        _, rebuild = read_workflow(REBUILD)
        rebuild_commit = step(rebuild["jobs"]["push"]["steps"], "Commit the rebuild as the GitHub App")["run"]
        self.assertIn(f'commit -m "{REBUILD_SUBJECT}"', rebuild_commit)
        for subject in (ADDITIONS_SUBJECT, REBUILD_SUBJECT):
            self.assertRegex(subject, bound)
        for subject in (f"{REBUILD_SUBJECT} (#12)", "fix: rebuild the ETW consumer", f"{ADDITIONS_SUBJECT} (#12)"):
            self.assertNotRegex(subject, bound)

    def test_it_holds_the_rebuild_workflow_s_condition_and_ends_on_a_run_the_rebuild_acts_on_before_the_bound(self) -> None:
        run = step(self.steps, "run")["run"]
        self.assertEqual(rebuild_condition(self.rebuild_text), condition(run))
        automatic = run[run.index('if [ "$GITHUB_EVENT_NAME" = workflow_run ]; then'):]
        self.assertLess(automatic.index('echo "pull-request=$pull_request"'), automatic.index("# Begin: the condition"))
        self.assertLess(automatic.index(textwrap.indent(REBUILD_ACTS, "  ")), automatic.index("bound='"))

    def test_the_rebuild_plan_job_filters_runs_as_the_additions_job_s_automatic_route_does(self) -> None:
        # The plan job's `if` adds only the Dependabot prefix, which the condition checks again, so the additions job
        # leaves no run to the rebuild that the plan job skips.
        self.assertEqual(plan_filter(self.rebuild_text), automatic_filter(self.text))
        self.assertIn('if [[ "$head_branch" != dependabot/* ]]; then', condition(step(self.steps, "run")["run"]))
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
                self.assertNotEqual(rebuild_condition(theirs), condition(additions_run(ours)))

    def test_the_comment_names_the_start_by_hand_of_the_caller_that_called_the_workflow(self) -> None:
        (comment,) = self.workflow["jobs"][COMMENT_JOB]["steps"]
        self.assertIn('caller="${GITHUB_WORKFLOW_REF%@*}"\ncaller="${caller##*/}"\n', comment["run"])
        self.assertIn(r'\ngh workflow run %s -f part=additions -f run-id=%s\n%s\n\n' + "' \"$fence\" \"$caller\" \"$RUN_ID\" \"$fence\"", comment["run"])
        _, caller = read_workflow(OWN_CALLER)
        self.assertEqual({"part", "run-id", "verify-token"}, set(caller["on"]["workflow_dispatch"]["inputs"]))

    def test_the_maintenance_guide_and_the_rebuild_workflow_give_the_manual_route_of_this_repository_s_caller(self) -> None:
        route = f"gh workflow run {OWN_CALLER.name} -f part=additions -f run-id="
        section = re.search(r"(?ms)^#### Apply recorded additions\n(.*?)(?=^#{2,4} )", MAINTAINING.read_text(encoding="utf-8"))[1]
        self.assertIn(f"{route}<run-id>", section)
        self.assertIn(f"{route}%s", self.rebuild_text)


if __name__ == "__main__":
    unittest.main()
