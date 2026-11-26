"""This repository requires only `ci / verdict`, removes its standalone callers, and listens on CI alone."""

from __future__ import annotations

import json
import re
import unittest

from tests.ci_policy_support import REPOSITORY_ROOT, flow_items


WORKFLOWS = REPOSITORY_ROOT / ".github" / "workflows"
AUTOMATION_CALLER = WORKFLOWS / "automation-caller.yml"
RETIRED_CALLERS = ("repository-hygiene-caller.yml", "python-tests-caller.yml", "dependabot-auto-merge-caller.yml")


class AutoMergeCallerTests(unittest.TestCase):
    def test_the_standalone_callers_are_gone_and_automation_listens_only_on_ci(self) -> None:
        for name in RETIRED_CALLERS:
            with self.subTest(caller=name):
                self.assertFalse((WORKFLOWS / name).exists(), f"{name} is retired")
        workflows = re.search(r"(?m)^  workflow_run:.*\n    workflows: \[(.*)\]\n    types: \[completed\]\n", AUTOMATION_CALLER.read_text(encoding="utf-8"))
        self.assertIsNotNone(workflows)
        ci_name = re.search(r"(?m)^name: (.*)$", (WORKFLOWS / "ci.yml").read_text(encoding="utf-8"))[1]
        self.assertEqual([ci_name], flow_items(workflows[1]))


class RulesetTemplateTests(unittest.TestCase):
    def test_the_ruleset_template_requires_only_ci_verdict(self) -> None:
        template = json.loads((REPOSITORY_ROOT / "actions" / "repository-ruleset" / "ruleset-main-template.json").read_text(encoding="utf-8"))
        rule = next(rule for rule in template["rules"] if rule["type"] == "required_status_checks")
        self.assertEqual([{"context": "ci / verdict", "integration_id": 15368}], rule["parameters"]["required_status_checks"])
        catalogue = json.loads((REPOSITORY_ROOT / "catalogue.json").read_text(encoding="utf-8"))
        pipeline = next(part for part in catalogue["parts"] if part["name"] == "Pipeline")
        self.assertEqual(pipeline["required_checks"], [item["context"] for item in rule["parameters"]["required_status_checks"]])


if __name__ == "__main__":
    unittest.main()
