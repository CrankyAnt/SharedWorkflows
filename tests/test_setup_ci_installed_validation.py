"""The validation template targets reusable selection workflows."""

from __future__ import annotations

import unittest
from pathlib import Path
from tests.setup_ci_support import read_yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SELECTION_KIT = REPOSITORY_ROOT / "actions" / "suite-selection" / "consumer-kit"

OWN_WORKFLOWS = REPOSITORY_ROOT / ".github" / "workflows"


class LocalPathInstalledFormTest(unittest.TestCase):
    def test_the_validation_template_calls_both_reusable_selection_workflows_with_the_rights_of_their_job(self) -> None:
        _, template = read_yaml(SELECTION_KIT / "validation.yml")
        for workflow, job, rights in (("suite-selection.yml", "select", {"actions": "read", "contents": "read", "pull-requests": "read"}),
                                      ("suite-verdict.yml", "validation", {"contents": "read"})):
            with self.subTest(workflow=workflow):
                self.assertEqual(f"CrankyAnt/SharedWorkflows/.github/workflows/{workflow}@v1", template["jobs"][job]["uses"])
                shared = (OWN_WORKFLOWS / workflow).read_text(encoding="utf-8")
                self.assertRegex(shared, r"(?m)^on:\n  workflow_call:")
                self.assertEqual(rights, template["jobs"][job]["permissions"])
                self.assertIn("    permissions:\n" + "".join(f"      {right}: {value}\n" for right, value in rights.items()), shared)


if __name__ == "__main__":
    unittest.main()
