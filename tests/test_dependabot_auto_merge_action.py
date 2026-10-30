"""The Dependabot resource action remains a composite action without checkout."""

import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class ActionTests(unittest.TestCase):
    def test_the_resource_action_is_composite_without_checkout(self) -> None:
        action = (REPOSITORY_ROOT / "actions/dependabot-auto-merge/action.yml").read_text(encoding="utf-8")
        self.assertIn("using: composite", action)
        self.assertNotIn("checkout", action)


if __name__ == "__main__":
    unittest.main()
