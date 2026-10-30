"""The Dependabot configuration template."""

from __future__ import annotations

import unittest
from pathlib import Path
from tests.setup_ci_support import assert_update_entry_follows_the_template, read_yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
AUTO_MERGE_KIT = REPOSITORY_ROOT / "actions" / "dependabot-auto-merge" / "consumer-kit"


class DependabotTemplateTest(unittest.TestCase):
    def setUp(self) -> None:
        _, self.configuration = read_yaml(AUTO_MERGE_KIT / "dependabot.yml")

    def test_holds_one_github_actions_entry_at_the_root_with_the_ignore_entry_for_shared_workflows(self) -> None:
        self.assertEqual(2, self.configuration["version"])
        self.assertEqual(1, len(self.configuration["updates"]))
        entry = self.configuration["updates"][0]
        self.assertEqual("github-actions", entry["package-ecosystem"])
        self.assertEqual("/", entry["directory"])
        self.assertNotIn("directories", entry)
        self.assertEqual([{"dependency-name": "CrankyAnt/SharedWorkflows"}], entry["ignore"])

    def test_the_entry_has_the_weekly_schedule_the_7_day_cooldown_and_the_minor_and_patch_group(self) -> None:
        assert_update_entry_follows_the_template(self, self.configuration["updates"][0])


if __name__ == "__main__":
    unittest.main()
