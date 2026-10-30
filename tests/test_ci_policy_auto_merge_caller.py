"""This repository's auto-merge caller reacts to every workflow that produces a required check."""

from __future__ import annotations

import json
import re
import unittest

from tests.ci_policy_support import REPOSITORY_ROOT


WORKFLOWS = REPOSITORY_ROOT / ".github" / "workflows"
CALLER = WORKFLOWS / "dependabot-auto-merge-caller.yml"
# The workflows of the checks that the default-branch ruleset requires: hygiene / hygiene and the verdict of validate.yml.
REQUIRED_CHECK_WORKFLOWS = ("repository-hygiene-caller.yml", "validate.yml")


class AutoMergeCallerTests(unittest.TestCase):
    def test_the_caller_names_every_workflow_that_produces_a_required_check_and_no_other(self) -> None:
        workflows = re.search(r"(?m)^  workflow_run:.*\n    workflows: \[(.*)\]\n    types: \[completed\]\n", CALLER.read_text(encoding="utf-8"))
        self.assertIsNotNone(workflows)
        names = [re.search(r"(?m)^name: (.*)$", (WORKFLOWS / name).read_text(encoding="utf-8"))[1] for name in REQUIRED_CHECK_WORKFLOWS]
        self.assertEqual(sorted(names), sorted(name.strip() for name in workflows[1].split(",")))


class RulesetTemplateTests(unittest.TestCase):
    def test_the_ruleset_template_requires_the_hygiene_check_and_the_verdict_s_check(self) -> None:
        template = json.loads((REPOSITORY_ROOT / "actions" / "repository-ruleset" / "ruleset-main-template.json").read_text(encoding="utf-8"))
        rule = next(rule for rule in template["rules"] if rule["type"] == "required_status_checks")
        self.assertEqual([{"context": "hygiene / hygiene", "integration_id": 15368}, {"context": "validation / validation", "integration_id": 15368}],
                         rule["parameters"]["required_status_checks"])


if __name__ == "__main__":
    unittest.main()
