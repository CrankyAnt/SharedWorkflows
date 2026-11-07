"""The caller template and the README of the consumer kit of Python tests."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from tests.consumer_kit_support import assert_caller_passes_no_secrets_and_runs_no_step, read_yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
KIT = REPOSITORY_ROOT / "actions" / "python-tests" / "consumer-kit"


class PythonTestsCallerTemplateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.text, self.caller = read_yaml(KIT / "python-tests.yml")

    def test_the_kit_holds_one_template(self) -> None:
        self.assertEqual(["python-tests.yml"], sorted(path.name for path in KIT.glob("*.yml")))

    def test_job_tests_holds_the_read_rights_of_the_push_lookup_and_calls_the_shared_workflow_at_v1(self) -> None:
        self.assertEqual({}, self.caller["permissions"])
        self.assertEqual(["tests"], list(self.caller["jobs"]))
        job = self.caller["jobs"]["tests"]
        self.assertEqual({"actions": "read", "contents": "read", "pull-requests": "read"}, job["permissions"])
        self.assertEqual("CrankyAnt/SharedWorkflows/.github/workflows/python-tests.yml@v1", job["uses"])
        self.assertEqual({"python-version": "<python-version>", "install": "<install>"}, job["with"])
        self.assertIn('python-version: "<python-version>"', self.text, "the quotes keep a version such as 3.10 a string")

    def test_runs_on_pull_request_without_edited_on_push_to_the_default_branch_and_on_a_dependabot_merge(self) -> None:
        self.assertEqual({"pull_request": None, "push": {"branches": ["<default-branch>"]}, "repository_dispatch": {"types": ["dependabot-merged"]}},
                         self.caller["on"])
        self.assertNotIn("edited", self.text)
        self.assertNotIn("paths", self.text)

    def test_a_newer_head_cancels_the_older_pull_request_run_and_a_default_branch_run_is_never_cancelled(self) -> None:
        self.assertEqual({
            "group": "${{ github.workflow }}-${{ github.event_name == 'pull_request' && github.event.pull_request.number || github.run_id }}",
            "cancel-in-progress": "${{ github.event_name == 'pull_request' }}",
        }, self.caller["concurrency"])

    def test_passes_no_secrets_and_runs_no_step(self) -> None:
        assert_caller_passes_no_secrets_and_runs_no_step(self, self.text)
        self.assertNotIn("$/", self.text)


class PythonTestsReadmeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.readme = (KIT / "README.md").read_text(encoding="utf-8")

    def test_the_catalogue_entry_names_the_kit_the_check_and_the_exclusion_of_the_suite_selection(self) -> None:
        entries = {entry["name"]: entry for entry in json.loads((REPOSITORY_ROOT / "catalogue.json").read_text(encoding="utf-8"))["parts"]}
        entry = entries["Python tests"]
        self.assertEqual("actions/python-tests/consumer-kit", entry["consumer_kit"])
        self.assertEqual([".github/workflows/python-tests.yml"], entry["workflows"])
        self.assertEqual(["actions/python-tests", "actions/python-language-step"], entry["steps"])
        self.assertEqual(["tests / tests"], entry["required_checks"])
        self.assertIn("suite selection", entry["applies"])

    def test_the_readme_states_what_runs_what_it_needs_and_when_it_applies(self) -> None:
        for text in ("CrankyAnt/SharedWorkflows/.github/workflows/python-tests.yml@v1", "actions/setup-python", "without a cache",
                     "pip install -r", 'pip install ".[<extra>]"', "python -m pytest", "python -m unittest discover", "[tool.pytest.ini_options]",
                     "pytest.ini", "tox.ini", "[tool:pytest]", "conftest.py", "ends in `.md`", "dependabot-merged", "`tests / tests`",
                     "pytest is not installed", "finds no tests", "also when the language step failed", "## When a repository adopts it"):
            with self.subTest(text=text):
                self.assertIn(text, self.readme)

    def test_the_readme_names_every_input_with_its_default(self) -> None:
        for text in ("`python-version` | required", "`working-directory` | `.`", "`install` | empty", "`runner-label` | `ubuntu-26.04`",
                     '`format-check` | `"false"`', "windows-2025"):
            with self.subTest(text=text):
                self.assertIn(text, self.readme)

    def test_the_readme_excludes_the_suite_selection_leads_to_it_and_gives_one_caller_job_per_test_root(self) -> None:
        for text in ("either the suite selection or Python tests, never both", "## Moving to the suite selection", "`validation / validation`",
                     "## Several test roots", "one caller job per root", "`tests-parser / tests`",
                     "## When a repository adopted it earlier", "## When `v1` moves"):
            with self.subTest(text=text):
                self.assertIn(text, self.readme)

    def test_the_readme_names_nothing_that_exists_only_in_shared_workflows(self) -> None:
        for text in ("python-tests-caller", "tests/fixtures", "maintaining.md", "docs/agents", "$/", "pins.json", "python_tests.py"):
            with self.subTest(text=text):
                self.assertNotIn(text, self.readme)


if __name__ == "__main__":
    unittest.main()
