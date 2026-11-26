"""The caller template grants the permissions its shared workflow requires."""

from __future__ import annotations

import unittest
from pathlib import Path
from tests.ci_policy_support import read_workflow
from tests.consumer_kit_support import read_yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
HYGIENE_KIT = REPOSITORY_ROOT / "actions" / "repository-hygiene" / "consumer-kit"

OWN_WORKFLOWS = REPOSITORY_ROOT / ".github" / "workflows"
CALLER_TEMPLATES = {"repository-hygiene.yml": HYGIENE_KIT}


def template_job(template: str) -> dict:
    _, caller = read_yaml(CALLER_TEMPLATES[template] / template)
    (job,) = caller["jobs"].values()
    return job


class TemplatePermissionsTest(unittest.TestCase):
    def test_each_caller_template_grants_the_rights_of_its_shared_workflow(self) -> None:
        for template in CALLER_TEMPLATES:
            with self.subTest(template=template):
                _, shared = read_workflow(OWN_WORKFLOWS / template)
                (shared_job,) = shared["jobs"].values()
                self.assertEqual(shared_job["permissions"], template_job(template)["permissions"])


if __name__ == "__main__":
    unittest.main()
