"""The automation workflow's invocations of the plan program, the auto-merge program and the issue-title program, parsed
with the programs' own parsers, and the plan that the workflow's command gives for this repository's configuration file."""

from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path

from tests import temporary_support
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
        self.assertEqual(("x", "x", "x", True), (options.event, options.ref, options.categories, options.apply))


class CategoryFileDescriptionTests(unittest.TestCase):
    """The fragment of ``issue-title`` describes the category file, so that an agent in a consumer can tell from the
    schema alone whether the part's input suffices."""

    EXAMPLE = "Invented example of the category file: "

    def setUp(self) -> None:
        self.description = json.loads((plan_program.SCHEMAS / "v1" / "parts" / "issue-title.json").read_text(encoding="utf-8"))["description"]
        self.program = issue_title_program()

    def example(self) -> dict:
        self.assertEqual(1, self.description.count(self.EXAMPLE))
        text = self.description.split(self.EXAMPLE, 1)[1]
        self.assertTrue(text.endswith("}."), "the example closes the description")
        return json.loads(text[:-1])

    def load(self, document: dict):
        with temporary_support.Directory() as directory:
            path = Path(directory) / "categories.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            return self.program.load_categories(path)

    def test_the_program_reads_the_example_of_the_description(self) -> None:
        component, generic = self.load(self.example())
        self.assertEqual(("tool", True, "tools", "TOOL.md", "[{component}]", "Tool", ("name",), ("category:tool",)),
                         (component.key, component.has_components, component.source_directory, component.marker, component.dynamic_title_prefix,
                          component.component_label, component.required_metadata, component.labels))
        self.assertEqual(("question", False, "[question]", ("category:question",)), (generic.key, generic.has_components, generic.title_prefix, generic.labels))

    def test_the_description_names_every_key_of_an_item_and_the_program_requires_each(self) -> None:
        example = self.example()
        for name, item in (("product_categories", example["product_categories"][0]), ("generic_forms", example["generic_forms"][0])):
            self.assertIn(f"`{name}`", self.description)
            for key in item:
                with self.subTest(list=name, key=key):
                    self.assertIn(f"`{key}`", self.description)
                    without = {**example, name: [{other: value for other, value in item.items() if other != key}]}
                    with self.assertRaises(self.program.CategoryError):
                        self.load(without)

    def test_the_description_states_the_forms_that_the_program_accepts_and_refuses(self) -> None:
        example = self.example()
        generic = example["generic_forms"][0]
        for text in ("a missing one counts as empty", "together they hold at least one item", "unique across both lists", "lists of non-empty strings and may be empty",
                     "a key that is not named here is ignored", "with `{component}` for the component's name"):
            with self.subTest(text=text):
                self.assertIn(text, self.description)
        self.assertEqual(1, len(self.load({"generic_forms": [{**generic, "labels": [], "form": "question.yml"}]})), "an empty list of labels and another key")
        for name, document in (("no item", {"product_categories": [], "generic_forms": []}),
                               ("a key that two categories share", {**example, "generic_forms": [{**generic, "key": "tool"}]}),
                               ("an empty string", {"generic_forms": [{**generic, "title_prefix": " "}]}),
                               ("labels that are no list", {"generic_forms": [{**generic, "labels": "category:question"}]}),
                               ("a prefix without the component", {**example, "product_categories": [{**example["product_categories"][0], "dynamic_title_prefix": "[tool]"}]})):
            with self.subTest(refused=name), self.assertRaises(self.program.CategoryError):
                self.load(document)


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

    def test_a_scheduled_full_recording_runs_only_in_even_iso_weeks_and_a_start_by_hand_runs_in_any_week(self) -> None:
        self.commit({CONFIGURATION: """\
            version: 1
            parts:
              full-recording:
                suites: ci/suites.json
                validation-workflow: .github/workflows/ci.yml
            """})
        odd = self.plan("--shared-workflow", "automation.yml", "--event", "schedule", "--iso-week", "15")
        self.assertEqual(0, odd.status, odd.summary)
        self.assertEqual((False, "odd ISO week"), (odd.plan["parts"]["full-recording"]["run"], odd.plan["parts"]["full-recording"]["reason"]))
        even = self.plan("--shared-workflow", "automation.yml", "--event", "schedule", "--iso-week", "16")
        self.assertEqual(0, even.status, even.summary)
        self.assertTrue(even.plan["parts"]["full-recording"]["run"])
        manual = self.plan("--shared-workflow", "automation.yml", "--event", "workflow_dispatch", "--iso-week", "15")
        self.assertEqual(0, manual.status, manual.summary)
        self.assertTrue(manual.plan["parts"]["full-recording"]["run"])


if __name__ == "__main__":
    unittest.main()
