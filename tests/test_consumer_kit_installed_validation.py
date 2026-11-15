"""The installed validation caller of an earlier kit, the fixture `tests/fixtures/registered_validation/validation.yml`,
calls the reusable selection workflows with the rights of their job; a consumer that keeps it keeps working."""

from __future__ import annotations

import unittest
from pathlib import Path
from tests.consumer_kit_support import read_yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPOSITORY_ROOT / "tests" / "fixtures" / "registered_validation"

OWN_WORKFLOWS = REPOSITORY_ROOT / ".github" / "workflows"


class LocalPathInstalledFormTest(unittest.TestCase):
    def test_the_installed_validation_caller_calls_both_reusable_selection_workflows_with_the_rights_of_their_job(self) -> None:
        _, template = read_yaml(FIXTURE / "validation.yml")
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
