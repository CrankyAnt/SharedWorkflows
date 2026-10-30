"""The hygiene caller template of the consumer kit of repository hygiene."""

from __future__ import annotations

import unittest
from pathlib import Path
from tests.setup_ci_support import assert_caller_passes_no_secrets_and_runs_no_step, read_yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
HYGIENE_KIT = REPOSITORY_ROOT / "actions" / "repository-hygiene" / "consumer-kit"


class HygieneCallerTemplateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.text, self.caller = read_yaml(HYGIENE_KIT / "repository-hygiene.yml")

    def test_job_hygiene_holds_the_read_rights_of_the_push_lookup_and_calls_the_shared_workflow_at_v1(self) -> None:
        self.assertEqual({}, self.caller["permissions"])
        self.assertEqual(["hygiene"], list(self.caller["jobs"]))
        job = self.caller["jobs"]["hygiene"]
        self.assertEqual({"actions": "read", "contents": "read", "pull-requests": "read"}, job["permissions"])
        self.assertEqual("CrankyAnt/SharedWorkflows/.github/workflows/repository-hygiene.yml@v1", job["uses"])

    def test_runs_on_pull_request_with_edited_on_push_to_the_default_branch_and_on_a_dependabot_merge(self) -> None:
        self.assertEqual({"pull_request": {"types": ["opened", "synchronize", "reopened", "edited"]}, "push": {"branches": ["<default-branch>"]}, "repository_dispatch": {"types": ["dependabot-merged"]}}, self.caller["on"])

    def test_passes_no_secrets_and_runs_no_step(self) -> None:
        assert_caller_passes_no_secrets_and_runs_no_step(self, self.text)


if __name__ == "__main__":
    unittest.main()
