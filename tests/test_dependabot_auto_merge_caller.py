"""Dependabot auto-merge caller contract."""

import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
OWN_CALLER = REPOSITORY_ROOT / ".github/workflows/dependabot-auto-merge-caller.yml"


class WorkflowTests(unittest.TestCase):
    def test_the_own_caller_grants_the_two_rights_on_its_job_only_and_runs_by_local_path(self) -> None:
        caller = OWN_CALLER.read_text(encoding="utf-8")
        self.assertRegex(caller, r"(?m)^  auto-merge:\n    permissions:\n      contents: write\n      pull-requests: write\n    uses: \$/\.github/workflows/dependabot-auto-merge\.yml\n")
        self.assertEqual(2, caller.count("permissions:"))
        self.assertNotIn("zizmor: ignore[self-repository]", caller)
        self.assertNotIn("@v1", caller)
        self.assertNotIn("secrets", caller)
        self.assertNotRegex(caller, r"(?m)^\s*(?:- )?run:")
        self.assertNotIn("with:", caller)

    def test_the_own_caller_runs_on_dependabot_pull_requests_and_on_completed_workflow_runs(self) -> None:
        caller = OWN_CALLER.read_text(encoding="utf-8")
        self.assertIn("on:\n  pull_request:\n    types: [opened, synchronize, reopened]\n  workflow_run: # zizmor: ignore[dangerous-triggers] the merge job checks out nothing and runs no pull-request code\n", caller)
        self.assertRegex(caller, r"(?m)^    workflows: \[.+\]\n    types: \[completed\]\n\npermissions: \{\}\n")


if __name__ == "__main__":
    unittest.main()
