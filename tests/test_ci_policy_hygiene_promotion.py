"""The v1 promotion workflow is constrained by the shared hygiene surface."""

from __future__ import annotations

import unittest

from tests.ci_policy_support import REPOSITORY_ROOT


PROMOTER = REPOSITORY_ROOT / ".github" / "workflows" / "promote-v1.yml"


class HygienePromotionTests(unittest.TestCase):
    def test_only_the_move_job_of_the_promotion_workflow_holds_contents_write(self) -> None:
        promoter = PROMOTER.read_text(encoding="utf-8")
        self.assertIn("on:\n  push:\n    branches: [main]\n    paths: [actions/repository-hygiene/pins.json]\n\n", promoter)
        self.assertEqual(1, promoter.count("contents: write"))
        self.assertRegex(promoter, r"(?m)^  move:\n    needs: decide\n    if: needs\.decide\.outputs\.move == 'true'\n    runs-on: ubuntu-slim\n    permissions:\n      contents: write$")
        self.assertRegex(promoter, r"(?m)^  decide:\n    runs-on: ubuntu-slim\n    timeout-minutes: 14\n    permissions:\n      actions: read\n      contents: read\n      pull-requests: read$")
        self.assertEqual(1, promoter.count("actions: read"))
        self.assertNotIn("workflow_run", promoter)
        self.assertIn("permission-metadata: read", promoter)
        self.assertIn('promote_v1.py decide --commit "$COMMIT" --v1 "$V1"', promoter)
        self.assertIn('--repository "$GITHUB_REPOSITORY" --output "$GITHUB_OUTPUT" --summary "$GITHUB_STEP_SUMMARY"', promoter)
        self.assertIn('promote_v1.py move --old "$OLD" --new "$NEW"', promoter)
        self.assertEqual(2, promoter.count("persist-credentials: false"))
        self.assertIn('promote_v1.py read-tag --repository "$GITHUB_REPOSITORY" --output "$GITHUB_OUTPUT"', promoter)
        self.assertNotIn("git ls-remote", promoter)
