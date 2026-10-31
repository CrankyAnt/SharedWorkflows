"""The validation caller template and the other templates of the consumer kit of the suite selection."""

from __future__ import annotations

import re
import unittest
from pathlib import Path
from tests.ci_policy_support import verdict_runs
from tests.setup_ci_support import assert_caller_passes_no_secrets_and_runs_no_step, read_yaml, read_yaml_text

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SELECTION_KIT = REPOSITORY_ROOT / "actions" / "suite-selection" / "consumer-kit"
KIT_TEMPLATES = ("validation.yml", "validation-workflow.yml", "full-recording.yml", "apply-additions.yml")
# What only this repository has: its suite list and tests, its own validation workflow, a local-path call of a shared
# workflow or step, and a resolved action pin in place of `<pin>`.
THIS_REPOSITORY_ONLY = re.compile(r"tests/|validate\.yml|\$/actions/|\$/\.github/workflows/(?:suite-selection|suite-verdict|repository-hygiene)\.yml|@[0-9a-f]{40}")


class SelectionConsumerKitTemplatesTest(unittest.TestCase):
    def test_the_kit_holds_its_four_templates(self) -> None:
        for template in KIT_TEMPLATES:
            with self.subTest(template=template):
                self.assertTrue((SELECTION_KIT / template).is_file())

    def test_the_kit_templates_carry_nothing_that_exists_only_in_this_repository(self) -> None:
        for template in KIT_TEMPLATES:
            with self.subTest(template=template):
                self.assertEqual([], THIS_REPOSITORY_ONLY.findall((SELECTION_KIT / template).read_text(encoding="utf-8")))


class ValidationCallerTemplateTest(unittest.TestCase):
    """The caller of the suite selection: the selection job, one job per validation workflow and the verdict job."""

    def setUp(self) -> None:
        self.text, self.caller = read_yaml(SELECTION_KIT / "validation.yml")
        self.jobs = self.caller["jobs"]

    def test_runs_on_pull_request_on_push_to_the_default_branch_and_on_a_dependabot_merge_without_a_path_filter(self) -> None:
        self.assertEqual({"pull_request": None, "push": {"branches": ["<default-branch>"]}, "repository_dispatch": {"types": ["dependabot-merged"]}}, self.caller["on"])
        self.assertNotIn("paths:", self.text)
        self.assertNotIn("paths-ignore:", self.text)

    def test_select_and_validation_call_the_shared_workflows_at_v1_with_read_rights_only(self) -> None:
        self.assertEqual({}, self.caller["permissions"])
        self.assertEqual("CrankyAnt/SharedWorkflows/.github/workflows/suite-selection.yml@v1", self.jobs["select"]["uses"])
        self.assertEqual({"suites": ".github/suites.json", "language-steps": "python", "runner-labels": "<runner-labels>",
                          "setup-versions": "<setup-versions>"}, self.jobs["select"]["with"])
        self.assertEqual("CrankyAnt/SharedWorkflows/.github/workflows/suite-verdict.yml@v1", self.jobs["validation"]["uses"])
        self.assertEqual({"needs": "${{ toJSON(needs) }}"}, self.jobs["validation"]["with"])
        self.assertEqual({"actions": "read", "contents": "read", "pull-requests": "read"}, self.jobs["select"]["permissions"])
        for name, job in self.jobs.items():
            if name != "select":
                with self.subTest(job=name):
                    self.assertEqual({"contents": "read"}, job["permissions"])
        assert_caller_passes_no_secrets_and_runs_no_step(self, self.text)

    def test_a_pull_request_run_is_cancelled_by_a_newer_head_and_push_and_dispatch_runs_never(self) -> None:
        self.assertEqual({
            "group": "${{ github.workflow }}-${{ github.event_name == 'pull_request' && github.event.pull_request.number || github.run_id }}",
            "cancel-in-progress": "${{ github.event_name == 'pull_request' }}",
        }, self.caller["concurrency"])

    def test_validation_needs_every_other_job_and_runs_under_always_but_on_a_push_of_a_tested_tree(self) -> None:
        self.assertEqual(sorted(name for name in self.jobs if name != "validation"), sorted(self.jobs["validation"]["needs"]))
        condition = self.jobs["validation"]["if"]
        self.assertFalse(verdict_runs(condition, "success", "true"), "a passed select of a tested tree skips the verdict")
        for result, identical in (("success", ""), ("failure", "true"), ("failure", ""), ("cancelled", "true"), ("skipped", "")):
            with self.subTest(select=result, identical=identical):
                self.assertTrue(verdict_runs(condition, result, identical))
        self.assertEqual(list(self.jobs)[0], "select")
        self.assertEqual(list(self.jobs)[-1], "validation")

    def test_each_validation_job_needs_select_and_passes_the_selection_to_a_local_workflow(self) -> None:
        validation_jobs = {name: job for name, job in self.jobs.items() if name not in ("select", "validation")}
        self.assertTrue(validation_jobs)
        for name, job in validation_jobs.items():
            with self.subTest(job=name):
                self.assertEqual("select", job["needs"])
                self.assertTrue(job["uses"].startswith("$/.github/workflows/"), job["uses"])
                self.assertEqual({"suites": "${{ needs.select.outputs.suites }}", "entries": "${{ needs.select.outputs.entries }}",
                                  "language-steps": "${{ needs.select.outputs.language-steps }}",
                                  "shared-digest": "${{ needs.select.outputs.shared-digest }}"}, job["with"])
                self.assertNotIn("if", job, "the validation workflow gates its own suite jobs")

    def test_each_validation_job_id_is_its_workflow_s_file_name_without_the_extension(self) -> None:
        """The job id rule: the check of a validation workflow is `<file name> / <job>`."""
        validation_jobs = {name: job for name, job in self.jobs.items() if name not in ("select", "validation")}
        self.assertTrue(validation_jobs)
        for name, job in validation_jobs.items():
            with self.subTest(job=name):
                self.assertEqual(f"$/.github/workflows/{name}.yml", job["uses"])
        self.assertNotIn("<job>", self.text, "the one placeholder of a validation job is the workflow's file name")
        filled = self.text.replace("<validation-workflow>", "tests")
        _, caller = read_yaml_text(filled)
        self.assertEqual("$/.github/workflows/tests.yml", caller["jobs"]["tests"]["uses"])
        self.assertEqual(["select", "tests"], caller["jobs"]["validation"]["needs"])


if __name__ == "__main__":
    unittest.main()
