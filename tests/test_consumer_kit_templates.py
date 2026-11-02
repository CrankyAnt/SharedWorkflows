"""The validation caller template and the other templates of the consumer kit of the suite selection, and the
section of every consumer kit for a repository that adopted its part earlier."""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path
from tests.ci_policy_support import V1_MOVED_CONTEXT, expression_value, read_workflow, verdict_runs
from tests.consumer_kit_support import assert_caller_passes_no_secrets_and_runs_no_step, read_yaml, read_yaml_text

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SELECTION_KIT = REPOSITORY_ROOT / "actions" / "suite-selection" / "consumer-kit"
KIT_TEMPLATES = ("validation.yml", "validation-workflow.yml", "full-recording.yml", "apply-additions.yml")
# What only this repository has: its suite list and tests, its own validation workflow, a local-path call of a shared
# workflow or step, and a resolved action pin in place of `<pin>`.
THIS_REPOSITORY_ONLY = re.compile(r"tests/|validate\.yml|\$/actions/|\$/\.github/workflows/(?:suite-selection|suite-verdict|repository-hygiene)\.yml|@[0-9a-f]{40}")
EARLIER_ADOPTION_HEADING = "## When a repository adopted it earlier"


def readme_section(readme: str, heading: str) -> str:
    """The section under ``heading`` up to the next level-two heading, or to the end of the README."""
    start = readme.index(heading + "\n")
    end = readme.find("\n## ", start + 1)
    return readme[start:] if end == -1 else readme[start:end]


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

    def test_runs_on_pull_request_on_push_to_the_default_branch_on_a_dependabot_merge_and_on_a_v1_move_without_a_path_filter(self) -> None:
        self.assertEqual({"pull_request": None, "push": {"branches": ["<default-branch>"]},
                          "repository_dispatch": {"types": ["dependabot-merged", "v1-moved"]}}, self.caller["on"])
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

    def test_the_one_validation_job_needs_select_and_passes_the_shards_and_the_windows_job_to_a_local_workflow(self) -> None:
        validation_jobs = {name: job for name, job in self.jobs.items() if name not in ("select", "validation")}
        self.assertEqual(["<validation-workflow>"], list(validation_jobs))
        for name, job in validation_jobs.items():
            with self.subTest(job=name):
                self.assertEqual("select", job["needs"])
                self.assertTrue(job["uses"].startswith("$/.github/workflows/"), job["uses"])
                self.assertEqual({"shards": "${{ needs.select.outputs.shards }}", "windows": "${{ needs.select.outputs.windows }}",
                                  "language-steps": "${{ needs.select.outputs.language-steps }}",
                                  "shared-digest": "${{ needs.select.outputs.shared-digest }}"}, job["with"])
                self.assertNotIn("if", job, "the validation workflow gates its own jobs")
        self.assertEqual(["select", "<validation-workflow>"], self.jobs["validation"]["needs"])

    def test_the_validation_job_passes_exactly_the_inputs_that_the_validation_workflow_template_declares(self) -> None:
        _, workflow = read_workflow(SELECTION_KIT / "validation-workflow.yml")
        self.assertEqual(set(workflow["on"]["workflow_call"]["inputs"]), set(self.jobs["<validation-workflow>"]["with"]))

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


class ShardedValidationWorkflowDocumentationTest(unittest.TestCase):
    """The kit holds the sharded form of the validation workflow alone, and its README and an ADR describe it."""

    def setUp(self) -> None:
        self.readme = (SELECTION_KIT / "README.md").read_text(encoding="utf-8")
        self.section = readme_section(self.readme, "## The validation workflow")

    def test_no_template_of_the_kit_gates_on_a_suite_or_reads_the_per_suite_inputs(self) -> None:
        for template in KIT_TEMPLATES:
            with self.subTest(template=template):
                text = (SELECTION_KIT / template).read_text(encoding="utf-8")
                for per_suite in ("inputs.suites", "inputs.entries", "outputs.suites", "outputs.entries", "<suite>", "<entry-files>"):
                    self.assertNotIn(per_suite, text)

    def test_the_readme_describes_the_sharded_form_only(self) -> None:
        for per_suite in ("<suite>", "<job>", "<entry-files>", "<windows-suite>", "passed.jsonl", "fromJSON(inputs.suites)",
                          "fromJSON(inputs.entries)", "suite-results-<job>", "one per validation workflow"):
            with self.subTest(absent=per_suite):
                self.assertNotIn(per_suite, self.readme)
        for placeholder in ("`<name>`", "`<linux-runner>`", "`<windows-job>`", "`<windows-runner>`", "`<python-version>`", "`<setup>`", "`<pin>`"):
            with self.subTest(placeholder=placeholder):
                self.assertIn(placeholder, self.section)
        for text in ("contains(matrix.shard.setups, '<setup>')", "contains(fromJSON(env.SETUPS), '<setup>')", "two at a time",
                     "log group", "`Check the recording against the suite list`", "matrix.shard.shard == 1",
                     "`suite-results-shard-<number>`", "`suite-results-windows`", "one after another"):
            with self.subTest(text=text):
                self.assertIn(text, self.section)

    def test_the_readme_names_the_one_linux_runner_label_the_check_names_and_that_a_step_change_runs_every_suite(self) -> None:
        self.assertIn("all on one runner label", self.section)
        self.assertIn("the runner label that `runner-labels` gives for `linux`", self.section)
        self.assertIn("`<file name> / shard <number>`", self.readme)
        self.assertIn("`<file name> / <windows-job>`", self.readme)
        self.assertIn("none of them is a required check", self.readme)
        self.assertIn("a change to one of its steps runs every suite", self.section)

    def test_an_adr_records_the_shards_on_one_linux_runner_label_and_that_a_step_change_runs_every_suite(self) -> None:
        records = [path for path in (REPOSITORY_ROOT / "docs" / "adr").glob("*.md") if "timed shards" in path.read_text(encoding="utf-8")]
        self.assertEqual(1, len(records))
        text = records[0].read_text(encoding="utf-8")
        self.assertIn("one runner label", text)
        self.assertIn("therefore runs every suite", text)

    def test_a_checkout_keeps_the_repository_s_own_options(self) -> None:
        self.assertIn("keeps the repository's own options beside these two", self.section)
        self.assertIn("`fetch-depth: 0`", self.section)

    def test_setups_route_a_runner_context_value_past_the_job_s_env(self) -> None:
        # A job's `env` does not allow the `runner` context, and a variable of the step that runs exec never reaches
        # the entry files.
        section = readme_section(self.readme, "## Setups")
        self.assertNotIn("a variable that an entry file needs and no setup provides belongs in the job's `env`", section)
        self.assertIn("goes in the job's `env`", section)
        self.assertIn("`RUNNER_TEMP`", section)
        self.assertIn("`provide --variable`", section)
        self.assertIn("A variable of the step that runs the entry files does not reach them", section)
        self.assertIn("no other value replaces them", section)


class EarlierAdoptionSectionTest(unittest.TestCase):
    """Every consumer kit leads a repository that adopted the part earlier onto the kit's current templates."""

    def setUp(self) -> None:
        catalogue = json.loads((REPOSITORY_ROOT / "catalogue.json").read_text(encoding="utf-8"))
        self.kits = [REPOSITORY_ROOT / part["consumer_kit"] for part in catalogue["parts"] if part["consumer_kit"]]

    def test_every_kit_readme_has_the_section_once(self) -> None:
        self.assertTrue(self.kits)
        for kit in self.kits:
            with self.subTest(kit=kit.parent.name):
                readme = (kit / "README.md").read_text(encoding="utf-8")
                self.assertEqual(1, readme.count(EARLIER_ADOPTION_HEADING + "\n"))

    def test_the_section_links_every_template_of_its_kit(self) -> None:
        for kit in self.kits:
            templates = sorted(path.name for path in kit.glob("*.yml"))
            self.assertTrue(templates)
            section = readme_section((kit / "README.md").read_text(encoding="utf-8"), EARLIER_ADOPTION_HEADING)
            for template in templates:
                with self.subTest(kit=kit.parent.name, template=template):
                    self.assertIn(f"]({template})", section)

    def test_the_suite_selection_section_leads_an_earlier_caller_to_the_v1_moved_trigger(self) -> None:
        section = readme_section((SELECTION_KIT / "README.md").read_text(encoding="utf-8"), EARLIER_ADOPTION_HEADING)
        self.assertIn("`v1-moved`", section)
        self.assertIn("(#when-v1-moves)", section)

    def test_the_earlier_adoption_carries_over_each_job_s_checkout_options_and_env_variables(self) -> None:
        section = readme_section((SELECTION_KIT / "README.md").read_text(encoding="utf-8"), EARLIER_ADOPTION_HEADING)
        self.assertIn("the checkout options of each job, in the validation workflow and in the full recording", section)
        self.assertIn("the variables in each job's `env`", section)


class V1MovedRunTest(unittest.TestCase):
    """A ``v1-moved`` run of the validation workflow template: ``repository_dispatch`` without a ``client_payload``."""

    def test_every_checkout_takes_the_default_branch_s_tip(self) -> None:
        _, validation = read_workflow(SELECTION_KIT / "validation-workflow.yml")
        checkouts = [step for job in validation["jobs"].values() for step in job.get("steps", []) if str(step.get("uses", "")).startswith("actions/checkout@")]
        self.assertGreaterEqual(len(checkouts), 2)
        for step in checkouts:
            with self.subTest(job=step.get("name")):
                self.assertEqual("", expression_value(step["with"]["ref"], V1_MOVED_CONTEXT), "an empty ref checks out github.sha, the default branch's tip")

    def test_the_expression_value_returns_operands_as_github_does(self) -> None:
        context = {"github": {"event_name": "repository_dispatch", "event": {"client_payload": {"sha": "abc"}}, "sha": "tip"}}
        self.assertEqual("abc", expression_value("${{ github.event_name == 'repository_dispatch' && github.event.client_payload.sha || '' }}", context))
        self.assertEqual("tip", expression_value("${{ github.event_name == 'push' && github.event.client_payload.sha || github.sha }}", context))
        self.assertTrue(expression_value("!(github.event_name == 'push')", context))


if __name__ == "__main__":
    unittest.main()
