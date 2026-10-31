"""The template targets expose reusable workflows."""

from __future__ import annotations

import unittest
from pathlib import Path
from tests.consumer_kit_support import read_yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
HYGIENE_KIT = REPOSITORY_ROOT / "actions" / "repository-hygiene" / "consumer-kit"
AUTO_MERGE_KIT = REPOSITORY_ROOT / "actions" / "dependabot-auto-merge" / "consumer-kit"

OWN_WORKFLOWS = REPOSITORY_ROOT / ".github" / "workflows"
CALLER_TEMPLATES = {"repository-hygiene.yml": HYGIENE_KIT, "dependabot-auto-merge.yml": AUTO_MERGE_KIT}


def template_job(template: str) -> dict:
    _, caller = read_yaml(CALLER_TEMPLATES[template] / template)
    (job,) = caller["jobs"].values()
    return job


class LocalPathInstalledFormTest(unittest.TestCase):
    def test_each_caller_install_target_here_is_the_shared_workflow_the_template_calls(self) -> None:
        for template in CALLER_TEMPLATES:
            with self.subTest(template=template):
                self.assertEqual(f"CrankyAnt/SharedWorkflows/.github/workflows/{template}@v1", template_job(template)["uses"])
                shared = (OWN_WORKFLOWS / template).read_text(encoding="utf-8")
                self.assertRegex(shared, r"(?m)^on:\n  workflow_call:")


if __name__ == "__main__":
    unittest.main()
