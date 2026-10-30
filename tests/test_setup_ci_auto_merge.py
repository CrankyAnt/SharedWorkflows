"""The Dependabot auto-merge caller template."""

from __future__ import annotations

import unittest
from pathlib import Path
from tests.setup_ci_support import assert_caller_passes_no_secrets_and_runs_no_step, read_yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
AUTO_MERGE_KIT = REPOSITORY_ROOT / "actions" / "dependabot-auto-merge" / "consumer-kit"


class AutoMergeCallerTemplateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.text, self.caller = read_yaml(AUTO_MERGE_KIT / "dependabot-auto-merge.yml")

    def test_job_dependabot_auto_merge_holds_the_two_write_rights_on_the_job_only(self) -> None:
        self.assertEqual({}, self.caller["permissions"])
        self.assertEqual(["dependabot-auto-merge"], list(self.caller["jobs"]))
        job = self.caller["jobs"]["dependabot-auto-merge"]
        self.assertEqual({"contents": "write", "pull-requests": "write"}, job["permissions"])
        self.assertEqual("CrankyAnt/SharedWorkflows/.github/workflows/dependabot-auto-merge.yml@v1", job["uses"])
        self.assertEqual(2, self.text.count("permissions:"))

    def test_runs_on_pull_requests_and_on_each_completed_workflow_that_produces_a_required_check(self) -> None:
        self.assertEqual({"pull_request": {"types": ["opened", "synchronize", "reopened"]},
                          "workflow_run": {"workflows": ["<required-check-workflows>"], "types": ["completed"]}}, self.caller["on"])
        self.assertRegex(self.text, r"(?m)^  workflow_run: # zizmor: ignore\[dangerous-triggers\] \S")

    def test_passes_no_inputs_no_secrets_and_runs_no_step(self) -> None:
        self.assertNotIn("with", self.caller["jobs"]["dependabot-auto-merge"])
        assert_caller_passes_no_secrets_and_runs_no_step(self, self.text)


if __name__ == "__main__":
    unittest.main()
