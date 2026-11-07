"""Text contract of this repository's Python-tests caller and the fixture project it runs the shared workflow on."""

from __future__ import annotations

import tomllib
import unittest

from tests.ci_policy_support import REPOSITORY_ROOT, read_workflow

WORKFLOWS = REPOSITORY_ROOT / ".github" / "workflows"
CALLER = WORKFLOWS / "python-tests-caller.yml"
FIXTURE = "tests/fixtures/python_tests"


class PythonTestsCallerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text, self.caller = read_workflow(CALLER)

    def test_the_caller_runs_on_pull_requests_main_pushes_and_dependabot_merges_by_local_path(self) -> None:
        self.assertIn("on:\n  pull_request:\n  push:\n    branches: [main]\n  repository_dispatch:\n    types: [dependabot-merged]\n\n", self.text)
        self.assertNotIn("edited", self.text, "a title edit starts no test run")
        self.assertEqual("{}", self.caller["permissions"])
        self.assertEqual(["tests"], list(self.caller["jobs"]))
        job = self.caller["jobs"]["tests"]
        self.assertEqual({"actions": "read", "contents": "read", "pull-requests": "read"}, job["permissions"])
        self.assertEqual("$/.github/workflows/python-tests.yml", job["uses"])
        self.assertNotIn("@v1", self.text)
        self.assertNotIn("run:", self.text)
        self.assertNotIn("secrets", self.text)

    def test_a_newer_head_cancels_the_older_pull_request_run_and_a_main_run_is_never_cancelled(self) -> None:
        self.assertEqual({
            "group": "${{ github.workflow }}-${{ github.event_name == 'pull_request' && github.event.pull_request.number || github.run_id }}",
            "cancel-in-progress": "${{ github.event_name == 'pull_request' }}",
        }, self.caller["concurrency"])

    def test_the_caller_runs_the_shared_workflow_on_the_fixture_project_with_its_dev_extra(self) -> None:
        self.assertEqual({"python-version": "3.14", "working-directory": FIXTURE, "install": "dev"}, self.caller["jobs"]["tests"]["with"])


class PythonTestsFixtureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = REPOSITORY_ROOT / FIXTURE
        self.project = tomllib.loads((self.root / "pyproject.toml").read_text(encoding="utf-8"))

    def test_the_fixture_has_a_dev_extra_with_pytest_one_pytest_test_and_a_ruff_configuration(self) -> None:
        self.assertEqual(["pytest"], [requirement.split("==")[0] for requirement in self.project["project"]["optional-dependencies"]["dev"]])
        self.assertIn("ini_options", self.project["tool"]["pytest"], "the fixture configures pytest, so the job runs pytest")
        self.assertIn("ruff", self.project["tool"])
        tests = sorted(path.relative_to(self.root).as_posix() for path in self.root.rglob("test_*.py"))
        self.assertEqual(["tests/test_greeting.py"], tests)
        self.assertEqual(1, (self.root / tests[0]).read_text(encoding="utf-8").count("\ndef test_"))


if __name__ == "__main__":
    unittest.main()
