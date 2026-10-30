"""The pins updater workflow is constrained by the shared hygiene surface."""

from __future__ import annotations

import json
import unittest

from tests.ci_policy_support import REPOSITORY_ROOT


UPDATER = REPOSITORY_ROOT / ".github" / "workflows" / "update-ci-pins.yml"
SURFACE = REPOSITORY_ROOT / "actions" / "repository-hygiene" / "shared-surface.json"


class HygieneUpdaterTests(unittest.TestCase):
    def test_the_updater_workflow_reports_without_apply_and_proposes_one_pins_only_pull_request(self) -> None:
        updater = UPDATER.read_text(encoding="utf-8")
        self.assertIn('cron: "36 17 * * 1"', updater)
        self.assertIn("default: false", updater)
        self.assertRegex(updater, r"(?m)^  plan:\n    runs-on: ubuntu-slim\n    permissions:\n      contents: read$")
        self.assertRegex(updater, r"(?m)^  propose:\n    needs: plan\n    if: .*\n    runs-on: ubuntu-slim\n    permissions:\n      contents: read$")
        self.assertRegex(updater, r"(?m)^        run: .*update_pins\.py --cooldown-days \"\$COOLDOWN_DAYS\" --output \"\$GITHUB_OUTPUT\" --summary \"\$GITHUB_STEP_SUMMARY\"$")
        self.assertIn("default: 7", updater)
        self.assertEqual(2, updater.count("COOLDOWN_DAYS: ${{ inputs.cooldown_days || 7 }}"), "both steps take the cooldown; a scheduled run gets 7")
        self.assertIn("if: needs.plan.outputs.changed == 'true' && (github.event_name == 'schedule' || inputs.apply == true)", updater)
        self.assertIn("permission-contents: write\n          permission-pull-requests: write", updater)
        self.assertNotIn("permission-workflows", updater)
        self.assertIn("actions/repository-hygiene/pins.json) ;;", updater)
        self.assertIn('--force-with-lease="refs/heads/$branch:$remote_tip"', updater)
        self.assertIn('--log-opts="HEAD --not --remotes=origin"', updater)
        self.assertLess(updater.index("fetch --prune origin"), updater.index("--log-opts="))
        self.assertIn('--body-file "$RUNNER_TEMP/pins-pr-body.md"', updater)
        self.assertEqual(2, updater.count('branch="chore/update-ci-pins"'))
        self.assertEqual("chore/update-ci-pins", json.loads(SURFACE.read_text(encoding="utf-8"))["updater_branch"])
