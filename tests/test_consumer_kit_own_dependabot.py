"""The repository Dependabot configuration."""

from __future__ import annotations

import unittest
from pathlib import Path
from tests.consumer_kit_support import assert_update_entry_follows_the_template, read_yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
OWN_DEPENDABOT = REPOSITORY_ROOT / ".github" / "dependabot.yml"


class OwnDependabotConfigurationTest(unittest.TestCase):
    def test_every_entry_has_the_7_day_cooldown_and_the_minor_and_patch_group(self) -> None:
        _, configuration = read_yaml(OWN_DEPENDABOT)
        entries = {(entry["package-ecosystem"], entry["directory"]): entry for entry in configuration["updates"]}
        self.assertEqual(
            {
                ("github-actions", "/"),
                ("nuget", "/actions/recording/etw-consumer"),
                ("dotnet-sdk", "/actions/recording/etw-consumer"),
                ("npm", "/actions/publication-checks"),
            },
            set(entries),
        )
        for key, entry in entries.items():
            with self.subTest(entry=key):
                assert_update_entry_follows_the_template(self, entry)
                self.assertEqual({"package-ecosystem", "directory", "schedule", "cooldown", "groups"}, set(entry))


if __name__ == "__main__":
    unittest.main()
