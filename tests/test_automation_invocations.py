"""The automation workflow's invocations of the plan program, the auto-merge program and the issue-title program, parsed
with the programs' own parsers, and the plan that the workflow's command gives for this repository's configuration file."""

from __future__ import annotations

import importlib.util
import json
import sys
import unittest

from tests.dependabot_auto_merge_source import auto_merge
from tests.pipeline_fixtures import CONFIGURATION, REPOSITORY_ROOT, PipelineFixture, plan_program
from tests.workflow_commands import script_invocations

WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "automation.yml"
PARTS = ["additions", "dependabot-auto-merge", "full-recording", "issue-title"]


def issue_title_program():
    spec = importlib.util.spec_from_file_location("issue_title", REPOSITORY_ROOT / "actions" / "issue-title" / "issue_title.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class InvocationTests(unittest.TestCase):
    def test_the_plan_decides_on_the_parts_of_the_automation_workflow_with_options_that_the_program_accepts(self) -> None:
        (tokens,) = script_invocations(WORKFLOW, "plan.py")
        options = plan_program.parser().parse_args(tokens)
        self.assertEqual(("plan", "automation.yml", "."), (options.command, options.shared_workflow, options.root))
        self.assertEqual(str(plan_program.SCHEMAS), options.schemas, "the workflow reads the schema of its own revision")
        self.assertEqual((None, None), (options.base, options.head), "the plan compares nothing: no part of the workflow names paths")
        for name in ("workflow", "event", "github_output", "summary"):
            self.assertEqual("x", getattr(options, name), name)

    def test_the_auto_merge_job_starts_the_merge_decision_with_options_that_the_program_accepts(self) -> None:
        (tokens,) = script_invocations(WORKFLOW, "auto_merge.py")
        options = auto_merge.build_parser().parse_args(tokens)
        self.assertEqual(("workflow-run", "x", "x", "x"), (options.event, options.head_branch, options.repository, options.suites))
        self.assertIsNotNone(options.summary)

    def test_the_issue_title_job_starts_the_program_with_options_that_it_accepts(self) -> None:
        (tokens,) = script_invocations(WORKFLOW, "issue_title.py")
        options = issue_title_program().parser().parse_args(tokens)
        self.assertEqual(("x", ".", "x", True), (options.event, options.root, options.categories, options.apply))


class PlanOfTheWorkflowTests(PipelineFixture):
    def setUp(self) -> None:
        super().setUp()
        self.schemas = plan_program.SCHEMAS

    def automation(self) -> dict:
        outcome = self.plan("--shared-workflow", "automation.yml")
        self.assertEqual(0, outcome.status, outcome.summary)
        return outcome.plan["parts"]

    def test_a_repository_that_states_no_part_of_the_workflow_runs_none(self) -> None:
        self.commit({CONFIGURATION: "version: 1\n"})
        self.assertEqual({part: {"run": False, "reason": "switched off", "config": {}} for part in PARTS}, self.automation())

    def test_each_switched_on_part_runs_with_its_values_and_the_pipeline_s_plan_leaves_them_out(self) -> None:
        self.commit({CONFIGURATION: """\
            version: 1
            parts:
              dependabot-auto-merge: {}
              issue-title:
                categories: intake/categories.json
              additions:
                suites: ci/suites.json
              full-recording:
                suites: ci/suites.json
                validation-workflow: .github/workflows/validation.yml
            """})
        parts = self.automation()
        self.assertEqual({part: True for part in PARTS}, {part: decision["run"] for part, decision in parts.items()})
        self.assertEqual({"additions": {"suites": "ci/suites.json"}, "dependabot-auto-merge": {}, "issue-title": {"categories": "intake/categories.json"},
                          "full-recording": {"suites": "ci/suites.json", "validation-workflow": ".github/workflows/validation.yml"}},
                         {part: decision["config"] for part, decision in parts.items()})
        pipeline = self.plan()
        self.assertEqual(0, pipeline.status, pipeline.summary)
        self.assertEqual([], [part for part in PARTS if part in pipeline.plan["parts"]])

    def test_a_part_without_its_required_value_or_with_a_path_outside_the_repository_fails_the_plan(self) -> None:
        for configuration, message in (
                ("version: 1\nparts:\n  issue-title: {}\n", "parts.issue-title.categories: is required"),
                ("version: 1\nparts:\n  additions: {}\n", "parts.additions.suites: is required"),
                ("version: 1\nparts:\n  full-recording:\n    suites: ci/suites.json\n", "parts.full-recording.validation-workflow: is required"),
                ("version: 1\nparts:\n  additions:\n    suites: ../suites.json\n", "parts.additions.suites: does not match the pattern"),
                ("version: 1\nparts:\n  issue-title:\n    categories: /etc/categories.json\n", "parts.issue-title.categories: does not match the pattern"),
                ("version: 1\nparts:\n  full-recording:\n    suites: ci/suites.json\n    validation-workflow: ci/validation.yml\n",
                 "parts.full-recording.validation-workflow: does not match the pattern")):
            with self.subTest(configuration=configuration):
                self.commit({CONFIGURATION: configuration})
                outcome = self.plan("--shared-workflow", "automation.yml")
                self.assertEqual(1, outcome.status)
                self.assertIn(message, outcome.summary)

    def test_this_repository_s_configuration_file_switches_every_part_of_the_workflow_on(self) -> None:
        self.repository = REPOSITORY_ROOT
        parts = self.automation()
        self.assertEqual(PARTS, [part for part, decision in parts.items() if decision["run"]])
        self.assertEqual("tests/suites.json", parts["full-recording"]["config"]["suites"])
        self.assertEqual(json.loads(json.dumps(parts["additions"]["config"])), {"suites": "tests/suites.json"})


if __name__ == "__main__":
    unittest.main()
