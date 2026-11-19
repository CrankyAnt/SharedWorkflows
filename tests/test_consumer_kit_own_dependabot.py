"""The repository Dependabot configuration."""

from __future__ import annotations

import unittest
from pathlib import Path
from tests.consumer_kit_support import assert_update_entry_follows_the_template, read_yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
OWN_DEPENDABOT = REPOSITORY_ROOT / ".github" / "dependabot.yml"


class OwnDependabotConfigurationTest(unittest.TestCase):
    def test_one_entry_per_ecosystem_with_the_group_of_its_shape_and_the_7_day_cooldown(self) -> None:
        _, configuration = read_yaml(OWN_DEPENDABOT)
        entries = {entry["package-ecosystem"]: entry for entry in configuration["updates"]}
        self.assertEqual(len(configuration["updates"]), len(entries), "one entry per ecosystem")
        self.assertEqual(
            {
                "github-actions": "/",
                "nuget": ["/actions/recording/etw-consumer", "/tests/fixtures/dotnet_test", "/tests/fixtures/dotnet_console"],
                "dotnet-sdk": "/actions/recording/etw-consumer",
                "npm": ["/actions/publication-checks", "/tests/fixtures/node_test"],
                "pip": "/tests/fixtures/python_tests",
            },
            {ecosystem: entry.get("directories", entry.get("directory")) for ecosystem, entry in entries.items()},
        )
        for ecosystem, entry in entries.items():
            with self.subTest(entry=ecosystem):
                assert_update_entry_follows_the_template(self, entry)
                place = "directories" if "directories" in entry else "directory"
                self.assertEqual({"package-ecosystem", place, "schedule", "cooldown", "groups"}, set(entry))


if __name__ == "__main__":
    unittest.main()
