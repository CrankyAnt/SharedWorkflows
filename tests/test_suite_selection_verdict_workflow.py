"""Reusable workflow input, output and permission contract."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github/workflows"

class SharedWorkflowTests(unittest.TestCase):
    def test_the_verdict_workflow_holds_the_contract(self) -> None:
        workflow = (WORKFLOWS / "suite-verdict.yml").read_text(encoding="utf-8")
        self.assertIn("workflow_call:", workflow)
        self.assertRegex(workflow, r"(?m)^      needs:\n        description: .*\n        type: string\n        required: true$")
        self.assertIn("permissions: {}", workflow)
        self.assertRegex(workflow, r"(?m)^  validation:\n    runs-on: ubuntu-slim\n    permissions:\n      contents: read$")
        self.assertEqual(1, workflow.count("permissions:\n      contents: read"))
        self.assertNotIn("write", workflow)
        self.assertNotIn("secrets", workflow)
        self.assertNotIn("checkout", workflow, "the verdict reads only its input")
        self.assertEqual(["$/actions/suite-selection"], [reference for reference in re.findall(r"uses: (\$/\S+)", workflow)])
        self.assertIn("NEEDS: ${{ inputs.needs }}", workflow)
        self.assertIn('suite_selection.py" verdict --needs "$RUNNER_TEMP/needs.json" --summary "$GITHUB_STEP_SUMMARY"', workflow)
        verdict = workflow[workflow.index("      - name: Decide the verdict"):]
        self.assertNotRegex(verdict, r"(?m)^\s+if:", "the verdict reports on every run that calls it")



if __name__ == "__main__":
    unittest.main()
