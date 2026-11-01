"""The shared zizmor policy grants ref pins only to the shared workflows and the shared actions a consumer calls."""

from __future__ import annotations

import json
import re
import unittest

from tests.ci_policy_support import REPOSITORY_ROOT


SHARED_POLICY = REPOSITORY_ROOT / "actions" / "repository-hygiene" / "zizmor-policy.yml"
SHARED_SURFACE = REPOSITORY_ROOT / "actions" / "repository-hygiene" / "shared-surface.json"
SHARED_CI = REPOSITORY_ROOT / "actions"


def shared_workflows() -> set[str]:
    surface = json.loads(SHARED_SURFACE.read_text(encoding="utf-8"))
    return {path for path in surface["paths"] if path.startswith(".github/workflows/")}


def consumer_actions() -> set[str]:
    """The shared actions that the consumer kits call, as their paths in this repository."""
    templates = SHARED_CI.glob("*/consumer-kit/*.yml")
    return {path for template in sorted(templates)
            for path in re.findall(r"uses:\s*CrankyAnt/SharedWorkflows/(actions/[^@\s]+)@", template.read_text(encoding="utf-8"))}


def ref_pin_exceptions(policy: str) -> set[str]:
    return {double or single or plain for double, single, plain in re.findall(
        r"(?m)^\s*(?:\"([^\"]+)\"|'([^']+)'|([^\s:]+)):\s*ref-pin\s*(?:#.*)?$", policy
    )}


class ZizmorPolicyTests(unittest.TestCase):
    def test_zizmor_policies_grant_ref_pin_to_exactly_the_shared_workflows(self) -> None:
        shared_workflow_exceptions = {f"CrankyAnt/SharedWorkflows/{workflow}" for workflow in shared_workflows()}
        expected_exceptions = {
            SHARED_POLICY: shared_workflow_exceptions | {f"CrankyAnt/SharedWorkflows/{action}" for action in consumer_actions()},
        }
        for policy_path, expected in expected_exceptions.items():
            policy = policy_path.read_text(encoding="utf-8")
            self.assertIn('"*": hash-pin', policy)
            found = ref_pin_exceptions(policy)
            self.assertSetEqual(
                expected,
                found,
                f"{policy_path.relative_to(REPOSITORY_ROOT)} differs from shared-surface.json and the consumer kits. "
                "Update this policy's ref-pin exceptions to match them; missing and extra entries are shown below.",
            )
            self.assertSetEqual(expected, ref_pin_exceptions(policy.replace(": ref-pin", ": ref-pin # fixture note")))
            extra = "fixture/extra"
            for key in (extra, f'"{extra}"', f"'{extra}'"):
                self.assertSetEqual(expected | {extra}, ref_pin_exceptions(policy + f"\n{key}: ref-pin # extra exception\n"))

    def test_the_shared_actions_a_consumer_calls_are_shared_actions(self) -> None:
        surface = json.loads(SHARED_SURFACE.read_text(encoding="utf-8"))["paths"]
        self.assertTrue(consumer_actions())
        for action in sorted(consumer_actions()):
            with self.subTest(action=action):
                self.assertIn(action, surface)
                self.assertTrue((REPOSITORY_ROOT / action / "action.yml").is_file())
