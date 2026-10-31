"""The suite-selection part source boundary."""

from __future__ import annotations

import unittest
from pathlib import Path

TOOL = Path(__file__).resolve().parents[1] / "actions/suite-selection"

class ToolBoundaryTests(unittest.TestCase):
    def test_the_part_holds_no_marker_or_guide_of_its_own(self) -> None:
        self.assertEqual([], [path.name for path in TOOL.glob("*.md")], "the documentation of the suite selection is docs/maintaining.md")
        self.assertTrue((TOOL / "suite_selection.py").is_file())



if __name__ == "__main__":
    unittest.main()
