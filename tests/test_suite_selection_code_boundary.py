"""The suite-selection part source boundary."""

from __future__ import annotations

import unittest
from pathlib import Path

TOOL = Path(__file__).resolve().parents[1] / "actions/suite-selection"

class ToolBoundaryTests(unittest.TestCase):
    def test_the_tool_s_code_and_defaults_name_no_path_of_this_repository_and_no_other_part(self) -> None:
        forbidden = ("SharedWorkflows", "tests/", "lib/", "automation/", "repository-hygiene", "publication-checks", "dependabot-auto-merge",
                     "validate.yml", "tests/suites.json")
        for pattern in ("*.py", "*.ps1"):
            for path in sorted(TOOL.glob(pattern)):
                text = path.read_text(encoding="utf-8")
                for token in forbidden:
                    self.assertNotIn(token, text, f"{path.name} names {token}")



if __name__ == "__main__":
    unittest.main()
