"""The standalone Python-tests caller is gone; the fixture project runs through the pipeline."""

from __future__ import annotations

import tomllib
import unittest

from tests.ci_policy_support import REPOSITORY_ROOT

WORKFLOWS = REPOSITORY_ROOT / ".github" / "workflows"
CALLER = WORKFLOWS / "python-tests-caller.yml"
FIXTURE = "tests/fixtures/python_tests"


class PythonTestsCallerTests(unittest.TestCase):
    def test_the_standalone_python_tests_caller_is_gone(self) -> None:
        self.assertFalse(CALLER.exists())


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
