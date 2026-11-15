"""The Dependabot resource action remains a composite action without checkout."""

import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class ActionTests(unittest.TestCase):
    def test_the_resource_action_is_composite_without_checkout(self) -> None:
        action = (REPOSITORY_ROOT / "actions/dependabot-auto-merge/action.yml").read_text(encoding="utf-8")
        self.assertIn("using: composite", action)
        self.assertNotIn("checkout", action)

    def test_the_consumer_kit_names_dependabot(self) -> None:
        kit = (REPOSITORY_ROOT / "actions/dependabot-auto-merge/consumer-kit/README.md").read_text(encoding="utf-8")
        self.assertIn("Dependabot", kit)


if __name__ == "__main__":
    unittest.main()
