"""The additions job of the shared automation workflow contains no ETW rebuild logic, and this repository rebuilds the
ETW consumer from one route (`rebuild-etw-consumer.yml`) whose plan and comment jobs share the rebuild condition."""

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
BOUND = "bound='^chore: apply the recorded additions of run [0-9]+$'"
CONDITION = re.compile(r"(?ms)^[ ]*# Begin: the condition under which the rebuild workflow acts on the run\b.*?^[ ]*# End: the condition[^\n]*\n")


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


class AdditionsAndRebuildBoundaryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.text, self.workflow = read_workflow(AUTOMATION)
        self.rebuild_text = REBUILD.read_text(encoding="utf-8")
        self.steps = self.workflow["jobs"][JOB]["steps"]

    def test_the_shared_automation_workflow_contains_no_etw_rebuild_logic(self) -> None:
        lowered = self.text.lower()
        for forbidden in ("etw-consumer", "rebuild the etw consumer", "rebuild_reason", "rebuild_pull_request"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, lowered)

    def test_the_bound_recognises_only_the_additions_commit_subject(self) -> None:
        run = step(self.steps, "run")["run"]
        self.assertIn(BOUND, run)
        bound = re.search(r"bound='(.+)'", run)[1]
        self.assertRegex(ADDITIONS_SUBJECT, bound)
        for subject in (REBUILD_SUBJECT, f"{ADDITIONS_SUBJECT} (#12)", "fix: apply the recorded additions of run 4100"):
            self.assertNotRegex(subject, bound)

    def test_this_repository_rebuilds_the_etw_consumer_from_one_route_whose_plan_and_comment_share_the_condition(self) -> None:
        for workflow in sorted(WORKFLOWS.glob("*.yml")):
            if workflow != REBUILD:
                with self.subTest(workflow=workflow.name):
                    self.assertNotIn("etw-consumer", workflow.read_text(encoding="utf-8"))
        self.assertEqual(rebuild_condition(self.rebuild_text), comment_condition(self.rebuild_text))
        plan_part, comment_part = self.rebuild_text.split("  comment:\n")
        mutated = plan_part.replace('[[ "$head_branch" != dependabot/* ]]', '[[ "$head_branch" != dependabot/nuget/* ]]') + "  comment:\n" + comment_part
        self.assertNotEqual(self.rebuild_text, mutated, "the mutation applies")
        self.assertNotEqual(rebuild_condition(mutated), comment_condition(mutated), "a plan job that differs from the comment job fails")

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
        self.assertNotIn("leaves the recorded additions", self.rebuild_text)


if __name__ == "__main__":
    unittest.main()
