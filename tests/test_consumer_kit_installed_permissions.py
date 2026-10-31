"""The installed callers preserve template permissions."""

from __future__ import annotations

import unittest
from pathlib import Path
from tests.consumer_kit_support import read_yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
HYGIENE_KIT = REPOSITORY_ROOT / "actions" / "repository-hygiene" / "consumer-kit"
AUTO_MERGE_KIT = REPOSITORY_ROOT / "actions" / "dependabot-auto-merge" / "consumer-kit"

OWN_WORKFLOWS = REPOSITORY_ROOT / ".github" / "workflows"
OWN_CALLERS = ("dependabot-auto-merge-caller.yml", "repository-hygiene-caller.yml")
CALLER_TEMPLATES = {"repository-hygiene.yml": HYGIENE_KIT, "dependabot-auto-merge.yml": AUTO_MERGE_KIT}


def template_job(template: str) -> dict:
    _, caller = read_yaml(CALLER_TEMPLATES[template] / template)
    (job,) = caller["jobs"].values()
    return job


def local_path_callers() -> dict[str, dict]:
    """Each job of this repository's own callers that calls a workflow by local path, keyed by the called path."""
    callers: dict[str, dict] = {}
    for name in OWN_CALLERS:
        path = OWN_WORKFLOWS / name
        _, workflow = read_yaml(path)
        for job in workflow["jobs"].values():
            uses = job.get("uses", "")
            if uses.startswith("$/.github/workflows/"):
                callers[uses.removeprefix("$/")] = job
    return callers


class LocalPathInstalledFormTest(unittest.TestCase):
    def test_a_local_path_caller_with_the_template_rights_calls_each_shared_workflow(self) -> None:
        callers = local_path_callers()
        for template in CALLER_TEMPLATES:
            with self.subTest(template=template):
                self.assertIn(f".github/workflows/{template}", callers)
                self.assertEqual(template_job(template)["permissions"], callers[f".github/workflows/{template}"]["permissions"])


if __name__ == "__main__":
    unittest.main()
